"""Writes a four-chord progression."""

from checklib import bars, pitch_classes, track


CHORD_TONES = [{9, 0, 4}, {5, 9, 0}, {0, 4, 7}, {7, 11, 2}]


def check(state, initial):
    clips = track(state, "Bass")["clip_slots"]
    first = clips.get("0") or {}
    by_bar = bars(first.get("notes") or [])
    return {"clip": bool(first.get("notes")),
            "four-bars": first.get("length", 0) >= 16,
            "chords": len(by_bar) >= 4 and all(tones <= pitch_classes(notes)
                                                for tones, notes in zip(CHORD_TONES, by_bar))}
