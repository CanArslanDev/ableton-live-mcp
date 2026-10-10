"""Regression tests for the Live-side Remote Script's TCP surface.

Each test pins a defect this change fixes:

* the socket bound every interface, exposing an unauthenticated command
  surface to the whole network,
* each recv() chunk was decoded on its own, so a multi-byte UTF-8 character
  split across a chunk boundary desynchronised the connection,
* an incomplete command grew the buffer without bound,
* get_browser_tree never recursed and never reported a folder count.

The script normally runs inside Live's interpreter, so ``_Framework`` is
stubbed and the class is instantiated without ``__init__`` (which would open a
socket). No Ableton, no network.
"""

import errno
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_SCRIPT = REPO_ROOT / "AbletonMCP_Remote_Script" / "__init__.py"
BUNDLED_SCRIPT = (REPO_ROOT / "MCP_Server" / "bundled_ableton_remote_script"
                  / "AbletonMCP_init.py")


def _load_script_module(fresh=False):
    """Import the Live-side script with _Framework stubbed out.

    Each call re-executes the module, so ``fresh`` is only a readability hint
    at call sites that depend on module-level state being re-evaluated (the
    ABLETON_MCP_HOST lookup).
    """
    framework = types.ModuleType("_Framework")
    control_surface_mod = types.ModuleType("_Framework.ControlSurface")

    class _StubControlSurface(object):
        def __init__(self, *args, **kwargs):
            pass

        def log_message(self, *args, **kwargs):
            pass

        def show_message(self, *args, **kwargs):
            pass

    control_surface_mod.ControlSurface = _StubControlSurface
    framework.ControlSurface = control_surface_mod
    sys.modules.setdefault("_Framework", framework)
    sys.modules.setdefault("_Framework.ControlSurface", control_surface_mod)

    spec = importlib.util.spec_from_file_location(
        "ableton_mcp_remote_script_under_test", BUNDLED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script_module():
    return _load_script_module()


def make_instance(script_module, application=None):
    inst = script_module.AbletonMCP.__new__(script_module.AbletonMCP)
    inst.application = lambda: application
    inst.log_message = lambda *a, **k: None
    inst.show_message = lambda *a, **k: None
    return inst


# ------------------------------------------------------------------ binding

def test_socket_binds_loopback_by_default(script_module):
    """The command surface is unauthenticated: it must not listen publicly
    unless the user opts in via ABLETON_MCP_HOST."""
    assert script_module.HOST == "127.0.0.1"


def test_bind_host_is_overridable_for_remote_setups(monkeypatch):
    """Running Live and the MCP server on separate machines stays possible,
    but only as a deliberate choice."""
    monkeypatch.setenv("ABLETON_MCP_HOST", "0.0.0.0")
    assert _load_script_module(fresh=True).HOST == "0.0.0.0"


def test_repo_and_bundled_copies_are_identical():
    """The installer ships the bundled copy; drift would install stale code."""
    assert REPO_SCRIPT.read_bytes() == BUNDLED_SCRIPT.read_bytes()


# ------------------------------------------------------------------ framing

class FakeSocket(object):
    """A non-blocking client socket: one prepared chunk per recv(), then
    "would block" (or EOF with eof=True). Records what the script sent."""

    def __init__(self, chunks=(), eof=True, allowance=None):
        self.chunks = list(chunks)
        self.eof = eof
        # Bytes the kernel would take before send() blocks (None: unlimited)
        self.allowance = allowance
        self.sent = bytearray()
        self.closed = False

    def recv(self, _size):
        if self.chunks:
            return self.chunks.pop(0)
        if self.eof:
            return b""
        raise BlockingIOError(errno.EAGAIN, "would block")

    def send(self, data):
        count = len(data)
        if self.allowance is not None:
            if not self.allowance:
                raise BlockingIOError(errno.EAGAIN, "would block")
            count = min(count, self.allowance)
            self.allowance -= count
        self.sent += bytes(data[:count])
        return count

    def close(self):
        self.closed = True

    def replies(self):
        return [json.loads(line) for line in bytes(self.sent).split(b"\n") if line.strip()]


def make_transport(script_module, token=None, echo=True):
    """An instance with the transport set up but no listening socket."""
    inst = make_instance(script_module)
    inst._transport_init()
    inst.running = True
    inst._session_token = token
    inst.client_type = script_module._Client
    received = []
    if echo:
        def _process(command):
            received.append(command)
            return {"status": "success", "result": {"echo": command.get("type")}}
        inst._process_command = _process
    return inst, received


def serve(inst, sock, polls=3):
    """Poll a client the way Live's timer would."""
    client = inst.client_type(sock, ("127.0.0.1", 50000))
    inst._clients.append(client)
    for _ in range(polls):
        inst._poll_sockets()
    return client


def test_command_split_mid_utf8_character_still_parses(script_module):
    """Regression: decoding each chunk alone raised on the split, dropped the
    chunk, and wrote an error frame into a stream still awaiting the real
    response — leaving request and response permanently out of step.

    ensure_ascii=False is what puts raw multi-byte on the wire.
    """
    payload = json.dumps(
        {"type": "set_clip_name", "params": {"name": "café été"}},
        ensure_ascii=False,
    ).encode("utf-8")
    split = payload.index(b"\xc3") + 1  # between the two bytes of 'é'
    assert payload[:split].endswith(b"\xc3")

    inst, received = make_transport(script_module)
    sock = FakeSocket([payload[:split], payload[split:]])
    serve(inst, sock)

    assert len(received) == 1
    assert received[0]["params"]["name"] == "café été"
    assert sock.replies() == [{"status": "success", "result": {"echo": "set_clip_name"}}]


def test_command_split_across_many_chunks_parses_once(script_module):
    payload = json.dumps({"type": "get_session_info", "params": {}}).encode("utf-8")
    chunks = [payload[i:i + 3] for i in range(0, len(payload), 3)]

    inst, received = make_transport(script_module)
    sock = FakeSocket(chunks)
    serve(inst, sock)

    assert len(received) == 1
    assert len(sock.replies()) == 1


def test_back_to_back_commands_each_get_one_reply(script_module):
    first = json.dumps({"type": "start_playback", "params": {}}).encode("utf-8")
    second = json.dumps({"type": "stop_playback", "params": {}}).encode("utf-8")

    # Protocol 1: bare objects in separate chunks
    inst, received = make_transport(script_module)
    sock = FakeSocket([first, second])
    serve(inst, sock)
    assert [c["type"] for c in received] == ["start_playback", "stop_playback"]
    assert len(sock.replies()) == 2

    # Protocol 2: several lines in one chunk
    inst, received = make_transport(script_module)
    sock = FakeSocket([first + b"\n" + second + b"\n"])
    serve(inst, sock)
    assert [c["type"] for c in received] == ["start_playback", "stop_playback"]
    assert len(sock.replies()) == 2


def test_replies_end_with_a_newline_and_echo_the_request_id(script_module):
    inst, _received = make_transport(script_module)
    request = json.dumps({"id": 41, "type": "lom_get", "params": {}}).encode("utf-8")
    sock = FakeSocket([request + b"\n"])
    serve(inst, sock)
    assert bytes(sock.sent).endswith(b"\n")
    assert sock.replies() == [{"status": "success", "result": {"echo": "lom_get"}, "id": 41}]


def test_oversized_request_is_refused_instead_of_buffered_forever(
        script_module, monkeypatch):
    """A client that never completes a command must not grow the buffer."""
    monkeypatch.setattr(script_module, "MAX_REQUEST_BYTES", 1024)
    junk = b'{"type": "' + b"x" * 4096

    inst, received = make_transport(script_module)
    sock = FakeSocket([junk, junk], eof=False)
    serve(inst, sock)

    assert received == []
    replies = sock.replies()
    assert len(replies) == 1 and replies[0]["status"] == "error"
    assert sock.closed


def test_a_chunk_ending_in_a_brace_mid_command_waits_for_the_rest(script_module):
    """A "}" that closes a nested object mid-command must not be mistaken for
    the end of the request."""
    command = {"type": "lom_get", "params": {"path": "live_set", "properties": ["tempo"]}}
    payload = json.dumps(command).encode("utf-8")
    cut = payload.index(b"}") + 1  # right after the inner object closes

    inst, received = make_transport(script_module)
    sock = FakeSocket([payload[:cut], payload[cut:]])
    serve(inst, sock)
    assert received == [command]
    assert len(sock.replies()) == 1


def test_a_connection_that_does_not_send_json_is_dropped_unanswered(script_module):
    """A web page can make a browser POST to localhost; with line framing the
    JSON body would otherwise run as a command."""
    body = json.dumps({"type": "lom_call", "params": {"path": "live_set",
                                                       "method": "stop_playing"}})
    http = ("POST / HTTP/1.1\r\nHost: 127.0.0.1:9877\r\nContent-Type: text/plain\r\n\r\n"
            + body + "\n").encode("utf-8")
    inst, received = make_transport(script_module)
    sock = FakeSocket([http], eof=False)
    serve(inst, sock)
    assert received == []
    assert bytes(sock.sent) == b""
    assert sock.closed


def test_commands_wait_for_hello_with_the_session_token(script_module):
    def line(command_type, **params):
        return json.dumps({"type": command_type, "params": params}).encode("utf-8") + b"\n"

    inst, received = make_transport(script_module, token="s3cret")
    inst._get_script_info = lambda: {"script_version": "test"}
    sock = FakeSocket([line("get_script_info"), line("lom_get", path="live_set"),
                       line("hello", token="s3cret"), line("lom_get", path="live_set")],
                      eof=False)
    serve(inst, sock)
    replies = sock.replies()
    assert replies[0] == {"status": "success", "result": {"echo": "get_script_info"}}
    assert replies[1]["status"] == "error" and "Authentication required" in replies[1]["message"]
    assert replies[2] == {"status": "success", "result": {"script_version": "test"}}
    assert replies[3] == {"status": "success", "result": {"echo": "lom_get"}}
    assert [c["type"] for c in received] == ["get_script_info", "lom_get"]


def test_a_wrong_token_closes_the_connection(script_module):
    inst, received = make_transport(script_module, token="s3cret")
    hello = json.dumps({"type": "hello", "params": {"token": "guess"}}).encode("utf-8")
    after = json.dumps({"type": "lom_get", "params": {}}).encode("utf-8")
    sock = FakeSocket([hello + b"\n" + after + b"\n"], eof=False)
    serve(inst, sock)
    replies = sock.replies()
    assert len(replies) == 1 and "Invalid session token" in replies[0]["message"]
    assert received == []
    assert sock.closed


def test_partial_sends_are_finished_on_later_polls(script_module):
    inst, _received = make_transport(script_module)
    request = json.dumps({"id": 1, "type": "x" * 100, "params": {}}).encode("utf-8")
    sock = FakeSocket([request + b"\n"], eof=False, allowance=16)
    client = serve(inst, sock, polls=1)
    assert len(sock.sent) == 16 and client.outbox
    for _ in range(20):
        sock.allowance = 16  # the client read some
        inst._poll_sockets()
    assert sock.replies()[0]["id"] == 1 and not client.outbox


def test_a_request_sent_just_before_closing_still_runs(script_module):
    inst, received = make_transport(script_module)
    sock = FakeSocket([b'{"type": "stop_playback", "params": {}}'], eof=True)
    client = serve(inst, sock, polls=2)
    assert [c["type"] for c in received] == ["stop_playback"]
    assert sock.closed and client not in inst._clients


def test_poll_events_can_wait_for_the_next_event(script_module):
    inst, _received = make_transport(script_module, echo=False)
    inst._lom_init()
    request = json.dumps({"id": 7, "type": "poll_events",
                          "params": {"since": 0, "wait": 5}}).encode("utf-8")
    sock = FakeSocket([request + b"\n"], eof=False)
    client = serve(inst, sock, polls=2)
    assert sock.replies() == [] and client.waiting is not None
    inst._push_event({"observer_id": 1, "property": "tempo", "value": 99.0})
    inst._poll_sockets()
    reply = sock.replies()[0]
    assert reply["id"] == 7 and reply["result"]["events"][0]["value"] == 99.0


def test_poll_events_wait_ends_without_events(script_module):
    import time as _time

    inst, _received = make_transport(script_module, echo=False)
    inst._lom_init()
    request = json.dumps({"type": "poll_events", "params": {"since": 0, "wait": 0.05}})
    sock = FakeSocket([request.encode("utf-8") + b"\n"], eof=False)
    serve(inst, sock, polls=1)
    assert sock.replies() == []
    _time.sleep(0.06)
    inst._poll_sockets()
    assert sock.replies()[0]["result"]["events"] == []


def test_split_messages_handles_both_framings(script_module):
    split = script_module.split_messages
    assert split(b'{"a": 1}\n{"b": 2}\n{"c"') == ([b'{"a": 1}', b'{"b": 2}'], b'{"c"')
    assert split(b'{"a": {"b": 1}') == ([], b'{"a": {"b": 1}')
    assert split(b'  {"a": 1}  ') == ([b'{"a": 1}'], b"")
    assert split(b"\n\n") == ([], b"")


def test_the_session_file_is_private_and_removed_on_disconnect(script_module, tmp_path,
                                                                 monkeypatch):
    import os
    import stat

    session = tmp_path / "sub" / "live-session.json"
    monkeypatch.setattr(script_module, "SESSION_FILE", str(session))
    inst, _received = make_transport(script_module)
    token = inst._publish_session()
    data = json.loads(session.read_text())
    assert data["token"] == token and len(token) == 32
    assert data["port"] == script_module.DEFAULT_PORT
    if os.name == "posix":
        assert stat.S_IMODE(session.stat().st_mode) == 0o600
        assert stat.S_IMODE(session.parent.stat().st_mode) == 0o700
    inst._session_token = token
    inst._withdraw_session()
    assert not session.exists()


def test_auth_can_be_switched_off(script_module, monkeypatch):
    monkeypatch.setattr(script_module, "AUTH_REQUIRED", False)
    inst, _received = make_transport(script_module)
    assert inst._publish_session() is None


# ------------------------------------------------------------- browser tree

class FakeBrowserItem(object):
    def __init__(self, name, children=(), loadable=False, uri=None):
        self.name = name
        self.children = list(children)
        self.is_device = False
        self.is_loadable = loadable
        self.uri = uri


class FakeBrowser(object):
    def __init__(self, instruments):
        self.instruments = instruments


class FakeApplication(object):
    def __init__(self, browser=None):
        self.browser = browser


def test_browser_tree_returns_children_and_a_truthful_folder_count(script_module):
    """Regression: process_item never recursed (children was always []) and
    total_folders was never returned, so the server's header always read
    'showing 0 folders'."""
    kits = FakeBrowserItem("Kits", [FakeBrowserItem("808 Core Kit", loadable=True)])
    browser = FakeBrowser(FakeBrowserItem("Instruments", [kits]))
    inst = make_instance(script_module, application=FakeApplication(browser=browser))

    tree = inst.get_browser_tree("instruments")

    assert tree["total_folders"] >= 2  # Instruments and Kits both have children
    category = tree["categories"][0]
    assert [child["name"] for child in category["children"]] == ["Kits"]
    # Depth is capped one level below the category, so Kits reports more.
    assert category["children"][0]["has_more"] is True


def test_browser_tree_caps_a_huge_folder_and_flags_it(script_module):
    many = [FakeBrowserItem("Preset %d" % i, loadable=True) for i in range(200)]
    browser = FakeBrowser(FakeBrowserItem("Instruments", many))
    inst = make_instance(script_module, application=FakeApplication(browser=browser))

    category = inst.get_browser_tree("instruments")["categories"][0]

    assert len(category["children"]) == 64
    assert category["has_more"] is True
