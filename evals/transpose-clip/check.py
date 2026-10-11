"""Transposes an existing clip."""

from checklib import track


def check(state, initial):
    before = track(initial, "Bass")["clip_slots"]["0"]["notes"]
    after = (track(state, "Bass")["clip_slots"].get("0") or {}).get("notes") or []
    moved = [[n[0] + 7] + n[1:] for n in before]
    return {"up-a-fifth": after == moved}
