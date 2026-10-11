"""A Euclidean rhythm, E(5,8)."""

from checklib import new_midi_clips


def check(state, initial):
    clips = new_midi_clips(state, initial, "Drums")
    hats = [n for n in (clips[0]["notes"] if clips else []) if n[0] == 42]
    steps = sorted({round(n[1] * 4) % 8 for n in hats})
    on_grid = all(abs(n[1] * 4 - round(n[1] * 4)) < 1e-6 for n in hats)
    gaps = [(steps[(i + 1) % len(steps)] - s) % 8 or 8 for i, s in enumerate(steps)] if steps else []
    return {"hats-written": bool(hats) and on_grid,
            "five-of-eight": len(steps) == 5 and sorted(gaps) == [1, 1, 2, 2, 2]}
