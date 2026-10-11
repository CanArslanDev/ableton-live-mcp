"""Arrangement automation and transitions against the simulator (the real
Remote Script over fake Live, which splits clips and records automation the
way Live 12.4 does)."""

import asyncio
import json

import pytest

from MCP_Server import extended_tools, journal
from MCP_Server import server as live_server
from MCP_Server import transitions
from MCP_Server.simulator import Simulator, free_port


@pytest.fixture
def sim(tmp_path, monkeypatch):
    session = tmp_path / "live-session.json"
    monkeypatch.setenv("ABLETON_MCP_SESSION_FILE", str(session))
    monkeypatch.delenv("ABLETON_PORT", raising=False)
    monkeypatch.setattr(extended_tools, "_get_connection", live_server.get_ableton_connection)
    simulator = Simulator(free_port(), session)
    # A 4-bar bass part in the Arrangement, one note per beat
    simulator.run_setup([
        {"type": "lom_call", "params": {"path": "live_set tracks 1", "method": "create_midi_clip",
                                        "args": [0.0, 16.0]}},
        {"type": "lom_set", "params": {"path": "live_set tracks 1 arrangement_clips 0",
                                       "property": "name", "value": "Bassline"}},
        {"type": "clip_write_notes", "params": {
            "clip_path": "live_set tracks 1 arrangement_clips 0",
            "notes": [{"pitch": 36 + b % 5, "start_time": b, "duration": 0.5, "velocity": 100}
                      for b in range(16)]}}])
    simulator.start()
    journal.clear()
    yield simulator
    if live_server._ableton_connection is not None:
        live_server._ableton_connection.disconnect()
        live_server._ableton_connection = None
    simulator.stop()


def call(tool, **arguments):
    result = asyncio.run(live_server.mcp.call_tool(tool, arguments))
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


def played(track_state, start, end):
    """The notes a track's Arrangement plays between start and end, as Live would."""
    notes = []
    for clip in track_state.get("arrangement_clips", []):
        clip = dict(clip, looping=clip["looping"])
        raw = [{"pitch": n[0], "start_time": n[1], "duration": n[2], "velocity": n[3]}
               for n in clip.get("notes", [])]
        notes += transitions.unroll_notes(raw, clip, start, end)
    return sorted((n["pitch"], n["start_time"]) for n in notes)


def test_automation_lands_without_touching_the_notes(sim):
    before = played(sim.state()["tracks"][1], 0, 16)
    result = call("write_arrangement_automation", track="Bass", device="Operator",
                  parameter="Filter Freq", start_bar=2, bars=2, shape="ramp_up")
    assert result["parameter"] == "Filter Freq" and result["notes"] == 8
    bass = sim.state()["tracks"][1]
    spans = [(c["start_time"], c["end_time"], c["name"]) for c in bass["arrangement_clips"]]
    assert spans == [(0, 4, "Bassline"), (4, 12, "Bassline"), (12, 16, "Bassline")]
    assert played(bass, 0, 16) == before
    steps = bass["arrangement_clips"][1]["envelopes"]["Filter Freq"]
    assert steps[0][2] == 0 and steps[-1][2] > 0.95 and steps[-1][0] == 7.75
    assert not any(bass["clip_slots"].values())  # the scratch clip is gone
    # Audio tracks cannot take it
    with pytest.raises(Exception, match="record_automation"):
        asyncio.run(live_server.mcp.call_tool("write_arrangement_automation", {
            "track": "Vox", "parameter": "volume", "start_bar": 1, "bars": 1,
            "shape": "ramp_up"}))
    # The whole thing is undone at once
    call("agent_changes", undo=1)
    assert [(c["start_time"], c["end_time"]) for c in
            sim.state()["tracks"][1]["arrangement_clips"]] == [(0, 16)]


def test_transitions_into_bar_five(sim):
    riser = call("design_transition", kind="riser", at_bar=5)
    assert (riser["from_beat"], riser["to_beat"], riser["track"]) == (0, 16, "Riser")
    state = sim.state()
    track = next(t for t in state["tracks"] if t["name"] == "Riser")
    assert track["devices"][0]["name"] == "Operator"
    assert track["devices"][0]["parameters"]["Osc-A Wave"] == "Saw 16"
    clip = track["arrangement_clips"][0]
    pitches = [n[0] for n in clip["notes"]]
    assert pitches == sorted(pitches) and (clip["start_time"], clip["end_time"]) == (0, 16)
    assert set(clip["envelopes"]) == {"Filter Freq", "Track Volume"}

    cymbal = call("design_transition", kind="reverse_cymbal", at_bar=5)
    track = next(t for t in sim.state()["tracks"] if t["name"] == "Reverse Cymbal")
    assert track["devices"][0]["reversed"] and track["devices"][0]["sample"] == "Crash 505.wav"
    clip = track["arrangement_clips"][0]
    swell = 2.76 * 2  # seconds at 120 BPM, in beats
    assert clip["end_time"] == 16 and clip["start_time"] == pytest.approx(16 - swell, abs=0.01)
    assert clip["notes"][0][2] == pytest.approx(swell, abs=0.01) and cymbal["notes"] == 1

    fill = call("design_transition", kind="drum_fill", at_bar=5, fill="snare_roll")
    assert fill["track"] == "Drums"
    drums = sim.state()["tracks"][0]
    roll = [n for n in drums["arrangement_clips"][-1]["notes"]]
    assert {n[0] for n in roll} == {36, 38} and drums["arrangement_clips"][-1]["end_time"] == 16

    gap = call("design_transition", kind="gap", at_bar=4)
    assert set(gap["tracks"]) >= {"Bass"}
    assert [p for p, at in played(sim.state()["tracks"][1], 11, 12)] == []
    assert played(sim.state()["tracks"][1], 10, 11)  # the beat before still plays

    sweep = call("design_transition", kind="filter_sweep", at_bar=5, tracks=["Bass"], bars=2)
    bass = sim.state()["tracks"][1]
    auto = next(d for d in bass["devices"] if d["name"] == "Auto Filter")
    assert auto["parameters"]["Filter Type"] == "High-pass" and sweep["tracks"] == ["Bass"]
    swept = next(c for c in bass["arrangement_clips"] if c["start_time"] == 8)
    assert "Frequency" in swept["envelopes"]
    with pytest.raises(Exception, match="kind must be"):
        asyncio.run(live_server.mcp.call_tool("design_transition", {"kind": "wobble",
                                                                     "at_bar": 5}))


def test_recorded_automation_on_the_main_track(sim):
    sim.song.tempo = 960.0  # sixteen beats a second: the pass takes about half a second
    sim.song.tracks[2].arm = True
    result = call("record_automation", track="master", parameter="volume", start_bar=2,
                  bars=1, shape="ramp_down", low=0.2, high=0.9)
    assert result["points"] >= 3
    state = sim.state()
    lane = state["master"]["automation"]["mixer/Track Volume"]
    inside = [v for t, v in lane if 4 <= t <= 8]
    assert inside[0] > inside[-1] and lane[-1][1] == 0.85  # back to the value before
    # Settings are put back; one undo step for the pass, as in Live
    assert not state["record_mode"] and not state["is_playing"] and state["tracks"][2]["arm"]
    # As in Live 12.4: a step for every value set while recording (the return
    # to the old value too), plus one for the pass
    changes = call("agent_changes")["changes"]
    assert changes[0]["tool"] == "record_automation"
    assert changes[0]["undo_steps"] == result["undo_steps"] == result["points"] + 2
    assert len(sim.song._undo) == result["undo_steps"]  # nothing earlier counted
    call("agent_changes", undo=1)
    state = sim.state()
    assert "automation" not in state["master"] and state["master"]["volume"]["value"] == 0.85
