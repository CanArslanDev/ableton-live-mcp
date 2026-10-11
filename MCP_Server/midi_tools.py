"""Max for Live MIDI Tools for Live 12's Generate and Transform tabs.

Three devices the server's generators have and Live's own tools do not:

- Usul (Generator): Turkish rhythmic cycles over the time selection
- Makam Melody (Generator): a phrase in a makam, arriving through its first
  tetrachord
- Makam Snap (Transformation): notes moved to the nearest makam pitch

Each is an .amxd of type "nagg" (generator) or "natt" (transformation), as
Live's templates in Misc/Max MIDI Tools: live.miditool.in sends the context
and the clip's notes to a v8.codebox, whose code (midi_tools/*.js, with the
usul and makam tables written from turkish.py and music.py) answers with
notes for live.miditool.out. The code is in the device, so it needs no other
file. Live lists them under the User Library's MIDI Tools folders.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import music, turkish
from .m4l_device import Patch, amxd

JS_DIR = Path(__file__).resolve().parent / "midi_tools"
GENERATOR = b"nagg"
TRANSFORMATION = b"natt"
FOLDERS = {GENERATOR: ("MIDI Tools", "Max Generators"),
           TRANSFORMATION: ("MIDI Tools", "Max Transformations")}

USUL_NAMES = list(turkish.USULS)
MAKAM_NAMES = sorted(turkish.MAKAMS)
KARAR_ITEMS = ["Usual"] + list(music.NOTE_NAMES)

# How the menus show them, written as in Turkish
USUL_LABELS = {
    "nim_sofyan": "Nim Sofyan", "semai": "Semâî", "sofyan": "Sofyan",
    "turk_aksagi": "Türk Aksağı", "yuruk_semai": "Yürük Semâî", "devr_i_hindi": "Devr-i Hindî",
    "devr_i_turan": "Devr-i Turan", "duyek": "Düyek", "musemmen": "Müsemmen",
    "aksak": "Aksak", "aksak_semai": "Aksak Semâî", "curcuna": "Curcuna",
}
MAKAM_LABELS = {
    "acemasiran": "Acemaşiran", "bayati": "Bayâtî", "buselik": "Bûselik", "hicaz": "Hicaz",
    "hicazkar": "Hicazkâr", "humayun": "Hümâyun", "huseyni": "Hüseynî", "huzzam": "Hüzzam",
    "karcigar": "Karcığar", "kurdi": "Kürdî", "kurdilihicazkar": "Kürdîlihicazkâr",
    "mahur": "Mâhur", "muhayyer": "Muhayyer", "neva": "Nevâ", "nihavend": "Nihâvend",
    "nikriz": "Nikrîz", "rast": "Rast", "saba": "Sabâ", "segah": "Segâh",
    "sultaniyegah": "Sultânîyegâh", "suzinak": "Sûzinak", "ussak": "Uşşak", "uzzal": "Uzzâl",
    "zirguleli_hicaz": "Zirgüleli Hicaz",
}


def data_prelude() -> str:
    """The tables the JS code reads, from the Python ones."""
    usuls = {name: {"numerator": n, "denominator": d, "units": sum(u for _s, u in strokes),
                    "strokes": [[s, u] for s, u in strokes], "vocal": vocal}
             for name, (n, d, strokes, vocal) in turkish.USULS.items()}
    makams = {}
    for name in MAKAM_NAMES:
        info = turkish.makam(name)
        makams[name] = {"intervals": info["intervals_12tet"],
                        "karar": music.parse_pitch_class(info["karar"]),
                        "guclu": music.parse_pitch_class(info["guclu"])}
    tables = {
        "USULS": usuls, "USUL_NAMES": USUL_NAMES,
        "STROKES": {stroke: weight for stroke, (_role, weight) in turkish.STROKES.items()},
        "MAKAMS": makams, "MAKAM_NAMES": MAKAM_NAMES, "NOTE_NAMES": list(music.NOTE_NAMES),
        "MELODY_RHYTHMS": music._MELODY_RHYTHMS,
    }
    return "".join(f"var {name} = {json.dumps(value, ensure_ascii=False)};\n"
                   for name, value in tables.items())


def code(tool: str) -> str:
    """The whole v8 program of one tool: tables, shared code, the tool."""
    return (data_prelude() + "\n" + (JS_DIR / "common.js").read_text(encoding="utf-8")
            + "\n" + (JS_DIR / f"{tool}.js").read_text(encoding="utf-8"))


# ── Controls ────────────────────────────────────────────────────────────────

def _parameter(longname: str, shortname: str, kind: int, initial: float,
               **valueof: Any) -> dict:
    return {"valueof": dict({"parameter_longname": longname, "parameter_shortname": shortname,
                             "parameter_type": kind, "parameter_initial": [initial],
                             "parameter_initial_enable": 1, "parameter_linknames": 1,
                             "parameter_modmode": 0}, **valueof)}


def menu(name: str, items: list[str], initial: int = 0) -> dict:
    return {"maxclass": "live.menu", "outlets": 3, "outlettype": ["", "", "float"],
            "size": [104.0, 15.0],
            "attributes": _parameter(name, name, 2, initial, parameter_enum=items,
                                     parameter_mmax=len(items) - 1)}


def note_box(name: str, initial: int) -> dict:
    return {"maxclass": "live.numbox", "outlets": 2, "outlettype": ["", "float"],
            "size": [40.0, 15.0],
            "attributes": _parameter(name, name, 1, initial, parameter_mmin=0,
                                     parameter_mmax=127, parameter_unitstyle=8)}


def number_box(name: str, initial: int, low: int, high: int) -> dict:
    return {"maxclass": "live.numbox", "outlets": 2, "outlettype": ["", "float"],
            "size": [40.0, 15.0],
            "attributes": _parameter(name, name, 1, initial, parameter_mmin=low,
                                     parameter_mmax=high, parameter_unitstyle=0)}


def dial(name: str, initial: float) -> dict:
    return {"maxclass": "live.dial", "outlets": 2, "outlettype": ["", "float"],
            "size": [44.0, 48.0], "extra": {"showname": 0},  # the label above names it
            "attributes": _parameter(name, name, 0, initial, parameter_mmin=0.0,
                                     parameter_mmax=100.0, parameter_unitstyle=5)}


# Each tool: its JS file, type, and controls as (label, control, JS function)
TOOLS: dict[str, dict] = {
    "Usul": {"js": "usul", "type": GENERATOR, "controls": [
        ("Usul", menu("Usul", [USUL_LABELS[n] for n in USUL_NAMES], USUL_NAMES.index("aksak")),
         "usul"),
        ("Düm", note_box("Düm", 36), "dum"),
        ("Tek", note_box("Tek", 38), "tek"),
        ("Te/Ke", note_box("Te/Ke", 37), "light"),
        ("Ornament", dial("Ornament", 0.0), "ornament")]},
    "Makam Melody": {"js": "makam_melody", "type": GENERATOR, "controls": [
        ("Makam", menu("Makam", [MAKAM_LABELS[n] for n in MAKAM_NAMES], MAKAM_NAMES.index("hicaz")),
         "makam"),
        ("Karar", menu("Karar", KARAR_ITEMS, 0), "karar"),
        ("Octave", number_box("Octave", 3, 0, 6), "octave"),
        ("Density", dial("Density", 60.0), "density")]},
    "Makam Snap": {"js": "makam_snap", "type": TRANSFORMATION, "controls": [
        ("Makam", menu("Makam", [MAKAM_LABELS[n] for n in MAKAM_NAMES], MAKAM_NAMES.index("hicaz")),
         "makam"),
        ("Karar", menu("Karar", KARAR_ITEMS, 0), "karar")]},
}


def patch(name: str) -> dict:
    tool = TOOLS[name]
    p = Patch()
    miditool_in = p.add("live.miditool.in", 10, 10, inlets=1, outlets=2)
    program = p.add("", 10, 160, inlets=2, outlets=2, maxclass="v8.codebox",
                    code=code(tool["js"]), filename="none", fontname="Menlo", fontsize=10.0,
                    saved_object_attributes={"parameter_enable": 0})
    p.boxes[-1]["box"]["patching_rect"] = [10.0, 160.0, 520.0, 360.0]
    miditool_out = p.add("live.miditool.out", 10, 540, inlets=1, outlets=0)
    p.connect(miditool_in, 0, program, 0)  # the notes (after the context)
    p.connect(miditool_in, 1, program, 1)  # the context
    p.connect(program, 0, miditool_out, 0)
    p.connect(program, 1, miditool_in, 0)  # apply again when a control changes
    # In the device (presentation): a label and its control per row; the dial
    # in the right column, under the first row's menu
    y = 4.0
    for index, (label, control, function) in enumerate(tool["controls"]):
        width, height = control["size"]
        if control["maxclass"] == "live.dial":
            label_rect, control_rect = [100.0, 24.0, 52.0, 15.0], [104.0, 38.0, width, height]
        else:
            label_rect, control_rect = [4.0, y, 40.0, 15.0], [44.0, y, width, height]
            y += 20.0
        patch_y = 10.0 + 40.0 * index
        p.add(label, 560, patch_y, inlets=1, outlets=0, maxclass="live.comment",
              presentation=1, presentation_rect=label_rect, textjustification=0)
        box = p.add("", 660, patch_y, inlets=1, outlets=control["outlets"],
                    outlettype=control["outlettype"], maxclass=control["maxclass"],
                    parameter_enable=1, presentation=1, presentation_rect=control_rect,
                    saved_attribute_attributes=control["attributes"],
                    varname=control["attributes"]["valueof"]["parameter_longname"],
                    **control.get("extra", {}))
        p.boxes[-1]["box"]["patching_rect"] = [660.0, patch_y, width, height]
        forward = p.add(f"prepend {function}", 800, patch_y)
        p.connect(box, 0, forward, 0)
        p.connect(forward, 0, program, 0)
    amxd_type = int.from_bytes(tool["type"], "big")
    return {"patcher": {
        "fileversion": 1,
        "appversion": {"major": 9, "minor": 0, "revision": 0, "architecture": "x64",
                       "modernui": 1},
        "classnamespace": "box",
        "rect": [100.0, 100.0, 1000.0, 620.0],
        "openrect": [0.0, 0.0, 152.0, 147.0],
        "openinpresentation": 1,
        "default_fontsize": 10.0, "default_fontface": 0, "default_fontname": "Arial Bold",
        "gridsize": [8.0, 8.0], "devicewidth": 152.0,
        "description": f"AbletonMCP {name}", "digest": "", "tags": "", "style": "",
        "subpatcher_template": "",
        "boxes": p.boxes, "lines": p.lines, "dependency_cache": [], "latency": 0,
        "is_mpe": 0, "minimum_live_version": "12.0", "minimum_max_version": "9.0.0",
        "platform_compatibility": 0,
        "project": {"version": 1, "creationdate": 3590052493, "modificationdate": 3590052493,
                    "viewrect": [25.0, 124.0, 300.0, 500.0], "autoorganize": 1,
                    "hideprojectwindow": 1, "showdependencies": 1, "autolocalize": 0,
                    "contents": {"patchers": {}}, "layout": {}, "searchpath": {},
                    "detailsvisible": 0, "amxdtype": amxd_type, "readonly": 0,
                    "devpathtype": 0, "devpath": ".", "sortmode": 0, "viewmode": 0},
        "autosave": 0,
    }}


def device(name: str) -> bytes:
    return amxd(patch(name), TOOLS[name]["type"])


def install(libraries: list[Path]) -> list[Path]:
    """Write every tool into the User Libraries' MIDI Tools folders (only
    when it changed); the paths written or already current."""
    written = []
    for library in libraries:
        for name, tool in TOOLS.items():
            target = library.joinpath(*FOLDERS[tool["type"]]) / f"{name}.amxd"
            data = device(name)
            if not target.is_file() or target.read_bytes() != data:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            written.append(target)
    return written
