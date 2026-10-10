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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fake_live  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
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
    surface._passive_events = []
    surface._passive_lock = __import__("threading").Lock()
    surface._lom_init()
    surface.schedule_message = lambda delay, fn: fn()
    browser = fake_live.FakeBrowser(surface._song)
    surface.application = lambda: fake_live.FakeObj(view=fake_live.FakeObj(), browser=browser)
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

    from mcp.server.fastmcp import FastMCP
    from MCP_Server import extended_tools
    mcp = FastMCP("test")
    extended_tools.register(mcp, lambda: connection)

    def call(_tool, **arguments):
        content = asyncio.run(mcp.call_tool(_tool, arguments))
        blocks = content[0] if isinstance(content, tuple) else content
        text = blocks[0].text
        assert not text.startswith("Error"), text
        try:
            return json.loads(text)
        except ValueError:
            return text

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
    assert all(n["name"].endswith("1") for n in notes["notes"])

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
    from mcp.server.fastmcp.exceptions import ToolError

    script, call = live_server
    result = call("set_track_mixer", track="Nope", mute=True)
    assert result["ok"] is False
    assert "No item named 'Nope'" in result["steps"][0]["error"]
    # FastMCP turns the exception into a result with isError=true
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
    starts = lambda track: [(c.start_time, c.name) for c in track.arrangement_clips]
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
