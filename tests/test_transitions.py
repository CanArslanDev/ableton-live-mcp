"""Arrangement transitions as pure functions (MCP_Server/transitions.py)."""

import pytest

from MCP_Server import transitions as tr


def note(pitch, start, duration=0.5):
    return {"pitch": pitch, "start_time": start, "duration": duration, "velocity": 100}


# The clips Live 12.4 left when a 4-beat clip was copied over 4..8 of a 16-beat
# one: both parts hold all 16 notes; the tail starts at clip time 8
WHOLE = [note(60 + b, b) for b in range(16)]
HEAD = {"start_time": 0, "end_time": 4, "start_marker": 0, "looping": True, "loop_start": 0,
        "loop_end": 16}
TAIL = dict(HEAD, start_time=8, end_time=16, start_marker=8)


def test_split_clips_play_only_their_part():
    assert tr.clip_segments(TAIL) == [(8.0, 8.0, 8.0)]
    assert [n["pitch"] for n in tr.unroll_notes(WHOLE, HEAD, 0, 100)] == [60, 61, 62, 63]
    tail = tr.unroll_notes(WHOLE, TAIL, 0, 100)
    assert [(n["pitch"], n["start_time"]) for n in tail] == [(68 + i, 8 + i) for i in range(8)]
    assert all("note_id" not in n for n in tail)


def test_loops_unroll_across_the_clip():
    bar = {"start_time": 32, "end_time": 48, "start_marker": 0, "looping": True,
           "loop_start": 0, "loop_end": 4}
    played = tr.unroll_notes([note(48, 0), note(50, 2)], bar, 36, 44)
    assert [n["start_time"] for n in played] == [36, 38, 40, 42]
    # Starting inside the loop: from the marker to the loop end, then the loop
    late = dict(bar, start_time=0, end_time=8, start_marker=2)
    assert tr.clip_segments(late) == [(0.0, 2.0, 2.0), (2.0, 0.0, 4.0), (6.0, 0.0, 2.0)]
    assert [n["start_time"] for n in tr.unroll_notes([note(48, 0), note(50, 2)], late, 0, 8)] \
        == [0, 2, 4, 6]
    # Notes are cut at the loop end and at the range end
    long = tr.unroll_notes([note(48, 3, 3)], bar, 32, 37.5)
    assert [(n["start_time"], n["duration"]) for n in long] == [(35, 1)]
    plain = dict(bar, looping=False, end_time=36)
    assert [n["start_time"] for n in tr.unroll_notes([note(48, 0), note(50, 2)], plain, 0, 99)] \
        == [32, 34]


def test_gaps_and_replacements():
    notes = [note(36, 0, 4), note(38, 1), note(42, 3.5), note(36, 4)]
    gapped = tr.cut_gap(notes, 3, 4)
    assert [(n["pitch"], n["start_time"], n["duration"]) for n in gapped] == [
        (36, 0, 3), (38, 1, 0.5), (36, 4, 0.5)]
    replaced = tr.replace_span(notes, 3, 4, [note(38, 3.25, 0.25)])
    assert [n["start_time"] for n in replaced] == [0, 1, 3.25, 4]


def test_riser_climbs_and_grows():
    notes = tr.riser_notes(8, root=48)
    pitches = [n["pitch"] for n in notes]
    assert pitches == sorted(pitches) and pitches[0] == 48 and pitches[-1] == 72
    assert [n["velocity"] for n in notes] == sorted(n["velocity"] for n in notes)
    assert max(n["start_time"] + n["duration"] for n in notes) <= 8
    steps = sorted({round(b["start_time"] - a["start_time"], 3) for a, b in zip(notes, notes[1:])})
    assert steps == [0.125, 0.25]  # faster for the last quarter


@pytest.mark.parametrize("kind", ["snare_roll", "toms", "build"])
def test_fills_fit_and_end_busy(kind):
    beats = 8 if kind == "build" else 4
    notes = tr.fill_notes(beats, kind)
    assert notes and all(0 <= n["start_time"] < beats for n in notes)
    assert all(n["start_time"] + n["duration"] <= beats + 1e-9 for n in notes)
    last_beat = [n for n in notes if n["start_time"] >= beats - 1]
    first_beat = [n for n in notes if n["start_time"] < 1]
    if kind == "toms":  # down the kit, high tom to floor tom
        toms = [n["pitch"] for n in notes if n["pitch"] != 38]
        assert toms[0] == 50 and toms[-1] == 41 and toms == sorted(toms, reverse=True)
    else:
        assert len(last_beat) > len(first_beat)
    if kind == "snare_roll":
        assert {n["pitch"] for n in notes} == {36, 38}
    with pytest.raises(ValueError):
        tr.fill_notes(4, "cowbell")


def test_curves_and_points():
    assert tr.curve("ramp_up", 0.25, 0.2, 1.0) == pytest.approx(0.4)
    assert tr.curve("ramp_down", 1.0, 0.0, 1.0) == 0
    assert tr.curve("triangle", 0.5, 0, 1) == 1
    assert tr.points_value([{"time": 0, "value": 0}, {"time": 4, "value": 1}], 1) == 0.25
    assert tr.points_value([{"time": 1, "value": 0.5}], 9) == 0.5
    with pytest.raises(ValueError):
        tr.curve("wobble", 0, 0, 1)
