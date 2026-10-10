"""Build the Claude Desktop extension (.mcpb).

    uv run python packaging/build_mcpb.py              # dist/ableton-live-mcp-<version>.mcpb
    uv run python packaging/build_mcpb.py --stage-only # just the folder, to inspect

The bundle is a "uv" extension (MCPB manifest 0.4): the server's source with
pyproject.toml and uv.lock, which Claude Desktop installs with uv the first
time it starts the server. The manifest is packaging/mcpb/manifest.json with
the version taken from pyproject.toml and the tools from the registered
server, so neither can drift. Packing (and validating) is done by Anthropic's
`mcpb` CLI through npx, so Node.js is needed for that step.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = ROOT / "packaging" / "mcpb"
MCPB_CLI = "@anthropic-ai/mcpb@2.1.2"

# What the bundle needs to install and run the server, relative to ROOT
CONTENTS = ["pyproject.toml", "uv.lock", "README.md", "LICENSE", "MCP_Server"]
SKIP = shutil.ignore_patterns("__pycache__", "*.pyc", "config.py", ".DS_Store")

MAIN = '''"""Entry point of the Claude Desktop extension: runs the MCP server on stdio."""

from MCP_Server.server import main

main()
'''


def project_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    return re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE).group(1)


def _summary(description: str | None, limit: int = 200) -> str:
    """The first sentence of a tool description, on one line."""
    paragraph = (description or "").strip().split("\n\n")[0]
    text = " ".join(paragraph.split())
    match = re.match(r"(.+?[.:;])(\s|$)", text)
    sentence = match.group(1) if match else text
    sentence = sentence.rstrip(":;") + ("." if sentence.endswith((":", ";")) else "")
    return sentence if len(sentence) <= limit else sentence[:limit - 1].rstrip() + "…"


def tool_entries() -> list[dict]:
    """The tools of the default profile, as the manifest lists them."""
    os.environ.setdefault("ABLETON_MCP_PROFILE", "core")
    os.environ.setdefault("ABLETON_MCP_DISABLE_TELEMETRY", "true")
    sys.path.insert(0, str(ROOT))
    from MCP_Server.server import mcp

    tools = asyncio.run(mcp.list_tools())
    return [{"name": t.name, "description": _summary(t.description)} for t in tools]


def build_manifest() -> dict:
    manifest = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))
    manifest["version"] = project_version()
    manifest["tools"] = tool_entries()
    return manifest


def stage(out: Path) -> Path:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for name in CONTENTS:
        source = ROOT / name
        if source.is_dir():
            shutil.copytree(source, out / name, ignore=SKIP)
        else:
            shutil.copy2(source, out / name)
    shutil.copy2(HERE / "icon.png", out / "icon.png")
    (out / "main.py").write_text(MAIN, encoding="utf-8")
    (out / "manifest.json").write_text(
        json.dumps(build_manifest(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return out


def pack(staged: Path, target: Path) -> None:
    npx = shutil.which("npx")
    if npx is None:
        raise SystemExit("npx not found: install Node.js to pack the bundle "
                         "(or use --stage-only)")
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([npx, "-y", MCPB_CLI, "pack", str(staged), str(target)], check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--stage-only", action="store_true",
                        help="Assemble the bundle folder without packing it")
    args = parser.parse_args(argv)

    staged = stage(args.out_dir / "mcpb")
    print(f"Staged {staged}")
    if not args.stage_only:
        target = args.out_dir / f"ableton-live-mcp-{project_version()}.mcpb"
        pack(staged, target)
        print(f"Built {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
