"""Helpers for the eval checks (evals/<case>/check.py).

A check reads the simulator's description of the set (see
MCP_Server/simulator.describe_song): `check(state, initial)` returns
{name: bool}, and the simulator writes the verdicts to checks.json in the run's
workspace, where the case's regex graders look for "check:<name>": true.
Notes are [pitch, start, duration, velocity] (+ "muted"), times in beats.
"""

from __future__ import annotations

import re

SCALES = {
    "major": [0, 2, 4, 5, 7, 9, 11], "minor": [0, 2, 3, 5, 7, 8, 10],
    "dorian": [0, 2, 3, 5, 7, 9, 10], "mixolydian": [0, 2, 4, 5, 7, 9, 10],
}
NOTE = {"C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3, "E": 4, "F": 5, "F#": 6,
        "Gb": 6, "G": 7, "G#": 8, "Ab": 8, "A": 9, "A#": 10, "Bb": 10, "B": 11}


def track(state: dict, name: str) -> dict | None:
    wanted = name.strip().lower()
    return next((t for t in state["tracks"] if t["name"].strip().lower() == wanted), None)


def track_names(state: dict) -> list[str]:
    return [t["name"] for t in state["tracks"]]


def session_clips(t: dict | None) -> list[dict]:
    return [t["clip_slots"][k] for k in sorted(t["clip_slots"], key=int)] if t else []


def midi_clips(t: dict | None) -> list[dict]:
    return [c for c in session_clips(t) if c.get("notes")]


def new_midi_clips(state: dict, initial: dict, name: str) -> list[dict]:
    """The track's MIDI clips that were not there before (or changed)."""
    before = track(initial, name)
    old = [c for c in session_clips(before)]
    return [c for c in midi_clips(track(state, name)) if c not in old]


def pitch_classes(notes: list) -> set[int]:
    return {n[0] % 12 for n in notes}


def notes_at(notes: list, pitch: int) -> list[float]:
    return sorted({round(n[1] % 1e9, 3) for n in notes if n[0] == pitch})


def bars(notes: list, beats_per_bar: float = 4.0) -> list[list]:
    """The notes starting in each bar."""
    if not notes:
        return []
    count = int(max(n[1] for n in notes) // beats_per_bar) + 1
    grouped: list[list] = [[] for _ in range(count)]
    for n in notes:
        grouped[int(n[1] // beats_per_bar)].append(n)
    return grouped


def in_scale(notes: list, root: int, intervals: list[int]) -> float:
    """The share of notes whose pitch class is in the scale."""
    if not notes:
        return 0.0
    allowed = {(root + i) % 12 for i in intervals}
    return sum(1 for n in notes if n[0] % 12 in allowed) / len(notes)


def number(display: str) -> float | None:
    """The number in a parameter display: "-6.0 dB" → -6.0, "4.00 : 1" → 4.0."""
    match = re.search(r"-?\d+(?:\.\d+)?|-?\.\d+", str(display))
    return float(match.group()) if match else None


def device(t: dict | None, name: str) -> dict | None:
    if not t:
        return None
    return next((d for d in t["devices"] if d["name"].lower() == name.lower()), None)


def mixers(state: dict) -> list:
    """Every track's mixer settings, to tell that nothing else moved."""
    every = state["tracks"] + state["return_tracks"] + [state["master"]]
    return [(t["name"], t["volume"]["value"], t["pan"]["value"], t["mute"], t["solo"],
             [s["value"] for s in t["sends"]]) for t in every]


def no_changes(state: dict, initial: dict) -> bool:
    """Nothing in the set changed: no step entered Live's undo history."""
    return state["undo"]["steps"] == 0 and state["tracks"] == initial["tracks"] \
        and state["tempo"] == initial["tempo"]


def played_notes(t: dict | None, start: float = 0.0, end: float = 1e9) -> list[list]:
    """What a track's Arrangement plays between start and end, as Live plays its
    clips (split, looped): [pitch, start, duration, velocity] at Arrangement times."""
    from MCP_Server.transitions import unroll_notes

    notes: list[dict] = []
    for clip in (t or {}).get("arrangement_clips", []):
        raw = [{"pitch": n[0], "start_time": n[1], "duration": n[2], "velocity": n[3]}
               for n in clip.get("notes", [])]
        notes += unroll_notes(raw, clip, start, end)
    return sorted([[n["pitch"], n["start_time"], n["duration"], n["velocity"]] for n in notes],
                  key=lambda n: (n[1], n[0]))


def set_notes(state: dict) -> list[dict]:
    """The notes the memory tool keeps inside the set (Song.set_data)."""
    from MCP_Server.memory import SET_KEY, parse

    return parse(state.get("data", {}).get(SET_KEY, ""))
