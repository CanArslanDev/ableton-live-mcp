"""Distribution: the .mcpb manifest, the Claude Code plugin and its skills,
and the Remote Script's compatibility with Live's Python."""

import ast
import asyncio
import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_build_mcpb():
    spec = importlib.util.spec_from_file_location("build_mcpb", ROOT / "packaging" / "build_mcpb.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _registered_tools() -> set[str]:
    import MCP_Server.server as server
    return {t.name for t in asyncio.run(server.mcp.list_tools())}


def test_versions_agree():
    version = _load_build_mcpb().project_version()
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert plugin["version"] == version
    assert [p["version"] for p in marketplace["plugins"]] == [version]
    assert marketplace["plugins"][0]["name"] == plugin["name"]


def test_mcpb_manifest_is_filled_from_the_code():
    build = _load_build_mcpb()
    manifest = build.build_manifest()
    for field in ("manifest_version", "name", "version", "description", "author", "server"):
        assert manifest[field], field
    assert manifest["version"] == build.project_version()
    server = manifest["server"]
    assert server["type"] == "uv"
    assert "${__dirname}" in server["mcp_config"]["args"]
    assert server["mcp_config"]["args"][-1] == server["entry_point"]
    listed = [t["name"] for t in manifest["tools"]]
    assert set(listed) == _registered_tools() and len(listed) == len(set(listed))
    assert all(0 < len(t["description"]) <= 200 for t in manifest["tools"])
    assert manifest["user_config"]["profile"]["default"] == "core"
    assert (ROOT / "packaging" / "mcpb" / manifest["icon"]).is_file()


def test_tool_summary_is_the_first_sentence():
    summary = _load_build_mcpb()._summary
    assert summary("Do a thing. Then more.\n\nDetails") == "Do a thing."
    assert summary("Kinds of edit:\n- a\n- b") == "Kinds of edit."
    assert summary("x " * 200).endswith("…")


def test_mcpb_stage_has_what_uv_needs(tmp_path):
    staged = _load_build_mcpb().stage(tmp_path / "mcpb")
    for name in ("manifest.json", "main.py", "icon.png", "pyproject.toml", "uv.lock",
                 "README.md", "LICENSE", "MCP_Server/server.py", "MCP_Server/ui/panels.py",
                 "MCP_Server/bundled_ableton_remote_script/AbletonMCP_init.py"):
        assert (staged / name).is_file(), name
    assert not list(staged.rglob("__pycache__")) and not list(staged.rglob("config.py"))


def _skills():
    return sorted((ROOT / "skills").glob("*/SKILL.md"))


def test_skills_have_frontmatter():
    assert len(_skills()) >= 5
    for path in _skills():
        text = path.read_text(encoding="utf-8")
        match = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
        assert match, path
        fields = dict(line.split(": ", 1) for line in match.group(1).splitlines())
        assert fields["name"] == path.parent.name
        assert 50 < len(fields["description"]) <= 1024


@pytest.mark.parametrize("path", _skills(), ids=lambda p: p.parent.name)
def test_skills_only_name_real_tools(path):
    tools = _registered_tools()
    called = set(re.findall(r"`([a-z_]+)\(", path.read_text(encoding="utf-8")))
    assert called, path
    assert called <= tools, sorted(called - tools)


def test_remote_script_runs_on_lives_python():
    # Live 11 embeds Python 3.7; the script avoids f-strings as well
    source = (ROOT / "AbletonMCP_Remote_Script" / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 7))
    assert not any(isinstance(node, ast.JoinedStr) for node in ast.walk(tree))


def _tool_server():
    from mcp.server.mcpserver import MCPServer

    from MCP_Server import extended_tools

    server = MCPServer("t")
    extended_tools.register(server, extended_tools._get_connection)
    return server


def test_install_remote_script_tool(tmp_path, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    from MCP_Server import remote_script_install as installer

    library = tmp_path / "User Library" / "Remote Scripts"
    monkeypatch.setattr(installer, "discover_user_library_remote_script_dirs", lambda: [library])
    server = _tool_server()

    first = json.loads(asyncio.run(server.call_tool("install_remote_script", {})).content[0].text)
    assert first["results"][0]["status"] == "installed"
    assert first["next"].startswith("Restart Live")
    installed = library / "AbletonMCP" / "__init__.py"
    assert installed.read_bytes() == (ROOT / "AbletonMCP_Remote_Script" / "__init__.py").read_bytes()
    again = json.loads(asyncio.run(server.call_tool("install_remote_script", {})).content[0].text)
    assert again["results"][0]["status"] == "unchanged"
    assert again["next"].startswith("Already up to date")

    monkeypatch.setattr(installer, "discover_user_library_remote_script_dirs", lambda: [])
    with pytest.raises(ToolError, match="No Ableton User Library found"):
        asyncio.run(server.call_tool("install_remote_script", {}))
