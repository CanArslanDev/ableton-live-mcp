"""Turkish makam music: makam scales and usul rhythms.

Makams are given in the Arel-Ezgi-Uzdilek (AEU) system: the octave is 53
Holdrian commas (koma, 22.64 cents) and a makam's scale (dizi) is a chain of
steps of 4 (bakiye), 5 (küçük mücenneb), 8 (büyük mücenneb), 9 (tanini) and
12-13 (artık ikili) commas, built from tetrachords and pentachords (çeşni).
Two renderings come out of it:

- a 12-tone approximation (each degree rounded to the nearest semitone) that
  the generators, scale snapping and Live's own scale features can use;
- an .ascl tuning file for Live 12 that retunes the makam's degrees to their
  comma pitches, so the same MIDI notes sound the real intervals.

Usuls are cycles of strokes: düm (low, strong), tek (high), and the light
te / ke / ka, each lasting a number of time units (zaman) of the usul's
mertebe (the time signature's denominator). Patterns follow the common
teaching forms ("Düüm Teek Tek" = düm 2, tek 2, tek 1).
"""

from __future__ import annotations

import random
from pathlib import Path

COMMA_CENTS = 1200 / 53
_FOLD = str.maketrans("çğıöşüâîûÇĞİÖŞÜÂÎÛ", "cgiosuaiuCGIOSUAIU")

# Çeşni (AEU, commas): building blocks of the makam scales
TETRACHORDS = {
    "çargâh": [9, 9, 4], "bûselik": [9, 4, 9], "kürdî": [4, 9, 9],
    "rast": [9, 8, 5], "uşşak": [8, 5, 9], "hicaz": [5, 12, 5], "saba": [8, 5, 5],
}
PENTACHORDS = {name: steps + [9] for name, steps in TETRACHORDS.items() if name != "saba"}

# name: (AEU steps in commas from the karar, karar note, güçlü note, how it is built)
MAKAMS = {
    "rast": ([9, 8, 5, 9, 9, 8, 5], "G", "D", "Rast pentachord + Rast tetrachord"),
    "mahur": ([9, 9, 4, 9, 9, 9, 4], "G", "G", "Çargâh pentachord + Çargâh tetrachord on Rast"),
    "ussak": ([8, 5, 9, 9, 4, 9, 9], "A", "D", "Uşşak tetrachord + Bûselik pentachord"),
    "bayati": ([8, 5, 9, 9, 4, 9, 9], "A", "D", "Uşşak tetrachord + Bûselik pentachord "
               "(the Uşşak scale, sung around Neva)"),
    "huseyni": ([8, 5, 9, 9, 8, 5, 9], "A", "E", "Hüseynî pentachord + Uşşak tetrachord"),
    "muhayyer": ([8, 5, 9, 9, 8, 5, 9], "A", "E", "Hüseynî scale, starting from the top"),
    "neva": ([8, 5, 9, 9, 8, 5, 9], "A", "D", "Uşşak tetrachord + Rast pentachord on Neva"),
    "kurdi": ([4, 9, 9, 9, 4, 9, 9], "A", "D", "Kürdî tetrachord + Bûselik pentachord"),
    "buselik": ([9, 4, 9, 9, 4, 9, 9], "A", "E", "Bûselik pentachord + Kürdî tetrachord"),
    "hicaz": ([5, 12, 5, 9, 8, 5, 9], "A", "D", "Hicaz tetrachord + Rast pentachord on Neva"),
    "uzzal": ([5, 12, 5, 9, 8, 5, 9], "A", "E", "Hicaz pentachord + Uşşak tetrachord"),
    "humayun": ([5, 12, 5, 9, 4, 9, 9], "A", "D", "Hicaz tetrachord + Bûselik pentachord"),
    "zirguleli_hicaz": ([5, 12, 5, 9, 5, 12, 5], "A", "D",
                        "Hicaz pentachord + Hicaz tetrachord"),
    "karcigar": ([8, 5, 9, 5, 12, 5, 9], "A", "D", "Uşşak tetrachord + Hicaz pentachord"),
    "saba": ([8, 5, 5, 12, 5, 9, 9], "A", "C", "Saba tetrachord + Hicaz pentachord on Çargâh"),
    "segah": ([5, 9, 9, 8, 5, 9, 8], "B", "D", "Segâh trichord + Rast pentachord on Neva"),
    "huzzam": ([5, 9, 5, 12, 5, 9, 8], "B", "D", "Segâh trichord + Hicaz tetrachord on Neva"),
    "nihavend": ([9, 4, 9, 9, 4, 9, 9], "G", "D", "Bûselik pentachord + Kürdî tetrachord "
                 "on Rast"),
    "suzinak": ([9, 8, 5, 9, 5, 12, 5], "G", "D", "Rast pentachord + Hicaz tetrachord"),
    "hicazkar": ([5, 12, 5, 9, 5, 12, 5], "G", "D", "Zirgüleli Hicaz on Rast"),
    "kurdilihicazkar": ([4, 9, 9, 9, 4, 9, 9], "G", "C", "Kürdî scale on Rast"),
    "nikriz": ([9, 4, 13, 5, 9, 8, 5], "G", "D", "Nikriz pentachord + Rast tetrachord"),
    "acemasiran": ([9, 9, 4, 9, 9, 9, 4], "F", "C", "Çargâh scale on Acemaşiran"),
    "sultaniyegah": ([9, 4, 9, 9, 5, 12, 5], "D", "A", "Bûselik pentachord + Hicaz "
                     "tetrachord on Yegâh"),
}
_ALIASES = {"beyati": "bayati", "huseyni_asiran": "huseyni", "zirgule": "zirguleli_hicaz",
            "kurdili_hicazkar": "kurdilihicazkar", "nihavent": "nihavend",
            "acem_asiran": "acemasiran", "sultani_yegah": "sultaniyegah", "segah_": "segah"}

# name: (meter numerator, denominator, strokes as (stroke, units), what it is)
USULS = {
    "nim_sofyan": (2, 4, [("düm", 1), ("tek", 1)], "Düm Tek"),
    "semai": (3, 4, [("düm", 1), ("tek", 1), ("tek", 1)], "Düm Tek Tek"),
    "sofyan": (4, 4, [("düm", 2), ("te", 1), ("ke", 1)], "Düüm Te Ke"),
    "turk_aksagi": (5, 8, [("düm", 2), ("tek", 2), ("tek", 1)], "Düüm Teek Tek (2+3)"),
    "yuruk_semai": (6, 8, [("düm", 1), ("tek", 1), ("tek", 1), ("düm", 1), ("tek", 2)],
                    "Düm Tek Tek Düm Teek"),
    "devr_i_hindi": (7, 8, [("düm", 1), ("tek", 1), ("tek", 1), ("düm", 2), ("tek", 2)],
                     "Düm Tek Tek Düüm Teek (semai + sofyan)"),
    "devr_i_turan": (7, 8, [("düm", 2), ("tek", 2), ("tek", 3)], "Düüm Teek Teeek (2+2+3)"),
    "duyek": (8, 8, [("düm", 1), ("tek", 2), ("tek", 1), ("düm", 2), ("tek", 2)],
              "Düm Teek Tek Düüm Teek"),
    "musemmen": (8, 8, [("düm", 3), ("tek", 2), ("tek", 3)], "Düüüm Teek Teeek (3+2+3)"),
    "aksak": (9, 8, [("düm", 2), ("te", 1), ("ke", 1), ("düm", 2), ("tek", 2), ("tek", 1)],
              "Düüm Te Ke Düüm Teek Tek (2+2+2+3)"),
    "aksak_semai": (10, 8, [("düm", 3), ("te", 1), ("ka", 1), ("düm", 2), ("tek", 2),
                            ("tek", 1)], "Düüüm Te Ka Düüm Teek Tek (3+2+2+3)"),
    "curcuna": (10, 16, [("düm", 3), ("te", 1), ("ka", 1), ("düm", 2), ("tek", 2),
                         ("tek", 1)], "aksak semai's strokes at 10/16 (3+2+2+3)"),
}
_USUL_ALIASES = {"nimsofyan": "nim_sofyan", "turkaksagi": "turk_aksagi",
                 "yuruksemai": "yuruk_semai", "devrihindi": "devr_i_hindi",
                 "devrituran": "devr_i_turan", "aksaksemai": "aksak_semai",
                 "katakofti": "musemmen"}
# Strokes → drum role and how hard they are hit (velocity factor)
STROKES = {"düm": ("kick", 1.0), "tek": ("snare", 0.85), "te": ("rim", 0.7),
           "ke": ("rim", 0.6), "ka": ("rim", 0.6)}


def normalize(name: str) -> str:
    """"Kürdîlihicazkâr" → "kurdilihicazkar", "Devr-i Hindî" → "devr_i_hindi"."""
    folded = name.strip().translate(_FOLD).lower()
    return folded.replace("-", "_").replace(" ", "_").replace("'", "")


def find_makam(name: str) -> str | None:
    key = normalize(name)
    key = _ALIASES.get(key, key)
    if key in MAKAMS:
        return key
    if key.endswith("_makami"):
        return find_makam(key[: -len("_makami")])
    return None


def makam(name: str) -> dict:
    key = find_makam(name)
    if key is None:
        raise ValueError(f"Unknown makam {name!r}. Known: {', '.join(sorted(MAKAMS))}")
    steps, karar, guclu, built = MAKAMS[key]
    commas = [sum(steps[:i]) for i in range(len(steps))]
    cents = [round(c * COMMA_CENTS, 1) for c in commas]
    return {"makam": key, "karar": karar, "guclu": guclu, "built_from": built,
            "steps_commas": steps, "degrees_commas": commas, "degrees_cents": cents,
            "intervals_12tet": [int(round(c / 100.0)) % 12 for c in cents]}


def intervals_12tet(name: str) -> list[int]:
    """The makam's degrees rounded to semitones above the karar."""
    return makam(name)["intervals_12tet"]


def ascl(name: str, tonic_pc: int, note_names: list[str]) -> tuple[str, str]:
    """(file name, .ascl text): a 12-note tuning for Live 12 that keeps the
    karar on its usual pitch and moves each degree of the makam to its comma
    pitch; the other five keys stay equal-tempered.

    It is written like Ableton's own example (C as the implied first note,
    REFERENCE_PITCH octave/index/Hz with C3 = MIDI 60), anchored on the karar
    so it stays in tune with the rest of the set.
    """
    info = makam(name)
    absolute = [pc * 100.0 for pc in range(12)]
    for cents, rounded in zip(info["degrees_cents"], info["intervals_12tet"]):
        pc = (tonic_pc + rounded) % 12
        absolute[pc] = pc * 100.0 + (cents - round(cents / 100.0) * 100.0)
    relative = [absolute[pc] - absolute[0] for pc in range(12)]
    tonic = note_names[tonic_pc]
    title = f"Makam {info['makam'].replace('_', ' ').title()} ({tonic})"
    lines = [f"! {title}.ascl", "!",
             f"{title}: AEU 53-comma degrees on {tonic}, other keys equal-tempered", "12", "!"]
    for pc in range(1, 12):
        lines.append(f"{relative[pc]:.3f} ! {note_names[pc]}")
    lines.append(f"2/1 ! {note_names[0]}")
    reference = 440.0 * 2 ** ((60 + tonic_pc - 69) / 12.0)
    lines += ["!", "! @ABL NOTE_NAMES " + " ".join(f'"{n}"' for n in note_names),
              f"! @ABL REFERENCE_PITCH 3 {tonic_pc} {reference:.3f}", ""]
    return f"{title}.ascl", "\n".join(lines)


def tuning_folder() -> Path:
    """Live's User Library Tunings folder (macOS and Windows default places)."""
    for base in (Path.home() / "Music" / "Ableton" / "User Library",
                 Path.home() / "Documents" / "Ableton" / "User Library"):
        if base.is_dir():
            return base / "Tunings"
    return Path.home() / "Music" / "Ableton MCP" / "Tunings"


def find_usul(name: str) -> str | None:
    key = normalize(name)
    key = _USUL_ALIASES.get(key.replace("_", ""), key)
    return key if key in USULS else None


def usul(name: str) -> dict:
    key = find_usul(name)
    if key is None:
        raise ValueError(f"Unknown usul {name!r}. Known: {', '.join(USULS)}")
    numerator, denominator, strokes, sound = USULS[key]
    return {"usul": key, "meter": f"{numerator}/{denominator}", "numerator": numerator,
            "denominator": denominator, "strokes": strokes, "vocal": sound,
            "units": sum(units for _stroke, units in strokes)}


def usul_notes(name: str, cycles: int, pads: dict[str, int], velocity: int = 110,
               ornament: float = 0.0, seed: int | None = None) -> list[dict]:
    """Notes for `cycles` cycles of an usul. pads: stroke → MIDI note.

    ornament (0..1) fills long strokes with light ke/te hits on the unit
    grid, the way players decorate the plain form ("Düüm" → "Düm Te Ke").
    """
    info = usul(name)
    unit = 4.0 / info["denominator"]
    rng = random.Random(seed)
    notes = []
    for cycle in range(max(1, int(cycles))):
        position = cycle * info["units"] * unit
        for stroke, units in info["strokes"]:
            _role, weight = STROKES[stroke]
            notes.append({"pitch": pads[stroke], "start_time": round(position, 6),
                          "duration": round(unit * 0.9, 6),
                          "velocity": int(max(1, min(127, round(velocity * weight))))})
            for extra in range(1, units):
                if ornament > 0 and rng.random() < ornament:
                    light = "ke" if extra % 2 else "te"
                    notes.append({"pitch": pads[light],
                                  "start_time": round(position + extra * unit, 6),
                                  "duration": round(unit * 0.9, 6),
                                  "velocity": int(max(1, round(velocity * 0.45)))})
            position += units * unit
    return notes
