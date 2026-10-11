"""The simulator: the real Remote Script over the fake Live, served on a port."""

import asyncio
import json
import os
import socket
import subprocess
import sys

import pytest

from MCP_Server.simulator import Simulator


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def tools(monkeypatch):
    """The extended tools against a simulator, called like the live_server fixture does."""
    from mcp.server.mcpserver import MCPServer

    from MCP_Server import extended_tools
    from MCP_Server.connection import AbletonConnection

    def connect(simulator):
        monkeypatch.setenv("ABLETON_MCP_SESSION_FILE", str(simulator.session_file))
        connection = AbletonConnection(host="127.0.0.1", port=simulator.port)
        mcp = MCPServer("simulated")
        extended_tools.register(mcp, lambda: connection)

        def call(_tool, **arguments):
            result = asyncio.run(mcp.call_tool(_tool, arguments))
            text = result.content[0].text
            return json.loads(text) if text[:1] in "{[" else text

        call.connection = connection
        return call
    return connect


def test_tools_change_the_simulated_set_and_the_state_file_follows(tmp_path, tools):
    state_file = tmp_path / "state.json"
    with Simulator(free_port(), tmp_path / "session.json", state_file) as simulator:
        call = tools(simulator)
        call("manage_track", action="create_midi", name="Keys")
        call("generate_chord_progression", track="Keys", progression="Am F", clip_index=0)
        call("apply_device_chain", track="Bass", chain="bass")
        call("set_track_mixer", track="Keys", volume_db=-6, pan="30L")
        call.connection.disconnect()
        state = json.loads(state_file.read_text())
        assert state == simulator.state()

    keys = next(t for t in state["tracks"] if t["name"] == "Keys")
    assert keys["volume"]["display"] == "-6.0 dB" and keys["pan"]["display"] == "30L"
    clip = keys["clip_slots"]["0"]
    assert clip["length"] == 8 and {n[0] % 12 for n in clip["notes"]} == {9, 0, 4, 5}
    bass = next(t for t in state["tracks"] if t["name"] == "Bass")
    chain = {d["name"]: d["parameters"] for d in bass["devices"]}
    assert list(chain) == ["Operator", "EQ Eight", "Compressor", "Saturator", "Utility"]
    assert chain["EQ Eight"]["1 Filter Type A"] == "High Pass 12dB"
    assert chain["Compressor"]["Threshold"] == "-18.00 dB"
    assert chain["Saturator"]["Type"] == "Soft Sine" and chain["Utility"]["Bass Mono"] == "On"
    # One undo step per modifying command; none undone
    assert state["undo"]["steps"] >= 4 and state["undo"]["undone"] == 0


def test_setup_commands_shape_the_set_without_undo_history(tmp_path):
    simulator = Simulator(free_port(), tmp_path / "session.json")
    simulator.run_setup([
        {"type": "lom_set", "params": {"path": "live_set", "property": "tempo", "value": 92}},
        {"type": "lom_call", "params": {"path": "live_set tracks 1 clip_slots 0",
                                        "method": "create_clip", "args": [4.0]}},
        {"type": "clip_write_notes", "params": {"track_index": 1, "clip_index": 0, "notes": [
            {"pitch": 40, "start_time": 0, "duration": 1, "velocity": 100}]}},
    ])
    state = simulator.state()
    assert state["tempo"] == 92 and state["undo"]["steps"] == 0
    assert state["tracks"][1]["clip_slots"]["0"]["notes"] == [[40, 0, 1, 100]]
    with pytest.raises(RuntimeError, match="Setup command lom_set failed"):
        simulator.run_setup([{"type": "lom_set", "params": {"path": "live_set nothing",
                                                           "property": "x", "value": 1}}])


def test_the_command_line_simulator_serves_until_stopped(tmp_path):
    port, session, state = free_port(), tmp_path / "session.json", tmp_path / "state.json"
    setup = tmp_path / "setup.json"
    setup.write_text(json.dumps([{"type": "lom_set", "params": {
        "path": "live_set", "property": "tempo", "value": 101}}]))
    process = subprocess.Popen(
        [sys.executable, "-m", "MCP_Server.simulator", "--port", str(port), "--session-file",
         str(session), "--state-file", str(state), "--setup", str(setup)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=dict(os.environ, ABLETON_MCP_DISABLE_TELEMETRY="true"))
    try:
        assert "127.0.0.1:%d" % port in process.stdout.readline()
        from MCP_Server.connection import AbletonConnection

        os.environ["ABLETON_MCP_SESSION_FILE"] = str(session)
        try:
            connection = AbletonConnection(host="127.0.0.1", port=port)
            assert connection.connect(), connection.last_error
            assert connection.send_command("lom_get", {"path": "live_set",
                                                       "properties": ["tempo"]})["tempo"] == 101
            connection.send_command("lom_set", {"path": "live_set", "property": "tempo",
                                                "value": 133})
            connection.disconnect()
        finally:
            del os.environ["ABLETON_MCP_SESSION_FILE"]
        assert json.loads(state.read_text())["tempo"] == 133
    finally:
        process.terminate()
        process.wait(timeout=10)
    if sys.platform != "win32":  # Windows' terminate() is a kill: no clean-up to check
        assert process.returncode == 0 and not session.exists()


def test_the_fake_live_undoes_like_live():
    from MCP_Server.fake_live import FakeSong

    song = FakeSong()
    bass = song.tracks[1]

    def command(change):
        song.begin_undo_step()
        change()
        song.end_undo_step()

    def mixed():
        bass.mixer_device.volume.value = 0.5
        bass.name = "Sub"

    command(mixed)
    assert len(song._undo) == 2
    command(lambda: setattr(bass, "solo", True))  # Live leaves solo out
    command(lambda: setattr(bass, "name", "Sub"))  # the same name again
    assert len(song._undo) == 2
    command(lambda: song.create_scene(-1))
    command(song.undo)  # undo inside a command is no step of its own
    assert len(song.scenes) == 2 and len(song._undo) == 2 and song.can_redo
    song.undo()
    song.undo()
    assert (bass.name, bass.mixer_device.volume.value, bass.solo) == ("Bass", 0.85, True)
    assert not song.can_undo
    song.redo()
    song.redo()
    assert {bass.name, bass.mixer_device.volume.value} == {"Sub", 0.5}
    command(lambda: song.delete_track(0))  # a new change drops what could be redone
    assert not song.can_redo and [t.name for t in song.tracks] == ["Sub", "Vox"]
    song.undo()
    assert [t.name for t in song.tracks] == ["Drums", "Sub", "Vox"]


def test_the_server_finds_live_through_its_session_file(tmp_path, monkeypatch):
    from MCP_Server.connection import live_port

    session = tmp_path / "live-session.json"
    monkeypatch.setenv("ABLETON_MCP_SESSION_FILE", str(session))
    monkeypatch.delenv("ABLETON_PORT", raising=False)
    assert live_port() == 9877  # nothing published
    session.write_text(json.dumps({"port": 9911, "token": "x"}))
    assert live_port() == 9911
    monkeypatch.setenv("ABLETON_PORT", "9922")
    assert live_port() == 9922


def test_capabilities_come_with_every_connection(tmp_path, monkeypatch):
    """A server started before Live has no handshake; connecting fills it in."""
    from MCP_Server import script_handshake
    from MCP_Server.connection import AbletonConnection

    monkeypatch.setattr(script_handshake, "_script_info", None)
    assert not script_handshake.script_has_capability("create_locator")
    with Simulator(free_port(), tmp_path / "session.json") as simulator:
        monkeypatch.setenv("ABLETON_MCP_SESSION_FILE", str(simulator.session_file))
        connection = AbletonConnection(host="127.0.0.1", port=simulator.port)
        assert connection.connect(), connection.last_error
        connection.disconnect()
    assert script_handshake.script_has_capability("create_locator")
    assert script_handshake.script_version_ok()
