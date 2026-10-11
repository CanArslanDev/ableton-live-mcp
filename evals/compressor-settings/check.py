"""Adds a device and sets it by display."""

from checklib import device, number, track


def check(state, initial):
    comp = device(track(state, "Vox"), "Compressor")
    ratio = number(comp["parameters"]["Ratio"]) if comp else None
    threshold = number(comp["parameters"]["Threshold"]) if comp else None
    return {"compressor": comp is not None,
            "ratio": ratio is not None and abs(ratio - 4) <= 0.2,
            "threshold": threshold is not None and abs(threshold + 20) <= 0.6}
