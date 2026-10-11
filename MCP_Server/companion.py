"""The AbletonMCP Companion (extension/), seen from the server.

It is an Extensions SDK add-on that Live 12.4.5+ Suite (beta) runs inside
Live. It listens on 127.0.0.1:9878 with the Remote Script's framing (a JSON
object per line, replies echo the request's `id`, `hello` with a token first)
and writes the port and token to companion-session.json: in ~/.ableton-mcp,
or, where Live only lets extensions write in their storage folder, there,
which is why this module also searches Ableton's folders for the file.

ABLETON_MCP_COMPANION_TOKEN (and ABLETON_MCP_COMPANION_PORT) override the
file, for setups where the server cannot read it.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any

SESSION_FILE = "companion-session.json"
DEFAULT_PORT = 9878


class CompanionUnavailable(RuntimeError):
    pass


def _ableton_folders() -> list[Path]:
    home = Path.home()
    if sys.platform == "darwin":
        return [home / "Library" / "Application Support" / "Ableton",
                home / "Library" / "Preferences" / "Ableton"]
    if sys.platform == "win32":
        roots = [os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA")]
        return [Path(r) / "Ableton" for r in roots if r]
    return []


def session_files() -> list[Path]:
    """Session files that exist, newest first."""
    found = [Path.home() / ".ableton-mcp" / SESSION_FILE]
    for folder in _ableton_folders():
        if folder.is_dir():
            found += folder.rglob(SESSION_FILE)
    existing = [p for p in found if p.is_file()]
    return sorted(existing, key=lambda p: p.stat().st_mtime, reverse=True)


def find_session() -> dict:
    token = os.environ.get("ABLETON_MCP_COMPANION_TOKEN")
    if token:
        return {"port": int(os.environ.get("ABLETON_MCP_COMPANION_PORT") or DEFAULT_PORT),
                "token": token, "source": "environment"}
    for path in session_files():
        try:
            session = json.loads(path.read_text(encoding="utf-8"))
            return {"port": int(session["port"]), "token": str(session["token"]),
                    "companion": session.get("companion"), "source": str(path)}
        except (OSError, ValueError, KeyError) as e:
            raise CompanionUnavailable(f"Unreadable companion session file {path}: {e}") from e
    raise CompanionUnavailable(
        "The AbletonMCP Companion is not running. It is an optional add-on for Live 12.4.5+ "
        "Suite (Extensions SDK beta): build and install extension/ (see its README), then "
        "enable it in Live's Settings › Extensions.")


def request(command: str, params: dict | None = None, timeout: float = 10.0) -> Any:
    """Send one command to the companion and return its result."""
    session = find_session()
    try:
        sock = socket.create_connection(("127.0.0.1", session["port"]), timeout=3.0)
    except OSError as e:
        raise CompanionUnavailable(
            f"The AbletonMCP Companion does not answer on port {session['port']} ({e}); is Live "
            "open with the extension enabled? (session from "
            f"{session['source']})") from e
    with sock:
        sock.settimeout(timeout)
        messages = [{"id": 1, "type": "hello", "params": {"token": session["token"]}},
                    {"id": 2, "type": command, "params": params or {}}]
        sock.sendall("".join(json.dumps(m) + "\n" for m in messages).encode("utf-8"))
        replies: dict[int, dict] = {}
        buffer = b""
        deadline = time.monotonic() + timeout
        while 2 not in replies:
            if time.monotonic() > deadline:
                raise TimeoutError(f"The companion did not answer {command} in {timeout:.0f} s")
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                raise TimeoutError(f"The companion did not answer {command} in {timeout:.0f} s") from None
            if not chunk:
                raise CompanionUnavailable("The companion closed the connection")
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if line.strip():
                    reply = json.loads(line)
                    replies[reply.get("id")] = reply
                    if reply.get("id") == 1 and reply.get("status") != "success":
                        raise CompanionUnavailable(f"The companion refused the token: "
                                                   f"{reply.get('message')}")
    reply = replies[2]
    if reply.get("status") != "success":
        raise RuntimeError(reply.get("message") or f"{command} failed")
    return reply.get("result")
