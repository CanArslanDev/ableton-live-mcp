"""An usul and its meter."""

from checklib import new_midi_clips


def check(state, initial):
    clips = new_midi_clips(state, initial, "Drums")
    return {"meter": state["signature"] == "9/8",
            "pattern": bool(clips) and len(clips[0]["notes"]) >= 4}
