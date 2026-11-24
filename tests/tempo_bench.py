"""A tempo benchmark: synthetic grooves in the styles the tempo estimate
meets, each with the tempo a listener taps. Short (8 bars at 22.05 kHz) so
the whole set runs in a few seconds."""

import numpy as np

RATE = 22050


def _tone(freq, decay, amp, length=0.15, sweep=0.0):
    n = np.arange(int(length * RATE))
    f = freq * np.exp(-sweep * n / RATE)
    return amp * np.exp(-n / (decay * RATE)) * np.sin(2 * np.pi * np.cumsum(f) / RATE)


def _noise(decay, amp, seed, length=0.08):
    n = np.arange(int(length * RATE))
    return amp * np.exp(-n / (decay * RATE)) * np.random.default_rng(seed).normal(0, 1, len(n))


KICK = _tone(120, 0.09, 0.9, sweep=12)
SNARE = _noise(0.06, 0.45, 0, 0.15) + _tone(190, 0.04, 0.2)
CLAP = _noise(0.03, 0.4, 2)
HAT = _noise(0.012, 0.09, 1)
OPEN_HAT = _noise(0.08, 0.1, 3, 0.2)
STAB = _tone(523, 0.12, 0.6, 0.3) + _tone(659, 0.12, 0.5, 0.3)
CHORD = _tone(330, 0.15, 0.25, 0.3) + _tone(392, 0.15, 0.2, 0.3)
CLICK = _tone(1000, 0.004, 0.5, 0.03)


def render(bpm, events, beats_per_bar=4, bars=8):
    """events: (beat within the bar, sound), repeated every bar."""
    beat = 60.0 / bpm
    y = np.zeros(int(RATE * beat * beats_per_bar * bars) + RATE // 2)
    for bar in range(bars):
        for at, sound in events:
            start = int(round((bar * beats_per_bar + at) * beat * RATE))
            if start < len(y):
                y[start:start + len(sound)] += sound[:len(y) - start]
    return y


def eighths(sound, swing=0.0):
    return [(i / 2 + (swing if i % 2 else 0), sound) for i in range(8)]


def sixteenths(sound):
    return [(i / 4, sound) for i in range(16)]


STYLES = {
    "click": [(i, CLICK) for i in range(4)],
    "four on the floor": [(i, KICK) for i in range(4)] + [(i + 0.5, HAT) for i in range(4)],
    "house": [(i, KICK) for i in range(4)] + [(i + 0.5, OPEN_HAT) for i in range(4)]
    + [(1, CLAP), (3, CLAP)] + sixteenths(_noise(0.008, 0.03, 4)),
    "trap": [(0, KICK), (0.75, KICK), (2.5, KICK), (2, SNARE)] + sixteenths(HAT),
    "drill": [(0, KICK), (1.75, KICK), (2.5, KICK), (1, SNARE), (3.5, SNARE)]
    + [(i / 3, HAT) for i in range(12)],
    "boom bap": [(0, KICK), (1.75, KICK), (2.5, KICK), (1, SNARE), (3, SNARE)]
    + eighths(HAT, swing=0.08),
    "half-time": [(0, KICK), (1.5, KICK), (2, SNARE)] + eighths(HAT),
    "drum and bass": [(0, KICK), (2.5, KICK), (1, SNARE), (3, SNARE)] + eighths(HAT),
    "reggaeton": [(i, KICK) for i in range(4)]
    + [(b + 0.75, SNARE) for b in (0, 2)] + [(b + 1.5, SNARE) for b in (0, 2)] + eighths(HAT),
    # the remake session's song: ~136 BPM whose accents fall every 4/3 beat
    # (a half-note triplet feel over the beat), read as 102
    "triplet accents": [(k * 4 / 3, STAB) for k in range(3)] + [(0, KICK), (2, KICK)]
    + [(i, _noise(0.01, 0.02, 5)) for i in range(4)],
}

# (style, tapped BPM, beats per bar)
CASES = [
    ("click", 90, 4), ("click", 128, 4), ("click", 137, 4), ("click", 175, 4),
    ("four on the floor", 120, 4), ("four on the floor", 128, 4), ("house", 124, 4),
    ("trap", 140, 4), ("drill", 142, 4), ("drill", 145, 4),
    ("boom bap", 90, 4), ("boom bap", 95, 4), ("half-time", 85, 4),
    ("drum and bass", 172, 4), ("drum and bass", 174, 4), ("reggaeton", 95, 4),
    ("waltz", 150, 3), ("waltz", 96, 3), ("triplet accents", 136, 4),
]
STYLES["waltz"] = [(0, KICK), (1, CHORD), (2, CHORD)] + [(i, HAT) for i in range(3)]


def case_audio(style, bpm, beats_per_bar):
    return render(bpm, STYLES[style], beats_per_bar)
