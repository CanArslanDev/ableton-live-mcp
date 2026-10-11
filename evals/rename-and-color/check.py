"""Renames and recolors a track."""

from checklib import track


def check(state, initial):
    vocal = track(state, "Lead Vocal")
    rgb = int(vocal["color"].lstrip("#"), 16) if vocal else 0
    red, green, blue = rgb >> 16, (rgb >> 8) & 255, rgb & 255
    return {"renamed": bool(vocal) and track(state, "Vox") is None,
            "red": bool(vocal) and (red >= 0xB0 and green <= 0x70 and blue <= 0x70
                                    or vocal["color_index"] == 14)}
