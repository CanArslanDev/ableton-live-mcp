"""A Drum Rack built from one-shot samples."""

from checklib import track


def check(state, initial):
    kit = track(state, "Kit 2")
    rack = next((d for d in kit["devices"] if d["class_name"] == "DrumGroupDevice"), None) if kit else None
    pads = " | ".join((rack or {}).get("pads", {}).values()).lower()
    return {"track": kit is not None, "drum-rack": rack is not None,
            "kick": "kick 808" in pads, "snare": "snare tight" in pads,
            "closed-hat": "hihat closed" in pads, "open-hat": "hihat open" in pads}
