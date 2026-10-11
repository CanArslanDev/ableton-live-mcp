"""Undoes one change and keeps the other."""

from checklib import track


def check(state, initial):
    return {"tempo-back": state["tempo"] == 120, "drums-still-muted": track(state, "Drums")["mute"]}
