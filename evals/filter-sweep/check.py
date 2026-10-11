"""Clip automation of a device parameter."""

from checklib import track


def check(state, initial):
    clip = track(state, "Bass")["clip_slots"].get("0") or {}
    steps = (clip.get("envelopes") or {}).get("Filter Freq") or []
    return {"envelope": len(steps) >= 2,
            "opens": len(steps) >= 2 and steps[0][2] <= 0.2 and steps[-1][2] >= 0.8,
            "over-four-bars": len(steps) >= 2 and steps[-1][0] >= 12}
