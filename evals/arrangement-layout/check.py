"""Session clips laid out as sections."""

from checklib import track


def covered(t, start, end):
    spans = sorted((c["start_time"], c["end_time"]) for c in t.get("arrangement_clips", []))
    position = start
    for a, b in spans:
        if a > position + 1e-6:
            break
        position = max(position, b)
    return position >= end - 1e-6


def check(state, initial):
    drums, bass = track(state, "Drums"), track(state, "Bass")
    return {"drums-all-along": covered(drums, 0, 48),
            "bass-from-bar-5": covered(bass, 16, 48),
            "no-bass-in-intro": all(c["start_time"] >= 16 - 1e-6
                                    for c in bass.get("arrangement_clips", []))}
