"""Writes a one-bar beat note by note."""

from checklib import new_midi_clips


def positions(notes, pitch):
    return sorted({round(n[1] % 4, 3) for n in notes if n[0] == pitch})


def check(state, initial):
    clips = new_midi_clips(state, initial, "Drums")
    notes = clips[0]["notes"] if clips else []
    return {"kick": positions(notes, 36) == [0, 1, 2, 3],
            "snare": positions(notes, 38) == [1, 3],
            "hats": positions(notes, 42) == [i / 2 for i in range(8)]}
