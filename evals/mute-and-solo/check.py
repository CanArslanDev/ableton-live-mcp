"""Mute one track, solo another, leave the rest."""

from checklib import track


def check(state, initial):
    drums, vox, bass = track(state, "Drums"), track(state, "Vox"), track(state, "Bass")
    return {"drums-muted": drums["mute"], "vox-solo": vox["solo"],
            "bass-untouched": not bass["mute"] and not bass["solo"]}
