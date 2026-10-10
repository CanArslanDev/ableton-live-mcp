"""Music-theory helpers and note generators.

Pure functions over plain note dicts ({pitch, start_time, duration, velocity,
...}, times in beats). Nothing here talks to Live, so it is fully unit-tested;
the MCP tools in extended_tools.py read and write the notes.
"""

from __future__ import annotations

import random
import re
from typing import Iterable

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_LETTER_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}

SCALES: dict[str, list[int]] = {
    "major": [0, 2, 4, 5, 7, 9, 11],
    "minor": [0, 2, 3, 5, 7, 8, 10],
    "dorian": [0, 2, 3, 5, 7, 9, 10],
    "phrygian": [0, 1, 3, 5, 7, 8, 10],
    "lydian": [0, 2, 4, 6, 7, 9, 11],
    "mixolydian": [0, 2, 4, 5, 7, 9, 10],
    "locrian": [0, 1, 3, 5, 6, 8, 10],
    "harmonic_minor": [0, 2, 3, 5, 7, 8, 11],
    "melodic_minor": [0, 2, 3, 5, 7, 9, 11],
    "phrygian_dominant": [0, 1, 4, 5, 7, 8, 10],
    "major_pentatonic": [0, 2, 4, 7, 9],
    "minor_pentatonic": [0, 3, 5, 7, 10],
    "blues": [0, 3, 5, 6, 7, 10],
    "hijaz": [0, 1, 4, 5, 7, 8, 10],
    "whole_tone": [0, 2, 4, 6, 8, 10],
    "chromatic": list(range(12)),
}
_SCALE_ALIASES = {
    "ionian": "major", "aeolian": "minor", "natural_minor": "minor",
    "maj": "major", "min": "minor", "m": "minor", "pentatonic": "minor_pentatonic",
}

CHORD_QUALITIES: dict[str, list[int]] = {
    "": [0, 4, 7], "maj": [0, 4, 7], "m": [0, 3, 7], "min": [0, 3, 7],
    "dim": [0, 3, 6], "°": [0, 3, 6], "o": [0, 3, 6], "aug": [0, 4, 8], "+": [0, 4, 8],
    "sus2": [0, 2, 7], "sus4": [0, 5, 7], "sus": [0, 5, 7], "5": [0, 7],
    "6": [0, 4, 7, 9], "m6": [0, 3, 7, 9],
    "7": [0, 4, 7, 10], "maj7": [0, 4, 7, 11], "M7": [0, 4, 7, 11],
    "m7": [0, 3, 7, 10], "min7": [0, 3, 7, 10], "mmaj7": [0, 3, 7, 11],
    "m7b5": [0, 3, 6, 10], "ø": [0, 3, 6, 10], "ø7": [0, 3, 6, 10],
    "dim7": [0, 3, 6, 9], "°7": [0, 3, 6, 9], "o7": [0, 3, 6, 9],
    "7sus4": [0, 5, 7, 10],
    "9": [0, 4, 7, 10, 14], "maj9": [0, 4, 7, 11, 14], "m9": [0, 3, 7, 10, 14],
    "add9": [0, 4, 7, 14], "madd9": [0, 3, 7, 14],
    "11": [0, 4, 7, 10, 14, 17], "m11": [0, 3, 7, 10, 14, 17],
    "13": [0, 4, 7, 10, 14, 21],
}

_ROMAN = ["VII", "VI", "IV", "V", "III", "II", "I"]
_ROMAN_DEGREE = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7}
_ROMAN_RE = re.compile(r"^(?P<acc>[b#♭♯]*)(?P<num>VII|VI|IV|V|III|II|I|vii|vi|iv|v|iii|ii|i)"
                       r"(?P<qual>[^/]*)(?:/(?P<bass>.+))?$")
_SYMBOL_RE = re.compile(r"^(?P<root>[A-Ga-g])(?P<acc>[#b♯♭]?)(?P<qual>[^/]*)(?:/(?P<bass>.+))?$")


def note_name(pitch: int) -> str:
    """MIDI pitch → Ableton-style name (60 = C3)."""
    return f"{NOTE_NAMES[pitch % 12]}{pitch // 12 - 2}"


def parse_pitch_class(text: str) -> int:
    text = text.strip()
    if not text:
        raise ValueError("Empty note name")
    letter = text[0].upper()
    if letter not in _LETTER_PC:
        raise ValueError(f"Not a note name: {text!r}")
    pc = _LETTER_PC[letter]
    for ch in text[1:]:
        if ch in "#♯":
            pc += 1
        elif ch in "b♭":
            pc -= 1
        else:
            raise ValueError(f"Not a note name: {text!r}")
    return pc % 12


def parse_note(text: str | int) -> int:
    """"C3" / "F#4" / 60 → MIDI pitch (Ableton octave numbering, C3 = 60)."""
    if isinstance(text, int):
        return text
    s = str(text).strip()
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    m = re.fullmatch(r"([A-Ga-g][#b♯♭]*)(-?\d+)", s)
    if not m:
        raise ValueError(f"Not a note: {text!r} (use e.g. C3, F#2 or a MIDI number)")
    return parse_pitch_class(m.group(1)) + (int(m.group(2)) + 2) * 12


def scale_intervals(scale: str | Iterable[int]) -> list[int]:
    """Intervals of a named scale, or an interval list passed through.

    A list is what Live 12 reports as Song.scale_intervals, e.g. [0, 2, 3, 5, 7, 9, 10].
    """
    if not isinstance(scale, str):
        intervals = sorted({int(i) % 12 for i in scale})
        if not intervals or intervals[0] != 0:
            raise ValueError(f"Scale intervals must start at 0, got {list(scale)!r}")
        return intervals
    key = scale.strip().lower().replace(" ", "_").replace("-", "_")
    key = _SCALE_ALIASES.get(key, key)
    if key not in SCALES:
        raise ValueError(f"Unknown scale {scale!r}. Known: {', '.join(sorted(SCALES))}")
    return SCALES[key]


def parse_key(key: str, scale: str | Iterable[int] | None = None) -> tuple[int, list[int]]:
    """"A minor" / "F#m" / "Eb" (+ optional scale) → (root pitch class, intervals)."""
    text = key.strip()
    parts = text.split(None, 1)
    root_text = parts[0]
    implied = parts[1] if len(parts) > 1 else None
    for suffix in ("min", "m"):
        if len(root_text) > len(suffix) and root_text.endswith(suffix):
            root_text = root_text[: -len(suffix)]
            implied = implied or "minor"
            break
    root = parse_pitch_class(root_text)
    if scale is not None and not isinstance(scale, str):
        return root, scale_intervals(scale)
    return root, scale_intervals(scale or implied or "major")


def beats_per_bar(numerator: int, denominator: int) -> float:
    """Bar length in Live's beats (quarter notes): 4/4 → 4, 6/8 → 3, 7/8 → 3.5."""
    return numerator * 4.0 / denominator


def is_minor(intervals: list[int]) -> bool:
    return 3 in intervals and 4 not in intervals


# Scale degrees of a typical four-chord progression for each mode
_MODE_PROGRESSIONS = {
    (0, 2, 4, 5, 7, 9, 11): [1, 5, 6, 4],   # major:          I V vi IV
    (0, 2, 3, 5, 7, 8, 10): [1, 6, 3, 7],   # natural minor:  i VI III VII
    (0, 2, 3, 5, 7, 9, 10): [1, 4, 1, 7],   # dorian:         i IV i VII
    (0, 1, 3, 5, 7, 8, 10): [1, 2, 1, 7],   # phrygian:       i bII i vii
    (0, 2, 4, 6, 7, 9, 11): [1, 2, 1, 5],   # lydian:         I II I V
    (0, 2, 4, 5, 7, 9, 10): [1, 7, 4, 1],   # mixolydian:     I bVII IV I
    (0, 2, 3, 5, 7, 8, 11): [1, 4, 5, 1],   # harmonic minor: i iv V i
    (0, 2, 3, 5, 7, 9, 11): [1, 4, 5, 1],   # melodic minor
    (0, 1, 4, 5, 7, 8, 10): [1, 2, 1, 7],   # phrygian dominant / hijaz
}


def default_progression(key: str, scale: str | Iterable[int] | None = None) -> str:
    """A four-chord progression that stays inside the key's scale, as chord symbols.

    Built from the scale's own triads, so modes get their characteristic
    chords (D Dorian: Dm G Dm C) instead of minor-key numerals.
    """
    _root, intervals = parse_key(key, scale)
    if len(intervals) != 7:
        return "i VI III VII" if is_minor(intervals) else "I V vi IV"
    degrees = _MODE_PROGRESSIONS.get(tuple(intervals),
                                     [1, 6, 3, 7] if is_minor(intervals) else [1, 5, 6, 4])
    chords = diatonic_chords(key, scale)
    if any("?" in chords[d - 1]["symbol"] for d in degrees):
        return "i VI III VII" if is_minor(intervals) else "I V vi IV"
    return " ".join(chords[d - 1]["symbol"] for d in degrees)


def scale_pitches(root_pc: int, intervals: list[int], low: int, high: int) -> list[int]:
    return [p for p in range(low, high + 1) if (p - root_pc) % 12 in intervals]


def _quality_intervals(quality: str, minor_numeral: bool | None) -> list[int]:
    q = quality.strip()
    if minor_numeral is not None:
        # In roman numerals case carries major/minor; the suffix refines it.
        if minor_numeral:
            if q in ("", "m"):
                return CHORD_QUALITIES["m"]
            if q == "7":
                return CHORD_QUALITIES["m7"]
            if q in ("6", "9", "11", "add9"):
                return CHORD_QUALITIES["m" + q] if "m" + q in CHORD_QUALITIES else CHORD_QUALITIES[q]
            if q in ("maj7", "M7"):
                return CHORD_QUALITIES["mmaj7"]
    if q not in CHORD_QUALITIES:
        raise ValueError(f"Unknown chord quality {quality!r}. Known: "
                         f"{', '.join(sorted(k for k in CHORD_QUALITIES if k))}")
    return CHORD_QUALITIES[q]


def parse_chord(token: str, key_root: int, intervals: list[int]) -> dict:
    """Roman numeral ("ii7", "bVII", "V/5"?) or symbol ("Am7", "C/E") → chord.

    Returns {"symbol", "root_pc", "intervals", "bass_pc"}. Roman numerals are
    read against the given scale, so "VI" in A minor is F.
    """
    t = token.strip()
    m = _ROMAN_RE.match(t)
    if m:
        numeral = m.group("num")
        degree = _ROMAN_DEGREE[numeral.upper()]
        steps = intervals if len(intervals) == 7 else SCALES["major"]
        root = key_root + steps[degree - 1]
        for ch in m.group("acc"):
            root += 1 if ch in "#♯" else -1
        chord_intervals = _quality_intervals(m.group("qual"), numeral.islower())
        bass = m.group("bass")
    else:
        m = _SYMBOL_RE.match(t)
        if not m:
            raise ValueError(f"Cannot read chord {token!r}")
        root = parse_pitch_class(m.group("root") + m.group("acc"))
        chord_intervals = _quality_intervals(m.group("qual"), None)
        bass = m.group("bass")
    bass_pc = None
    if bass:
        bass_pc = parse_pitch_class(bass) if bass[0].upper() in _LETTER_PC else None
    return {"symbol": t, "root_pc": root % 12, "intervals": chord_intervals, "bass_pc": bass_pc}


def parse_progression(progression: str | Iterable[str], key: str,
                      scale: str | Iterable[int] | None = None) -> list[dict]:
    root, intervals = parse_key(key, scale)
    tokens = re.split(r"[\s,\-|]+", progression.strip()) if isinstance(progression, str) \
        else list(progression)
    return [parse_chord(t, root, intervals) for t in tokens if t]


def diatonic_chords(key: str, scale: str | Iterable[int] | None = None,
                    sevenths: bool = False) -> list[dict]:
    root, intervals = parse_key(key, scale)
    if len(intervals) != 7:
        raise ValueError("Diatonic chords need a seven-note scale")
    chords = []
    for degree in range(7):
        stack = [intervals[(degree + k) % 7] + 12 * ((degree + k) // 7) for k in
                 (0, 2, 4, 6)[: 4 if sevenths else 3]]
        rel = [s - stack[0] for s in stack]
        quality = next((q for q, iv in CHORD_QUALITIES.items()
                        if iv == rel and q in ("", "m", "dim", "aug", "7", "maj7", "m7",
                                               "m7b5", "dim7", "mmaj7")), "?")
        pc = (root + stack[0]) % 12
        numeral = ["I", "II", "III", "IV", "V", "VI", "VII"][degree]
        if quality.startswith("m") and not quality.startswith("maj") or quality in ("dim", "m7b5", "dim7"):
            numeral = numeral.lower()
        suffix = {"dim": "°", "m7b5": "ø7", "dim7": "°7", "m": "", "m7": "7"}.get(quality, quality)
        chords.append({"degree": degree + 1, "numeral": numeral + suffix,
                       "symbol": NOTE_NAMES[pc] + quality,
                       "notes": [NOTE_NAMES[(pc + r) % 12] for r in rel]})
    return chords


# ── Voicing ──────────────────────────────────────────────────────────────────

def _close_voicing(chord: dict, octave: int) -> list[int]:
    base = chord["root_pc"] + (octave + 2) * 12
    return [base + i for i in chord["intervals"]]


def _inversions(pitches: list[int]) -> list[list[int]]:
    out = [pitches]
    current = pitches
    for _ in range(len(pitches) - 1):
        current = current[1:] + [current[0] + 12]
        out.append(current)
    return out


def voice_progression(chords: list[dict], octave: int = 3, voicing: str = "smooth",
                      bass_note: bool = False) -> list[list[int]]:
    """Pitches for each chord. voicing: close | smooth | open | drop2 | spread."""
    voiced: list[list[int]] = []
    previous: list[int] | None = None
    for chord in chords:
        close = _close_voicing(chord, octave)
        if voicing == "smooth" and previous:
            centre = sum(previous) / len(previous)
            candidates = []
            for inv in _inversions(close):
                for shift in (-12, 0, 12):
                    cand = [p + shift for p in inv]
                    cost = sum(min(abs(p - q) for q in previous) for p in cand)
                    cost += abs(sum(cand) / len(cand) - centre) * 0.5
                    candidates.append((cost, cand))
            pitches = min(candidates, key=lambda c: c[0])[1]
        elif voicing == "open" and len(close) >= 3:
            pitches = [close[0], close[2], close[1] + 12] + [p + 12 for p in close[3:]]
        elif voicing == "drop2" and len(close) >= 4:
            top = sorted(close)
            dropped = top[-2] - 12
            pitches = sorted([dropped] + top[:-2] + [top[-1]])
        elif voicing == "spread":
            pitches = [close[0] - 12] + [p + 12 * (i % 2) for i, p in enumerate(close[1:])]
        else:
            pitches = close
        pitches = sorted(pitches)
        if bass_note or chord.get("bass_pc") is not None:
            bass_pc = chord["bass_pc"] if chord.get("bass_pc") is not None else chord["root_pc"]
            bass = bass_pc + (octave + 1) * 12
            while bass >= pitches[0]:
                bass -= 12
            pitches = [bass] + pitches
        voiced.append(pitches)
        previous = pitches
    return voiced


# ── Rhythm helpers ───────────────────────────────────────────────────────────

CHORD_RHYTHMS: dict[str, list[tuple[float, float]]] = {
    # (offset, duration) within one bar of 4 beats
    "sustain": [(0.0, 4.0)],
    "half": [(0.0, 2.0), (2.0, 2.0)],
    "quarters": [(i, 0.9) for i in range(4)],
    "eighths": [(i * 0.5, 0.45) for i in range(8)],
    "offbeat": [(i + 0.5, 0.4) for i in range(4)],
    "syncopated": [(0.0, 1.4), (1.5, 1.0), (3.0, 0.9)],
    "charleston": [(0.0, 1.4), (1.5, 2.4)],
    "stabs": [(0.0, 0.25), (0.75, 0.25), (1.5, 0.25), (2.5, 0.25), (3.25, 0.25)],
}


def _note(pitch: int, start: float, duration: float, velocity: float) -> dict:
    return {"pitch": int(pitch), "start_time": round(start, 6),
            "duration": round(max(duration, 0.01), 6),
            "velocity": int(min(127, max(1, round(velocity))))}


def chord_notes(voiced: list[list[int]], bars_per_chord: float = 1.0,
                rhythm: str = "sustain", velocity: int = 90,
                beats_per_bar: float = 4.0) -> list[dict]:
    if rhythm not in CHORD_RHYTHMS:
        raise ValueError(f"Unknown rhythm {rhythm!r}. Known: {', '.join(CHORD_RHYTHMS)}")
    pattern = CHORD_RHYTHMS[rhythm]
    notes = []
    span = bars_per_chord * beats_per_bar
    for index, pitches in enumerate(voiced):
        chord_start = index * span
        bar = 0.0
        while bar < span - 1e-9:
            for offset, duration in pattern:
                # Patterns are written for a 4/4 bar. In other meters hits keep
                # their beat position and those past the bar line are dropped.
                if rhythm == "sustain":
                    duration = span
                elif offset >= beats_per_bar - 1e-9:
                    continue
                else:
                    duration = min(duration, beats_per_bar - offset)
                start = chord_start + bar + offset
                if start >= chord_start + span - 1e-9:
                    continue
                dur = min(duration, chord_start + span - start)
                for k, pitch in enumerate(pitches):
                    accent = 6 if offset == 0 else 0
                    notes.append(_note(pitch, start, dur, velocity + accent - k * 2))
            if rhythm == "sustain":
                break
            bar += beats_per_bar
    return notes


# ── Drums ────────────────────────────────────────────────────────────────────

GM_DRUMS = {
    "kick": 36, "rim": 37, "snare": 38, "clap": 39, "snare2": 40,
    "low_tom": 41, "closed_hat": 42, "mid_tom": 45, "pedal_hat": 44,
    "open_hat": 46, "high_tom": 48, "crash": 49, "ride": 51, "shaker": 70,
    "cowbell": 56, "perc": 60,
}

# 16-step grids per bar; characters: x = hit, X = accent, . = rest,
# r = roll (two 32nds), o = ghost.
DRUM_STYLES: dict[str, dict[str, str]] = {
    "house": {"kick": "x...x...x...x...", "clap": "....x.......x...",
              "closed_hat": "..x...x...x...x.", "open_hat": "..o...o...o...o."},
    "techno": {"kick": "X...x...X...x...", "clap": "....x.......x...",
               "closed_hat": "xxXxxxXxxxXxxxXx", "open_hat": "..x...x...x...x.",
               "perc": "...x..x....x..x."},
    "trap": {"kick": "x......x..x.....", "snare": "........X.......",
             "closed_hat": "x.x.x.x.x.xrx.xr", "open_hat": "..............x."},
    "drill": {"kick": "x.....x.....x...", "snare": "......X.......X.",
              "closed_hat": "x..x..x.x..x..xr", "perc": "...x.......x...."},
    "boom_bap": {"kick": "x.......x.x.....", "snare": "....X.......X...",
                 "closed_hat": "x.x.x.x.x.x.x.xo"},
    "lofi": {"kick": "x......x..x.....", "snare": "....X..o....X...",
             "closed_hat": "x.xox.x.x.xox.x.", "rim": "..........o....."},
    "dnb": {"kick": "x.........x.....", "snare": "....X.......X...",
            "closed_hat": "x.x.x.x.x.x.x.x.", "ride": "..x...x...x...x."},
    "breakbeat": {"kick": "x.........x.x...", "snare": "....X..o.o..X...",
                  "closed_hat": "x.x.x.x.x.x.x.x."},
    "uk_garage": {"kick": "x.........x.....", "snare": "....X.......X...",
                  "closed_hat": "..x...x.x.x...x.", "shaker": "x.xxx.xxx.xxx.xx"},
    "reggaeton": {"kick": "x...x...x...x...", "snare": "...x..x....x..x.",
                  "closed_hat": "x.x.x.x.x.x.x.x."},
    "afrobeats": {"kick": "x.....x...x.....", "snare": "...x..x.....x...",
                  "rim": "..x.....x.....x.", "shaker": "xxxXxxxXxxxXxxxX"},
    "rock": {"kick": "x.....x.x.......", "snare": "....X.......X...",
             "closed_hat": "x.x.x.x.x.x.x.x.", "crash": "x..............."},
    "funk": {"kick": "x..x...x..x.....", "snare": "....X..o.o..X..o",
             "closed_hat": "xxxxxxxxxxxxxxxx"},
    "disco": {"kick": "x...x...x...x...", "snare": "....x.......x...",
              "open_hat": "..x...x...x...x.", "closed_hat": "x...x...x...x..."},
    "halftime": {"kick": "x.......x.....x.", "snare": "........X.......",
                 "closed_hat": "x.x.x.x.x.x.x.x."},
}


def _fit_steps(steps: str, count: int) -> str:
    """A 16-step 4/4 grid cut or repeated to `count` 16th steps (another meter)."""
    if count <= len(steps):
        return steps[:count]
    return (steps * (count // len(steps) + 1))[:count]


def drum_pattern(style: str, bars: int = 2, mapping: dict[str, int] | None = None,
                 swing: float = 0.0, humanize: float = 0.0, fill: bool = True,
                 seed: int | None = None, velocity: int = 100,
                 beats_per_bar: float = 4.0) -> list[dict]:
    """Drum notes for `bars` bars. The 4/4 grids are cut or repeated to fit
    other meters (3/4 → 12 steps, 7/8 → 14, 9/8 → 18)."""
    if style not in DRUM_STYLES:
        raise ValueError(f"Unknown style {style!r}. Known: {', '.join(sorted(DRUM_STYLES))}")
    pads = dict(GM_DRUMS)
    if mapping:
        pads.update({k: parse_note(v) for k, v in mapping.items()})
    notes = []
    step_count = max(1, int(round(beats_per_bar * 4)))
    grid = {voice: _fit_steps(steps, step_count) for voice, steps in DRUM_STYLES[style].items()}
    for bar in range(bars):
        last_bar = fill and bars > 1 and bar == bars - 1
        for voice, steps in grid.items():
            pitch = pads.get(voice)
            if pitch is None:
                continue
            for step, ch in enumerate(steps):
                if ch == ".":
                    continue
                vel = {"X": velocity + 15, "o": velocity * 0.45}.get(ch, velocity * 0.85)
                start = bar * beats_per_bar + step * 0.25
                if step % 2 == 1:
                    start += swing * 0.25 * 0.5
                if ch == "r":
                    for k in range(2):
                        notes.append(_note(pitch, start + k * 0.125, 0.1,
                                           vel * (0.7 + 0.3 * k)))
                    continue
                notes.append(_note(pitch, start, 0.2, vel))
        if last_bar:
            snare = pads.get("snare", 38)
            fill_start = bar * beats_per_bar + max(0.0, beats_per_bar - 1)
            for k in range(4):
                notes.append(_note(snare, fill_start + k * 0.25, 0.2, 70 + k * 12))
    if humanize > 0:
        notes = humanize_notes(notes, timing=0.02 * humanize, velocity=12 * humanize, seed=seed)
    return notes


# ── Bass / arpeggio / melody ─────────────────────────────────────────────────

BASS_STYLES = ("root", "octave", "offbeat", "syncopated", "walking", "pulse", "808")


def bassline(chords: list[dict], bars_per_chord: float = 1.0, octave: int = 1,
             style: str = "root", key_root: int | None = None,
             intervals: list[int] | None = None, velocity: int = 100,
             seed: int | None = None, beats_per_bar: float = 4.0) -> list[dict]:
    if style not in BASS_STYLES:
        raise ValueError(f"Unknown bass style {style!r}. Known: {', '.join(BASS_STYLES)}")
    rng = random.Random(seed)
    notes = []
    span = bars_per_chord * beats_per_bar
    for index, chord in enumerate(chords):
        pc = chord["bass_pc"] if chord.get("bass_pc") is not None else chord["root_pc"]
        root = pc + (octave + 2) * 12
        fifth = root + 7
        start = index * span
        if style in ("root", "808"):
            notes.append(_note(root, start, span * (0.98 if style == "root" else 0.9), velocity))
        elif style == "octave":
            for k in range(int(span * 2)):
                notes.append(_note(root + (12 if k % 2 else 0), start + k * 0.5, 0.4,
                                   velocity - (10 if k % 2 else 0)))
        elif style == "offbeat":
            for k in range(int(span)):
                notes.append(_note(root, start + k + 0.5, 0.4, velocity))
        elif style == "pulse":
            for k in range(int(span * 4)):
                notes.append(_note(root, start + k * 0.25, 0.2, velocity - (0 if k % 4 == 0 else 15)))
        elif style == "syncopated":
            for offset, dur, pitch in ((0, 0.75, root), (0.75, 0.5, root), (1.5, 0.5, fifth),
                                       (2.5, 0.5, root + 12), (3.0, 0.75, root)):
                b = 0.0
                while b < span - 1e-9:
                    if offset < beats_per_bar and b + offset < span:
                        notes.append(_note(pitch, start + b + offset,
                                           min(dur, span - b - offset), velocity))
                    b += beats_per_bar
        elif style == "walking":
            next_chord = chords[(index + 1) % len(chords)]
            target = next_chord["root_pc"] + (octave + 2) * 12
            steps = int(span)
            pool = scale_pitches(key_root if key_root is not None else chord["root_pc"],
                                 intervals or SCALES["major"], root - 5, root + 12)
            line = [root]
            for k in range(1, steps):
                remaining = steps - k
                direction = 1 if target > line[-1] else -1
                options = sorted((p for p in pool if 0 < (p - line[-1]) * direction <= 4),
                                 key=lambda p: abs(p - line[-1])) or pool
                if remaining == 1:
                    approach = target - direction
                    line.append(approach)
                else:
                    line.append(rng.choice(options[:2]) if options else line[-1])
            for k, pitch in enumerate(line):
                notes.append(_note(pitch, start + k, 0.9, velocity - (8 if k % 2 else 0)))
    return notes


ARP_PATTERNS = ("up", "down", "updown", "downup", "random", "converge", "pinky")


def arpeggio(voiced: list[list[int]], bars_per_chord: float = 1.0, rate: float = 0.25,
             pattern: str = "up", octaves: int = 1, gate: float = 0.8,
             velocity: int = 90, seed: int | None = None,
             beats_per_bar: float = 4.0) -> list[dict]:
    if pattern not in ARP_PATTERNS:
        raise ValueError(f"Unknown pattern {pattern!r}. Known: {', '.join(ARP_PATTERNS)}")
    rng = random.Random(seed)
    notes = []
    span = bars_per_chord * beats_per_bar
    for index, pitches in enumerate(voiced):
        pool = [p + 12 * o for o in range(max(1, octaves)) for p in sorted(pitches)]
        if pattern == "down":
            seq = pool[::-1]
        elif pattern == "updown":
            seq = pool + pool[-2:0:-1]
        elif pattern == "downup":
            seq = pool[::-1] + pool[1:-1]
        elif pattern == "converge":
            seq = []
            lo, hi = 0, len(pool) - 1
            while lo <= hi:
                seq.append(pool[lo])
                if lo != hi:
                    seq.append(pool[hi])
                lo, hi = lo + 1, hi - 1
        elif pattern == "pinky":
            seq = []
            for p in pool[:-1]:
                seq += [p, pool[-1]]
        else:
            seq = pool
        steps = int(round(span / rate))
        for k in range(steps):
            pitch = rng.choice(pool) if pattern == "random" else seq[k % len(seq)]
            accent = 10 if (k * rate) % 1 == 0 else 0
            notes.append(_note(pitch, index * span + k * rate, rate * gate, velocity + accent))
    return notes


_MELODY_RHYTHMS = [
    [1.0, 1.0, 1.0, 1.0], [0.5, 0.5, 1.0, 2.0], [1.5, 0.5, 1.0, 1.0],
    [0.5, 0.5, 0.5, 0.5, 2.0], [1.0, 0.5, 0.5, 2.0], [0.75, 0.75, 0.5, 2.0],
    [2.0, 1.0, 1.0], [0.5, 1.0, 0.5, 1.0, 1.0],
]


def melody(key: str, scale: str | Iterable[int] | None = None, bars: int = 4,
           low: str | int = "C3", high: str | int = "C5", chords: list[dict] | None = None,
           bars_per_chord: float = 1.0, density: float = 0.6, seed: int | None = None,
           velocity: int = 95, beats_per_bar: float = 4.0) -> list[dict]:
    """Phrase-structured melody (A A' B A'') that leans on chord tones on strong beats."""
    root, intervals = parse_key(key, scale)
    rng = random.Random(seed)
    pool = scale_pitches(root, intervals, parse_note(low), parse_note(high))
    if not pool:
        raise ValueError("Empty pitch range")

    def chord_at(beat: float) -> set[int] | None:
        if not chords:
            return None
        chord = chords[int(beat // (bars_per_chord * beats_per_bar)) % len(chords)]
        return {(chord["root_pc"] + i) % 12 for i in chord["intervals"]}

    def make_bar(start_idx: int) -> tuple[list[tuple[float, float, int]], int]:
        rhythm = rng.choice(_MELODY_RHYTHMS)
        if density < 0.4:
            rhythm = [r * 2 for r in rhythm][: max(1, len(rhythm) // 2)]
        idx = start_idx
        out = []
        t = 0.0
        for dur in rhythm:
            if t >= beats_per_bar - 1e-9:
                break
            step = rng.choice([-2, -1, -1, 0, 1, 1, 2, 3, -3]) if rng.random() < 0.85 else rng.choice([-4, 4])
            idx = min(len(pool) - 1, max(0, idx + step))
            out.append((t, min(dur, beats_per_bar - t), idx))
            t += dur
        return out, idx

    motif_a, end_idx = make_bar(len(pool) // 2)
    motif_b, _ = make_bar(end_idx)
    plan = []
    for bar in range(bars):
        section = bar % 4
        if section in (0, 3):
            plan.append([(t, d, i + (0 if section == 0 else rng.choice([0, 0, 1, -1])))
                         for t, d, i in motif_a])
        elif section == 1:
            plan.append([(t, d, i + rng.choice([1, 2, -1])) for t, d, i in motif_a])
        else:
            plan.append(motif_b)

    def strong(t: float) -> bool:
        # Downbeat, plus the half bar in 4/4 and longer bars
        return t == 0.0 or (beats_per_bar >= 4 and abs(t - beats_per_bar / 2) < 1e-9)

    notes = []
    for bar, events in enumerate(plan):
        for t, d, i in events:
            if rng.random() > 0.35 + density * 0.65 and t > 0:
                continue
            i = min(len(pool) - 1, max(0, i))
            pitch = pool[i]
            beat = bar * beats_per_bar + t
            tones = chord_at(beat)
            if tones and strong(t) and pitch % 12 not in tones:
                candidates = [p for p in pool if p % 12 in tones]
                if candidates:
                    pitch = min(candidates, key=lambda p: abs(p - pitch))
            notes.append(_note(pitch, beat, d * 0.95, velocity + (8 if t == 0 else 0)))
    # Resolve the phrase onto the tonic
    if notes:
        last = notes[-1]
        tonic = min((p for p in pool if p % 12 == root), key=lambda p: abs(p - last["pitch"]),
                    default=last["pitch"])
        last["pitch"] = tonic
    return notes


# ── Transformations ──────────────────────────────────────────────────────────

def _copy(notes: list[dict]) -> list[dict]:
    return [dict(n) for n in notes]


def transpose(notes: list[dict], semitones: int) -> list[dict]:
    out = _copy(notes)
    for n in out:
        n["pitch"] = min(127, max(0, n["pitch"] + semitones))
    return out


def transpose_in_scale(notes: list[dict], steps: int, key: str,
                       scale: str | Iterable[int] | None = None) -> list[dict]:
    root, intervals = parse_key(key, scale)
    pool = scale_pitches(root, intervals, 0, 127)
    out = _copy(notes)
    for n in out:
        nearest = min(range(len(pool)), key=lambda i: abs(pool[i] - n["pitch"]))
        n["pitch"] = pool[min(len(pool) - 1, max(0, nearest + steps))]
    return out


def quantize(notes: list[dict], grid: float = 0.25, strength: float = 1.0,
             quantize_ends: bool = False) -> list[dict]:
    out = _copy(notes)
    for n in out:
        target = round(n["start_time"] / grid) * grid
        n["start_time"] = round(n["start_time"] + (target - n["start_time"]) * strength, 6)
        if quantize_ends:
            end = n["start_time"] + n["duration"]
            end_t = max(grid, round(end / grid) * grid)
            n["duration"] = max(0.01, round(end + (end_t - end) * strength - n["start_time"], 6))
    return out


def humanize_notes(notes: list[dict], timing: float = 0.01, velocity: float = 8,
                   seed: int | None = None) -> list[dict]:
    rng = random.Random(seed)
    out = _copy(notes)
    for n in out:
        n["start_time"] = round(max(0.0, n["start_time"] + rng.gauss(0, timing)), 6)
        n["velocity"] = int(min(127, max(1, round(n["velocity"] + rng.gauss(0, velocity)))))
    return out


def scale_velocity(notes: list[dict], factor: float = 1.0, offset: float = 0,
                   minimum: int = 1, maximum: int = 127) -> list[dict]:
    out = _copy(notes)
    for n in out:
        n["velocity"] = int(min(maximum, max(minimum, round(n["velocity"] * factor + offset))))
    return out


def velocity_ramp(notes: list[dict], start: int, end: int) -> list[dict]:
    out = _copy(notes)
    if not out:
        return out
    t0 = min(n["start_time"] for n in out)
    t1 = max(n["start_time"] for n in out)
    for n in out:
        frac = 0.0 if t1 == t0 else (n["start_time"] - t0) / (t1 - t0)
        n["velocity"] = int(min(127, max(1, round(start + (end - start) * frac))))
    return out


def reverse(notes: list[dict], length: float) -> list[dict]:
    out = _copy(notes)
    for n in out:
        n["start_time"] = round(max(0.0, length - n["start_time"] - n["duration"]), 6)
    return out


def invert(notes: list[dict], axis: int | None = None) -> list[dict]:
    out = _copy(notes)
    if not out:
        return out
    if axis is None:
        axis = out[0]["pitch"]
    for n in out:
        n["pitch"] = min(127, max(0, 2 * axis - n["pitch"]))
    return out


def legato(notes: list[dict], gap: float = 0.0) -> list[dict]:
    out = sorted(_copy(notes), key=lambda n: n["start_time"])
    starts = sorted({n["start_time"] for n in out})
    for n in out:
        later = [s for s in starts if s > n["start_time"] + 1e-9]
        if later:
            n["duration"] = max(0.01, round(later[0] - n["start_time"] - gap, 6))
    return out


def stretch(notes: list[dict], factor: float) -> list[dict]:
    out = _copy(notes)
    for n in out:
        n["start_time"] = round(n["start_time"] * factor, 6)
        n["duration"] = round(n["duration"] * factor, 6)
    return out


def set_field(notes: list[dict], field: str, value: float) -> list[dict]:
    out = _copy(notes)
    for n in out:
        n[field] = value
    return out


def dedupe(notes: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for n in sorted(notes, key=lambda n: (n["start_time"], n["pitch"])):
        key = (n["pitch"], round(n["start_time"], 4))
        if key not in seen:
            seen.add(key)
            out.append(dict(n))
    return out


def constrain_to_scale(notes: list[dict], key: str,
                       scale: str | Iterable[int] | None = None) -> list[dict]:
    root, intervals = parse_key(key, scale)
    pool = scale_pitches(root, intervals, 0, 127)
    out = _copy(notes)
    for n in out:
        n["pitch"] = min(pool, key=lambda p: (abs(p - n["pitch"]), p))
    return out


# ── Harmony analysis ─────────────────────────────────────────────────────────

# Krumhansl–Kessler key profiles (tonic first)
KEY_PROFILES = {
    "major": [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88],
    "minor": [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17],
}

# Chord qualities tried when naming notes, all readable by parse_chord so a
# detected progression can be fed straight back into the generators.
ANALYSIS_QUALITIES = ["", "m", "dim", "aug", "sus2", "sus4", "5", "7", "maj7", "m7",
                      "m7b5", "dim7", "6", "m6", "7sus4", "mmaj7", "add9", "madd9"]
_MINOR_QUALITIES = {"m", "dim", "m7", "m7b5", "dim7", "m6", "mmaj7", "madd9"}
_NUMERAL_SUFFIX = {"": "", "m": "", "dim": "°", "aug": "+", "sus2": "sus2", "sus4": "sus4",
                   "5": "5", "7": "7", "maj7": "maj7", "m7": "7", "m7b5": "ø7", "dim7": "°7",
                   "6": "6", "m6": "6", "7sus4": "7sus4", "mmaj7": "maj7", "add9": "add9",
                   "madd9": "add9"}
_PRESENT = 0.04  # share of a window's weight for a pitch class to count as sounding


def pitch_class_weights(notes: Iterable[dict], start: float | None = None,
                        end: float | None = None) -> list[float]:
    """How much each pitch class sounds (duration × loudness), optionally in [start, end)."""
    weights = [0.0] * 12
    for n in notes:
        s, e = n["start_time"], n["start_time"] + n["duration"]
        if start is not None:
            s, e = max(s, start), min(e, end)
        if e <= s or n.get("mute"):
            continue
        weights[n["pitch"] % 12] += (e - s) * (0.5 + n.get("velocity", 100) / 254.0)
    return weights


def _pearson(a: list[float], b: list[float]) -> float:
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a) ** 0.5
    vb = sum((y - mb) ** 2 for y in b) ** 0.5
    return cov / (va * vb) if va and vb else 0.0


def estimate_key(notes: Iterable[dict]) -> dict | None:
    """Key of a set of notes (Krumhansl–Schmuckler). None when nothing sounds."""
    weights = pitch_class_weights(notes)
    if not any(weights):
        return None
    best = None
    for tonic in range(12):
        for mode, profile in KEY_PROFILES.items():
            expected = [profile[(pc - tonic) % 12] for pc in range(12)]
            r = _pearson(weights, expected)
            if best is None or r > best[0] + 1e-12:
                best = (r, tonic, mode)
    r, tonic, mode = best
    return {"key": f"{NOTE_NAMES[tonic]} {mode}", "tonic": tonic, "mode": mode,
            "confidence": round(r, 2)}


def identify_chord(weights: list[float], bass_pc: int | None = None) -> dict | None:
    """Best chord for a pitch-class weighting. None for fewer than two pitch classes."""
    total = sum(weights)
    if total <= 0:
        return None
    share = [w / total for w in weights]
    if sum(1 for s in share if s >= _PRESENT) < 2:
        return None
    best = None
    for root in range(12):
        for quality in ANALYSIS_QUALITIES:
            tones = {(root + i) % 12 for i in CHORD_QUALITIES[quality]}
            covered = sum(share[pc] for pc in tones)
            missing = sum(1 for pc in tones if share[pc] < _PRESENT) / len(tones)
            # Explain what sounds, don't invent tones, prefer the simpler chord
            score = covered - 0.3 * missing - 0.01 * (len(tones) - 3)
            if bass_pc is not None:
                if bass_pc == root:
                    score += 0.08
                elif bass_pc not in tones:
                    score -= 0.05
            if best is None or score > best[0] + 1e-9:
                best = (score, root, quality, tones)
    score, root, quality, tones = best
    symbol = NOTE_NAMES[root] + quality
    if bass_pc is not None and bass_pc != root and bass_pc in tones:
        symbol += "/" + NOTE_NAMES[bass_pc]
    return {"symbol": symbol, "root_pc": root, "quality": quality, "score": round(score, 3)}


def roman_numeral(root_pc: int, quality: str, tonic: int, mode: str) -> str:
    """Roman numeral of a chord in a major or (natural) minor key: "V7", "bVII", "#vii°"."""
    scale = SCALES["minor" if mode == "minor" else "major"]
    offset = (root_pc - tonic) % 12
    if offset in scale:
        prefix, degree = "", scale.index(offset)
    elif offset == 1:
        prefix, degree = "b", 1  # Neapolitan bII in either mode
    elif mode == "minor" and (offset - 1) in scale:
        prefix, degree = "#", scale.index(offset - 1)  # raised 3rd/6th/7th
    else:
        prefix, degree = "b", scale.index((offset + 1) % 12)
    numeral = ["I", "II", "III", "IV", "V", "VI", "VII"][degree]
    if quality in _MINOR_QUALITIES:
        numeral = numeral.lower()
    return prefix + numeral + _NUMERAL_SUFFIX.get(quality, quality)


def _bass_pitch(notes: list[dict], start: float, end: float) -> int | None:
    """Lowest note that sounds for a meaningful part of the window."""
    span = end - start
    lowest = None
    for n in notes:
        if n.get("mute"):
            continue
        overlap = min(end, n["start_time"] + n["duration"]) - max(start, n["start_time"])
        if overlap >= min(0.125 * span, 0.5 * n["duration"]) and overlap > 0:
            if lowest is None or n["pitch"] < lowest:
                lowest = n["pitch"]
    return lowest


def analyze_harmony(notes: list[dict], beats_per_bar: float = 4.0,
                    segment: float | None = None, length: float | None = None,
                    key: str | None = None, scale: str | Iterable[int] | None = None) -> dict:
    """Chords per window, the key, roman numerals, and a progression the
    generators accept (with bars_per_chord) so new parts can follow it.

    segment: window length in beats (default one bar). key/scale: analyse
    against this key instead of estimating it from the notes.
    """
    segment = segment or beats_per_bar
    sounding = [n for n in notes if not n.get("mute")]
    end = length or max((n["start_time"] + n["duration"] for n in sounding), default=0.0)
    if key is not None:
        tonic, intervals = parse_key(key, scale)
        key_info = {"key": f"{NOTE_NAMES[tonic]} {'minor' if is_minor(intervals) else 'major'}",
                    "tonic": tonic, "mode": "minor" if is_minor(intervals) else "major",
                    "source": "given"}
    else:
        key_info = estimate_key(sounding)
        if key_info:
            key_info["source"] = "estimated from the notes"
    windows = []
    t = 0.0
    while t < end - 1e-9:
        stop = min(t + segment, end)
        weights = pitch_class_weights(sounding, t, stop)
        bass = _bass_pitch(sounding, t, stop)
        chord = identify_chord(weights, bass % 12 if bass is not None else None)
        entry = {"start": round(t, 4), "bar": int(t // beats_per_bar) + 1,
                 "beat": round(t % beats_per_bar + 1, 3),
                 "chord": chord["symbol"] if chord else "N.C.",
                 "notes": [NOTE_NAMES[pc] for pc in range(12)
                           if sum(weights) and weights[pc] / sum(weights) >= _PRESENT]}
        if chord and key_info:
            entry["roman"] = roman_numeral(chord["root_pc"], chord["quality"],
                                           key_info["tonic"], key_info["mode"])
        windows.append(entry)
        t += segment

    named = [w["chord"] for w in windows]
    first = next((c for c in named if c != "N.C."), None)
    progression, previous = [], first
    for chord in named:
        previous = chord if chord != "N.C." else previous
        progression.append(previous)
    runs: list[dict] = []
    for w in windows:
        if runs and runs[-1]["chord"] == w["chord"]:
            runs[-1]["beats"] = round(runs[-1]["beats"] + segment, 4)
        else:
            runs.append({"chord": w["chord"], "roman": w.get("roman"), "start": w["start"],
                         "beats": segment})
    return {
        "key": key_info,
        "windows": windows,
        "changes": runs,
        "progression": " ".join(progression) if first else None,
        "bars_per_chord": round(segment / beats_per_bar, 4),
    }
