"""Quantizes a loose clip to 16ths."""

from checklib import track


def check(state, initial):
    after = (track(state, "Bass")["clip_slots"].get("0") or {}).get("notes") or []
    starts = sorted(n[1] for n in after)
    return {"same-notes": len(after) == 6,
            "on-grid": bool(after) and all(abs(s * 4 - round(s * 4)) < 1e-6 for s in starts),
            "nearest": [round(s, 3) for s in starts] == [0, 1, 1.5, 2, 2.75, 3.25]}
