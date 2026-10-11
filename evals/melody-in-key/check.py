"""A melody that follows the set's Key & Scale."""

from checklib import SCALES, in_scale, midi_clips, track


def check(state, initial):
    lead = track(state, "Lead")
    clips = midi_clips(lead)
    notes = clips[0]["notes"] if clips else []
    return {"lead-track": bool(lead) and lead["type"] == "midi",
            "eight-bars": bool(clips) and clips[0]["length"] >= 32,
            "enough-notes": len(notes) >= 12,
            "in-d-minor": in_scale(notes, 2, SCALES["minor"]) >= 0.9}
