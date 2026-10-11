"""The eval suite (evals/): every case's check passes when the task is done
right, through the same server tools the plugin offers, against the simulator."""

import asyncio
import json
import re
from pathlib import Path

import pytest

from MCP_Server import extended_tools, journal
from MCP_Server import server as live_server
from MCP_Server.simulator import Simulator, free_port, load_check

EVALS = Path(__file__).resolve().parents[1] / "evals"
CASES = sorted(p.parent.name for p in EVALS.glob("*/case.yaml"))

KICK = [{"pitch": 36, "start_time": t, "duration": 0.25, "velocity": 110} for t in (0, 1, 2, 3)]
SNARE = [{"pitch": 38, "start_time": t, "duration": 0.25, "velocity": 100} for t in (1, 3)]
HATS = [{"pitch": 42, "start_time": t / 2, "duration": 0.125, "velocity": 80} for t in range(8)]

# What an agent that gets each case right does, in tool calls
SOLUTIONS = {
    "set-overview": [("get_session_overview", {})],
    "tempo-and-meter": [("set_song_properties", {"tempo": 128, "time_signature": "7/8"})],
    "key-and-scale": [("set_song_properties", {"root_note": "D", "scale_name": "Dorian"})],
    "create-tracks": [("manage_track", {"action": "create_midi", "name": "Lead Synth"}),
                      ("manage_track", {"action": "create_audio", "name": "Guitar DI"})],
    "mixer-balance": [("set_track_mixer", {"track": "Bass", "volume_db": -6}),
                      ("set_track_mixer", {"track": "Vox", "pan": "20L", "sends_db": {"A": -12}})],
    "mute-and-solo": [("set_track_mixer", {"track": "Drums", "mute": True}),
                      ("set_track_mixer", {"track": "Vox", "solo": True})],
    "rename-and-color": [("manage_track", {"action": "rename", "track": "Vox", "name": "Lead Vocal"}),
                         ("manage_track", {"action": "set_color", "track": "Lead Vocal",
                                           "color": "#FF2020"})],
    "delete-track": [("manage_track", {"action": "delete", "track": "Vox"})],
    "chord-progression": [("generate_chord_progression", {"track": "Bass", "progression": "Am F C G",
                                                          "clip_index": 0})],
    "drum-beat": [("manage_clip", {"action": "create_session_clip", "track": "Drums", "clip_index": 1,
                                   "length": 4}),
                  ("edit_clip_notes", {"track": "Drums", "clip_index": 1, "mode": "add",
                                       "notes": KICK + SNARE + HATS})],
    "euclidean-hats": [("generate_euclidean", {"track": "Drums", "clip_index": 1, "bars": 1,
                                               "voices": [{"pad": 42, "pulses": 5, "steps": 8}]})],
    "melody-in-key": [("manage_track", {"action": "create_midi", "name": "Lead"}),
                      ("generate_melody", {"track": "Lead", "bars": 8})],
    "bass-follows-chords": [("read_clip_notes", {"track": "Keys", "clip_index": 0}),
                            ("generate_bassline", {"track": "Bass", "progression": "Am F C G",
                                                   "clip_index": 0})],
    "transpose-clip": [("transform_clip_notes", {"track": "Bass", "clip_index": 0,
                                                 "operation": "transpose", "semitones": 7})],
    "quantize-clip": [("transform_clip_notes", {"track": "Bass", "clip_index": 0,
                                                "operation": "quantize", "grid": 0.25})],
    "makam-hicaz": [("generate_melody", {"track": "Bass", "key": "D", "scale": "hicaz", "bars": 4,
                                         "low": "D2", "high": "D4"})],
    "usul-aksak": [("generate_usul", {"track": "Drums", "usul": "aksak", "clip_index": 1,
                                      "set_meter": True})],
    "compressor-settings": [("add_device", {"track": "Vox", "device": "Compressor",
                                            "settings": {"Ratio": "4", "Threshold": "-20 dB"}})],
    "bass-chain": [("apply_device_chain", {"track": "Bass", "chain": "bass"})],
    "drum-kit": [("manage_track", {"action": "create_midi", "name": "Kit 2"}),
                 ("build_drum_kit", {"track": "Kit 2",
                                     "roles": ["kick", "snare", "closed_hat", "open_hat"],
                                     "sounds": {"kick": "Kick 808", "snare": "Snare Tight",
                                                "closed_hat": "Hihat Closed",
                                                "open_hat": "Hihat Open"}})],
    "locators": [("create_locator", {"name": "Intro", "time": 0}),
                 ("create_locator", {"name": "Verse", "time": 32}),
                 ("create_locator", {"name": "Drop", "time": 64})],
    "arrangement-layout": [("build_arrangement", {"locators": False, "sections": [
        {"name": "Intro", "bars": 4, "clips": {"Drums": 0}},
        {"name": "Main", "bars": 8, "clips": {"Drums": 0, "Bass": 0}}]})],
    "preview-no-write": [("generate_chord_progression", {"track": "Bass", "preview": True,
                                                         "progression": "Dm7 G7 Cmaj7"})],
    "undo-tempo": [("set_track_mixer", {"track": "Drums", "mute": True}),
                   ("set_song_properties", {"tempo": 90}),
                   ("agent_changes", {"undo": 1})],
    "missing-track": [],
    "filter-sweep": [("write_clip_automation", {"track": "Bass", "clip_index": 0,
                                                "device": "Operator", "parameter": "Filter Freq",
                                                "shape": "ramp_up", "low": 0.0, "high": 1.0})],
    "drum-fill": [("design_transition", {"kind": "drum_fill", "at_bar": 9, "fill": "snare_roll"})],
    "build-up": [("design_transition", {"kind": "riser", "at_bar": 9, "bars": 4}),
                 ("design_transition", {"kind": "filter_sweep", "at_bar": 9, "bars": 4,
                                        "tracks": ["Bass"]})],
    "fade-out": [("record_automation", {"track": "master", "parameter": "volume", "start_bar": 13,
                                        "bars": 4, "shape": "ramp_down", "hold": True})],
    "scenes": [("manage_scene", {"action": "create", "name": "Verse"}),
               ("manage_scene", {"action": "create", "name": "Chorus"})],
}
# Cases about leaving the set alone: their checks pass before anything happens
UNTOUCHED_PASSES = {"set-overview", "preview-no-write", "missing-track", "delete-track"}


@pytest.fixture
def simulated_case(tmp_path, monkeypatch):
    started = []

    # Other tests register the tools against their own connections; these run
    # through the server's, as the plugin does
    monkeypatch.setattr(extended_tools, "_get_connection", live_server.get_ableton_connection)

    def start(name):
        folder = EVALS / name
        session = tmp_path / "live-session.json"
        monkeypatch.setenv("ABLETON_MCP_SESSION_FILE", str(session))
        monkeypatch.delenv("ABLETON_PORT", raising=False)
        simulator = Simulator(free_port(), session, check=load_check(folder / "check.py"))
        if (folder / "setup.json").exists():
            simulator.run_setup(json.loads((folder / "setup.json").read_text()))
        started.append(simulator.start())
        journal.clear()
        return simulator

    yield start
    if live_server._ableton_connection is not None:
        live_server._ableton_connection.disconnect()
        live_server._ableton_connection = None
    for simulator in started:
        simulator.stop()


def test_every_case_has_a_solution_and_its_files():
    assert len(CASES) >= 20 and set(CASES) == set(SOLUTIONS)
    for name in CASES:
        folder = EVALS / name
        text = (folder / "case.yaml").read_text()
        assert f"name: {name}\n" in text and "scaffold_script: scaffold.sh" in text
        assert (folder / "scaffold.sh").stat().st_mode & 0o111, f"{name}/scaffold.sh not executable"
        assert (folder / "check.py").exists()


@pytest.mark.parametrize("name", CASES)
def test_the_right_actions_pass_every_check(name, simulated_case):
    simulator = simulated_case(name)
    before = simulator.checks()
    graded = set(re.findall(r'"check:([a-z0-9-]+)"', (EVALS / name / "case.yaml").read_text()))
    assert graded == {key.split(":", 1)[1] for key in before}, "graders and check disagree"
    if name not in UNTOUCHED_PASSES:
        assert not all(before.values()), "the check passes without any work"
    for tool, arguments in SOLUTIONS[name]:
        result = asyncio.run(live_server.mcp.call_tool(tool, arguments))
        assert not result.is_error, (tool, result.content[0].text)
    after = simulator.checks()
    assert after and all(after.values()), after
