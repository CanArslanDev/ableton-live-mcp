"""Volume, pan and a send by their displayed values."""

from checklib import number, track


def check(state, initial):
    bass, vox, drums = track(state, "Bass"), track(state, "Vox"), track(state, "Drums")
    send = number(vox["sends"][0]["display"])
    return {"bass-volume": abs(number(bass["volume"]["display"]) + 6) <= 0.3,
            "vox-pan": vox["pan"]["display"] == "20L",
            "vox-reverb-send": send is not None and -13.5 <= send <= -10.5,
            "drums-untouched": drums["volume"] == track(initial, "Drums")["volume"]}
