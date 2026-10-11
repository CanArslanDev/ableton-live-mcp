"""The server's side of the AbletonMCP Companion (Extensions SDK add-on),
against a stand-in that speaks its protocol (extension/src/bridge.ts)."""

import asyncio
import json
import socket
import threading
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from MCP_Server import companion, doctor


class FakeCompanion:
    """One-line-per-message JSON server with hello/token, like bridge.ts."""

    def __init__(self, token="t0ken", handlers=None):
        self.token = token
        self.handlers = handlers or {}
        self.calls = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            self._answer(conn)
        except OSError:  # the client hung up after its answer
            pass

    def _answer(self, conn):
        authenticated = False
        with conn, conn.makefile("rwb") as stream:
            for raw in stream:
                request = json.loads(raw)
                kind, params = request["type"], request.get("params") or {}
                if kind == "hello":
                    authenticated = params.get("token") == self.token
                    reply = ({"status": "success", "result": {"companion": "0.1.0"}} if authenticated
                             else {"status": "error", "message": "Invalid session token"})
                elif not authenticated:
                    reply = {"status": "error", "message": "Authentication required"}
                elif kind in self.handlers:
                    self.calls.append((kind, params))
                    try:
                        reply = {"status": "success", "result": self.handlers[kind](params)}
                    except Exception as e:
                        reply = {"status": "error", "message": str(e)}
                else:
                    reply = {"status": "error", "message": f"Unknown command: {kind}"}
                stream.write((json.dumps({"id": request.get("id"), **reply}) + "\n").encode())
                stream.flush()

    def close(self):
        # On Linux, close() alone leaves a thread blocked in accept() serving
        # the port; shutdown() wakes it (macOS refuses shutdown on a listener)
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(companion.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(companion, "_ableton_folders", lambda: [tmp_path / "Ableton"])
    monkeypatch.delenv("ABLETON_MCP_COMPANION_TOKEN", raising=False)
    monkeypatch.delenv("ABLETON_MCP_COMPANION_PORT", raising=False)
    return tmp_path


def write_session(folder: Path, port: int, token: str = "t0ken") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / companion.SESSION_FILE
    path.write_text(json.dumps({"port": port, "token": token, "companion": "0.1.0", "pid": 1}))
    return path


def test_no_companion_explains_what_it_is(home):
    with pytest.raises(companion.CompanionUnavailable, match="Live 12.4.5\\+ Suite"):
        companion.find_session()


def test_session_from_home_storage_folder_or_environment(home, monkeypatch):
    stored = write_session(home / "Ableton" / "Live 12.4.15" / "Extensions" / "x" / "storage", 1111)
    assert companion.find_session()["port"] == 1111
    assert companion.find_session()["source"] == str(stored)
    write_session(home / ".ableton-mcp", 2222)
    import os
    os.utime(stored, (1, 1))  # the home copy is newer now
    assert companion.find_session()["port"] == 2222
    monkeypatch.setenv("ABLETON_MCP_COMPANION_TOKEN", "abc")
    monkeypatch.setenv("ABLETON_MCP_COMPANION_PORT", "3333")
    assert companion.find_session() == {"port": 3333, "token": "abc", "source": "environment"}


def test_request_round_trip_and_failures(home):
    fake = FakeCompanion(handlers={"song": lambda p: {"tempo": 124},
                                   "boom": lambda p: (_ for _ in ()).throw(ValueError("no"))})
    try:
        write_session(home / ".ableton-mcp", fake.port)
        assert companion.request("song") == {"tempo": 124}
        with pytest.raises(RuntimeError, match="^no$"):
            companion.request("boom")
        with pytest.raises(RuntimeError, match="Unknown command: what"):
            companion.request("what")
        write_session(home / ".ableton-mcp", fake.port, token="wrong")
        with pytest.raises(companion.CompanionUnavailable, match="refused the token"):
            companion.request("song")
    finally:
        fake.close()
    with pytest.raises(companion.CompanionUnavailable, match="does not answer on port"):
        companion.request("song")


def _tools():
    from mcp.server.mcpserver import MCPServer

    from MCP_Server import extended_tools

    server = MCPServer("t")
    extended_tools.register(server, extended_tools._get_connection)
    return server


def _call(server, tool, **arguments):
    return json.loads(asyncio.run(server.call_tool(tool, arguments)).content[0].text)


def test_selection_confirm_and_render_tools(home, tmp_path):
    wav = tmp_path / "render.wav"
    sf.write(wav, 0.25 * np.sin(2 * np.pi * 220 * np.arange(22050) / 22050), 22050)
    shared = {"value": None}
    fake = FakeCompanion(handlers={
        "get_selection": lambda p: shared["value"],
        "confirm": lambda p: {"confirmed": p["message"].startswith("Replace")},
        "render_audio": lambda p: {"file": str(wav), "track": p["track"],
                                   "start_beat": p["start_beat"], "end_beat": p["end_beat"]},
    })
    try:
        write_session(home / ".ableton-mcp", fake.port)
        server = _tools()
        from mcp.server.mcpserver.exceptions import ToolError
        with pytest.raises(ToolError, match="Nothing shared yet"):
            asyncio.run(server.call_tool("get_live_selection", {}))
        shared["value"] = {"kind": "arrangement", "start_beat": 8, "end_beat": 24,
                           "tracks": ["Drums"], "shared_at": "now"}
        selection = _call(server, "get_live_selection")
        # No Live connection in the test: the meter falls back to 4/4
        assert selection["start_bar"] == 3.0 and selection["bars"] == 4.0
        assert _call(server, "confirm_in_live", message="Replace the drums?") == {"confirmed": True}
        assert fake.calls[-1][1]["cancel_label"] == "Cancel"
        rendered = _call(server, "render_track_audio", track="Vox", start_beat=0, end_beat=8)
        assert rendered["file"] == str(wav)
        assert rendered["analysis"]["integrated_lufs"] < 0
    finally:
        fake.close()


def test_doctor_reports_the_companion(home):
    assert doctor.check_companion()["status"] == "info"
    fake = FakeCompanion(handlers={"song": lambda p: {"tracks": [{"name": "A"}, {"name": "B"}]}})
    try:
        write_session(home / ".ableton-mcp", fake.port)
        check = doctor.check_companion()
        assert check["status"] == "ok" and "2 tracks" in check["detail"]
    finally:
        fake.close()
    assert doctor.check_companion()["status"] == "warn"  # a session file, nobody listening
