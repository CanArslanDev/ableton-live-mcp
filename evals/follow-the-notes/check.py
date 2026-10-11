"""Finds an earlier decision in the set's notes and applies it."""

from checklib import number, track


def check(state, initial):
    vox = track(state, "Vox")
    level = number(vox["volume"]["display"])
    return {"vox-level": level is not None and abs(level + 3) <= 0.3,
            "vox-center": vox["pan"]["display"] == "C"}
