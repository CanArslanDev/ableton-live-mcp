"""The generic LOM bridge in the Remote Script, driven against a fake Live.

The first group calls the command handlers directly. The last group runs the
real TCP server from the Remote Script and the MCP extended tools against it,
so the whole path (tool → JSON → socket → main-thread dispatch → LOM) is
exercised without Ableton.
"""

import asyncio
import importlib.util
import json
import socket
import sys
import types
from pathlib import Path

import pytest

from MCP_Server import fake_live

REPO_ROOT = Path(__file__).resolve().parents[1]


def note_dicts(result):
    """A tool result's notes (rows under note_columns) as one dict per note."""
    extras = result.get("note_extras", {})
    return [dict(zip(result["note_columns"], row), **extras.get(str(i), {}))
            for i, row in enumerate(result["notes"])]
BUNDLED_SCRIPT = REPO_ROOT / "MCP_Server" / "bundled_ableton_remote_script" / "AbletonMCP_init.py"


def _load_script_module():
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
    spec = importlib.util.spec_from_file_location("ableton_mcp_lom_under_test", BUNDLED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def script():
    fake_live.install_fake_live_module()
    module = _load_script_module()
    surface = module.AbletonMCP.__new__(module.AbletonMCP)
    surface._song = fake_live.FakeSong()
    surface._lom_init()
    surface.schedule_message = lambda delay, fn: fn()
    browser = fake_live.FakeBrowser(surface._song)
    app = fake_live.FakeApplication(browser)
    surface.application = lambda: app
    surface.module = module
    return surface


def run(surface, command, **params):
    response = surface._process_command({"type": command, "params": params})
    assert response["status"] == "success", response
    return response["result"]


def run_error(surface, command, **params):
    response = surface._process_command({"type": command, "params": params})
    assert response["status"] == "error", response
    return response["message"]


# ── Path resolution & serialisation ─────────────────────────────────────────

def test_paths_by_index_negative_index_name_and_id(script):
    by_index = run(script, "lom_get", path="live_set tracks 1", properties=["name"])
    assert by_index["name"] == "Bass"
    assert run(script, "lom_get", path="live_set tracks -1", properties=["name"])["name"] == "Vox"
    by_name = run(script, "lom_get", path='live_set tracks "bass" devices "Operator" parameters '
                                          '"Filter Freq"', properties=["name", "value"])
    assert by_name["name"] == "Filter Freq"
    again = run(script, "lom_get", path="id %d" % by_name["$id"], properties=["value"])
    assert again["value"] == 0.5
    assert run(script, "lom_get", path=["live_set", "tracks", 0], properties=["name"])["name"] \
        == "Drums"


def test_describe_lists_properties_children_and_methods(script):
    info = run(script, "lom_get", path="live_set")
    assert info["properties"]["tempo"] == 120.0
    assert info["children"]["tracks"] == {"$len": 3}
    assert "create_midi_track" in info["methods"]
    assert not any(name.startswith("_") for name in info["properties"])


def test_sequence_get_reads_properties_per_item(script):
    result = run(script, "lom_get", path="live_set tracks", properties=["name", "mute"])
    assert [t["name"] for t in result["items"]] == ["Drums", "Bass", "Vox"]
    assert result["length"] == 3


def test_private_attributes_and_bad_paths_are_rejected(script):
    assert "Private attribute" in run_error(script, "lom_get", path="live_set __class__")
    assert "Private attribute" in run_error(script, "lom_call", path="live_set",
                                            method="__init__")
    assert "out of range" in run_error(script, "lom_get", path="live_set tracks 9")
    assert "Available: Drums, Bass, Vox" in run_error(script, "lom_get",
                                                      path="live_set tracks Nope")
    assert "Unknown id" in run_error(script, "lom_get", path="id 1")


def test_enum_values_serialise_with_their_name(script):
    script._song.tracks[0].clip_slots[0].create_clip(4.0)
    value = run(script, "lom_get", path="live_set tracks 0 clip_slots 0 clip",
                properties=["warp_mode"])["warp_mode"]
    assert value == {"$enum": "beats", "value": 0}


# ── lom_set / lom_call ──────────────────────────────────────────────────────

def test_set_coerces_ints_to_floats_and_names_to_enums(script):
    result = run(script, "lom_set", path="live_set", property="tempo", value=128)
    assert result == {"property": "tempo", "old_value": 120.0, "value": 128.0}
    script._song.tracks[0].clip_slots[0].create_clip(4.0)
    clip_path = "live_set tracks 0 clip_slots 0 clip"
    run(script, "lom_set", path=clip_path, property="warp_mode", value="complex")
    assert isinstance(script._song.tracks[0].clip_slots[0].clip.warp_mode, fake_live.WarpMode)
    run(script, "lom_set", path=clip_path, property="warp_mode", value=6)
    assert str(script._song.tracks[0].clip_slots[0].clip.warp_mode) == "complex_pro"
    assert "not one of" in run_error(script, "lom_set", path=clip_path, property="warp_mode",
                                     value="bogus")


def test_set_resolves_object_references(script):
    run(script, "lom_set", path="live_set tracks 2", property="input_routing_type",
        value={"$ref": 'live_set tracks 2 available_input_routing_types "Resampling"'})
    assert script._song.tracks[2].input_routing_type.display_name == "Resampling"


def test_call_passes_refs_and_enums(script):
    script._song.tracks[0].clip_slots[0].create_clip(4.0)
    run(script, "lom_call", path="live_set tracks 0 clip_slots 0", method="duplicate_clip_to",
        args=[{"$ref": "live_set tracks 0 clip_slots 3"}])
    assert script._song.tracks[0].clip_slots[3].has_clip
    run(script, "lom_call", path="live_set tracks 0 clip_slots 0 clip", method="quantize",
        args=[{"$enum": "Live.Song.RecordingQuantization", "value": 5}, 0.5])
    assert script._song.tracks[0].clip_slots[0].clip.calls[-1] == \
        ("quantize", "rec_q_sixtenth", 0.5)


# ── Batch ───────────────────────────────────────────────────────────────────

def test_batch_runs_in_one_undo_step_and_stops_on_error(script):
    result = run(script, "batch", commands=[
        {"type": "lom_call", "params": {"path": "live_set", "method": "create_midi_track",
                                        "args": [-1]}},
        {"type": "lom_set", "params": {"path": "live_set tracks -1", "property": "name",
                                       "value": "Pad"}},
        {"type": "lom_get", "params": {"path": "live_set tracks 99"}},
        {"type": "lom_set", "params": {"path": "live_set", "property": "tempo", "value": 90}},
    ])
    assert script._song.undo_log == ["begin", "end"]  # the whole batch is one undo step
    assert result["completed"] == 2 and result["failed"] == 1
    assert script._song.tracks[-1].name == "Pad"
    assert script._song.tempo == 120.0  # never reached


def test_batch_can_continue_past_errors_and_rejects_nesting(script):
    result = run(script, "batch", stop_on_error=False, commands=[
        {"type": "batch", "params": {}},
        {"type": "lom_set", "params": {"path": "live_set", "property": "tempo", "value": 90}},
    ])
    assert result["results"][0]["status"] == "error"
    assert script._song.tempo == 90.0
    assert script._song.undo_log == ["begin", "end"]


# ── Observers ───────────────────────────────────────────────────────────────

def test_observers_buffer_changes_until_removed(script):
    watched = run(script, "observe", path="live_set", property="tempo")
    assert watched["value"] == 120.0
    assert run(script, "observe", path="live_set", property="tempo")["already_observed"]
    script._song.tempo = 100.0
    script._song.tempo = 101.0
    events = run(script, "poll_events", since=0)
    assert [e["value"] for e in events["events"]] == [100.0, 101.0]
    later = run(script, "poll_events", since=events["latest_seq"])
    assert later["events"] == []
    run(script, "unobserve", observer_id="all")
    script._song.tempo = 102.0
    assert run(script, "poll_events", since=events["latest_seq"])["events"] == []
    assert "not observable" in run_error(script, "observe", path="live_set tracks 0 devices",
                                         property="name")


# ── Notes ───────────────────────────────────────────────────────────────────

def _clip(script):
    script._song.tracks[1].clip_slots[0].create_clip(8.0)
    return "live_set tracks 1 clip_slots 0 clip"


def test_note_modes_add_replace_modify_remove(script):
    path = _clip(script)
    added = run(script, "clip_write_notes", clip_path=path, mode="add", notes=[
        {"pitch": 60, "start_time": 0, "duration": 1, "velocity": 100, "probability": 0.5},
        {"pitch": 64, "start_time": 1, "duration": 1, "velocity": 300}])
    assert added["note_ids"] == [1, 2]
    notes = run(script, "clip_get_notes", clip_path=path)["notes"]
    assert notes[0]["probability"] == 0.5 and notes[1]["velocity"] == 127.0

    run(script, "clip_write_notes", clip_path=path, mode="modify",
        notes=[{"note_id": 2, "pitch": 67}])
    assert [n["pitch"] for n in run(script, "clip_get_notes", clip_path=path)["notes"]] == [60, 67]

    run(script, "clip_write_notes", clip_path=path, mode="remove_ids", note_ids=[1])
    run(script, "clip_write_notes", clip_path=path, mode="replace", from_time=0, time_span=4,
        notes=[{"pitch": 48, "start_time": 2, "duration": 0.5}])
    pitches = [n["pitch"] for n in run(script, "clip_get_notes", clip_path=path)["notes"]]
    assert pitches == [48]
    assert "outside 0..127" in run_error(script, "clip_write_notes", clip_path=path,
                                         notes=[{"pitch": 200, "start_time": 0}])


def test_envelope_writes_clamped_steps(script):
    path = _clip(script)
    param = "live_set tracks 1 mixer_device volume"
    result = run(script, "clip_write_envelope", clip_path=path, parameter_path=param,
                 points=[{"time": 0, "value": -1, "duration": 1}, {"time": 1, "value": 0.5}])
    assert result["written"] == 2
    clip = script._song.tracks[1].clip_slots[0].clip
    steps = clip.envelopes[id(script._song.tracks[1].mixer_device.volume)].steps
    assert steps == [(0.0, 1.0, 0.0), (1.0, 0.0, 0.5)]


# ── Parameters by display value ─────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("800 Hz", 800.0), ("1.20 kHz", 1200.0), ("-6.0 dB", -6.0), ("350 ms", 350.0),
    ("1.50 s", 1500.0), ("50L", -50.0), ("C", 0.0), ("25R", 25.0), ("40 %", 40.0),
    ("-inf dB", float("-inf")), ("Lowpass", None),
    (".6", 0.6), ("-.5 dB", -0.5), ("4.00 : 1", 4.0),
])
def test_parse_display_number(script, text, expected):
    assert script.module.parse_display_number(text) == expected


def test_param_set_display_finds_values_by_bisection(script):
    vol = run(script, "param_set_display", path="live_set tracks 0 mixer_device volume",
              value="-6 dB")
    assert vol["new"] == "-6.0 dB"
    freq = run(script, "param_set_display",
               path='live_set tracks 1 devices 0 parameters "Filter Freq"', value="1.2 kHz")
    assert freq["new"] == "1.20 kHz"
    pan = run(script, "param_set_display", path="live_set tracks 0 mixer_device panning",
              value="20L")
    assert pan["new"] == "20L"
    silent = run(script, "param_set_display", path="live_set tracks 0 mixer_device volume",
                 value="-inf dB")
    assert silent["new"] == "-inf dB"
    kind = run(script, "param_set_display",
               path='live_set tracks 1 devices 0 parameters "Filter Type"', value="bandpass")
    assert kind["new"] == "Bandpass"


def test_session_overview(script):
    overview = run(script, "get_session_overview")
    assert [t["name"] for t in overview["tracks"]] == ["Drums", "Bass", "Vox"]
    assert overview["tracks"][2]["type"] == "audio"
    assert overview["return_tracks"][0]["name"] == "A-Reverb"
    assert overview["tracks"][0]["volume"] == "0.0 dB"
    assert overview["time_signature"] == "4/4"


def test_every_new_command_is_advertised(script):
    info = run(script, "get_script_info")
    for command in script._lom_command_handlers():
        assert command in info["capabilities"]


# ── End to end: MCP tools → TCP → Remote Script → fake Live ─────────────────

@pytest.fixture
def live_server(script, tmp_path, monkeypatch):
    import threading
    import time as _time

    module = script.module
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    module.DEFAULT_PORT = port
    session = tmp_path / "live-session.json"
    module.SESSION_FILE = str(session)
    monkeypatch.setenv("ABLETON_MCP_SESSION_FILE", str(session))
    script._transport_init()
    script.start_server()
    assert script._session_token, "the script should publish a session token"

    # Stand-in for Live.Base.Timer: poll the sockets from one "main" thread
    ticking = threading.Event()
    ticking.set()

    def tick():
        while ticking.is_set():
            script._poll_sockets()
            _time.sleep(0.002)

    ticker = threading.Thread(target=tick, daemon=True)
    ticker.start()
    from MCP_Server.server import AbletonConnection
    connection = AbletonConnection(host="127.0.0.1", port=port)

    from mcp.server.mcpserver import MCPServer
    from MCP_Server import extended_tools
    mcp = MCPServer("test")
    extended_tools.register(mcp, lambda: connection)

    def call(_tool, **arguments):
        content = asyncio.run(mcp.call_tool(_tool, arguments))
        blocks = content.content
        text = blocks[0].text
        assert not text.startswith("Error"), text
        try:
            return json.loads(text)
        except ValueError:
            return text

    call.mcp = mcp  # for tests that need a client session (elicitation)
    yield script, call
    connection.disconnect()
    ticking.clear()
    ticker.join()
    script.disconnect = lambda: None
    script.running = False
    for client in list(script._clients):
        script._drop_client(client)
    script.server.close()


def test_tools_mixer_tracks_and_scenes(live_server):
    script, call = live_server
    song = script._song
    result = call("set_track_mixer", track="Bass", volume_db=-6, pan="30R", mute=True,
                  sends_db={"A": -12})
    assert result["ok"]
    bass = song.tracks[1]
    assert bass.mixer_device.volume.str_for_value(bass.mixer_device.volume.value) == "-6.0 dB"
    assert bass.mixer_device.panning.str_for_value(bass.mixer_device.panning.value) == "30R"
    assert bass.mute is True
    call("set_track_mixer", track="return A", volume_db=-3)
    assert song.return_tracks[0].mixer_device.volume.str_for_value(
        song.return_tracks[0].mixer_device.volume.value) == "-3.0 dB"

    call("manage_track", action="create_midi", name="Keys", color="#FF8800")
    assert song.tracks[-1].name == "Keys" and song.tracks[-1].color == 0xFF8800
    call("manage_track", action="delete", track="Keys")
    assert [t.name for t in song.tracks] == ["Drums", "Bass", "Vox"]

    call("manage_scene", action="create", name="Outro")
    assert song.scenes[-1].name == "Outro"

    call("set_track_routing", track="Vox", input_type="Resampling")
    assert song.tracks[2].input_routing_type.display_name == "Resampling"
    options = call("set_track_routing", track="Vox")
    assert options["available_input_routing_types"] == ["Ext. In", "Resampling", "Drums"]


def test_tools_generators_and_transforms(live_server):
    script, call = live_server
    song = script._song
    result = call("generate_chord_progression", track="Bass", progression="i VI III VII",
                  key="A minor", clip_index=1, rhythm="quarters")
    assert [c["chord"] for c in result["chords"]] == ["i", "VI", "III", "VII"]
    clip = song.tracks[1].clip_slots[1].clip
    assert clip.length == 16.0 and clip.name == "i VI III VII"
    assert len(clip._notes) == 4 * 4 * 3

    call("transform_clip_notes", track="Bass", clip_index=1, operation="transpose",
         semitones=12, pitch_low="C4")
    pitches = sorted({n.pitch for n in clip._notes})
    assert max(pitches) >= 72

    drums = call("generate_drum_pattern", track="Drums", style="house", bars=2,
                 humanize=0, fill=False)
    assert drums["pad_mapping"] == {"kick": 36, "snare": 38, "closed_hat": 42}
    kick_hits = [n for n in song.tracks[0].clip_slots[0].clip._notes if n.pitch == 36]
    assert len(kick_hits) == 8

    call("generate_bassline", track="Bass", progression="I IV V", key="C", clip_index=2,
         style="octave")
    assert len(song.tracks[1].clip_slots[2].clip._notes) == 3 * 8

    notes = call("read_clip_notes", track="Bass", clip_index=2, pitch_low="C1", pitch_high="B1")
    assert all(n["name"].endswith("1") for n in note_dicts(notes))

    call("edit_clip_notes", track="Bass", clip_index=2, mode="add",
         notes=[{"pitch": "C3", "start_time": 0, "duration": 1}])
    assert any(n.pitch == 60 for n in song.tracks[1].clip_slots[2].clip._notes)


def test_tools_automation_devices_and_song(live_server):
    script, call = live_server
    song = script._song
    song.tracks[1].clip_slots[0].create_clip(8.0)
    result = call("write_clip_automation", track="Bass", clip_index=0, device="Operator",
                  parameter="Filter Freq", shape="ramp_up", resolution=1.0)
    assert result["written"] == 8 and result["parameter"] == "Filter Freq"

    call("set_device_parameter_by_name", track="Bass", device="Operator",
         parameter="Filter Freq", display_value="800 Hz")
    freq = song.tracks[1].devices[0].parameters[1]
    assert freq.str_for_value(freq.value) == "800 Hz"

    call("manage_device", track="Bass", device="Operator", action="disable")
    assert song.tracks[1].devices[0].parameters[0].value == 0.0
    call("manage_device", track="Bass", device="Operator", action="delete")
    assert song.tracks[1].devices == []

    call("set_song_properties", tempo=124, time_signature="7/8", swing=0.3,
         midi_recording_quantization="1/16")
    assert (song.tempo, song.signature_numerator, song.signature_denominator) == (124.0, 7, 8)
    assert str(song.midi_recording_quantization) == "rec_q_sixtenth"

    call("manage_clip", action="quantize", track="Bass", clip_index=0, grid="1/16",
         amount=0.5)
    assert song.tracks[1].clip_slots[0].clip.calls[-1] == ("quantize", "rec_q_sixtenth", 0.5)

    pads = call("get_drum_pads", track="Drums")
    assert [p["name"] for p in pads["pads"]] == ["Kick 808", "Snare Tight", "Closed Hat"]


def test_tools_watch_and_events(live_server):
    script, call = live_server
    started = call("watch_live", preset="transport")
    assert len(started["watching"]) == 4
    script._song.tempo = 99.0
    events = call("get_live_events", since=started["events_since"])
    assert events["events"][-1]["value"] == 99.0
    assert len(events["observers"]) == 4
    call("unwatch_live")
    assert call("get_live_events")["observers"] == []


def test_tool_errors_are_reported_as_tool_errors(live_server):
    from mcp.server.mcpserver.exceptions import ToolError

    script, call = live_server
    result = call("set_track_mixer", track="Nope", mute=True)
    assert result["ok"] is False
    assert "No item named 'Nope'" in result["steps"][0]["error"]
    # The SDK turns the ToolError into a result with isError=true
    with pytest.raises(ToolError, match="manage_scene: Unknown action 'explode'"):
        call("manage_scene", action="explode", scene=0)


def test_live_vector_types_are_treated_as_lists(script):
    class Vector(object):  # like Live's MidiNoteVector: no _live_ptr
        __module__ = "Live.Clip"

        def __init__(self, items):
            self.items = items

        def __len__(self):
            return len(self.items)

        def __getitem__(self, index):
            return self.items[index]

    script._song.vector = Vector([fake_live.FakeObj(name="x"), fake_live.FakeObj(name="y")])
    assert script.module.lom_is_sequence(script._song.vector)
    assert run(script, "lom_get", path="live_set vector 1", properties=["name"])["name"] == "y"


def test_value_objects_outside_the_live_module_become_references(script):
    class RoutingType(object):  # Live 12 reports module "Track", no _live_ptr
        __module__ = "Track"

        def __init__(self, display_name):
            self.display_name = display_name

    script._song.tracks[2].available_input_routing_types = [RoutingType("Ext. In"),
                                                            RoutingType("Resampling")]
    items = run(script, "lom_get", path="live_set tracks 2",
                properties=["available_input_routing_types"])["available_input_routing_types"]
    assert [i["display_name"] for i in items] == ["Ext. In", "Resampling"]
    assert all("$id" in i for i in items)


def test_each_modifying_command_is_its_own_undo_step(script):
    run(script, "lom_get", path="live_set", properties=["tempo"])
    assert script._song.undo_log == []
    run(script, "lom_set", path="live_set", property="tempo", value=99)
    run(script, "batch", commands=[
        {"type": "lom_set", "params": {"path": "live_set", "property": "tempo", "value": 98}},
        {"type": "lom_set", "params": {"path": "live_set", "property": "swing_amount",
                                       "value": 0.1}}])
    assert script._song.undo_log == ["begin", "end", "begin", "end"]


def test_generators_follow_the_sets_meter_and_key(live_server):
    script, call = live_server
    song = script._song
    song.signature_numerator, song.signature_denominator = 3, 4
    song.root_note, song.scale_name = 2, "Dorian"
    song.scale_intervals = [0, 2, 3, 5, 7, 9, 10]
    result = call("generate_chord_progression", track="Bass", clip_index=3)
    # The default progression uses the mode's own chords: D Dorian → i IV i VII
    assert [c["chord"] for c in result["chords"]] == ["Dm", "G", "Dm", "C"]
    assert (result["key"], result["scale"], result["time_signature"]) == ("D", "Dorian", "3/4")
    clip = song.tracks[1].clip_slots[3].clip
    assert clip.length == 4 * 3.0
    drums = call("generate_drum_pattern", track="Drums", style="house", bars=2, fill=False,
                 humanize=0, clip_index=2)
    assert drums["length_beats"] == 6.0
    kicks = [n for n in song.tracks[0].clip_slots[2].clip._notes if n.pitch == 36]
    assert len(kicks) == 6


def test_transforms_edit_notes_in_place(live_server):
    script, call = live_server
    song = script._song
    call("generate_chord_progression", track="Bass", progression="I IV", key="C",
         clip_index=1)
    clip = song.tracks[1].clip_slots[1].clip
    ids_before = sorted(n.note_id for n in clip._notes)
    result = call("transform_clip_notes", track="Bass", clip_index=1, operation="transpose",
                  semitones=2)
    assert result["notes_modified"] == len(ids_before)
    assert sorted(n.note_id for n in clip._notes) == ids_before
    # Only the chosen notes are removed, by id
    result = call("transform_clip_notes", track="Bass", clip_index=1, operation="delete",
                  time_start=4.0)
    assert result["notes_removed"] == 3
    assert all(n.start_time < 4.0 for n in clip._notes)
    assert all(n.note_id in ids_before for n in clip._notes)


def test_analyze_harmony_reads_back_a_written_progression(live_server):
    script, call = live_server
    call("generate_chord_progression", track="Bass", progression="Am F C G", key="C",
         clip_index=1, voicing="close")
    result = call("analyze_harmony", track="Bass", clip_index=1)
    assert result["progression"] == "Am F C G"
    assert [w["roman"] for w in result["windows"]] == ["vi", "IV", "I", "V"]
    assert result["bars_per_chord"] == 1.0
    # …and a bassline can follow it
    call("generate_bassline", track="Bass", clip_index=2, progression=result["progression"],
         key="C", bars_per_chord=result["bars_per_chord"])
    roots = sorted(n.start_time for n in script._song.tracks[1].clip_slots[2].clip._notes)
    assert roots == [0.0, 4.0, 8.0, 12.0]


def test_midi_files_round_trip_through_clips(live_server, tmp_path):
    script, call = live_server
    song = script._song
    call("generate_arpeggio", track="Bass", progression="I IV", key="C", clip_index=1)
    source = song.tracks[1].clip_slots[1].clip
    exported = call("export_clip_to_midi", track="Bass", clip_index=1,
                    path=str(tmp_path / "arp.mid"))
    assert exported["notes"] == len(source._notes) and exported["tempo_bpm"] == 120.0
    listing = call("import_midi_file", path=exported["file"])
    assert [t["note_count"] for t in listing["tracks"]] == [0, len(source._notes)]
    call("import_midi_file", path=exported["file"], track="Bass", clip_index=3,
         transpose=12)
    copy = song.tracks[1].clip_slots[3].clip
    assert copy.length == 8.0 and copy.name == "arp"
    assert sorted(n.pitch for n in copy._notes) == sorted(n.pitch + 12 for n in source._notes)
    with pytest.raises(Exception, match="exists"):
        call("export_clip_to_midi", track="Bass", clip_index=1, path=exported["file"])


def test_import_from_live_set_writes_the_played_notes(live_server, tmp_path):
    import als_sets

    script, call = live_server
    path = str(als_sets.project(tmp_path))
    info = call("read_live_set", path=path)
    assert info["tempo"] == 124.0 and [t["name"] for t in info["tracks"]][1] == "Keys"
    listing = call("import_from_live_set", path=path, track="Keys", clip_index=1)
    assert len(listing["notes"]) == 2 and listing["notes_outside"] == 1

    result = call("import_from_live_set", path=path, track="Keys", arrangement_clip_index=0,
                  to_track="Bass", to_clip_index=3, midi_path=str(tmp_path / "mid"))
    clip = script._song.tracks[1].clip_slots[3].clip
    assert clip.name == "Verse" and clip.length == 8.0
    assert sorted((n.pitch, n.start_time, n.duration) for n in clip._notes) == [
        (62, 0.0, 1.0), (65, 7.5, 0.5)]
    assert result["notes_written"] == 2 and result["source"]["notes_outside"] == 1
    assert Path(result["midi_file"]["file"]).name == "Verse.mid"
    assert result["midi_file"]["time_signature"] == "7/8" and result["midi_file"]["tempo_bpm"] == 124.0


def _fake_ml(monkeypatch, tmp_path):
    """ml.run without models: what each job would return, and what it was asked."""
    import numpy as np
    import soundfile as sf

    from MCP_Server import ml

    jobs = []
    monkeypatch.setattr(ml, "INDEX_DIR", tmp_path / "sound-index")

    def vector(text):
        return [1.0, 0.0, 0.0] if "kick" in text.lower() else [0.0, 1.0, 0.0]

    async def run(task, job, report=None, timeout=3600.0):
        jobs.append((task, job))
        if report:
            await report(0.5, task)
        if task == "separate":
            out = Path(job["out_dir"])
            out.mkdir(parents=True, exist_ok=True)
            stems = {}
            for i, name in enumerate(["drums", "bass", "other", "vocals"]):
                t = np.arange(44100) / 44100
                level = 0.0 if name == "vocals" else 0.3
                sf.write(out / f"{name}.wav", level * np.sin(2 * np.pi * 110 * (i + 1) * t), 44100)
                stems[name] = str(out / f"{name}.wav")
            return {"stems": stems, "sample_rate": 44100, "model": job["model"], "device": "cpu",
                    "seconds": 1.0}
        if task == "transcribe":
            # bends as Basic Pitch gives them: thirds of a semitone, one bin high
            return {"notes": [{"start": 0.0, "end": 0.5, "pitch": 57, "amplitude": 0.8,
                               "bends": [1, 1, 2, 3]},
                              {"start": 1.0, "end": 1.5, "pitch": 60, "amplitude": 0.4,
                               "bends": [1, 1, 1]}],
                    "frame_s": 256 / 22050}
        if task == "embed":
            out = {}
            if job["texts"]:
                out["texts"] = np.array([vector(t) for t in job["texts"]], np.float32)
            if job["files"]:
                out["files"] = np.array([vector(Path(f).name) for f in job["files"]], np.float32)
            np.savez(job["out"], **out)
            return {"texts": len(job["texts"]), "files": len(job["files"]), "failed": []}
        raise AssertionError(task)

    monkeypatch.setattr(ml, "run", run)
    return jobs


def test_separate_stems_from_a_clip_onto_new_tracks(live_server, tmp_path, monkeypatch):
    import numpy as np
    import soundfile as sf

    script, call = live_server
    jobs = _fake_ml(monkeypatch, tmp_path)
    song = script._song
    source = tmp_path / "Song.wav"
    sf.write(source, np.zeros(4410), 44100)
    clip = fake_live.FakeClip(name="Song", length=8.0, midi=False)
    object.__setattr__(clip, "file_path", str(source))
    object.__setattr__(clip, "warping", False)
    vox = song.tracks[2]
    vox.clip_slots[1].clip, vox.clip_slots[1].has_clip = clip, True

    result = call("separate_stems", track="Vox", clip_index=1, to_tracks=True,
                  out_dir=str(tmp_path / "stems"))
    assert jobs[0][0] == "separate" and jobs[0][1]["path"] == str(source)
    assert set(result["stems"]) == {"drums", "bass", "other", "vocals"}
    assert result["stems"]["vocals"]["silent"] and "vocals" in result["observations"][0]
    assert result["stems"]["drums"]["lufs"] > -30
    names = [t.name for t in song.tracks]
    assert names[3:7] == ["Song drums", "Song bass", "Song other", "Song vocals"]
    placed = song.tracks[3].clip_slots[1].clip
    assert placed.file_path == result["stems"]["drums"]["file"] and placed.warping is False
    assert result["tracks"][0] == {"track": "Song drums", "clip_index": 1}


def _riff(path, rate=44100):
    """The fake transcription's two notes as tones: A2 (MIDI 57) at -6 dB from
    0 s, C3 (60) 12 dB lower from 1 s."""
    import numpy as np
    import soundfile as sf

    t = np.arange(int(0.5 * rate)) / rate
    y = np.zeros(2 * rate)
    y[:len(t)] = 0.5 * np.sin(2 * np.pi * 220.0 * t)
    y[rate:rate + len(t)] = 0.5 * 10 ** (-12 / 20) * np.sin(2 * np.pi * 261.63 * t)
    sf.write(path, y, rate)
    return path


def test_audio_to_midi_writes_notes_in_beats(live_server, tmp_path, monkeypatch):
    script, call = live_server
    jobs = _fake_ml(monkeypatch, tmp_path)
    wav = _riff(tmp_path / "Riff.wav")
    listing = call("audio_to_midi", path=str(wav), lowest="A1")
    assert [n["start_time"] for n in note_dicts(listing)] == [0, 2]
    # Live's note names: A1 is MIDI 45, 110 Hz
    assert jobs[0][1]["minimum_frequency"] == pytest.approx(110.0)
    # The melodic preset by default; velocity from each note's level: 12 dB down is 64
    assert {k: jobs[0][1][k] for k in ("onset_threshold", "frame_threshold", "minimum_note_ms",
                                       "melodia_trick")} == {
        "onset_threshold": 0.6, "frame_threshold": 0.3, "minimum_note_ms": 35.0, "melodia_trick": False}
    assert [n["velocity"] for n in note_dicts(listing)] == [127, 64]
    assert listing["velocity"] == {"from": "level", "loudest_db": -6.0}
    assert listing["timing"] == {"offset_beats": 0.0, "method": "tempo", "tempo": 120.0}
    assert "bends" not in listing
    result = call("audio_to_midi", path=str(wav), to_track="Bass", to_clip_index=2,
                  midi_path=str(tmp_path / "mid"), preset="legacy")
    clip = script._song.tracks[1].clip_slots[2].clip
    assert clip.name == "Riff MIDI" and clip.length == 4.0
    # legacy: Basic Pitch's own settings and velocity 127 × its confidence
    assert sorted((n.pitch, n.start_time, n.duration, n.velocity) for n in clip._notes) == [
        (57, 0.0, 1.0, 102), (60, 2.0, 1.0, 51)]
    assert jobs[1][1]["minimum_note_ms"] == 127.7 and jobs[1][1]["melodia_trick"] is True
    assert result["tempo_used"] == 120.0 and result["notes_found"] == 2
    assert Path(result["midi_file"]["file"]).name == "Riff MIDI.mid"
    # Given settings win over the preset; frequencies in Hz pass through
    call("audio_to_midi", path=str(wav), preset="chords", onset_threshold=0.4, highest="2093 Hz")
    assert jobs[2][1]["onset_threshold"] == 0.4 and jobs[2][1]["melodia_trick"] is True
    assert jobs[2][1]["maximum_frequency"] == 2093.0


def test_audio_to_midi_lists_corrected_bends(live_server, tmp_path, monkeypatch):
    _script, call = live_server
    _fake_ml(monkeypatch, tmp_path)
    listing = call("audio_to_midi", path=str(_riff(tmp_path / "Riff.wav")), bends=True,
                   offset_beats=4)
    # Basic Pitch's [1, 1, 2, 3] is in tune, then a third and two thirds of a
    # semitone sharp; the second note never bends and is left out
    assert listing["bends"] == {"frame_s": 0.01161, "notes": [
        {"pitch": 57, "start_time": 4.0, "cents": [0, 0, 33, 67]}]}
    assert [n["start_time"] for n in note_dicts(listing)] == [4, 6]


def test_audio_to_midi_follows_the_clips_warp_and_start_marker(live_server, tmp_path, monkeypatch):
    script, call = live_server
    _fake_ml(monkeypatch, tmp_path)
    wav = _riff(tmp_path / "Riff.wav")
    vox = script._song.tracks[2]
    clip = vox.clip_slots[0].create_audio_clip(str(wav))
    # Warped: Live's own beat for each time (the fake warps the file at 90 BPM),
    # less the start marker in beats
    clip.file_tempo, clip.start_marker = 90.0, 0.5
    warped = call("audio_to_midi", track="Vox", clip_index=0)
    assert [n["start_time"] for n in note_dicts(warped)] == [0, 1]
    assert warped["timing"]["method"] == "warp" and warped["timing"]["start_marker_beats"] == 0.5
    # Unwarped: the file in real time from the start marker (seconds) at the
    # set's tempo; the note that ends before the marker is left out
    clip.warping, clip.start_marker = False, 0.75
    unwarped = call("audio_to_midi", track="Vox", clip_index=0, offset_beats=1)
    assert [n["start_time"] for n in note_dicts(unwarped)] == [1.5]
    assert unwarped["timing"] == {"offset_beats": 1.0, "note": "The clip loops; the notes follow its file once.",
                                  "method": "tempo", "tempo": 120.0, "start_marker_seconds": 0.75,
                                  "notes_before_clip_start": 1}
    # A warped clip Live cannot map falls back to the tempo, and says so; audio
    # it cannot read for levels leaves Basic Pitch's velocities, and says so
    clip.warping, clip.start_marker = True, 0.0
    (tmp_path / "Riff.txt").write_text("not audio")
    clip.file_path = str(tmp_path / "Riff.txt")

    def no_warp_map(self, sample_time):
        raise RuntimeError("no warp map")

    monkeypatch.setattr(fake_live.FakeClip, "sample_to_beat_time", no_warp_map)
    fallback = call("audio_to_midi", track="Vox", clip_index=0)
    assert fallback["timing"]["method"] == "tempo" and "did not map" in fallback["timing"]["warning"]
    assert [n["start_time"] for n in note_dicts(fallback)] == [0, 2]
    assert fallback["velocity"]["from"] == "amplitude" and "Could not read" in fallback["velocity"]["warning"]
    assert [n["velocity"] for n in note_dicts(fallback)] == [102, 51]


def test_find_similar_sounds_indexes_once_then_searches(live_server, tmp_path, monkeypatch):
    _script, call = live_server
    jobs = _fake_ml(monkeypatch, tmp_path)
    folder = tmp_path / "Samples"
    folder.mkdir()
    for name in ["Kick Deep.wav", "Hat Closed.wav", "Kick Short.wav"]:
        (folder / name).write_bytes(name.encode())
    first = call("find_similar_sounds", text="punchy kick", folder=str(folder), top=2)
    assert [r["file"] for r in first["results"]] == ["Kick Deep.wav", "Kick Short.wav"]
    assert first["indexed"] == 3 and "not_indexed_yet" not in first
    again = call("find_similar_sounds", text="crisp hat", folder=str(folder), top=1)
    assert again["results"][0]["file"] == "Hat Closed.wav"
    assert jobs[-1][1]["files"] == []  # nothing new to index
    by_example = call("find_similar_sounds", like=str(folder / "Kick Deep.wav"),
                      folder=str(folder), top=1)
    assert by_example["results"][0]["file"] == "Kick Short.wav"
    with pytest.raises(Exception, match="Give text"):
        call("find_similar_sounds", folder=str(folder))


def test_analyzer_tools_write_the_device_and_read_its_track(live_server, tmp_path, monkeypatch):
    import threading
    import time as _time

    from test_analyzer import frame, osc

    from MCP_Server import als, analyzer, m4l_device

    script, call = live_server
    monkeypatch.setattr(als, "_user_libraries", lambda: [tmp_path / "User Library"])
    listener = analyzer.Listener(port=0)
    monkeypatch.setattr(analyzer, "listener", lambda: listener)
    script._song.master_track.devices.append(
        fake_live.FakeDevice(m4l_device.NAME, "MxDeviceAudioEffect"))
    sending = threading.Event()
    sending.set()

    def device():
        while sending.is_set():
            listener.feed(osc(m4l_device.ADDRESS, "Master", 1, *frame()), _time.monotonic())
            _time.sleep(0.05)

    thread = threading.Thread(target=device, daemon=True)
    thread.start()
    try:
        installed = call("install_analyzer")
        assert installed["receiving"] and installed["on_track"] == 1
        written = tmp_path / "User Library/Presets/Audio Effects/Max Audio Effect/AbletonMCP Analyzer.amxd"
        assert written.read_bytes() == m4l_device.analyzer_device()
        reading = call("read_analyzer", seconds=0.5)
        assert reading["tracks"]["Master"]["frames"] >= 2
        assert reading["tracks"]["Master"]["correlation"] == 1.0
    finally:
        sending.clear()
        thread.join()
    with pytest.raises(Exception, match="No analyzer sent anything"):
        call("read_analyzer", seconds=0.5)


def test_add_device_inserts_by_name_and_applies_settings(live_server):
    script, call = live_server
    bass = script._song.tracks[1]
    result = call("add_device", track="Bass", device="Utility",
                  settings={"Bass Mono": "On", "Gain": 0.25})
    assert result["settings"]["ok"]
    utility = bass.devices[-1]
    assert utility.class_name == "StereoGain"
    assert utility.parameters[1].value == 1 and utility.parameters[2].value == 0.25
    with pytest.raises(Exception, match="Could not insert 'Nope'"):
        call("add_device", track="Bass", device="Nope")


def test_sidechain_chain_keys_the_compressor_from_another_track(live_server):
    script, call = live_server
    bass = script._song.tracks[1]
    result = call("apply_device_chain", track="Bass", chain="sidechain_duck",
                  sidechain_from="Drums")
    assert result["ok"], result
    compressor = bass.devices[-1]
    params = {p.name: p for p in compressor.parameters}
    assert params["S/C On"].value == 1 and params["Model"].value == 0
    assert params["Threshold"].str_for_value(params["Threshold"].value) == "-30.00 dB"
    assert params["Ratio"].str_for_value(params["Ratio"].value) == "8.00 : 1"
    assert compressor.input_routing_type.display_name == "Drums"
    with pytest.raises(Exception, match="needs sidechain_from"):
        call("apply_device_chain", track="Bass", chain="sidechain_duck")


def test_device_info_lists_parameter_values(live_server):
    script, call = live_server
    info = call("manage_device", track="Bass", device="Operator", action="info")
    names = [p["name"] for p in info["parameters"]]
    assert names == ["Device On", "Filter Freq", "Filter Type"]
    assert info["parameters"][2]["value_items"] == ["Lowpass", "Highpass", "Bandpass"]


def test_measure_levels_reads_live_meters_while_playing(live_server):
    import threading
    import time as _time

    script, call = live_server
    song = script._song
    bass, master = song.tracks[1], song.master_track
    stop = threading.Event()

    def play_audio():
        # What Live does while the set plays: the meters move
        levels = [0.7631, 0.8421, 0.7631]
        i = 0
        while not stop.is_set():
            bass.output_meter_left = levels[i % 3]
            bass.output_meter_right = levels[i % 3]
            master.output_meter_left = 0.80
            i += 1
            _time.sleep(0.05)

    song.continue_playing = lambda: setattr(song, "is_playing", True)
    player = threading.Thread(target=play_audio, daemon=True)
    player.start()
    try:
        result = call("measure_levels", seconds=1, tracks=["Bass", "master"])
    finally:
        stop.set()
        player.join()
    levels = {r["track"]: r for r in result["levels"]}
    assert levels["Bass"]["peak_dbfs"] == -6.0
    assert levels["Master"]["peak_dbfs"] == -9.2
    # Watchers it started are gone again, playback stopped
    assert call("get_live_events")["observers"] == []
    assert song.is_playing is False


# ── Remote Script 1.9 ────────────────────────────────────────────────────────

def test_display_pseudo_property_shows_live_text(script):
    result = run(script, "lom_get", path='live_set tracks "Bass" devices 0 parameters',
                 properties=["name", "$display"])
    displays = {p["name"]: p["$display"] for p in result["items"]}
    assert displays["Filter Type"] == "Lowpass"
    assert displays["Filter Freq"].endswith("Hz")


def test_value_objects_can_be_described(script):
    class TuningSystem(object):  # Live keeps these outside the Live module
        __module__ = "Song"
        name = "Rast"
        pseudo_octave_in_cents = 1200.0

    script._song.tuning_system = TuningSystem()
    described = run(script, "lom_get", path="live_set tuning_system")
    assert described["$type"] == "TuningSystem"
    assert described["properties"]["name"] == "Rast"
    assert "$id" in described


def test_reading_never_closes_an_undo_step(script):
    run(script, "lom_call", path="live_set tracks 1 devices 0 parameters 1",
        method="str_for_value", args=[0.5])
    run(script, "batch", commands=[
        {"type": "lom_get", "params": {"path": "live_set", "properties": ["tempo"]}},
        {"type": "clip_get_notes", "params": {"track_index": 0, "clip_index": 0}},
    ], stop_on_error=False)
    assert script._song.undo_log == []
    run(script, "batch", commands=[
        {"type": "lom_get", "params": {"path": "live_set", "properties": ["tempo"]}},
        {"type": "lom_set", "params": {"path": "live_set", "property": "tempo", "value": 97}}])
    assert script._song.undo_log == ["begin", "end"]


def test_bad_requests_are_not_logged_as_crashes(script):
    logged = []
    script.log_message = lambda message: logged.append(message)
    run_error(script, "lom_get", path="live_set tracks 99")
    assert not any("Traceback" in m for m in logged)

    def broken(_params):
        raise RuntimeError("bug in the script")

    script._handlers_cache["broken"] = broken
    run_error(script, "broken")
    assert any("Traceback" in m for m in logged)


def test_macro_mapping_command_is_gone(script):
    # Live has no API that maps a parameter to a rack macro
    assert "Unknown command" in run_error(script, "map_rack_magnitude")


def test_a_refused_command_keeps_the_connection(live_server):
    from MCP_Server.server import AbletonCommandError

    script, _call = live_server
    from MCP_Server import extended_tools
    connection = extended_tools._get_connection()
    connection.send_command("lom_get", {"path": "live_set", "properties": ["tempo"]})
    sock = connection.sock
    with pytest.raises(AbletonCommandError, match="No item named 'Nope'"):
        connection.send_command("lom_get", {"path": 'live_set tracks "Nope"'})
    assert connection.sock is sock
    assert connection.send_command("lom_get", {"path": "live_set",
                                               "properties": ["tempo"]})["tempo"] == 120.0


def test_doctor_checks_the_session_and_the_round_trip(live_server):
    from MCP_Server import doctor

    script, _call = live_server
    session = doctor.check_session()
    assert session["status"] == "ok", session
    checks, state = doctor.check_connection("127.0.0.1", script.module.DEFAULT_PORT)
    by_name = {c["check"]: c for c in checks}
    assert "protocol 2" in by_name["connection"]["detail"]
    assert "token checked" in by_name["connection"]["detail"]
    assert by_name["latency"]["status"] == "ok"
    assert state is not None


def test_get_live_events_waits_on_the_script(live_server):
    import threading

    script, call = live_server
    since = call("watch_live", path="live_set", property="tempo")["events_since"]
    timer = threading.Timer(0.3, lambda: setattr(script._song, "tempo", 99.0))
    timer.start()
    try:
        events = call("get_live_events", since=since, wait_seconds=5)
    finally:
        timer.join()
    assert [e["value"] for e in events["events"]] == [99.0]


def test_snapshots_save_diff_and_restore(live_server, tmp_path, monkeypatch):
    monkeypatch.setenv("ABLETON_MCP_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    script, call = live_server
    song = script._song
    bass = song.tracks[1]
    saved = call("snapshot", action="save", name="A")
    assert saved["tracks"][:3] == ["Drums", "Bass", "Vox"] and saved["parameters"] > 0

    bass.mixer_device.volume.value = 0.5
    bass.devices[0].parameters[1].value = 0.9          # Operator filter
    song.tracks[0].mute = True
    vox_pan = song.tracks[2].mixer_device.panning
    vox_pan.value = 0.4
    vox_pan.automation_state = 1                       # automated: never restored
    # A new track at the top shifts every index; matching is by name
    song.tracks.insert(0, fake_live.FakeTrack("New"))

    diff = call("snapshot", action="diff", name="A")
    rows = {(r["track"], r["parameter"]): r for r in diff["changes"]}
    assert rows[("Bass", "volume")]["saved"] == "0.0 dB"
    assert rows[("Bass", "volume")]["now"] == "-9.2 dB"
    assert ("Bass", "Filter Freq") in rows and ("Drums", "mute") in rows
    assert diff["automated"] == ["Vox / mixer / pan"]

    restored = call("snapshot", action="restore", name="A")
    assert restored["restored"] == 3 and not restored["errors"]
    assert bass.mixer_device.volume.value == 0.85
    assert bass.devices[0].parameters[1].value == 0.5
    assert song.tracks[1].mute is False
    assert vox_pan.value == 0.4                         # automation left alone
    assert call("snapshot", action="diff", name="A")["changed"] == 0

    call("snapshot", action="save", name="B", tracks=["Bass"])
    bass.mixer_device.volume.value = 0.7
    between = call("snapshot", action="diff", name="B", compare_to="A")
    assert between["changed"] == 0
    listed = call("snapshot", action="list")["snapshots"]
    assert {s["name"] for s in listed} == {"A", "B"}
    assert call("snapshot", action="delete", name="B")["deleted"] is True


def test_snapshots_reach_devices_inside_racks(script):
    from MCP_Server import snapshots

    inner = fake_live.FakeDevice("Saturator", "Saturator", [fake_live.FakeParam("Drive", 0.3)])
    chain = fake_live.FakeObj(name="Chain 1", devices=[inner], mixer_device=fake_live.FakeObj(
        volume=fake_live.FakeParam("Chain Volume", 0.85), panning=fake_live.FakeParam("Pan", 0.0)))
    rack = fake_live.FakeDevice("FX Rack", "AudioEffectGroupDevice",
                                [fake_live.FakeParam("Macro 1", 0.0)])
    object.__setattr__(rack, "can_have_chains", True)
    object.__setattr__(rack, "chains", [chain])
    script._song.tracks[2].devices.append(rack)

    def run_steps(steps):
        return script._process_command({"type": "batch", "params": {
            "commands": steps, "stop_on_error": False}})["result"]["results"]

    before = snapshots.capture(run_steps, ["live_set tracks 2"])
    saturator = before[0]["devices"][0]["chains"][0]["devices"][0]
    assert saturator["name"] == "Saturator"
    assert [p["name"] for p in saturator["parameters"]] == ["Device On", "Drive"]
    inner.parameters[1].value = 0.8
    chain.mixer_device.volume.value = 0.5
    report = snapshots.compare(before, snapshots.capture(run_steps, ["live_set tracks 2"]))
    changed = {(c["where"], c["parameter"]) for c in report["changes"]}
    assert changed == {("Saturator", "Drive"), ("FX Rack / Chain 1", "volume")}
    run_steps(snapshots.restore_ops(report["changes"]))
    assert inner.parameters[1].value == 0.3 and chain.mixer_device.volume.value == 0.85


def test_browser_search_walks_in_slices_and_returns_loadable_ids(script):
    first = run(script, "browser_search", query="kick", roots=["drums", "samples"],
                slice_ms=0.001, max_results=1)
    found, walk = list(first["results"]), first["walk_id"]
    while not first["done"]:
        first = run(script, "browser_search", walk_id=walk, slice_ms=0.001)
        found += first["results"]
    # Breadth first across the roots: shallow items come first
    paths = [r["path"] for r in found]
    assert paths == ["Kick 808 Long.wav", "Drum Hits/Kick/Kick 808 Long.wav",
                     "Drum Hits/Kick/Kick Acoustic.wav"]
    assert {r["kind"] for r in found} == {"sample"} and all("$id" in r for r in found)
    within = run(script, "browser_search", query="", roots=["drums"],
                 within=["Drum Hits", "hihat"])
    assert [r["name"] for r in within["results"]] == ["Hihat Closed 1.wav", "Hihat Open 1.wav"]
    assert "No folder" in run_error(script, "browser_search", query="", roots=["drums"],
                                    within=["Drum Hits", "Cowbell"])


def test_browser_ids_outlast_many_lom_objects(script):
    """A long walk (12,000 samples) used to push its results out of the shared
    4096-entry registry: browser_load then failed with "Unknown id"."""
    found = run(script, "browser_search", query="kick", roots=["drums"])["results"]
    item_id = found[0]["$id"]
    for _ in range(5000):  # far more objects than LOM_REGISTRY_MAX
        script._lom_register(object())
    assert run(script, "lom_get", path=f"id {item_id}", properties=["name"])["name"] == \
        found[0]["name"]


def test_browser_tools_search_load_and_preview(live_server):
    script, call = live_server
    song = script._song
    browser = script.application().browser
    found = call("browser_search", query="pad warm", category="sounds")
    assert [r["path"] for r in found["results"]] == ["Sounds/Pad/Warm Pad.adg"]
    assert found["results"][0]["kind"] == "preset"

    song.view.selected_track = song.tracks[0]
    loaded = call("browser_load", track="Vox", item=found["results"][0]["id"])
    assert loaded["devices"][-1] == "Warm Pad"
    assert song.view.selected_track is song.tracks[0]      # selection put back

    call("browser_preview", item="vocal chop")
    assert browser.previewing == "Vocal Chop.wav"
    call("browser_preview", stop=True)
    assert browser.previewing is None


def test_build_tracks_builds_a_set_from_a_spec(live_server):
    script, call = live_server
    song = script._song
    before = len(song.tracks)
    spec = [{"name": "Kick", "instrument": {"sample": "kick 808 long"}, "volume_db": -6.0,
             "clips": [{"start": 0, "length": 8, "name": "A",
                        "notes": [{"pitch": "C1", "start_time": t, "duration": 1, "velocity": 100}
                                  for t in range(8)]}]},
            {"name": "808 Bass", "instrument": "Operator",
             # the fake Compressor offers the set's own Drums and Bass as sidechain sources
             "effects": [{"device": "Compressor", "settings": {"Ratio": 4.0}, "sidechain_from": "Drums"}],
             "clips": [{"start": 16, "length": 4, "notes": [{"pitch": 24, "start_time": 0, "duration": 4}]}]}]
    result = call("build_tracks", tracks=spec)
    assert [r["track"] for r in result["tracks"]] == ["Kick", "808 Bass"]
    assert all(not r["problems"] for r in result["tracks"]), result
    assert len(song.tracks) == before + 2
    kick, bass = song.tracks[-2], song.tracks[-1]
    assert kick.name == "Kick" and bass.name == "808 Bass"
    assert [d.name for d in bass.devices] == ["Operator", "Compressor"]
    assert bass.devices[1].input_routing_type.display_name == "Drums"
    assert result["tracks"][0]["notes"] == 8 and result["sidechains"] == 1
    assert kick.arrangement_clips[0].name == "A"
    assert bass.arrangement_clips[0].start_time == 16
    # again with replace: the same two tracks, not four; a source Live does not
    # offer is reported, not raised
    spec[1]["effects"][0]["sidechain_from"] = "Nowhere"
    again = call("build_tracks", tracks=spec, replace=True)
    assert len(song.tracks) == before + 2
    assert "sidechain from Nowhere" in again["tracks"][1]["problems"][0]


def test_build_tracks_drum_rack_and_audio_tracks(live_server, tmp_path):
    import numpy as np
    import soundfile as sf
    script, call = live_server
    song = script._song
    stem = tmp_path / "drums.wav"
    sf.write(stem, np.zeros((4410, 2)), 44100)
    spec = [{"name": "Drums", "instrument": {"drum_rack": {
                "36": {"sample": "Kick 808 Long.wav", "settings": {"Volume": "0.0 dB"}},
                "42": {"sample": "hihat closed"}}},
             "clips": [{"start": 0, "length": 4, "notes": [{"pitch": 36, "start_time": 0, "duration": 1}]}]},
            {"name": "REF drums", "kind": "audio", "mute": True,
             "audio_clips": [{"file": str(stem), "start": 0.0}]}]
    result = call("build_tracks", tracks=spec, replace=True, sample_wait_s=0)
    drums, ref = result["tracks"]
    assert not drums["problems"] and not ref["problems"], result
    assert drums["instrument"]["36"].endswith("Kick 808 Long.wav")
    assert drums["instrument"]["42"].endswith("Hihat Closed 1.wav")
    track = next(t for t in song.tracks if t.name == "REF drums")
    assert track.mute and ref["audio_clips"][0]["warping"] is False
    assert ref["audio_clips"][0]["start_marker"] == 0.0


def _wavs(folder, *names):
    import numpy as np
    import soundfile as sf
    paths = []
    for name in names:
        sf.write(folder / name, np.zeros(4410), 44100)
        paths.append(str(folder / name))
    return paths


def test_build_tracks_loads_samples_by_path_with_simpler_options(live_server, tmp_path):
    script, call = live_server
    song, browser = script._song, script.application().browser
    bass, kick, closed, opened = _wavs(tmp_path, "808 C1.wav", "kick.wav", "hat c.wav", "hat o.wav")
    spec = [{"name": "808", "instrument": {"sample_path": bass, "warping": False, "playback_mode": "classic",
                                           "start_marker": 32},
             "settings": {"Volume": "0.0 dB", "Transpose": -3, "Detune": "-12 ct"}},
            {"name": "Beat", "instrument": {"drum_rack": {
                "36": {"sample_path": kick, "playback_mode": "one_shot", "start_marker": 64,
                       "settings": {"Detune": "25 ct"}},
                "42": {"sample_path": closed, "choke_group": 1},
                "46": {"sample_path": opened, "choke_group": 1},
                "38": {"sample": "Snare Tight.wav"}}}}]
    result = call("build_tracks", tracks=spec, signature=[7, 8], sample_wait_s=0)
    rows = {r["track"]: r for r in result["tracks"]}
    assert all(not r["problems"] and "loaded_by_name" not in r for r in rows.values()), result
    assert (song.signature_numerator, song.signature_denominator) == (7, 8)
    # The Simpler holds the file itself (replace_sample), its settings by display name
    simpler = next(t for t in song.tracks if t.name == "808").devices[0]
    assert rows["808"]["instrument"] == bass and simpler.sample.file_path == bass
    assert simpler.playback_mode == 0 and simpler.sample.start_marker == 32
    shown = {p.name: p.str_for_value(p.value) for p in simpler.parameters}
    assert (shown["Transpose"], shown["Detune"]) == ("-3 st", "-12 ct")
    assert {p.name: p.value for p in simpler.parameters}["Volume"] == pytest.approx(0.0, abs=0.05)
    # Pads by path: a chain per note with a Simpler, choke groups on the chains
    rack = next(t for t in song.tracks if t.name == "Beat").devices[0]
    pad = rack.drum_pads[36].chains[0].devices[0]
    assert pad.sample.file_path == kick and pad.playback_mode == 1 and pad.sample.start_marker == 64
    assert {p.name: p.str_for_value(p.value) for p in pad.parameters}["Detune"] == "25 ct"
    assert [rack.drum_pads[n].chains[0].choke_group for n in (42, 46)] == [1, 1]
    assert rack.drum_pads[46].chains[0].devices[0].sample.file_path == opened
    # Only the pad given by name went through the browser
    assert [(n, item) for _t, n, item in browser.loaded] == [(38, "Snare Tight.wav")]
    assert rows["Beat"]["instrument"] == {"36": kick, "42": closed, "46": opened,
                                          "38": "Drums/Drum Hits/Snare/Snare Tight.wav"}
    assert len(rack.chains) == 4


def test_build_tracks_loads_by_name_where_paths_cannot(live_server, tmp_path, monkeypatch):
    script, call = live_server
    song, browser = script._song, script.application().browser
    named, unknown = _wavs(tmp_path, "Kick 808 Long.wav", "Nowhere 1.wav")
    # Before Live 12.4 there is no replace_sample: the browser finds the file's name
    object.__setattr__(script.application(), "_version", (12, 3, 5))
    result = call("build_tracks", tracks=[{"name": "Kit", "instrument": {"drum_rack": {
        "36": {"sample_path": named, "choke_group": 2}}}}], sample_wait_s=0)
    row = result["tracks"][0]
    assert row["loaded_by_name"] == {"36": "replace_sample needs Live 12.4; this is 12.3.5"}
    assert row["instrument"]["36"].endswith("Kick 808 Long.wav") and not row["problems"]
    assert next(t for t in song.tracks if t.name == "Kit").devices[0].drum_pads[36].chains[0].choke_group == 2
    # Neither way: the call fails, saying why for both
    with pytest.raises(Exception, match="needs Live 12.4.*browser: Live's browser does not list 'Nowhere 1.wav'"):
        call("build_tracks", tracks=[{"name": "Kit 2", "instrument": {"drum_rack": {
            "36": {"sample_path": unknown}}}}], sample_wait_s=0)

    # On Live 12.4 a file replace_sample refuses goes to the browser too, and
    # the Simpler or chain that did not take it is removed
    object.__setattr__(script.application(), "_version", (12, 4, 6))

    def refuse(self, file_path):
        raise RuntimeError("Unsupported file")

    monkeypatch.setattr(fake_live.FakeSimpler, "replace_sample", refuse)
    result = call("build_tracks", tracks=[
        {"name": "One", "instrument": {"sample_path": named}},
        {"name": "Kit 3", "instrument": {"drum_rack": {"36": {"sample_path": named}}}}], sample_wait_s=0)
    one, kit = result["tracks"]
    assert one["loaded_by_name"] == "replace_sample: Unsupported file"
    assert one["devices"] == ["Kick 808 Long"]  # the browser's Simpler only
    assert kit["loaded_by_name"]["36"].startswith("replace_sample:")
    rack = next(t for t in song.tracks if t.name == "Kit 3").devices[0]
    assert len(rack.chains) == 1 and rack.drum_pads[36].chains[0].name == "Kick 808 Long"
    assert len([1 for _t, n, _i in browser.loaded if n == 36]) == 2


def test_build_tracks_refuses_sampler_options_that_cannot_work(live_server, tmp_path):
    script, call = live_server
    before = len(script._song.tracks)
    (kick,) = _wavs(tmp_path, "kick.wav")
    for pad, message in (({"sample_path": "kick.wav"}, "must be absolute"),
                         ({"sample_path": str(tmp_path / "gone.wav")}, "no such file"),
                         ({"sample_path": kick, "playback_mode": "loop"}, "use classic, one_shot or slicing"),
                         ({"sample_path": kick, "choke_group": 17}, "choke_group is 0"),
                         ({"choke_group": 1}, "give sample_path")):
        with pytest.raises(Exception, match=message):
            call("build_tracks", tracks=[{"name": "Kit", "instrument": {"drum_rack": {"36": pad}}}])
    with pytest.raises(Exception, match="1-99 over 1, 2, 4, 8 or 16"):
        call("build_tracks", tracks=[], signature="7/9")
    assert len(script._song.tracks) == before


def test_build_drum_kit_fills_general_midi_pads(live_server):
    script, call = live_server
    song = script._song
    browser = script.application().browser
    song.tracks.append(fake_live.FakeTrack("Beat"))
    kit = call("build_drum_kit", track="Beat", roles=["kick", "snare", "closed_hat",
                                                      "open_hat", "clap"], style="808")
    pads = {p["role"]: p for p in kit["pads"]}
    assert pads["kick"]["sample"].endswith("Kick 808 Long.wav")       # style word won
    assert pads["closed_hat"]["sample"].endswith("Hihat Closed 1.wav")
    assert pads["open_hat"]["sample"].endswith("Hihat Open 1.wav")
    assert kit["no_match"] == ["clap"]                                 # no Clap folder here
    rack = song.tracks[-1].devices[0]
    assert rack.class_name == "DrumGroupDevice"
    assert {n for _t, n, _i in browser.loaded} == {36, 38, 42, 46}
    assert rack.drum_pads[36].name == "Kick 808 Long"

    again = call("build_drum_kit", track="Beat", roles=["kick"])
    assert again["pads"] == [{"role": "kick", "note": 36, "kept": "pad already filled"}]


def test_build_arrangement_lays_out_sections_with_locators(live_server):
    script, call = live_server
    song = script._song
    drums, bass, vox = song.tracks
    drums.clip_slots[0].clip = fake_live.FakeClip(name="Beat", length=4.0)
    drums.clip_slots[0].has_clip = True
    bass.clip_slots[0].clip = fake_live.FakeClip(name="Bass A", length=8.0)
    bass.clip_slots[0].has_clip = True
    riff = fake_live.FakeClip(name="Bass B", length=3.0)
    riff.add_new_notes([fake_live.MidiNoteSpecification(pitch=40, start_time=t, duration=1.5,
                                                        velocity=100, mute=False)
                        for t in (0.0, 2.0)])
    bass.clip_slots[1].clip, bass.clip_slots[1].has_clip = riff, True
    vox.clip_slots[0].clip = fake_live.FakeClip(name="Chop", length=6.0, midi=False)
    vox.clip_slots[0].has_clip = True

    result = call("build_arrangement", sections=[
        {"name": "Intro", "bars": 2, "scene": 0, "except": ["Drums"]},
        {"name": "Drop", "bars": 4, "scene": "Intro"},
        {"name": "Outro", "bars": 1, "clips": {"Bass": "Bass B"}}])
    assert result["ok"] and result["end_bar"] == 8
    assert [s["bar"] for s in result["sections"]] == [1, 3, 7]
    def starts(track):
        return [(c.start_time, c.name) for c in track.arrangement_clips]

    assert starts(drums) == [(8.0, "Beat"), (12.0, "Beat"), (16.0, "Beat"), (20.0, "Beat")]
    assert starts(bass)[:3] == [(0.0, "Bass A"), (8.0, "Bass A"), (16.0, "Bass A")]
    # Outro: 4 beats of a 3-beat MIDI clip → one copy plus an exact 1-beat cut
    assert starts(bass)[3] == (24.0, "Bass B") and bass.arrangement_clips[4].length == 1.0
    cut = bass.arrangement_clips[4]._notes
    assert [(n.start_time, n.duration) for n in cut] == [(0.0, 1.0)]
    assert any("Chop" in w or "Vox" in w for w in result["warnings"])
    assert [(c.name, c.time) for c in song.cue_points] == [("Intro", 0.0), ("Drop", 8.0),
                                                          ("Outro", 24.0)]


def test_create_locator_toggles_after_the_playhead_moved(script):
    """Live applies a moved playhead after the callback, so the script toggles
    the cue on the next update (here schedule_message runs at once)."""
    song = script._song
    song.current_song_time = 4.0
    run(script, "create_locator", name="Drop", time=16.0)
    assert [(c.name, c.time) for c in song.cue_points] == [("Drop", 16.0)]
    assert song.current_song_time == 4.0
    run(script, "create_locator", name="Big Drop", time=16.0)   # renames, never toggles off
    assert [(c.name, c.time) for c in song.cue_points] == [("Big Drop", 16.0)]


def test_midi_tools_add_modify_and_remove_notes_in_place(live_server):
    script, call = live_server
    clip = fake_live.FakeClip(name="Keys", length=4.0)
    clip.add_new_notes([fake_live.MidiNoteSpecification(pitch=p, start_time=0.0, duration=2.0,
                                                        velocity=100, mute=False)
                        for p in (60, 64, 67)])
    keys = script._song.tracks[1]
    keys.clip_slots[3].clip, keys.clip_slots[3].has_clip = clip, True
    ids_before = {n.note_id for n in clip._notes}

    strummed = call("apply_midi_tool", track="Bass", clip_index=3, tool="strum",
                    settings={"amount": 0.1})
    assert strummed["notes_modified"] == 3 and strummed["notes_added"] == 0
    assert sorted(n.start_time for n in clip._notes) == [0.0, 0.1, 0.2]

    # A strummed chord no longer starts together; arpeggiate a fresh one
    clip._notes[:] = []
    clip.add_new_notes([fake_live.MidiNoteSpecification(pitch=p, start_time=0.0, duration=2.0,
                                                        velocity=100, mute=False)
                        for p in (60, 64, 67)])
    ids_before = {n.note_id for n in clip._notes}
    arp = call("apply_midi_tool", track="Bass", clip_index=3, tool="arpeggiate",
               settings={"style": "up", "rate": 0.5})
    assert arp["notes_modified"] == 3 and arp["notes_added"] == 1
    assert {n.note_id for n in clip._notes} >= ids_before      # the chord's notes live on
    assert sorted((n.start_time, n.pitch) for n in clip._notes)[:4] == [
        (0.0, 60), (0.5, 64), (1.0, 67), (1.5, 60)]

    with pytest.raises(Exception, match="does not take"):
        call("apply_midi_tool", track="Bass", clip_index=3, tool="chop",
             settings={"amount": 2})


def test_generate_euclidean_on_drum_pads(live_server):
    script, call = live_server
    result = call("generate_euclidean", track="Drums", clip_index=1, bars=1, voices=[
        {"pad": "kick", "pulses": 3, "steps": 8},
        {"pad": "closed_hat", "pulses": 5, "steps": 8, "rotation": 2}])
    assert [v["pattern"] for v in result["voices"]] == ["x..x..x.", "xx.xx.x."]
    notes = script._song.tracks[0].clip_slots[1].clip._notes
    kicks = sorted(n.start_time for n in notes if n.pitch == 36)
    assert kicks == [0.0, 0.75, 1.5, 2.0, 2.75, 3.5]
    assert {n.pitch for n in notes} == {36, 42}


def test_generate_usul_sets_the_meter_and_finds_darbuka_pads(live_server):
    script, call = live_server
    song = script._song
    result = call("generate_usul", track="Drums", usul="aksak", cycles=2, clip_index=2,
                  set_meter=True)
    assert result["meter"] == "9/8" and result["length_beats"] == 9.0
    assert (song.signature_numerator, song.signature_denominator) == (9, 8)
    assert result["pads"]["düm"] == "C1"                 # the kit's kick pad
    # A darbuka kit named after its strokes is used as such
    kit = song.tracks[0].devices[0]
    kit.drum_pads[0].name, kit.drum_pads[1].name = "Doum", "Tek"
    kit.drum_pads[2].name = "Ka Slap"
    darbuka = call("generate_usul", track="Drums", usul="sofyan", cycles=1, clip_index=3)
    assert darbuka["pads"] == {"düm": "C1", "tek": "D1", "te": "F#1", "ke": "F#1",
                               "ka": "F#1"}


def test_turkish_makam_info_and_tuning_file(live_server, tmp_path, monkeypatch):
    from MCP_Server import turkish

    monkeypatch.setattr(turkish, "tuning_folder", lambda: tmp_path / "Tunings")
    _script, call = live_server
    listing = call("turkish_makam")
    assert "hicaz" in listing["makams"] and "aksak" in listing["usuls"]
    info = call("turkish_makam", name="Hicaz", tonic="D", tuning_file=True)
    assert info["notes_12tet"] == ["D", "D#", "F#", "G", "A", "B", "C"]
    assert info["use_as_scale"] == 'key="D", scale="hicaz"'
    written = (tmp_path / "Tunings" / "Makam Hicaz (D).ascl").read_text(encoding="utf-8")
    assert "! @ABL REFERENCE_PITCH 3 2 293.665" in written
    chords = call("generate_chord_progression", track="Bass", clip_index=2,
                  progression="i II", key="D", scale="hicaz")
    assert chords["scale"] == "hicaz"


def test_parameter_names_from_another_live_version_still_work(live_server):
    """Live 12.4 renamed "Output Gain" to "Output" (and Resonance to Q, …);
    recipes and agents may use either name."""
    script, call = live_server
    added = call("add_device", track="Bass", device="Compressor",
                 settings={"Output Gain": "6 dB", "Threshold": "-30 dB"})
    assert added["settings"]["ok"], added
    compressor = script._song.tracks[1].devices[-1]
    output = next(p for p in compressor.parameters if p.name == "Output")
    assert output.str_for_value(output.value) == "6.00 dB"
    call("set_device_parameter_by_name", track="Bass", device="Compressor",
         parameter="Output Gain", display_value="-3 dB")
    assert output.str_for_value(output.value) == "-3.00 dB"
    from MCP_Server.extended_tools import parameter_aliases
    assert parameter_aliases("2 Resonance A") == ["2 Q A"]
    assert parameter_aliases("Output") == ["Output Gain", "Makeup", "Gain", "Master Output"]


def _updates_server():
    from mcp.server.mcpserver import MCPServer
    from mcp.server.subscriptions import InMemorySubscriptionBus

    from MCP_Server import extended_tools, live_updates
    from MCP_Server.connection import AbletonConnection

    bus = InMemorySubscriptionBus()
    server = MCPServer("t", subscriptions=bus)
    extended_tools.register(server, extended_tools._get_connection)
    port = extended_tools._get_connection().port
    # The watcher on a connection of its own, as the server sets it up
    live_updates.install(server, bus, connect=lambda: AbletonConnection("127.0.0.1", port))
    return server


def test_live_changes_reach_2025_era_subscribers(live_server):
    import threading
    import warnings

    import anyio
    from mcp.client import Client
    from mcp.types import ResourceUpdatedNotification

    script, _call = live_server
    server = _updates_server()
    updates = []

    async def on_message(message):
        if isinstance(message, ResourceUpdatedNotification):
            updates.append(message.params.uri)

    async def main():
        async with Client(server, mode="legacy", message_handler=on_message) as client:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                await client.subscribe_resource("ableton://events")
            await client.call_tool("watch_live", {"path": "live_set", "property": "tempo"})
            await anyio.sleep(0.5)
            threading.Timer(0.2, lambda: setattr(script._song, "tempo", 98.0)).start()
            with anyio.fail_after(8):
                while "ableton://events" not in updates:
                    await anyio.sleep(0.05)
            # Adding a track touches the overview
            threading.Timer(0.1, lambda: script._song.tracks.append(
                fake_live.FakeTrack("Late"))).start()
            await anyio.sleep(0.6)

    anyio.run(main)
    assert "ableton://events" in updates


def test_live_changes_reach_2026_era_listen_streams(live_server):
    import threading

    import anyio
    from mcp.client import Client

    script, _call = live_server
    server = _updates_server()
    seen = []

    async def main():
        async with Client(server) as client:
            await client.call_tool("watch_live", {"path": "live_set", "property": "tempo"})
            async with client.listen(resource_subscriptions=["ableton://events"]) as stream:
                threading.Timer(0.6, lambda: setattr(script._song, "tempo", 97.0)).start()
                with anyio.fail_after(8):
                    async for event in stream:
                        seen.append(getattr(event, "uri", None))
                        break

    anyio.run(main)
    assert seen == ["ableton://events"]


def test_long_tools_report_progress(live_server):
    import anyio
    from mcp.client import Client

    script, _call = live_server
    server = _updates_server()
    steps = []

    async def on_progress(progress, total, message):
        steps.append((progress, total, message))

    async def main():
        script._song.continue_playing = lambda: setattr(script._song, "is_playing", True)
        async with Client(server) as client:
            result = await client.call_tool("measure_levels", {"seconds": 1, "tracks": ["Bass"]},
                                            progress_callback=on_progress)
            assert not result.is_error

    anyio.run(main)
    assert steps and steps[-1][1] == 1.0
    assert all(message.startswith("Metering") for _p, _t, message in steps)


# ── Safety net: journal and undo, previews, confirmations ────────────────────

def test_agent_changes_lists_and_undoes_exactly_what_a_call_made(live_server):
    from MCP_Server import journal

    journal.clear()
    script, call = live_server
    song = script._song
    call("set_track_mixer", track="Bass", volume_db=-6)
    begins = song.undo_log.count("begin")
    call("generate_chord_progression", track="Bass", progression="Am F", clip_index=3)
    made = song.undo_log.count("begin") - begins
    changes = call("agent_changes")["changes"]
    assert [c["tool"] for c in changes] == ["generate_chord_progression", "set_track_mixer"]
    assert changes[0]["undo_steps"] == made >= 2
    assert changes[1]["undo_steps"] == 1 and changes[1]["snapshot_before"].startswith("auto ")
    call("lom_get_object", path="live_set", properties=["tempo"])  # reads are not changes
    assert len(call("agent_changes")["changes"]) == 2

    undos = song.undo_log.count("undo")
    bass = song.tracks[1]
    result = call("agent_changes", undo=1)
    assert song.undo_log.count("undo") - undos == made
    assert [c["tool"] for c in result["undone"]] == ["generate_chord_progression"]
    # The fake undoes like Live: the clip is gone, the volume change made before stays
    assert not bass.clip_slots[3].has_clip
    assert bass.mixer_device.volume.str_for_value(bass.mixer_device.volume.value) == "-6.0 dB"
    assert call("agent_changes", undo=5)["undo_steps"] == 1  # only the mixer change is left
    assert bass.mixer_device.volume.value == 0.85
    with pytest.raises(Exception, match="Nothing of the agent's to undo"):
        call("agent_changes", undo=1)
    # The undo itself is not recorded as a change
    assert [c.get("undone") for c in call("agent_changes")["changes"]] == [True, True]


def test_changes_that_live_keeps_out_of_its_undo_history_are_not_counted(live_server):
    from MCP_Server import journal

    journal.clear()
    script, call = live_server
    call("set_track_mixer", track="Bass", volume_db=-6)
    call("set_track_mixer", track="Bass", volume_db=-6, solo=True)  # same volume; solo: no step
    call("transport", action="play")
    # Listed for its snapshot (solo changed), but nothing for Live's undo; playing: not at all
    assert [(c["tool"], c["undo_steps"]) for c in call("agent_changes")["changes"]] == [
        ("set_track_mixer", 0), ("set_track_mixer", 1)]
    assert call("agent_changes", undo=1)["undo_steps"] == 1
    assert script._song.tracks[1].mixer_device.volume.value == 0.85


def test_a_snapshot_restore_keeps_the_state_it_replaces(live_server):
    script, call = live_server
    call("snapshot", action="save", name="mix A")
    call("set_track_mixer", track="Bass", volume_db=-20)
    call("snapshot", action="restore", name="mix A")
    automatic = call("snapshot", action="list")["automatic"]
    assert any(a["name"].endswith("before restoring mix A") for a in automatic)


def test_previews_change_nothing(live_server):
    script, call = live_server
    song = script._song
    bass = song.tracks[1]
    log = list(song.undo_log)
    preview = call("generate_chord_progression", track="Bass", progression="Am F C G",
                   clip_index=3, preview=True)
    assert "Nothing was written" in preview["preview"] and "clip" not in preview
    assert preview["note_columns"][:2] == ["pitch", "name"] and len(preview["notes"]) == 12
    assert not bass.clip_slots[3].has_clip and song.undo_log == log

    call("generate_chord_progression", track="Bass", progression="Am", clip_index=3)
    clip = bass.clip_slots[3].clip
    pitches = sorted(n.pitch for n in clip._notes)
    log = list(song.undo_log)
    moved = call("transform_clip_notes", track="Bass", clip_index=3, operation="transpose",
                 semitones=12, preview=True)
    assert sorted(n.pitch for n in clip._notes) == pitches and song.undo_log == log
    column = moved["note_columns"].index("pitch")
    assert sorted(row[column] for row in moved["notes"]) == [p + 12 for p in pitches]
    strum = call("apply_midi_tool", track="Bass", clip_index=3, tool="strum", preview=True)
    assert strum["notes_in_clip"] == len(pitches) and song.undo_log == log

    call("generate_usul", track="Drums", usul="aksak", clip_index=3, set_meter=True, preview=True)
    assert song.signature_numerator == 4 and song.undo_log == log


def _session(call, callback=None, mode="auto"):
    from mcp.client import Client

    return Client(call.mcp, elicitation_callback=callback, mode=mode)


@pytest.mark.parametrize("mode", ["legacy", "auto"])
def test_deletes_ask_the_user_when_the_client_can(live_server, mode):
    from mcp.types import ElicitResult

    script, call = live_server
    asked = []

    async def decline(_context, params):
        asked.append(params.message)
        return ElicitResult(action="decline")

    async def accept(_context, params):
        asked.append(params.message)
        return ElicitResult(action="accept", content={"confirm": True})

    async def delete(callback):
        async with _session(call, callback, mode) as client:
            return await client.call_tool("manage_track", {"action": "delete", "track": "Vox"})

    names = lambda: [t.name for t in script._song.tracks]  # noqa: E731
    refused = asyncio.run(delete(decline))
    assert refused.is_error and "declined" in refused.content[0].text and "Vox" in names()
    assert asked[0].startswith("Delete the track “Vox”") and "clips" in asked[0]
    assert not asyncio.run(delete(accept)).is_error and "Vox" not in names()


def test_clients_without_elicitation_are_not_asked(live_server, monkeypatch):
    script, call = live_server

    async def delete_scene():
        async with _session(call) as client:
            return await client.call_tool("manage_scene", {"action": "delete", "scene": "Drop"})

    assert not asyncio.run(delete_scene()).is_error
    assert [s.name for s in script._song.scenes] == ["Intro"]
    # ABLETON_MCP_CONFIRM=off skips the question even where it could be asked
    monkeypatch.setenv("ABLETON_MCP_CONFIRM", "off")

    async def never(_context, _params):
        raise AssertionError("asked")

    async def delete_device():
        async with _session(call, never) as client:
            return await client.call_tool("manage_device", {"track": "Bass", "device": 0,
                                                            "action": "delete"})

    assert not asyncio.run(delete_device()).is_error


def test_journal_matches_the_remote_scripts_read_only_rules(script):
    from MCP_Server import journal

    assert journal.READ_ONLY_COMMANDS == script.READ_ONLY_COMMANDS
    assert journal.READ_ONLY_METHODS == script.READ_ONLY_METHODS


def test_tools_do_not_wait_behind_the_watchers_long_poll(live_server):
    """The watcher long-polls Live for 2 s at a time, on a connection of its
    own: a tool call meanwhile answers at once. (When they shared one, every
    call waited up to 2 s: a recorded automation pass lost whole seconds.)"""
    import time as _time
    import warnings

    import anyio
    from mcp.client import Client
    from mcp.server.mcpserver import MCPServer
    from mcp.server.subscriptions import InMemorySubscriptionBus

    from MCP_Server import extended_tools, live_updates
    from MCP_Server.connection import AbletonConnection

    script, _call = live_server
    bus = InMemorySubscriptionBus()
    server = MCPServer("t", subscriptions=bus)
    extended_tools.register(server, extended_tools._get_connection)
    port = script.module.DEFAULT_PORT
    live_updates.install(server, bus, connect=lambda: AbletonConnection("127.0.0.1", port))
    timings = []

    async def main():
        async with Client(server, mode="legacy") as client:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                await client.subscribe_resource("ableton://session/overview")
            await anyio.sleep(0.6)  # the watcher is waiting on Live now
            for _ in range(3):
                started = _time.monotonic()
                await client.call_tool("lom_get_object", {"path": "live_set",
                                                          "properties": ["tempo"]})
                timings.append(_time.monotonic() - started)
                await anyio.sleep(0.3)

    anyio.run(main)
    assert max(timings) < 0.5, timings


def test_memory_in_the_set_and_for_the_user(live_server):
    from MCP_Server import journal

    journal.clear()
    script, call = live_server
    call("memory", action="remember", text="Reference: Burial – Archangel", key="reference")
    call("memory", action="remember", text="Prefers warm, tape-saturated drums", scope="user")
    kept = call("memory")
    assert [n["text"] for n in kept["set"]] == ["Reference: Burial – Archangel"]
    assert "Prefers warm, tape-saturated drums" in [n["text"] for n in kept["user"]]
    # Inside the set (Song.set_data), and on the overview
    assert "Archangel" in script._song.get_data("ableton_mcp_memory", "")
    overview = call("get_session_overview")
    assert overview["memory"]["set"] == ["reference: Reference: Burial – Archangel"]
    # A keyed note is replaced; forgetting by words
    call("memory", action="remember", text="Reference: Four Tet – Baby", key="reference")
    assert [n["text"] for n in call("memory")["set"]] == ["Reference: Four Tet – Baby"]
    assert call("memory", action="forget", text="four tet")["forgot"]
    assert call("memory")["set"] == []
    # Not a change Live can undo: the journal stays empty
    assert call("agent_changes")["changes"] == []
    with pytest.raises(Exception, match="No set note matches"):
        call("memory", action="forget", text="nothing like this")


@pytest.fixture
def fake_musicgen(live_server, tmp_path, monkeypatch):
    """ml.run for "generate" without a model: a short sine per take, and Live's
    browser listing the file as the real one does after a moment."""
    import numpy as np
    import soundfile as sf

    from MCP_Server import als, ml

    script, _call = live_server
    monkeypatch.setattr(als, "_user_libraries", lambda: [tmp_path / "User Library"])
    jobs = []

    async def run(task, job, report=None, timeout=3600.0):
        assert task == "generate"
        jobs.append(job)
        folder = Path(job["out_dir"])
        folder.mkdir(parents=True, exist_ok=True)
        files = []
        for take in range(job["variations"]):
            path = folder / f"{job['stem']}{'' if job['variations'] == 1 else f'-{take + 1}'}.wav"
            tone = np.sin(np.linspace(0, 440 * 2 * np.pi * job["seconds"], int(32000 * job["seconds"])))
            sf.write(str(path), (0.5 * tone).astype("float32"), 32000)
            files.append(str(path))
        browser = script.application().browser
        browser.user_library.children.append(fake_live.FakeBrowserItem(Path(files[0]).name))
        return {"files": files, "rate": 32000, "seconds": job["seconds"], "model": job["model"],
                "device": "cpu"}

    monkeypatch.setattr(ml, "run", run)
    return jobs


def test_generated_audio_lands_on_a_track_a_pad_or_a_simpler(live_server, fake_musicgen):
    script, call = live_server
    song = script._song
    result = call("generate_audio", prompt="dusty lo-fi drum loop", seconds=4, track="Vox")
    job = fake_musicgen[0]
    # The set's tempo and key went into the prompt; the file into the User Library
    assert job["prompt"] == "dusty lo-fi drum loop, 120 bpm, in C major"
    assert "Samples/AbletonMCP Generated" in Path(job["out_dir"]).as_posix()
    assert result["placed"] == {"track": "Vox", "clip_index": 0}
    assert song.tracks[2].clip_slots[0].clip.file_path == result["files"][0]
    assert "non-commercial" in result["license"]
    # In the Arrangement
    call("generate_audio", prompt="vinyl crackle", seconds=2, track="Vox", arrangement_time=8,
         follow_set=False)
    assert fake_musicgen[1]["prompt"] == "vinyl crackle"
    assert song.tracks[2].arrangement_clips[-1].start_time == 8.0
    # On a Drum Rack pad, and in a Simpler on a MIDI track without a rack
    call("manage_track", action="create_midi", name="Gen Kit")
    call("add_device", track="Gen Kit", device="Drum Rack")
    placed = call("generate_audio", prompt="metallic snare hit", seconds=1, track="Gen Kit",
                  drum_pad="snare", variations=2)
    assert placed["placed"]["drum_pad"] == 38 and len(placed["files"]) == 2
    rack = song.tracks[-1].devices[0]
    assert next(p for p in rack.drum_pads if p.note == 38).name.startswith("metallic-snare-hit")
    simpler = call("generate_audio", prompt="warm pad swell", seconds=2, track="Bass")
    assert simpler["placed"]["device"] == "Simpler"
    assert song.tracks[1].devices[-1].class_name == "OriginalSimpler"
    with pytest.raises(Exception, match="seconds must be"):
        call("generate_audio", prompt="x", seconds=60)


# ── Every tool by name at least once (the tools no other test reached) ───────

def test_generic_tools_lom_set_run_batch_select_and_theory(live_server):
    script, call = live_server
    song = script._song
    call("lom_set_property", path="live_set", property="tempo", value=128.0)
    assert song.tempo == 128.0
    result = call("run_batch", commands=[
        {"type": "lom_set", "params": {"path": "live_set tracks 1", "property": "name", "value": "Sub"}},
        {"type": "lom_get", "params": {"path": "live_set", "properties": ["tempo"]}}])
    assert song.tracks[1].name == "Sub" and result["failed"] == 0
    assert result["results"][1]["result"]["tempo"] == 128.0
    call("select_in_live", track="Sub")
    assert song.view.selected_track is song.tracks[1]
    theory = call("music_theory", key="A minor", progression="i iv v")
    assert theory["root"] == "A" and theory["scale_notes"][2] == "C" and len(theory["progression"]) == 3


def test_set_clip_properties_and_rack_control(live_server):
    script, call = live_server
    song = script._song
    call("manage_clip", action="create_session_clip", track="Bass", clip_index=0, length=4.0)
    call("set_clip_properties", track="Bass", clip_index=0, name="Riff", looping=True, loop_end=2.0)
    clip = song.tracks[1].clip_slots[0].clip
    assert clip.name == "Riff" and clip.loop_end == 2.0
    # the fake Kit has no visible_macro_count: that used to crash (an error dict + 1)
    info = call("rack_control", track="Drums", device="Kit")
    assert info["name"] == "Kit" and isinstance(info["macros"], list)


def test_audio_analysis_tools(live_server, tmp_path):
    import numpy as np
    import soundfile as sf
    script, call = live_server
    path = tmp_path / "tone.wav"
    t = np.arange(2 * 44100) / 44100
    sf.write(path, np.stack([0.25 * np.sin(2 * np.pi * 440 * t)] * 2, 1), 44100)
    report = call("analyze_audio_file", path=str(path))
    assert -20 < report["integrated_lufs"] < -10 and report["clipped_samples"] == 0
    vox = script._song.tracks[2]
    vox.create_audio_clip(str(path), 0.0)
    clip_report = call("analyze_clip_audio", track="Vox", arrangement_clip_index=0)
    assert clip_report["integrated_lufs"] == report["integrated_lufs"]


def test_find_live_sets_and_install_midi_tools(live_server, tmp_path, monkeypatch):
    import als_sets

    from MCP_Server import als
    script, call = live_server
    als_sets.write_set(tmp_path / "Song Project" / "Song.als", als_sets.song())
    found = call("find_live_sets", folder=str(tmp_path))
    assert found["sets_found"] == 1 and found["sets"][0]["file"].endswith("Song.als")
    library = tmp_path / "User Library"
    library.mkdir()
    monkeypatch.setattr(als, "_user_libraries", lambda: [library])
    installed = call("install_midi_tools")
    assert len(installed["installed"]) == 3 and all(p.endswith(".amxd") for p in installed["installed"])


def test_original_tools_against_the_script(live_server, monkeypatch):
    """The original (full-profile) tools, through the real Remote Script."""
    import MCP_Server.script_handshake as sh
    import MCP_Server.server as server
    from MCP_Server.server import AbletonConnection
    script, _call = live_server
    song = script._song
    connection = AbletonConnection(host="127.0.0.1", port=script.module.DEFAULT_PORT)
    monkeypatch.setattr(server, "get_ableton_connection", lambda: connection)
    monkeypatch.setattr(sh, "require_capability", lambda name: None)
    try:
        info = json.loads(server.get_track_info(None, 1))
        assert info["name"] == "Bass"
        params = json.loads(server.get_device_parameters(None, 1, 0))
        assert any(p["name"] == "Filter Freq" for p in params["device"]["parameters"])
        assert "Renamed" in server.set_track_name(None, 1, "Low")
        assert song.tracks[1].name == "Low"
        song.tracks[1].create_midi_clip(0.0, 4.0)
        assert "Renamed arrangement clip" in server.set_arrangement_clip_name(None, 1, 0, "Verse")
        assert song.tracks[1].arrangement_clips[0].name == "Verse"
        song.tracks[1].clip_slots[0].create_clip(4.0)
        assert "Stopped" in server.stop_clip(None, 1, 0)
        assert json.loads(server.delete_clip(None, 1, 0))
        assert song.tracks[1].clip_slots[0].clip is None
        assert "Arrangement" in server.switch_to_arrangement_view(None)
        assert script.application().view.shown[-1] == "Arranger"
        assert "beat 8" in server.set_arrangement_time(None, 8.0)
        assert song.current_song_time == 8.0
    finally:
        connection.disconnect()
