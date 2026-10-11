"""Arrangement transitions and automation, as pure functions over note dicts.

No Live access: the tools in extended_tools read the Arrangement, call these,
and write the result back (see `rewrite_range` there). Times are beats.

Measured on Live 12.4, which the tools build on:
- A script cannot create envelopes on Arrangement clips ("Not a session
  clip"), but a Session clip copied into the Arrangement keeps its envelopes,
  and they play. A copy laid over existing clips splits them: what is left
  keeps all its notes and only moves its start/end markers.
- Parameter changes a script makes while the Arrangement records become
  track automation (any track, the main one too); the pass leaves an undo
  step per value set plus one.
- An envelope step of zero length does nothing; steps need a duration.
"""

from __future__ import annotations

import math
from typing import Any

from . import music


def clip_segments(clip: dict) -> list[tuple[float, float, float]]:
    """Where an Arrangement clip's own time plays: (arrangement start, clip
    time, length) for each stretch, unrolling its loop.

    `clip` holds start_time, end_time, start_marker, end_marker, looping,
    loop_start and loop_end, as Live reports them.
    """
    position, end = float(clip["start_time"]), float(clip["end_time"])
    marker = float(clip.get("start_marker", 0.0))
    if not clip.get("looping"):
        return [(position, marker, end - position)] if end > position else []
    loop_start, loop_end = float(clip["loop_start"]), float(clip["loop_end"])
    loop_length = loop_end - loop_start
    segments = []
    first = loop_end - marker
    if first <= 0 or loop_length <= 0:  # the marker sits past the loop: play the loop
        first, marker = loop_length, loop_start
    while position < end - 1e-9 and first > 0:
        length = min(first, end - position)
        segments.append((position, marker, length))
        position += length
        first, marker = loop_length, loop_start
    return segments


def unroll_notes(notes: list[dict], clip: dict, start: float, end: float) -> list[dict]:
    """The notes an Arrangement clip plays between start and end, at their
    Arrangement times; notes are cut where the loop or the range ends."""
    played = []
    for position, clip_time, length in clip_segments(clip):
        for note in notes:
            offset = float(note["start_time"]) - clip_time
            if not 0 <= offset < length:
                continue
            at = position + offset
            if not start <= at < end:
                continue
            duration = min(float(note["duration"]), length - offset, end - at)
            copy = {k: v for k, v in note.items() if k != "note_id"}
            copy.update(start_time=round(at, 6), duration=round(duration, 6))
            played.append(copy)
    played.sort(key=lambda n: (n["start_time"], n["pitch"]))
    return played


def cut_gap(notes: list[dict], start: float, end: float) -> list[dict]:
    """Silence between start and end: notes starting there go, notes ringing
    into it are shortened."""
    kept = []
    for note in notes:
        at, duration = float(note["start_time"]), float(note["duration"])
        if start <= at < end:
            continue
        if at < start < at + duration:
            note = dict(note, duration=round(start - at, 6))
        kept.append(note)
    return kept


def replace_span(notes: list[dict], start: float, end: float, new: list[dict]) -> list[dict]:
    """`new` in place of whatever played between start and end."""
    return sorted(cut_gap(notes, start, end) + new, key=lambda n: (n["start_time"], n["pitch"]))


def riser_notes(beats: float, root: int = 60, intervals: list[int] | None = None,
                rate: float = 0.25, octaves: int = 2, velocity: tuple[int, int] = (50, 120)
                ) -> list[dict]:
    """A climbing line for a riser: `rate`-beat notes walking up the scale
    over `octaves`, faster and louder towards the end (the last quarter in
    half the note length)."""
    intervals = intervals or [0, 2, 4, 5, 7, 9, 11]
    pitches = [root + 12 * octave + i for octave in range(octaves) for i in intervals]
    pitches.append(root + 12 * octaves)
    times = []
    t = 0.0
    while t < beats - 1e-9:
        times.append(t)
        t += rate if t < beats * 0.75 else rate / 2
    count = len(times)
    notes = []
    for k, at in enumerate(times):
        frac = k / max(1, count - 1)
        pitch = pitches[min(len(pitches) - 1, int(frac * (len(pitches) - 1) + 0.5))]
        step = rate if at < beats * 0.75 else rate / 2
        notes.append(music._note(pitch, at, min(step, beats - at) * 0.9,
                                 int(velocity[0] + (velocity[1] - velocity[0]) * frac)))
    return notes


# General MIDI drum notes a fill can use, by role
FILL_PADS = {"kick": 36, "snare": 38, "low_tom": 45, "mid_tom": 47, "high_tom": 50,
             "floor_tom": 41, "crash": 49, "closed_hat": 42}


def fill_notes(beats: float, kind: str = "snare_roll", pads: dict[str, int] | None = None
               ) -> list[dict]:
    """A drum fill `beats` long, ending where the next section starts.

    - snare_roll: eighths, then sixteenths, then thirty-seconds for the last
      beat, rising in velocity, with a kick on the first beat
    - toms: sixteenths down the toms (high, mid, low, floor), a snare on each
      beat
    - build: snare on every beat, then eighths, then sixteenths over the
      length (for a longer build-up, e.g. 4 bars)
    """
    pads = dict(FILL_PADS, **(pads or {}))
    notes: list[dict] = []

    def hit(role: str, at: float, length: float, velocity: float) -> None:
        notes.append(music._note(pads[role], round(at, 6), round(length * 0.8, 6),
                                 max(1, min(127, int(velocity)))))

    if kind == "snare_roll":
        t = 0.0
        while t < beats - 1e-9:
            left = beats - t
            step = 0.125 if left <= 1 else 0.25 if left <= max(2.0, beats / 2) else 0.5
            hit("snare", t, step, 70 + 55 * t / beats)
            t += step
        hit("kick", 0.0, 0.5, 110)
    elif kind == "toms":
        toms = ["high_tom", "mid_tom", "low_tom", "floor_tom"]
        count = int(round(beats / 0.25))
        for k in range(count):
            at = k * 0.25
            hit(toms[min(3, int(4 * k / count))], at, 0.25, 90 + 30 * k / count)
            if abs(at - round(at)) < 1e-9:
                hit("snare", at, 0.25, 100)
    elif kind == "build":
        t = 0.0
        while t < beats - 1e-9:
            frac = t / beats
            step = 1.0 if frac < 0.5 else 0.5 if frac < 0.75 else 0.25
            hit("snare", t, step, 70 + 55 * frac)
            t += step
    else:
        raise ValueError("kind must be snare_roll, toms or build")
    return sorted(notes, key=lambda n: (n["start_time"], n["pitch"]))


def curve(shape: str, frac: float, low: float, high: float, cycles: float = 1.0) -> float:
    """The value of an automation shape at `frac` (0..1) of its length."""
    frac = min(1.0, max(0.0, frac))
    phase = (frac * cycles) % 1.0
    shapes = {
        "ramp_up": frac, "ramp_down": 1 - frac, "exp_up": frac ** 3, "exp_down": (1 - frac) ** 3,
        "sine": 0.5 - 0.5 * math.cos(2 * math.pi * phase), "triangle": 1 - abs(2 * phase - 1),
        "square": 1.0 if phase < 0.5 else 0.0, "saw": phase,
    }
    if shape not in shapes:
        raise ValueError("shape must be one of " + ", ".join(shapes))
    return low + (high - low) * shapes[shape]


def points_value(points: list[dict], at: float) -> float:
    """Linear interpolation between {"time", "value"} points; held at the ends."""
    pts = sorted((float(p["time"]), float(p["value"])) for p in points)
    if not pts:
        raise ValueError("No automation points")
    if at <= pts[0][0]:
        return pts[0][1]
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        if at <= t1:
            return v0 if t1 == t0 else v0 + (v1 - v0) * (at - t0) / (t1 - t0)
    return pts[-1][1]


def summary(notes: list[dict]) -> dict[str, Any]:
    pitches = sorted({n["pitch"] for n in notes})
    return {"notes": len(notes),
            "pitch_range": [music.note_name(pitches[0]), music.note_name(pitches[-1])]
            if pitches else None}
