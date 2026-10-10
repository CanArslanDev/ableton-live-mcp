"""The server side of the socket protocol (MCP_Server/connection.py), against
small scripted stand-ins for the Remote Script."""

import json
import socket
import threading

import pytest

from MCP_Server.connection import AbletonCommandError, AbletonConnection, session_token


class FakeScript(object):
    """Accepts one connection and answers each request with reply(request)."""

    def __init__(self, reply, newline=True):
        self.reply = reply
        self.newline = newline
        self.requests = []
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        conn, _ = self.listener.accept()
        buffer = b""
        with conn:
            while True:
                data = conn.recv(65536)
                if not data:
                    return
                buffer += data
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    request = json.loads(line)
                    self.requests.append(request)
                    for answer in self.reply(request):
                        raw = json.dumps(answer).encode("utf-8")
                        conn.sendall(raw + b"\n" if self.newline else raw)

    def close(self):
        self.listener.close()


def test_a_script_before_2_0_gets_protocol_1(monkeypatch):
    """1.x scripts answer hello with 'Unknown command' and send bare objects."""
    def reply(request):
        if request["type"] == "hello":
            return [{"status": "error", "message": "Unknown command: hello"}]
        return [{"status": "success", "result": {"type": request["type"]}}]

    script = FakeScript(reply, newline=False)
    connection = AbletonConnection(host="127.0.0.1", port=script.port)
    try:
        assert connection.connect()
        assert connection.protocol == 1
        assert connection.send_command("lom_get", {"path": "live_set"}) == {"type": "lom_get"}
        assert connection.send_command("get_script_info") == {"type": "get_script_info"}
    finally:
        connection.disconnect()
        script.close()


def test_hello_sends_the_token_and_replies_are_matched_by_id(monkeypatch, tmp_path):
    session = tmp_path / "live-session.json"
    session.write_text(json.dumps({"port": 9877, "token": "abc123"}))
    monkeypatch.setenv("ABLETON_MCP_SESSION_FILE", str(session))
    monkeypatch.delenv("ABLETON_MCP_TOKEN", raising=False)

    def reply(request):
        if request["type"] == "hello":
            ok = request["params"]["token"] == "abc123"
            return [{"id": request["id"], "status": "success" if ok else "error",
                     "result": {"script_version": "2.0.0"}, "message": "bad token"}]
        # A late reply to an earlier request arrives first; it must be skipped
        return [{"id": request["id"] - 1, "status": "success", "result": "stale"},
                {"id": request["id"], "status": "success", "result": request["params"]}]

    script = FakeScript(reply)
    connection = AbletonConnection(host="127.0.0.1", port=script.port)
    try:
        assert connection.connect()
        assert connection.protocol == 2
        assert connection.script_info == {"script_version": "2.0.0"}
        assert connection.send_command("lom_get", {"n": 1}) == {"n": 1}
        assert connection.send_command("lom_get", {"n": 2}) == {"n": 2}
        assert [r["type"] for r in script.requests] == ["hello", "lom_get", "lom_get"]
    finally:
        connection.disconnect()
        script.close()


def test_a_rejected_token_fails_the_connection_with_the_reason(monkeypatch):
    monkeypatch.setenv("ABLETON_MCP_TOKEN", "wrong")

    def reply(request):
        return [{"id": request["id"], "status": "error",
                 "message": "Invalid session token. Live writes a new one ..."}]

    script = FakeScript(reply)
    connection = AbletonConnection(host="127.0.0.1", port=script.port)
    try:
        assert not connection.connect()
        assert "Invalid session token" in connection.last_error
        assert connection.sock is None
    finally:
        script.close()


def test_refused_commands_raise_and_keep_the_socket(monkeypatch):
    monkeypatch.setenv("ABLETON_MCP_TOKEN", "t")

    def reply(request):
        if request["type"] == "boom":
            return [{"id": request["id"], "status": "error", "message": "No item named 'x'"}]
        return [{"id": request["id"], "status": "success", "result": {}}]

    script = FakeScript(reply)
    connection = AbletonConnection(host="127.0.0.1", port=script.port)
    try:
        assert connection.connect()
        sock = connection.sock
        with pytest.raises(AbletonCommandError, match="No item named"):
            connection.send_command("boom")
        assert connection.sock is sock
    finally:
        connection.disconnect()
        script.close()


def test_a_timeout_drops_the_socket(monkeypatch):
    monkeypatch.setenv("ABLETON_MCP_TOKEN", "t")

    def reply(request):
        if request["type"] == "hello":
            return [{"id": request["id"], "status": "success", "result": {}}]
        return []  # never answers

    script = FakeScript(reply)
    connection = AbletonConnection(host="127.0.0.1", port=script.port)
    try:
        assert connection.connect()
        with pytest.raises(Exception, match="Timeout"):
            connection.send_command("lom_get", timeout=0.2)
        assert connection.sock is None
    finally:
        connection.disconnect()
        script.close()


def test_the_token_comes_from_the_environment_first(monkeypatch, tmp_path):
    session = tmp_path / "live-session.json"
    session.write_text(json.dumps({"token": "from-file"}))
    monkeypatch.setenv("ABLETON_MCP_SESSION_FILE", str(session))
    monkeypatch.delenv("ABLETON_MCP_TOKEN", raising=False)
    assert session_token() == "from-file"
    monkeypatch.setenv("ABLETON_MCP_TOKEN", "from-env")
    assert session_token() == "from-env"
    monkeypatch.delenv("ABLETON_MCP_TOKEN")
    session.unlink()
    assert session_token() is None
