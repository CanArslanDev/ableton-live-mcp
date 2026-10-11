"""Creates and names a MIDI and an audio track."""

from checklib import track, track_names


def check(state, initial):
    lead, guitar = track(state, "Lead Synth"), track(state, "Guitar DI")
    return {"lead-synth": bool(lead) and lead["type"] == "midi",
            "guitar-di": bool(guitar) and guitar["type"] == "audio",
            "others-kept": all(name in track_names(state) for name in track_names(initial))}
