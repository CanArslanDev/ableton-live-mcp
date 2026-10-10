"""Ready-made device chains built from Live's native devices.

Starting points to adjust by ear, not presets. Parameter names and value
displays are the ones Live 12.3 shows (names Live 12.4 changed, like "Output
Gain" → "Output", are mapped when applied); a string value is set by display
("-18 dB", "120 Hz", "High Pass 48dB"), a number is a raw parameter value
(used where Live's display has no unit, e.g. Glue Compressor's stepped
Attack/Release/Ratio).
"""

from __future__ import annotations

# Glue Compressor's stepped controls, as raw values
GLUE_ATTACK_MS = {0.01: 0.0, 0.1: 1.0, 0.3: 2.0, 1: 3.0, 3: 4.0, 10: 5.0, 30: 6.0}
GLUE_RELEASE_S = {0.1: 0.0, 0.2: 1.0, 0.4: 2.0, 0.6: 3.0, 0.8: 4.0, 1.2: 5.0, "auto": 6.0}
GLUE_RATIO = {2: 0.0, 4: 1.0, 10: 2.0}

RECIPES: dict[str, dict] = {
    "vocal": {
        "description": "Lead vocal: low cut, mud dip, compression, gentle saturation, air.",
        "devices": [
            ("EQ Eight", {"1 Filter Type A": "High Pass 48dB", "1 Frequency A": "90 Hz",
                          "2 Frequency A": "300 Hz", "2 Gain A": "-2 dB",
                          "4 Frequency A": "10 kHz", "4 Gain A": "2 dB"}),
            ("Compressor", {"Threshold": "-20 dB", "Ratio": "3", "Attack": "5 ms",
                            "Release": "60 ms", "Output Gain": "3 dB"}),
            ("Saturator", {"Type": "Soft Sine", "Drive": "3 dB", "Dry/Wet": "25 %"}),
        ],
    },
    "drum_bus": {
        "description": "Drum group: Drum Buss weight and crunch, then parallel glue.",
        "devices": [
            ("Drum Buss", {"Drive": "20 %", "Crunch": "10 %", "Transients": "0.2"}),
            ("Glue Compressor", {"Threshold": "-12 dB", "Ratio": GLUE_RATIO[4],
                                 "Attack": GLUE_ATTACK_MS[10], "Release": GLUE_RELEASE_S[0.2],
                                 "Makeup": "2 dB", "Dry/Wet": "70 %"}),
        ],
    },
    "bass": {
        "description": "Bass: sub cleanup, mud dip, steady compression, harmonics, mono lows.",
        "devices": [
            ("EQ Eight", {"1 Filter Type A": "High Pass 12dB", "1 Frequency A": "30 Hz",
                          "2 Frequency A": "250 Hz", "2 Gain A": "-2 dB"}),
            ("Compressor", {"Threshold": "-18 dB", "Ratio": "4", "Attack": "10 ms",
                            "Release": "100 ms", "Output Gain": "2 dB"}),
            ("Saturator", {"Type": "Soft Sine", "Drive": "4 dB", "Dry/Wet": "30 %"}),
            ("Utility", {"Bass Mono": "On", "Bass Freq": "120 Hz"}),
        ],
    },
    "pad": {
        "description": "Pad: clear the lows, widen, chorus and a long reverb.",
        "devices": [
            ("EQ Eight", {"1 Filter Type A": "High Pass 48dB", "1 Frequency A": "150 Hz"}),
            ("Chorus-Ensemble", {"Dry/Wet": "25 %"}),
            ("Reverb", {"Decay Time": "3.5 s", "Dry/Wet": "30 %"}),
            ("Utility", {"Stereo Width": "120 %"}),
        ],
    },
    "master": {
        "description": "Master bus: subsonic cut, 2:1 glue, mono below 120 Hz, true-peak "
                       "limiter at -1 dBTP. Set the limiter's gain by ear or with "
                       "render_and_analyze.",
        "devices": [
            ("EQ Eight", {"1 Filter Type A": "High Pass 12dB", "1 Frequency A": "25 Hz"}),
            ("Glue Compressor", {"Threshold": "-6 dB", "Ratio": GLUE_RATIO[2],
                                 "Attack": GLUE_ATTACK_MS[10],
                                 "Release": GLUE_RELEASE_S["auto"], "Makeup": "1 dB"}),
            ("Utility", {"Bass Mono": "On", "Bass Freq": "120 Hz"}),
            ("Limiter", {"Mode": "True Peak", "Ceiling": "-1 dB"}),
        ],
    },
    "sidechain_duck": {
        "description": "Ducks this track whenever the sidechain source (usually the kick) "
                       "hits. Needs sidechain_from.",
        "needs_sidechain": True,
        "devices": [
            ("Compressor", {"S/C On": "On", "Model": "Peak", "Threshold": "-30 dB",
                            "Ratio": "8", "Attack": "1 ms", "Release": "150 ms"}),
        ],
    },
}


def recipe(name: str) -> dict:
    key = name.strip().lower().replace(" ", "_").replace("-", "_")
    if key not in RECIPES:
        raise ValueError(f"Unknown chain {name!r}. Known: {', '.join(RECIPES)}")
    return RECIPES[key]
