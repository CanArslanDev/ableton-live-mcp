"""A bassline under existing chords."""

from checklib import bars, new_midi_clips


ROOTS = [9, 5, 0, 7]  # Am F C G, one bar each


def check(state, initial):
    clips = new_midi_clips(state, initial, "Bass")
    notes = clips[0]["notes"] if clips else []
    lowest = [min(bar, key=lambda n: n[0])[0] % 12 if bar else None for bar in bars(notes)]
    hits = sum(1 for root, low in zip(ROOTS, lowest) if root == low)
    return {"bassline": bool(notes) and clips[0]["length"] >= 16,
            "follows-roots": hits >= 3,
            "low-register": bool(notes) and sum(n[0] < 60 for n in notes) >= 0.8 * len(notes)}
