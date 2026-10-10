"""Extended Ableton tools built on the Remote Script's generic LOM bridge.

Everything here is expressed as lom_get / lom_set / lom_call / batch commands
(plus a few dedicated note/envelope/parameter commands), so new Live features
rarely need Remote Script changes: the bridge already reaches the whole Live
Object Model.

Addressing conventions shared by the tools:
- track: index (0-based), exact track name, "master", or "return A" / "return 0"
- device: index in the track's chain or device name
- clips: clip_index = Session slot; arrangement_clip_index = Arrangement clip
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import time
from pathlib import Path
from typing import Any, Callable

from mcp.server.mcpserver import Context

from . import music
from .connection import AbletonCommandError
from .ui import panels as ui
from .telemetry_decorator import telemetry_tool
from .tooling import register_tool

logger = logging.getLogger("AbletonMCPServer")

_get_connection: Callable[[], Any] | None = None

# ── Enum vocabularies ────────────────────────────────────────────────────────

WARP_MODES = {"beats": 0, "tones": 1, "texture": 2, "re_pitch": 3, "repitch": 3,
              "complex": 4, "rex": 5, "complex_pro": 6}
LAUNCH_MODES = {"trigger": 0, "gate": 1, "toggle": 2, "repeat": 3}
_QUANT_ORDER = ["none", "8 bars", "4 bars", "2 bars", "1 bar", "1/2", "1/2T", "1/4",
                "1/4T", "1/8", "1/8T", "1/16", "1/16T", "1/32"]
SONG_QUANTIZATION = {name: i for i, name in enumerate(_QUANT_ORDER)}
CLIP_LAUNCH_QUANTIZATION = dict({"global": 0}, **{n: i + 1 for i, n in enumerate(_QUANT_ORDER)})
NOTE_QUANTIZE_GRID = {"1/4": 1, "1/8": 2, "1/8T": 3, "1/8+T": 4, "1/16": 5, "1/16T": 6,
                      "1/16+T": 7, "1/32": 8}
MONITORING = {"in": 0, "auto": 1, "off": 2}
VIEWS = {"session": "Session", "arrangement": "Arranger", "arranger": "Arranger",
         "clip": "Detail/Clip", "device": "Detail/DeviceChain", "browser": "Browser",
         "detail": "Detail"}

# Browser categories → the roots Live's browser shows in its sidebar. "all"
# leaves out user_library and packs: their items also appear under the others.
BROWSER_CATEGORIES = {
    "all": ["sounds", "drums", "instruments", "audio_effects", "midi_effects", "max_for_live",
            "plugins", "clips", "samples"],
    "sounds": ["sounds"], "drums": ["drums"], "instruments": ["instruments"],
    "audio_effects": ["audio_effects"], "effects": ["audio_effects"],
    "midi_effects": ["midi_effects"], "max_for_live": ["max_for_live"],
    "plugins": ["plugins"], "clips": ["clips"], "samples": ["samples"],
    "user_library": ["user_library"], "packs": ["packs"], "current_project": ["current_project"],
}

# Drum kit roles → (folder in Drums › Drum Hits, words that pick the right
# kind inside it). The pad is the role's General MIDI note (music.GM_DRUMS).
DRUM_KIT_ROLES = {
    "kick": ("Kick", []), "snare": ("Snare", []), "snare2": ("Snare", []),
    "clap": ("Clap", []), "rim": ("Rim", []),
    "closed_hat": ("Hihat", ["closed", "chh", "closed hat"]),
    "open_hat": ("Hihat", ["open", "ohh"]), "pedal_hat": ("Hihat", ["pedal", "foot"]),
    "low_tom": ("Tom", ["low", "floor"]), "mid_tom": ("Tom", ["mid"]),
    "high_tom": ("Tom", ["high", "hi"]), "crash": ("Cymbal", ["crash"]),
    "ride": ("Ride", []), "shaker": ("Shaker", []), "cowbell": ("Bell", ["cowbell", "cow"]),
    "perc": ("Misc Percussion", []),
}
DEFAULT_KIT = ["kick", "snare", "clap", "closed_hat", "open_hat", "rim", "perc", "shaker",
               "crash", "low_tom", "high_tom"]

_NEW_COMMANDS = {"lom_get", "lom_set", "lom_call", "batch", "observe", "unobserve",
                 "list_observers", "poll_events", "clip_get_notes", "clip_write_notes",
                 "clip_write_envelope", "param_set_display", "get_session_overview",
                 "browser_search"}


# ── Plumbing ─────────────────────────────────────────────────────────────────

def send(command: str, params: dict | None = None, timeout: float | None = None) -> Any:
    if _get_connection is None:
        raise RuntimeError("extended tools not registered")
    try:
        if timeout is None:
            return _get_connection().send_command(command, params or {})
        return _get_connection().send_command(command, params or {}, timeout=timeout)
    except Exception as e:
        if command in _NEW_COMMANDS and "Unknown command" in str(e):
            raise RuntimeError(
                "The AbletonMCP Remote Script loaded in Live is older than this server "
                "(missing '%s'). Run `ableton-mcp-install-script`, then restart Live." % command
            ) from e
        raise


async def asend(command: str, params: dict | None = None,
                timeout: float | None = None) -> Any:
    return await asyncio.to_thread(send, command, params, timeout)


async def report_progress(ctx: Context | None, done: float, total: float | None,
                          message: str) -> None:
    """Progress for the client, when it asked for it (a no-op otherwise)."""
    if ctx is None:
        return
    try:
        await ctx.report_progress(done, total, message)
    except Exception as e:  # called outside a request (tests, direct calls)
        logger.debug("No progress channel: %s", e)


def lom_get(path: str, properties: list[str] | None = None) -> Any:
    params: dict[str, Any] = {"path": path}
    if properties:
        params["properties"] = properties
    return send("lom_get", params)


def batch(commands: list[dict], stop_on_error: bool = True) -> dict:
    return send("batch", {"commands": commands, "stop_on_error": stop_on_error})


def op_set(path: str, prop: str, value: Any) -> dict:
    return {"type": "lom_set", "params": {"path": path, "property": prop, "value": value}}


def op_call(path: str, method: str, *args: Any) -> dict:
    return {"type": "lom_call", "params": {"path": path, "method": method, "args": list(args)}}


def op_display(path: str, value: Any) -> dict:
    return {"type": "param_set_display", "params": {"path": path, "value": value}}


def ref(path: str) -> dict:
    return {"$ref": path}


def to_json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


def selector(value: int | str) -> str:
    if isinstance(value, int) or re.fullmatch(r"-?\d+", str(value).strip()):
        return str(int(value))
    return shlex.quote(str(value))


def track_path(track: int | str) -> str:
    if isinstance(track, int):
        return f"live_set tracks {track}"
    text = str(track).strip()
    low = text.lower()
    if low in ("master", "main", "master_track"):
        return "live_set master_track"
    m = re.fullmatch(r"(?:return|ret)\s*[:#]?\s*([a-z]|\d+)", low)
    if m:
        token = m.group(1)
        index = int(token) if token.isdigit() else ord(token) - ord("a")
        return f"live_set return_tracks {index}"
    if low.startswith("return:"):
        return f"live_set return_tracks {selector(text[7:].strip())}"
    return f"live_set tracks {selector(text)}"


def device_path(track: int | str, device: int | str) -> str:
    return f"{track_path(track)} devices {selector(device)}"


def clip_path(track: int | str, clip_index: int | None = None,
              arrangement_clip_index: int | None = None) -> str:
    if arrangement_clip_index is not None:
        return f"{track_path(track)} arrangement_clips {int(arrangement_clip_index)}"
    if clip_index is None:
        raise ValueError("Give clip_index (Session slot) or arrangement_clip_index")
    return f"{track_path(track)} clip_slots {int(clip_index)} clip"


def parameter_path(track: int | str, device: int | str, parameter: int | str) -> str:
    if str(device).lower() in ("mixer", "mixer_device"):
        p = str(parameter).strip().lower()
        if p in ("volume", "vol"):
            return f"{track_path(track)} mixer_device volume"
        if p in ("pan", "panning"):
            return f"{track_path(track)} mixer_device panning"
        m = re.fullmatch(r"send\s*([a-z]|\d+)", p)
        if m:
            token = m.group(1)
            index = int(token) if token.isdigit() else ord(token) - ord("a")
            return f"{track_path(track)} mixer_device sends {index}"
        raise ValueError("Mixer parameter must be volume, pan or 'send A'/'send 0'")
    return f"{device_path(track, device)} parameters {selector(parameter)}"


def index_in(collection_path: str, item: int | str) -> int:
    """Resolve a name (or negative index) to a non-negative index."""
    listing = lom_get(collection_path, ["name"])
    items = listing.get("items", [])
    if isinstance(item, int) or re.fullmatch(r"-?\d+", str(item).strip()):
        index = int(item)
        if index < 0:
            index += len(items)
        if not 0 <= index < len(items):
            raise ValueError(f"Index {item} out of range ({len(items)} items)")
        return index
    names = [i.get("name") for i in items]
    for exact in (True, False):
        for index, name in enumerate(names):
            if name == item if exact else (name or "").lower() == str(item).lower():
                return index
    raise ValueError(f"No item named {item!r}. Available: {', '.join(map(str, names))}")


def track_collection(track: int | str) -> tuple[str, int]:
    """(collection path, index) for delete/duplicate style song methods."""
    path = track_path(track)
    if path.startswith("live_set return_tracks"):
        return "live_set return_tracks", index_in("live_set return_tracks",
                                                  shlex.split(path)[-1])
    if path == "live_set master_track":
        raise ValueError("The master track cannot be deleted or duplicated")
    item: int | str = track if isinstance(track, int) else str(track).strip()
    return "live_set tracks", index_in("live_set tracks", item)


def parse_color(color: str | int) -> int:
    if isinstance(color, int):
        return color
    text = str(color).strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", text):
        raise ValueError("Color must be a hex RGB string like '#FF8800'")
    return int(text, 16)


def batch_summary(result: dict) -> dict:
    """Compact batch result: drop bulky successes, keep every error."""
    steps = []
    for step in result.get("results", []):
        if step.get("status") == "error":
            steps.append({"step": step.get("index"), "type": step.get("type"),
                          "error": step.get("message")})
        else:
            inner = step.get("result")
            if isinstance(inner, dict) and "property" in inner:
                steps.append({"set": inner["property"], "value": inner.get("value")})
            elif isinstance(inner, dict) and "new" in inner:
                steps.append({"set": inner.get("name"), "value": inner.get("new")})
            elif isinstance(inner, dict) and "method" in inner:
                steps.append({"called": inner["method"], "result": inner.get("result")})
            else:
                steps.append(inner)
    return {"ok": result.get("failed", 0) == 0, "completed": result.get("completed"),
            "total": result.get("total"),
            "steps": steps}


# ── Shared note helpers ──────────────────────────────────────────────────────

def read_notes(path: str, **window: Any) -> dict:
    return send("clip_get_notes", dict({"clip_path": path}, **window))


def write_notes(path: str, notes: list[dict], mode: str = "replace", **extra: Any) -> dict:
    return send("clip_write_notes", dict({"clip_path": path, "notes": notes, "mode": mode},
                                         **extra))


def apply_note_edit(path: str, notes: list[dict], chosen: list[dict],
                    changed: list[dict]) -> dict:
    """Write `changed` in place of `chosen` as one batch (one undo step).

    Notes that kept a note_id are modified in place (Live 11+: ids and the
    per-note expression the API cannot read survive), chosen notes whose id
    is gone are removed, notes without an id are added. Without note ids
    (Live 10) the clip is rewritten.
    """
    if notes and all("note_id" in n for n in notes):
        kept = {n["note_id"] for n in changed if "note_id" in n}
        doomed = [n["note_id"] for n in chosen if n["note_id"] not in kept]
        modified = [n for n in changed if "note_id" in n]
        added = [n for n in changed if "note_id" not in n]
        steps = []
        if doomed:
            steps.append({"type": "clip_write_notes", "params": {
                "clip_path": path, "notes": [], "mode": "remove_ids", "note_ids": doomed}})
        for mode, group in (("modify", modified), ("add", added)):
            if group:
                steps.append({"type": "clip_write_notes", "params": {
                    "clip_path": path, "notes": group, "mode": mode}})
        if steps:
            result = batch(steps)
            if result.get("failed"):
                raise RuntimeError("Live refused the note edit: "
                                   + to_json(batch_summary(result)))
        return {"notes_modified": len(modified), "notes_added": len(added),
                "notes_removed": len(doomed),
                "notes_in_clip": len(notes) - len(doomed) + len(added)}
    chosen_ids = {id(n) for n in chosen}
    final = [n for n in notes if id(n) not in chosen_ids] + changed
    for n in final:
        n.pop("note_id", None)
    return {"notes_in_clip": write_notes(path, final, "replace").get("written")}


def prepare_clip(track: int | str, clip_index: int | None, length: float,
                 arrangement_time: float | None = None, name: str | None = None) -> str:
    """Return a path to a MIDI clip at least `length` beats long, creating it if needed."""
    if arrangement_time is not None:
        created = send("lom_call", {"path": track_path(track), "method": "create_midi_clip",
                                    "args": [float(arrangement_time), float(length)]})
        clip_id = created["result"]["$id"]
        path = f"id {clip_id}"
    else:
        if clip_index is None:
            raise ValueError("Give clip_index (Session slot) or arrangement_time")
        slot = f"{track_path(track)} clip_slots {int(clip_index)}"
        if not lom_get(slot, ["has_clip"])["has_clip"]:
            send("lom_call", {"path": slot, "method": "create_clip", "args": [float(length)]})
        path = f"{slot} clip"
        info = lom_get(path, ["is_midi_clip", "length"])
        if not info["is_midi_clip"]:
            raise ValueError("That slot holds an audio clip")
        if info["length"] < length:
            batch([op_set(path, "loop_end", float(length)),
                   op_set(path, "end_marker", float(length))], stop_on_error=False)
    if name:
        send("lom_set", {"path": path, "property": "name", "value": name})
    return path


def generated_result(path: str, notes: list[dict], written: dict, extra: dict | None = None) -> str:
    pitches = sorted({n["pitch"] for n in notes})
    payload = {"clip": path, "notes_written": written.get("written", len(notes)),
               "pitch_range": [music.note_name(pitches[0]), music.note_name(pitches[-1])]
               if pitches else None}
    if extra:
        payload.update(extra)
    return to_json(payload)


def song_context(optional: bool = False) -> dict:
    """The set's bar length and Key & Scale, which generators default to.

    Live 11 has no Key & Scale; then "key" is None. With optional=True a
    missing connection gives 4/4 and no key instead of an error.
    """
    try:
        info = lom_get("live_set", ["signature_numerator", "signature_denominator",
                                    "root_note", "scale_name", "scale_intervals"])
    except Exception:
        if not optional:
            raise
        info = {}
    num, den = info.get("signature_numerator"), info.get("signature_denominator")
    meter_known = isinstance(num, int) and isinstance(den, int) and num > 0 and den > 0
    root, intervals = info.get("root_note"), info.get("scale_intervals")
    has_key = isinstance(root, int) and isinstance(intervals, list) and bool(intervals)
    return {
        "beats_per_bar": music.beats_per_bar(num, den) if meter_known else 4.0,
        "time_signature": f"{num}/{den}" if meter_known else "4/4",
        "key": music.NOTE_NAMES[root % 12] if has_key else None,
        "scale": [int(i) for i in intervals] if has_key else None,
        "scale_name": info.get("scale_name") if has_key else None,
    }


def resolve_key(key: str | None, scale: Any, context: dict) -> tuple[str, Any]:
    """The caller's key/scale, else the set's (Live 12), else C major."""
    if key is not None:
        return key, scale
    if context.get("key") is not None:
        return context["key"], scale if scale is not None else context["scale"]
    return "C", scale


def musical_context(key: str, scale: Any, context: dict) -> dict:
    """What a generator used, for its result: key, scale, time signature."""
    if isinstance(scale, list) and scale == context.get("scale"):
        scale = context.get("scale_name") or scale
    return {"key": key, "scale": scale, "time_signature": context["time_signature"]}


# Parameters Live 12.4 renamed: (Live 12.3 name, Live 12.4 name)
PARAMETER_RENAMES = [
    ("Output Gain", "Output"), ("Makeup", "Output"), ("Gain", "Output"),
    ("Master Output", "Output"),
    ("In LowCut On", "In Lo Cut On"), ("In HighCut On", "In Hi Cut On"),
    ("In Filter Freq", "Input Freq"), ("In Filter Width", "Input Width"),
    ("HiFilter On", "Diff. Hi On"), ("HiFilter Type", "Diff. Hi Type"),
    ("HiFilter Freq", "Diff. Hi Freq"), ("LowShelf On", "Diff. Lo On"),
    ("LowShelf Freq", "Diff. Lo Freq"),
    ("Mod Freq", "LFO Freq"), ("Dly < Mod", "LFO > Delay"), ("Filter < Mod", "LFO > Filter"),
    ("Morph", "Filter Morph"), ("Type", "Filter Type"), ("Slope", "Filter Slope"),
    ("Feedback Inv", "FB Invert"), ("FB Inv", "FB Invert"), ("Fb Inv On", "FB Invert"),
    ("HPF Enabled", "HP On"), ("HPF Freq", "HP Freq"), ("Fb Amt", "Feedback"),
    ("Fb Time Mode", "FB Time Mode"), ("Fb Time", "FB Time"), ("Fb Synced", "FB Synced"),
    ("Fb Note", "FB Note"), ("Fb Freq", "FB Freq"), ("Fb Width", "FB Width"),
]


def parameter_aliases(name: str) -> list[str]:
    """Other names the same parameter has in another Live version."""
    found = [new for old, new in PARAMETER_RENAMES if old == name]
    found += [old for old, new in PARAMETER_RENAMES if new == name]
    eq_band = re.fullmatch(r"(\d) (Resonance|Q) ([AB])", name)
    if eq_band:
        number, kind, side = eq_band.groups()
        found.append(f"{number} {'Q' if kind == 'Resonance' else 'Resonance'} {side}")
    return found


def resolve_parameter_names(device: str, names: list[str]) -> dict[str, str]:
    """Map setting names onto the device's actual parameter names, so a name
    from Live 12.3 ("Output Gain") also works on 12.4 ("Output") and back."""
    present = {p.get("name") for p in send("lom_get", {
        "path": f"{device} parameters", "properties": ["name"], "ids": False})["items"]}
    mapping = {}
    for name in names:
        if name in present or isinstance(name, int) or str(name).isdigit():
            mapping[name] = name
            continue
        mapping[name] = next((alias for alias in parameter_aliases(name)
                              if alias in present), name)
    return mapping


def setting_ops(device: str, settings: dict[str, Any],
                names: dict[str, str] | None = None) -> list[dict]:
    """Batch steps setting device parameters: text by display, numbers raw.
    names: setting name → the device's parameter name (resolve_parameter_names)."""
    ops = []
    for name, value in settings.items():
        parameter = f"{device} parameters {selector((names or {}).get(name, name))}"
        if isinstance(value, str):
            ops.append(op_display(parameter, value))
        else:
            ops.append(op_set(parameter, "value", float(value)))
    return ops


def inserted_device_ids(track: str, inserts: list[list[Any]]) -> list[int]:
    """Insert devices with Track.insert_device (Live 12.3+); their ids, in order."""
    result = batch([op_call(track, "insert_device", *args) for args in inserts])
    ids = []
    for args, step in zip(inserts, result["results"]):
        if step["status"] == "error":
            message = step["message"]
            if "has no method 'insert_device'" in message:
                message = ("inserting devices by name needs Live 12.3 or later; use "
                           "load_instrument_or_effect with a browser URI")
            raise RuntimeError(f"Could not insert {args[0]!r}: {message}")
        ids.append(step["result"]["result"]["$id"])
    return ids


DRUM_KEYWORDS = [
    ("open_hat", ("open", "ohh", "oh ")),
    ("closed_hat", ("closed", "chh", "hihat", "hi-hat", "hi hat", "hat", "hh")),
    ("kick", ("kick", "bd", "bass drum", "808")),
    ("clap", ("clap",)),
    ("snare", ("snare", "sd", "snr")),
    ("rim", ("rim", "stick")),
    ("crash", ("crash", "cymbal")),
    ("ride", ("ride",)),
    ("shaker", ("shaker", "shk", "tamb")),
    ("cowbell", ("cowbell", "bell")),
    ("low_tom", ("low tom", "tom lo", "floor")),
    ("high_tom", ("high tom", "hi tom", "tom hi")),
    ("mid_tom", ("tom",)),
    ("perc", ("perc", "conga", "bongo", "fx", "click")),
]


def place_locators(cues: list[tuple[str, float]]) -> list[dict]:
    """Create or rename named locators at beat positions.

    Song.set_or_delete_cue() toggles a cue at the playhead, and a playhead
    moved in the same call is not in effect yet (Live 12.4), so each cue
    takes two calls: move, then toggle. Existing cues are only renamed, and
    the playhead goes back where it was.
    """
    def current() -> list[dict]:
        return lom_get("live_set cue_points", ["name", "time"])["items"]

    original = lom_get("live_set", ["current_song_time"])["current_song_time"]
    have = [c["time"] for c in current()]
    for _name, at in cues:
        if any(abs(t - at) < 1e-3 for t in have):
            continue
        send("lom_set", {"path": "live_set", "property": "current_song_time", "value": at})
        time.sleep(0.05)
        send("lom_call", {"path": "live_set", "method": "set_or_delete_cue"})
        have.append(at)
    placed = current()
    renames, result = [], []
    for name, at in cues:
        cue = next((c for c in placed if abs(c["time"] - at) < 1e-3), None)
        if cue is None:
            result.append({"name": name, "time": at, "error": "Live did not create it"})
            continue
        renames.append(op_set(f"id {cue['$id']}", "name", name))
        result.append({"name": name, "time": at})
    batch(renames + [op_set("live_set", "current_song_time", original)], stop_on_error=False)
    return result


def drum_mapping_from_rack(track: int | str) -> dict[str, int]:
    devices = lom_get(f"{track_path(track)} devices", ["class_name"])["items"]
    rack_index = next((i for i, d in enumerate(devices)
                       if d.get("class_name") == "DrumGroupDevice"), None)
    if rack_index is None:
        return {}
    pads = lom_get(f"{track_path(track)} devices {rack_index} drum_pads",
                   ["note", "name", "chains"])["items"]
    mapping: dict[str, int] = {}
    for pad in pads:
        if not pad.get("chains"):
            continue
        name = f" {str(pad.get('name', '')).lower()} "
        for voice, words in DRUM_KEYWORDS:
            if voice not in mapping and any(w in name for w in words):
                mapping[voice] = int(pad["note"])
                break
    return mapping


DARBUKA_WORDS = {"düm": ("doum", "dum", "düm", "doom"), "tek": ("tek", "tak", "tik"),
                 "ka": ("ka", "slap", "pop"), "ke": ("ke",), "te": ("te",)}


def darbuka_pads(track: int | str) -> dict[str, int]:
    """Stroke → pad for a darbuka/doumbek kit whose pads are named after the
    strokes (doum, tek, ka). Empty for an ordinary drum kit."""
    devices = lom_get(f"{track_path(track)} devices", ["class_name"])["items"]
    rack = next((i for i, d in enumerate(devices) if d.get("class_name") == "DrumGroupDevice"),
                None)
    if rack is None:
        return {}
    pads = lom_get(f"{track_path(track)} devices {rack} drum_pads",
                   ["note", "name", "chains"])["items"]
    found: dict[str, int] = {}
    for pad in pads:
        if not pad.get("chains"):
            continue
        words = re.findall(r"[a-zçğıöşü]+", str(pad.get("name", "")).lower())
        for stroke, names in DARBUKA_WORDS.items():
            if stroke not in found and any(w in names for w in words):
                found[stroke] = int(pad["note"])
    if "ka" in found:
        found.setdefault("ke", found["ka"])
        found.setdefault("te", found["ka"])
    return found if "düm" in found and "tek" in found else {}


# ── Envelope shapes ──────────────────────────────────────────────────────────

def envelope_points(shape: str, start: float, end: float, low: float, high: float,
                    cycles: float, resolution: float) -> list[tuple[float, float]]:
    import math
    points = []
    span = max(end - start, resolution)
    steps = max(1, int(round(span / resolution)))
    for k in range(steps):
        t = start + k * resolution
        phase = ((t - start) / span * cycles) % 1.0
        frac = (t - start) / span
        if shape == "ramp_up":
            v = frac
        elif shape == "ramp_down":
            v = 1 - frac
        elif shape == "sine":
            v = 0.5 - 0.5 * math.cos(2 * math.pi * phase)
        elif shape == "triangle":
            v = 1 - abs(2 * phase - 1)
        elif shape == "square":
            v = 1.0 if phase < 0.5 else 0.0
        elif shape == "saw":
            v = phase
        elif shape == "exp_up":
            v = frac ** 3
        elif shape == "exp_down":
            v = (1 - frac) ** 3
        else:
            raise ValueError("shape must be ramp_up, ramp_down, sine, triangle, square, "
                             "saw, exp_up or exp_down")
        points.append((t, low + (high - low) * v))
    return points


def interpolate_points(points: list[dict], resolution: float) -> list[tuple[float, float]]:
    pts = sorted((float(p["time"]), float(p["value"])) for p in points)
    if len(pts) == 1:
        return pts
    out = []
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        steps = max(1, int(round((t1 - t0) / resolution)))
        for k in range(steps):
            out.append((t0 + (t1 - t0) * k / steps, v0 + (v1 - v0) * k / steps))
    out.append(pts[-1])
    return out


# ── Registration ─────────────────────────────────────────────────────────────

def register(mcp, get_connection: Callable[[], Any]) -> None:
    global _get_connection
    _get_connection = get_connection

    def tool(fn: Callable | None = None, **hints: Any) -> Callable:
        """@tool, or @tool(read_only=True | destructive=True | idempotent=True).

        Hints become MCP tool annotations; see tooling.register_tool.
        """
        def decorate(f: Callable) -> Callable:
            return register_tool(mcp, telemetry_tool(f.__name__)(f), **hints)
        return decorate(fn) if fn is not None else decorate

    # ── Generic LOM access ───────────────────────────────────────────────────

    @tool(read_only=True)
    def lom_get_object(path: str, properties: list[str] | None = None) -> str:
        """Read anything in the Live Object Model (LOM) by path.

        Without `properties` an object is described: its scalar properties,
        child objects/lists (with lengths) and callable methods. With
        `properties`, only those are read; on a list each item is read.

        Path syntax (space separated, like Max for Live):
        - "live_set" (the Song), "live_app", "live_set view", "live_app view"
        - "live_set tracks 0 devices 1 parameters 2"
        - select by name with quotes: 'live_set tracks "Bass" devices "Wavetable"'
        - negative indexes: "live_set tracks -1"
        - ids returned earlier: "id 12345"

        Use this to discover features that have no dedicated tool. Objects in
        results carry "$id"; pass "id <n>" as a path to reach them again.
        """
        return to_json(lom_get(path, properties))

    @tool(idempotent=True)
    def lom_set_property(path: str, property: str, value: Any) -> str:
        """Set any LOM property. `value` may be a number, bool, string, enum
        member name, or a Live object reference {"$ref": "<path>"} / {"$id": n}
        (e.g. a routing type: {"$ref": 'live_set tracks 2 available_input_routing_types "Resampling"'}).
        """
        return to_json(send("lom_set", {"path": path, "property": property, "value": value}))

    @tool(destructive=True)
    def lom_call_method(path: str, method: str, args: list[Any] | None = None,
                        kwargs: dict[str, Any] | None = None) -> str:
        """Call any LOM method, e.g. path="live_set", method="create_scene", args=[-1];
        path="live_set tracks 0 clip_slots 0 clip", method="duplicate_loop".
        Arguments that must be Live objects are written {"$ref": "<path>"}.
        """
        return to_json(send("lom_call", {"path": path, "method": method,
                                         "args": args or [], "kwargs": kwargs or {}}))

    @tool(destructive=True)
    def run_batch(commands: list[dict], stop_on_error: bool = True) -> str:
        """Run several Remote Script commands in one round-trip, on Live's main
        thread. Each command call is its own undo history entry; Live keeps
        parameter changes (volume, tempo, mute, device values) as a separate
        step from structural edits (names, colors, new tracks/clips/notes), so
        undoing a mixed batch can take two Cmd+Z.

        Each command is {"type": <command>, "params": {...}}. Useful types:
        lom_get, lom_set, lom_call, param_set_display, clip_write_notes,
        clip_write_envelope, create_midi_track, create_clip, add_notes_to_clip,
        set_device_parameter, load_browser_item, fire_clip, ...
        Example: [{"type": "lom_call", "params": {"path": "live_set",
        "method": "create_midi_track", "args": [-1]}}, {"type": "lom_set",
        "params": {"path": "live_set tracks -1", "property": "name", "value": "Pad"}}]
        """
        return to_json(batch(commands, stop_on_error))

    @tool(read_only=True, ui=ui.SESSION)
    def get_session_overview() -> str:
        """Compact map of the whole set: every track (type, group, mute/solo/arm,
        volume, pan, sends, devices, Session clips), returns, master, scenes,
        cue points, tempo, loop, key/scale. Cheaper than get_session_snapshot —
        call this first to orient yourself.
        """
        return to_json(send("get_session_overview"))

    # ── Tracks & mixer ───────────────────────────────────────────────────────

    @tool(idempotent=True)
    def set_track_mixer(track: int | str, volume_db: float | None = None,
                        pan: float | str | None = None, mute: bool | None = None,
                        solo: bool | None = None, arm: bool | None = None,
                        sends_db: dict[str, float] | None = None,
                        monitoring: str | None = None) -> str:
        """Set mixer state for a track, return track ("return A") or "master".

        - volume_db: fader in dB (0 = unity, +6 max; -70 or lower = -inf)
        - pan: -1.0 (left) .. 1.0 (right), or Live text like "25L", "C", "40R"
        - sends_db: {"A": -12, "B": -6} (letters or 0-based indexes)
        - monitoring: "in", "auto" or "off"
        Applied in one round-trip; undoing it takes one Cmd+Z (two if a name
        or color change is mixed in — Live files those separately).
        """
        tp = track_path(track)
        ops = []
        if volume_db is not None:
            # Live's fader drops to -inf below about -70 dB
            text = "-inf dB" if volume_db <= -70 else f"{volume_db} dB"
            ops.append(op_display(f"{tp} mixer_device volume", text))
        if pan is not None:
            if isinstance(pan, str):
                ops.append(op_display(f"{tp} mixer_device panning", pan))
            else:
                ops.append(op_set(f"{tp} mixer_device panning", "value",
                                  max(-1.0, min(1.0, float(pan)))))
        for prop, value in (("mute", mute), ("solo", solo), ("arm", arm)):
            if value is not None:
                ops.append(op_set(tp, prop, bool(value)))
        for send_key, db in (sends_db or {}).items():
            key = str(send_key).strip().lower()
            index = int(key) if key.isdigit() else ord(key) - ord("a")
            ops.append(op_display(f"{tp} mixer_device sends {index}", f"{db} dB"))
        if monitoring is not None:
            ops.append(op_set(tp, "current_monitoring_state", MONITORING[monitoring.lower()]))
        if not ops:
            return "Nothing to change"
        return to_json(batch_summary(batch(ops)))

    @tool(destructive=True)
    def manage_track(action: str, track: int | str | None = None, name: str | None = None,
                     color: str | int | None = None, index: int = -1) -> str:
        """Create, delete, duplicate, rename, recolor or fold tracks.

        action: create_midi | create_audio | create_return | delete | duplicate |
                rename | set_color | fold | unfold
        - index: insert position for create_* (-1 = end)
        - color: hex "#RRGGBB" or a Live color index (0-69)
        """
        action = action.lower()
        if action in ("create_midi", "create_audio", "create_return"):
            ops = []
            if action == "create_return":
                ops.append(op_call("live_set", "create_return_track"))
                new_path = "live_set return_tracks -1"
            else:
                ops.append(op_call("live_set", f"{action}_track", index))
                new_path = f"live_set tracks {index}"
            if name:
                ops.append(op_set(new_path, "name", name))
            if color is not None:
                ops.append(op_set(new_path, "color_index" if isinstance(color, int) else "color",
                                  parse_color(color)))
            ops.append({"type": "lom_get", "params": {"path": new_path, "properties": ["name"]}})
            return to_json(batch_summary(batch(ops)))
        if track is None:
            raise ValueError("track is required for this action")
        if action in ("delete", "duplicate"):
            collection, idx = track_collection(track)
            method = {"live_set tracks": f"{action}_track",
                      "live_set return_tracks": f"{action}_return_track"}[collection]
            if collection == "live_set return_tracks" and action == "duplicate":
                raise ValueError("Live cannot duplicate return tracks")
            return to_json(send("lom_call", {"path": "live_set", "method": method,
                                             "args": [idx]}))
        tp = track_path(track)
        if action == "rename":
            return to_json(send("lom_set", {"path": tp, "property": "name", "value": name}))
        if action == "set_color":
            if color is None:
                raise ValueError("color is required")
            prop = "color_index" if isinstance(color, int) else "color"
            return to_json(send("lom_set", {"path": tp, "property": prop,
                                            "value": parse_color(color)}))
        if action in ("fold", "unfold"):
            return to_json(send("lom_set", {"path": tp, "property": "fold_state",
                                            "value": action == "fold"}))
        raise ValueError(f"Unknown action {action!r}")

    @tool(idempotent=True)
    def set_track_routing(track: int | str, input_type: str | None = None,
                          input_channel: str | None = None, output_type: str | None = None,
                          output_channel: str | None = None) -> str:
        """Read or change a track's input/output routing.

        Call with only `track` to list the available routing options. Values are
        matched against Live's display names, e.g. input_type="Resampling",
        input_type="Ext. In", input_channel="1/2", output_type="Master",
        output_type="Sends Only", or another track's name.
        """
        tp = track_path(track)
        if not any((input_type, input_channel, output_type, output_channel)):
            props = ["input_routing_type", "input_routing_channel", "output_routing_type",
                     "output_routing_channel", "available_input_routing_types",
                     "available_input_routing_channels", "available_output_routing_types",
                     "available_output_routing_channels"]
            info = lom_get(tp, props)

            def names(value: Any) -> Any:
                if isinstance(value, list):
                    return [v.get("display_name") for v in value if isinstance(v, dict)]
                if isinstance(value, dict):
                    return value.get("display_name")
                return value
            return to_json({k: names(v) for k, v in info.items() if not k.startswith("$")})
        ops = []
        for prop, value in (("input_routing_type", input_type),
                            ("input_routing_channel", input_channel),
                            ("output_routing_type", output_type),
                            ("output_routing_channel", output_channel)):
            if value is not None:
                ops.append(op_set(tp, prop, ref(f"{tp} available_{prop}s {shlex.quote(value)}")))
        return to_json(batch_summary(batch(ops)))

    # ── Scenes ───────────────────────────────────────────────────────────────

    @tool(destructive=True)
    def manage_scene(action: str, scene: int | str | None = None, name: str | None = None,
                     tempo: float | None = None, index: int = -1) -> str:
        """Scenes: create | delete | duplicate | fire | rename | set_tempo |
        clear_tempo | stop_all_clips | capture (capture playing clips into a new scene).
        scene: index or name. index: insert position for create (-1 = end).
        """
        action = action.lower()
        if action == "create":
            ops = [op_call("live_set", "create_scene", index)]
            if name:
                ops.append(op_set(f"live_set scenes {index}", "name", name))
            return to_json(batch_summary(batch(ops)))
        if action == "stop_all_clips":
            return to_json(send("lom_call", {"path": "live_set", "method": "stop_all_clips"}))
        if action == "capture":
            return to_json(send("lom_call", {"path": "live_set",
                                             "method": "capture_and_insert_scene"}))
        if scene is None:
            raise ValueError("scene is required for this action")
        if action in ("delete", "duplicate"):
            idx = index_in("live_set scenes", scene)
            return to_json(send("lom_call", {"path": "live_set", "method": f"{action}_scene",
                                             "args": [idx]}))
        sp = f"live_set scenes {selector(scene)}"
        if action == "fire":
            return to_json(send("lom_call", {"path": sp, "method": "fire"}))
        if action == "rename":
            return to_json(send("lom_set", {"path": sp, "property": "name", "value": name}))
        if action in ("set_tempo", "clear_tempo"):
            ops = []
            if action == "set_tempo":
                if tempo is None:
                    raise ValueError("tempo is required")
                ops.append(op_set(sp, "tempo_enabled", True))
                ops.append(op_set(sp, "tempo", float(tempo)))
            else:
                ops.append(op_set(sp, "tempo_enabled", False))
            return to_json(batch_summary(batch(ops, stop_on_error=False)))
        raise ValueError(f"Unknown action {action!r}")

    # ── Clips ────────────────────────────────────────────────────────────────

    @tool(idempotent=True)
    def set_clip_properties(track: int | str, clip_index: int | None = None,
                            arrangement_clip_index: int | None = None,
                            name: str | None = None, color: str | int | None = None,
                            looping: bool | None = None, loop_start: float | None = None,
                            loop_end: float | None = None, start_marker: float | None = None,
                            end_marker: float | None = None, muted: bool | None = None,
                            warping: bool | None = None, warp_mode: str | None = None,
                            gain: float | None = None, pitch_coarse: int | None = None,
                            pitch_fine: float | None = None, launch_mode: str | None = None,
                            launch_quantization: str | None = None,
                            legato: bool | None = None,
                            velocity_amount: float | None = None) -> str:
        """Edit clip settings (Session slot via clip_index, Arrangement via
        arrangement_clip_index). Times are in beats.

        - warp_mode: beats | tones | texture | re_pitch | complex | complex_pro (audio)
        - gain: 0.0..1.0 (Live's normalized clip gain; ~0.4 ≈ 0 dB), audio clips
        - pitch_coarse: semitones -48..48, pitch_fine: cents -50..49 (audio)
        - launch_mode: trigger | gate | toggle | repeat
        - launch_quantization: global | none | 8 bars | 4 bars | 2 bars | 1 bar |
          1/2 | 1/2T | 1/4 | 1/4T | 1/8 | 1/8T | 1/16 | 1/16T | 1/32
        - muted: deactivates the clip
        """
        cp = clip_path(track, clip_index, arrangement_clip_index)
        ops = []
        if loop_start is not None or loop_end is not None:
            current = lom_get(cp, ["loop_start", "loop_end"])
            pairs = [("loop_start", loop_start), ("loop_end", loop_end)]
            # Move the end first when the new start would pass the current end
            if loop_start is not None and loop_start >= current["loop_end"]:
                pairs.reverse()
            ops += [op_set(cp, prop, float(v)) for prop, v in pairs if v is not None]
        if start_marker is not None or end_marker is not None:
            current = lom_get(cp, ["start_marker", "end_marker"])
            pairs = [("start_marker", start_marker), ("end_marker", end_marker)]
            if start_marker is not None and start_marker >= current["end_marker"]:
                pairs.reverse()
            ops += [op_set(cp, prop, float(v)) for prop, v in pairs if v is not None]
        simple = {"name": name, "looping": looping, "warping": warping, "legato": legato,
                  "gain": gain, "pitch_coarse": pitch_coarse, "pitch_fine": pitch_fine,
                  "velocity_amount": velocity_amount}
        for prop, value in simple.items():
            if value is not None:
                ops.append(op_set(cp, prop, value))
        if muted is not None:
            ops.append(op_set(cp, "muted", bool(muted)))
        if color is not None:
            ops.append(op_set(cp, "color_index" if isinstance(color, int) else "color",
                              parse_color(color)))
        if warp_mode is not None:
            ops.append(op_set(cp, "warp_mode", WARP_MODES[warp_mode.lower()]))
        if launch_mode is not None:
            ops.append(op_set(cp, "launch_mode", LAUNCH_MODES[launch_mode.lower()]))
        if launch_quantization is not None:
            ops.append(op_set(cp, "launch_quantization",
                              CLIP_LAUNCH_QUANTIZATION[launch_quantization]))
        if not ops:
            return to_json(lom_get(cp))
        return to_json(batch_summary(batch(ops)))

    @tool(destructive=True)
    def manage_clip(action: str, track: int | str, clip_index: int | None = None,
                    arrangement_clip_index: int | None = None,
                    target_track: int | str | None = None,
                    target_clip_index: int | None = None, grid: str = "1/16",
                    amount: float = 1.0, start_time: float | None = None,
                    length: float = 4.0, name: str | None = None) -> str:
        """Clip operations.

        action:
        - fire | stop | delete | select
        - duplicate_to_slot: copy a Session clip to target_track/target_clip_index
        - duplicate_loop: double the loop and its content
        - crop: crop the clip to its loop/markers
        - quantize: grid (1/4, 1/8, 1/8T, 1/16, 1/16T, 1/32) with amount 0..1
        - create_session_clip: empty MIDI clip of `length` beats in clip_index
        - create_arrangement_clip: empty MIDI clip at start_time (beats) in the
          Arrangement (Live 12+)
        """
        action = action.lower()
        tp = track_path(track)
        if action == "create_session_clip":
            path = prepare_clip(track, clip_index, length, name=name)
            return to_json({"created": path, "length": length})
        if action == "create_arrangement_clip":
            if start_time is None:
                raise ValueError("start_time is required")
            path = prepare_clip(track, None, length, arrangement_time=start_time, name=name)
            return to_json({"created": path, "start_time": start_time, "length": length})
        cp = clip_path(track, clip_index, arrangement_clip_index)
        if action in ("fire", "stop", "duplicate_loop", "crop"):
            return to_json(send("lom_call", {"path": cp, "method": action}))
        if action == "delete":
            if arrangement_clip_index is not None:
                return to_json(send("lom_call", {"path": tp, "method": "delete_clip",
                                                 "args": [ref(cp)]}))
            return to_json(send("lom_call", {"path": f"{tp} clip_slots {int(clip_index)}",
                                             "method": "delete_clip"}))
        if action == "quantize":
            grid_enum = {"$enum": "Live.Song.RecordingQuantization",
                         "value": NOTE_QUANTIZE_GRID[grid]}
            return to_json(send("lom_call", {"path": cp, "method": "quantize",
                                             "args": [grid_enum, float(amount)]}))
        if action == "duplicate_to_slot":
            if target_clip_index is None:
                raise ValueError("target_clip_index is required")
            dest = f"{track_path(target_track if target_track is not None else track)} " \
                   f"clip_slots {int(target_clip_index)}"
            return to_json(send("lom_call", {"path": f"{tp} clip_slots {int(clip_index)}",
                                             "method": "duplicate_clip_to",
                                             "args": [ref(dest)]}))
        if action == "select":
            ops = [op_set("live_set view", "detail_clip", ref(cp))]
            if clip_index is not None:
                ops.append(op_set("live_set view", "highlighted_clip_slot",
                                  ref(f"{tp} clip_slots {int(clip_index)}")))
            ops.append(op_call("live_app view", "show_view", "Detail/Clip"))
            return to_json(batch_summary(batch(ops, stop_on_error=False)))
        raise ValueError(f"Unknown action {action!r}")

    @tool(destructive=True)
    def write_clip_automation(track: int | str, clip_index: int,
                              parameter: int | str = "volume",
                              device: int | str = "mixer",
                              points: list[dict] | None = None, shape: str | None = None,
                              start: float = 0.0, end: float | None = None,
                              low: float = 0.0, high: float = 1.0, cycles: float = 1.0,
                              resolution: float = 0.25, normalized: bool = True,
                              clear_existing: bool = True) -> str:
        """Draw a clip automation envelope for any device or mixer parameter.

        - device="mixer" with parameter "volume" | "pan" | "send A"; otherwise a
          device index/name and a parameter index/name (e.g. "Filter Freq").
        - points: [{"time": beats, "value": v}, ...] — linearly interpolated.
        - or shape: ramp_up | ramp_down | sine | triangle | square | saw |
          exp_up | exp_down between start and end (default: clip end), from low
          to high, repeated `cycles` times.
        - normalized=True: values are 0..1 of the parameter range; False: raw
          parameter units.
        - points=[] with shape=None just clears the envelope.
        Times are clip-relative beats. Session clips only: Live's API refuses
        envelope edits on Arrangement clips and has no track automation. To
        get automation into the Arrangement, write it on a Session clip and
        duplicate_to_arrangement — the copy keeps its envelopes.
        """
        cp = clip_path(track, clip_index)
        pp = parameter_path(track, device, parameter)
        info = lom_get(pp, ["min", "max", "name"])
        pmin, pmax = float(info["min"]), float(info["max"])
        if shape:
            if end is None:
                end = float(lom_get(cp, ["length"])["length"])
            raw = envelope_points(shape.lower(), start, end, low, high, cycles, resolution)
        elif points:
            raw = interpolate_points(points, resolution)
        else:
            raw = []
        scale = (lambda v: pmin + (pmax - pmin) * v) if normalized else (lambda v: v)
        steps = [{"time": round(t, 6), "value": scale(v), "duration": resolution} for t, v in raw]
        result = send("clip_write_envelope", {"clip_path": cp, "parameter_path": pp,
                                              "points": steps, "clear": clear_existing})
        result["parameter"] = info["name"]
        return to_json(result)

    # ── Notes ────────────────────────────────────────────────────────────────

    @tool(read_only=True, ui=ui.NOTES)
    def read_clip_notes(track: int | str, clip_index: int | None = None,
                        arrangement_clip_index: int | None = None,
                        from_time: float | None = None, time_span: float | None = None,
                        pitch_low: int | str | None = None,
                        pitch_high: int | str | None = None) -> str:
        """Read notes (with note_id, probability, velocity_deviation,
        release_velocity) from a Session or Arrangement MIDI clip, optionally
        within a time window (beats) and pitch range (MIDI number or "C3").
        """
        window: dict[str, Any] = {}
        if from_time is not None:
            window["from_time"] = from_time
        if time_span is not None:
            window["time_span"] = time_span
        if pitch_low is not None or pitch_high is not None:
            lo = music.parse_note(pitch_low) if pitch_low is not None else 0
            hi = music.parse_note(pitch_high) if pitch_high is not None else 127
            window.update(from_pitch=lo, pitch_span=hi - lo + 1)
        result = read_notes(clip_path(track, clip_index, arrangement_clip_index), **window)
        for n in result["notes"]:
            n["name"] = music.note_name(n["pitch"])
        return to_json(result)

    @tool(destructive=True)
    def edit_clip_notes(track: int | str, mode: str = "add", notes: list[dict] | None = None,
                        clip_index: int | None = None,
                        arrangement_clip_index: int | None = None,
                        note_ids: list[int] | None = None,
                        from_time: float | None = None, time_span: float | None = None) -> str:
        """Write notes with full Live 11+ expression.

        mode:
        - add: add `notes`
        - replace: delete notes in [from_time, from_time+time_span) (default: all)
          and write `notes`
        - modify: update existing notes by note_id (get ids from read_clip_notes);
          each entry is {"note_id": n, <fields to change>}
        - remove_ids: delete the notes in note_ids
        Note fields: pitch (MIDI or "C3"), start_time, duration, velocity (1-127),
        mute, probability (0-1), velocity_deviation (-127..127), release_velocity.
        """
        prepared = []
        for n in notes or []:
            n = dict(n)
            if "pitch" in n:
                n["pitch"] = music.parse_note(n["pitch"])
            prepared.append(n)
        extra: dict[str, Any] = {}
        if note_ids:
            extra["note_ids"] = note_ids
        if from_time is not None:
            extra["from_time"] = from_time
        if time_span is not None:
            extra["time_span"] = time_span
        return to_json(write_notes(clip_path(track, clip_index, arrangement_clip_index),
                                   prepared, mode, **extra))

    @tool(destructive=True, ui=ui.NOTES)
    def transform_clip_notes(track: int | str, operation: str,
                             clip_index: int | None = None,
                             arrangement_clip_index: int | None = None,
                             semitones: int = 0, steps: int = 0, key: str | None = None,
                             scale: str | None = None, grid: float = 0.25,
                             strength: float = 1.0, timing: float = 0.01,
                             velocity_amount: float = 8.0, factor: float = 1.0,
                             offset: float = 0.0, start_velocity: int = 40,
                             end_velocity: int = 120, axis: int | str | None = None,
                             value: float = 1.0, seed: int | None = None,
                             pitch_low: int | str | None = None,
                             pitch_high: int | str | None = None,
                             time_start: float | None = None,
                             time_end: float | None = None) -> str:
        """Transform existing MIDI notes (optionally only those within a pitch
        range and/or time range), written back as one edit.

        operation:
        - transpose (semitones) | transpose_in_scale (steps, key, scale)
        - constrain_to_scale (key, scale): snap wrong notes into the scale
        - quantize (grid in beats e.g. 0.25 = 1/16, strength 0..1)
        - humanize (timing in beats, velocity_amount, seed)
        - velocity_scale (factor, offset) | velocity_ramp (start_velocity → end_velocity)
        - reverse | invert (axis pitch) | legato
        - double_speed | half_speed
        - set_probability (value 0..1) | set_velocity_deviation (value)
        - dedupe | delete
        key/scale default to the set's Key & Scale (Live 12).
        Notes are edited in place (Live 11+), so note ids and per-note
        expression the API cannot read (MPE) are kept.
        """
        cp = clip_path(track, clip_index, arrangement_clip_index)
        data = read_notes(cp)
        notes = data["notes"]
        lo = music.parse_note(pitch_low) if pitch_low is not None else 0
        hi = music.parse_note(pitch_high) if pitch_high is not None else 127
        t0 = time_start if time_start is not None else float("-inf")
        t1 = time_end if time_end is not None else float("inf")
        chosen = [n for n in notes if lo <= n["pitch"] <= hi and t0 <= n["start_time"] < t1]
        op = operation.lower()
        if op in ("transpose_in_scale", "constrain_to_scale"):
            key, scale = resolve_key(key, scale, song_context())
        ops: dict[str, Callable[[list[dict]], list[dict]]] = {
            "transpose": lambda ns: music.transpose(ns, semitones),
            "transpose_in_scale": lambda ns: music.transpose_in_scale(ns, steps, key, scale),
            "constrain_to_scale": lambda ns: music.constrain_to_scale(ns, key, scale),
            "quantize": lambda ns: music.quantize(ns, grid, strength),
            "humanize": lambda ns: music.humanize_notes(ns, timing, velocity_amount, seed),
            "velocity_scale": lambda ns: music.scale_velocity(ns, factor, offset),
            "velocity_ramp": lambda ns: music.velocity_ramp(ns, start_velocity, end_velocity),
            "reverse": lambda ns: music.reverse(ns, data["length"]),
            "invert": lambda ns: music.invert(ns, music.parse_note(axis) if axis is not None
                                              else None),
            "legato": lambda ns: music.legato(ns),
            "double_speed": lambda ns: music.stretch(ns, 0.5),
            "half_speed": lambda ns: music.stretch(ns, 2.0),
            "set_probability": lambda ns: music.set_field(ns, "probability",
                                                          max(0.0, min(1.0, value))),
            "set_velocity_deviation": lambda ns: music.set_field(ns, "velocity_deviation", value),
            "dedupe": music.dedupe,
            "delete": lambda ns: [],
        }
        if op not in ops:
            raise ValueError(f"Unknown operation {operation!r}. Known: {', '.join(ops)}")
        changed = ops[op](chosen)
        summary = {"operation": op, "notes_affected": len(chosen)}
        summary.update(apply_note_edit(cp, notes, chosen, changed))
        return to_json(summary)

    MIDI_TOOLS: dict[str, tuple[Callable, set[str]]] = {
        "strum": (music.strum, {"amount", "direction"}),
        "ornament": (music.ornament, {"kind", "spacing", "velocity", "step", "key", "scale"}),
        "chop": (music.chop, {"parts", "velocity_decay"}),
        "connect": (music.connect, {"rate", "max_notes", "key", "scale"}),
        "time_warp": (music.time_warp, {"amount", "mode"}),
        "velocity_shaper": (music.velocity_shape, {"shape", "low", "high", "cycles", "seed"}),
        "recombine": (music.recombine, {"mode", "part", "amount", "seed"}),
        "span": (music.span, {"mode", "value"}),
        "arpeggiate": (music.arpeggiate, {"style", "rate", "gate", "octaves", "seed"}),
    }

    @tool(destructive=True, ui=ui.NOTES)
    def apply_midi_tool(track: int | str, tool: str, clip_index: int | None = None,
                        arrangement_clip_index: int | None = None,
                        settings: dict[str, Any] | None = None,
                        pitch_low: int | str | None = None,
                        pitch_high: int | str | None = None,
                        time_start: float | None = None,
                        time_end: float | None = None) -> str:
        """Live 12's MIDI Tools (Transformations) on a clip's notes — all of
        them, or those within a pitch and/or time range. One undo step; notes
        that survive keep their ids and MPE.

        tool → settings (defaults):
        - strum: amount (0.04 beats between strings), direction up|down|alternate
        - ornament: kind flam|grace, spacing (0.0625 beats), velocity (0.6 ×),
          step (-1; grace notes move by scale steps in the set's key)
        - chop: parts (2), velocity_decay (0..1, 0) — ratchets
        - connect: rate (0.25), max_notes (8) — scale passing notes between notes
        - time_warp: mode accelerando|ritardando, amount (0..1, 0.5)
        - velocity_shaper: shape ramp_up|ramp_down|swell|dip|sine|random|accent,
          low (40), high (120), cycles (1), seed
        - recombine: mode rotate|reverse|shuffle, part pitch|velocity|duration,
          amount (1), seed — new order over the same rhythm
        - span: mode percent|fixed|legato|scale, value (0.5)
        - arpeggiate: style up|down|updown|downup|random|played, rate (0.25),
          gate (0.9), octaves (1), seed — chords become arpeggios
        e.g. tool="strum", settings={"amount": 0.03, "direction": "alternate"}.
        transform_clip_notes has transpose, quantize, humanize and more.
        """
        name = tool.strip().lower()
        if name not in MIDI_TOOLS:
            raise ValueError(f"Unknown tool {tool!r}. Known: {', '.join(MIDI_TOOLS)}")
        function, allowed = MIDI_TOOLS[name]
        options = dict(settings or {})
        unknown = set(options) - allowed
        if unknown:
            raise ValueError(f"{name} does not take {sorted(unknown)}; it takes "
                             f"{sorted(allowed)}")
        cp = clip_path(track, clip_index, arrangement_clip_index)
        data = read_notes(cp)
        notes = data["notes"]
        lo = music.parse_note(pitch_low) if pitch_low is not None else 0
        hi = music.parse_note(pitch_high) if pitch_high is not None else 127
        t0 = time_start if time_start is not None else float("-inf")
        t1 = time_end if time_end is not None else float("inf")
        chosen = [n for n in notes if lo <= n["pitch"] <= hi and t0 <= n["start_time"] < t1]
        if name == "connect" or (name == "ornament" and options.get("kind") == "grace"):
            options["key"], options["scale"] = resolve_key(options.get("key"),
                                                           options.get("scale"), song_context())
        if name in ("time_warp", "velocity_shaper", "span"):
            options["length"] = float(data["length"])
        changed = function(chosen, **options)
        summary = {"tool": name, "notes_affected": len(chosen)}
        summary.update(apply_note_edit(cp, notes, chosen, changed))
        return to_json(summary)

    # ── Generators ───────────────────────────────────────────────────────────

    @tool(read_only=True)
    def music_theory(key: str | None = None, scale: str | None = None,
                     progression: str | None = None, sevenths: bool = False) -> str:
        """Scale notes, diatonic chords and (optionally) a parsed progression.

        key: "C", "F#", "A minor", "Ebm" (default: the set's Key & Scale in
        Live 12, else C major); scale: major, minor, dorian, phrygian,
        lydian, mixolydian, locrian, harmonic_minor, melodic_minor, hijaz,
        phrygian_dominant, major_pentatonic, minor_pentatonic, blues, ...
        progression: roman numerals ("ii7 V7 Imaj7", "i VI III VII", "bVII")
        or chord symbols ("Am7 F C/E G").
        """
        if key is None:
            key, scale = resolve_key(key, scale, song_context(optional=True))
        root, intervals = music.parse_key(key, scale)
        result: dict[str, Any] = {
            "root": music.NOTE_NAMES[root],
            "scale_notes": [music.NOTE_NAMES[(root + i) % 12] for i in intervals],
            "available_scales": sorted(music.SCALES),
        }
        if len(intervals) == 7:
            result["diatonic_chords"] = music.diatonic_chords(key, scale, sevenths)
        if progression:
            chords = music.parse_progression(progression, key, scale)
            voiced = music.voice_progression(chords, 3, "smooth")
            result["progression"] = [{"chord": c["symbol"],
                                      "notes": [music.note_name(p) for p in v]}
                                     for c, v in zip(chords, voiced)]
        return to_json(result)

    @tool(destructive=True, ui=ui.NOTES)
    def generate_chord_progression(track: int | str, progression: str | None = None,
                                   key: str | None = None, scale: str | None = None,
                                   clip_index: int | None = 0,
                                   arrangement_time: float | None = None,
                                   bars_per_chord: float = 1.0, octave: int = 3,
                                   voicing: str = "smooth", rhythm: str = "sustain",
                                   add_bass_note: bool = False, velocity: int = 90,
                                   replace: bool = True, clip_name: str | None = None) -> str:
        """Write a chord progression into a MIDI clip (created if missing).

        - progression: roman numerals ("ii7 V7 Imaj7", "i iv bVII III") or chord
          symbols ("Am F C G", "Dm7 G7 Cmaj7", "C/E"); default: four chords
          from the scale's own triads (C major: C G Am F; D Dorian: Dm G Dm C)
        - key/scale: default to the set's Key & Scale (Live 12); bars follow the
          set's time signature
        - voicing: smooth (voice-led inversions) | close | open | drop2 | spread
        - rhythm: sustain | half | quarters | eighths | offbeat | syncopated |
          charleston | stabs
        - octave: 3 puts close chords around middle C (C3 = 60 in Live)
        - arrangement_time: write into a new Arrangement clip at this beat
          instead of a Session slot (Live 12+)
        """
        context = song_context()
        key, scale = resolve_key(key, scale, context)
        progression = progression or music.default_progression(key, scale)
        bar = context["beats_per_bar"]
        chords = music.parse_progression(progression, key, scale)
        voiced = music.voice_progression(chords, octave, voicing, add_bass_note)
        notes = music.chord_notes(voiced, bars_per_chord, rhythm, velocity, bar)
        length = len(chords) * bars_per_chord * bar
        path = prepare_clip(track, clip_index, length, arrangement_time,
                            clip_name or progression)
        written = write_notes(path, notes, "replace" if replace else "add")
        return generated_result(path, notes, written, dict(musical_context(key, scale, context), **{
            "chords": [{"chord": c["symbol"], "notes": [music.note_name(p) for p in v]}
                       for c, v in zip(chords, voiced)], "length_beats": length}))

    @tool(destructive=True, ui=ui.NOTES)
    def generate_drum_pattern(track: int | str, style: str = "house", bars: int = 2,
                              clip_index: int | None = 0,
                              arrangement_time: float | None = None,
                              swing: float = 0.0, humanize: float = 0.3, fill: bool = True,
                              mapping: dict[str, int | str] | None = None,
                              auto_map_drum_rack: bool = True, seed: int | None = None,
                              velocity: int = 100, replace: bool = True,
                              clip_name: str | None = None) -> str:
        """Write a drum groove into a MIDI clip on a Drum Rack track.

        style: house | techno | trap | drill | boom_bap | lofi | dnb | breakbeat |
        uk_garage | reggaeton | afrobeats | rock | funk | disco | halftime
        Bars follow the set's time signature: the 4/4 grooves are cut or
        extended to fit other meters.
        - swing 0..1 delays off-beat 16ths; humanize 0..1 adds timing/velocity variation
        - fill: snare fill on the last beat of the last bar
        - auto_map_drum_rack: find kick/snare/hat/... pads by their names in the
          track's Drum Rack; otherwise General MIDI notes (kick C1=36, snare D1=38,
          clap D#1=39, closed hat F#1=42, open hat A#1=46)
        - mapping overrides: {"kick": 36, "closed_hat": "F#1", ...}
        """
        context = song_context()
        bar = context["beats_per_bar"]
        pads = drum_mapping_from_rack(track) if auto_map_drum_rack else {}
        if mapping:
            pads.update({k: music.parse_note(v) for k, v in mapping.items()})
        notes = music.drum_pattern(style, bars, pads, swing, humanize, fill, seed, velocity,
                                   bar)
        path = prepare_clip(track, clip_index, bars * bar, arrangement_time,
                            clip_name or f"{style} drums")
        written = write_notes(path, notes, "replace" if replace else "add")
        used = sorted({n["pitch"] for n in notes})
        return generated_result(path, notes, written, {
            "pad_mapping": pads or "General MIDI defaults",
            "pads_used": [music.note_name(p) for p in used],
            "time_signature": context["time_signature"], "length_beats": bars * bar})

    @tool(destructive=True, ui=ui.NOTES)
    def generate_euclidean(track: int | str, voices: list[dict], bars: int = 2,
                           step: float | str = "1/16", clip_index: int | None = 0,
                           arrangement_time: float | None = None, replace: bool = True,
                           clip_name: str | None = None) -> str:
        """Euclidean rhythms (Live 12's Euclidean generator): each voice spreads
        `pulses` hits as evenly as possible over `steps`, and its cycle repeats
        regardless of bar lines, so voices of different lengths drift apart.

        voices: [{"pad": "kick", "pulses": 3, "steps": 8, "rotation": 0,
        "velocity": 100, "accent": 120}, …] — pad is a role found on the
        track's Drum Rack (kick, snare, closed_hat, …), a note name ("C1") or a
        MIDI number; accent (optional) is the velocity of each cycle's first hit.
        step: length of one step, "1/16" (0.25 beat), "1/8", "1/8T", or beats.
        Classics: E(3,8) tresillo, E(5,8) cinquillo, E(7,16), E(5,16) bossa;
        E(4,9) = 2+2+2+3, the Turkish aksak.
        """
        grids = {"1/4": 1.0, "1/8": 0.5, "1/8t": 1 / 3, "1/16": 0.25, "1/16t": 1 / 6,
                 "1/32": 0.125}
        length = float(grids[str(step).lower()]) if str(step).lower() in grids \
            else float(step)
        context = song_context()
        pads = {**music.GM_DRUMS, **drum_mapping_from_rack(track)}
        resolved = []
        for voice in voices:
            pad = voice.get("pad", voice.get("pitch", "kick"))
            pitch = pads[str(pad).lower()] if str(pad).lower() in pads \
                else music.parse_note(pad)
            resolved.append(dict(voice, pitch=pitch))
        total = bars * context["beats_per_bar"]
        notes = music.euclidean_notes(resolved, total, length)
        path = prepare_clip(track, clip_index, total, arrangement_time,
                            clip_name or "Euclid " + " ".join(
                                f"E({v['pulses']},{v['steps']})" for v in voices))
        written = write_notes(path, notes, "replace" if replace else "add")
        return generated_result(path, notes, written, {
            "voices": [{"pad": music.note_name(v["pitch"]), "pattern": "".join(
                "x" if hit else "." for hit in music.euclidean(
                    int(v["pulses"]), int(v["steps"]), int(v.get("rotation", 0))))}
                for v in resolved],
            "time_signature": context["time_signature"], "length_beats": total})

    @tool(idempotent=True, ui=ui.MAKAM)
    def turkish_makam(name: str | None = None, tonic: str | None = None,
                      tuning_file: bool = False) -> str:
        """Turkish makams (Arel-Ezgi-Uzdilek system, 53 commas per octave) and
        usuls. Without a name: every makam and usul. With a name (rast, uşşak,
        hicaz, hüseynî, kürdî, saba, segâh, hüzzam, nihavend, karcığar, …):
        its steps in commas, degrees in cents, karar (tonic) and güçlü, and the
        12-tone approximation on `tonic` (default: its karar).

        Every generator and scale tool accepts a makam as the scale
        (scale="hicaz", key="A"), using that 12-tone approximation.
        tuning_file=True also writes an .ascl tuning to Live's User Library ›
        Tunings that moves the makam's degrees to their true comma pitches;
        double-click it in Live's browser (Tunings) to hear the real intervals
        on the same notes.
        """
        from . import turkish

        if name is None:
            return to_json({
                "makams": {key: {"karar": karar, "guclu": guclu, "built_from": built,
                                 "steps_commas": steps}
                           for key, (steps, karar, guclu, built) in turkish.MAKAMS.items()},
                "usuls": {key: f"{n}/{d}: {vocal}"
                          for key, (n, d, _strokes, vocal) in turkish.USULS.items()},
                "comma_cents": round(turkish.COMMA_CENTS, 2)})
        info = turkish.makam(name)
        root = music.parse_pitch_class(tonic or info["karar"])
        info["tonic"] = music.NOTE_NAMES[root]
        info["notes_12tet"] = [music.NOTE_NAMES[(root + i) % 12]
                               for i in info["intervals_12tet"]]
        info["offsets_from_12tet_cents"] = [
            round(c - round(c / 100.0) * 100.0, 1) for c in info["degrees_cents"]]
        info["use_as_scale"] = f'key="{info["tonic"]}", scale="{info["makam"]}"'
        if tuning_file:
            file_name, text = turkish.ascl(info["makam"], root, music.NOTE_NAMES)
            folder = turkish.tuning_folder()
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / file_name
            target.write_text(text, encoding="utf-8")
            info["tuning_file"] = str(target)
            info["load_it"] = ("In Live's browser open Tunings (or User Library › Tunings) "
                               f"and double-click '{file_name}'. Live's API cannot load "
                               "tunings itself.")
        return to_json(info)

    @tool(destructive=True, ui=ui.NOTES)
    def generate_usul(track: int | str, usul: str = "aksak", cycles: int = 4,
                      clip_index: int | None = 0, arrangement_time: float | None = None,
                      mapping: dict[str, int | str] | None = None, velocity: int = 110,
                      ornament: float = 0.0, seed: int | None = None,
                      set_meter: bool = False, replace: bool = True,
                      clip_name: str | None = None) -> str:
        """Write a Turkish usul (rhythmic cycle) as MIDI on a drum track.

        usul: nim_sofyan 2/4, semai 3/4, sofyan 4/4, turk_aksagi 5/8,
        yuruk_semai 6/8, devr_i_hindi 7/8, devr_i_turan 7/8, duyek 8/8,
        musemmen 8/8, aksak 9/8, aksak_semai 10/8, curcuna 10/16.
        Strokes: düm (low) → the kick pad, tek (high) → snare, te/ke/ka
        (light) → rim — or pads named doum/dum, tek/tak, ka/slap on a darbuka
        kit. mapping overrides: {"düm": "C1", "tek": 38, "ke": "C#1"}.
        - ornament 0..1: fill long strokes with light te/ke hits
        - set_meter: also set the set's time signature to the usul's meter
        Length: `cycles` cycles of the usul.
        """
        from . import turkish

        info = turkish.usul(usul)
        roles = {**music.GM_DRUMS, **drum_mapping_from_rack(track)}
        pads = {stroke: roles.get(role, music.GM_DRUMS[role])
                for stroke, (role, _weight) in turkish.STROKES.items()}
        pads.update(darbuka_pads(track))
        for stroke, note in (mapping or {}).items():
            key = turkish.normalize(stroke).replace("dum", "düm")
            if key not in turkish.STROKES:
                raise ValueError(f"Unknown stroke {stroke!r}: use düm, tek, te, ke or ka")
            pads[key] = music.parse_note(note)
        notes = turkish.usul_notes(info["usul"], cycles, pads, velocity, ornament, seed)
        length = cycles * info["units"] * 4.0 / info["denominator"]
        if set_meter:
            batch([op_set("live_set", "signature_numerator", info["numerator"]),
                   op_set("live_set", "signature_denominator", info["denominator"])])
        path = prepare_clip(track, clip_index, length, arrangement_time,
                            clip_name or f"{info['usul'].replace('_', ' ').title()} "
                                          f"{info['meter']}")
        written = write_notes(path, notes, "replace" if replace else "add")
        return generated_result(path, notes, written, {
            "usul": info["usul"], "meter": info["meter"], "vocal": info["vocal"],
            "pads": {stroke: music.note_name(note) for stroke, note in pads.items()},
            "length_beats": length, "meter_set": set_meter})

    @tool(destructive=True, ui=ui.NOTES)
    def generate_bassline(track: int | str, progression: str | None = None,
                          key: str | None = None, scale: str | None = None,
                          clip_index: int | None = 0,
                          arrangement_time: float | None = None,
                          bars_per_chord: float = 1.0, octave: int = 1,
                          style: str = "root", velocity: int = 100,
                          seed: int | None = None, replace: bool = True,
                          clip_name: str | None = None) -> str:
        """Write a bassline that follows a chord progression.

        style: root (whole notes) | octave (8th-note octave jumps) | offbeat
        (house) | syncopated | walking (scale steps approaching the next root) |
        pulse (16ths) | 808 (long notes). octave=1 → roots around C1 (36).
        key/scale default to the set's Key & Scale (Live 12); the progression
        to four chords from the scale's own triads. To follow chords already
        in a clip, run analyze_harmony and pass its progression and
        bars_per_chord.
        """
        context = song_context()
        key, scale = resolve_key(key, scale, context)
        root, intervals = music.parse_key(key, scale)
        progression = progression or music.default_progression(key, scale)
        bar = context["beats_per_bar"]
        chords = music.parse_progression(progression, key, scale)
        notes = music.bassline(chords, bars_per_chord, octave, style, root, intervals,
                               velocity, seed, bar)
        length = len(chords) * bars_per_chord * bar
        path = prepare_clip(track, clip_index, length, arrangement_time,
                            clip_name or f"bass {style}")
        written = write_notes(path, notes, "replace" if replace else "add")
        return generated_result(path, notes, written, dict(
            musical_context(key, scale, context), progression=progression, length_beats=length))

    @tool(destructive=True, ui=ui.NOTES)
    def generate_arpeggio(track: int | str, progression: str | None = None,
                          key: str | None = None, scale: str | None = None,
                          clip_index: int | None = 0, arrangement_time: float | None = None,
                          bars_per_chord: float = 1.0, octave: int = 4,
                          pattern: str = "up", rate: float = 0.25, octaves: int = 1,
                          gate: float = 0.8, velocity: int = 90, seed: int | None = None,
                          replace: bool = True, clip_name: str | None = None) -> str:
        """Write an arpeggio over a chord progression.

        pattern: up | down | updown | downup | random | converge | pinky
        rate: note length in beats (0.25 = 1/16, 0.5 = 1/8, 1/3 = 1/8 triplet)
        octaves: how many octaves the arpeggio spans; gate: note length fraction
        key/scale default to the set's Key & Scale (Live 12); the progression
        to four chords from the scale's own triads.
        """
        context = song_context()
        key, scale = resolve_key(key, scale, context)
        progression = progression or music.default_progression(key, scale)
        bar = context["beats_per_bar"]
        chords = music.parse_progression(progression, key, scale)
        voiced = music.voice_progression(chords, octave, "smooth")
        notes = music.arpeggio(voiced, bars_per_chord, rate, pattern, octaves, gate,
                               velocity, seed, bar)
        length = len(chords) * bars_per_chord * bar
        path = prepare_clip(track, clip_index, length, arrangement_time,
                            clip_name or f"arp {pattern}")
        written = write_notes(path, notes, "replace" if replace else "add")
        return generated_result(path, notes, written, dict(
            musical_context(key, scale, context), progression=progression, length_beats=length))

    @tool(destructive=True, ui=ui.NOTES)
    def generate_melody(track: int | str, key: str | None = None, scale: str | None = None,
                        bars: int = 4, progression: str | None = None,
                        bars_per_chord: float = 1.0, clip_index: int | None = 0,
                        arrangement_time: float | None = None, low: str = "C4",
                        high: str = "C6", density: float = 0.6, seed: int | None = None,
                        velocity: int = 95, replace: bool = True,
                        clip_name: str | None = None) -> str:
        """Write a phrase-structured melody (A A' B A'') in a key/scale.

        If a progression is given, strong beats land on chord tones. density
        0..1 controls how busy it is; low/high bound the range; vary `seed` for
        alternatives and keep it to reproduce one. key/scale default to the
        set's Key & Scale (Live 12); bars follow the set's time signature.
        """
        context = song_context()
        key, scale = resolve_key(key, scale, context)
        bar = context["beats_per_bar"]
        chords = music.parse_progression(progression, key, scale) if progression else None
        notes = music.melody(key, scale, bars, low, high, chords, bars_per_chord, density,
                             seed, velocity, bar)
        path = prepare_clip(track, clip_index, bars * bar, arrangement_time,
                            clip_name or "melody")
        written = write_notes(path, notes, "replace" if replace else "add")
        return generated_result(path, notes, written, dict(
            musical_context(key, scale, context), seed=seed, length_beats=bars * bar))

    # ── Harmony analysis & MIDI files ────────────────────────────────────────

    @tool(read_only=True)
    def analyze_harmony(track: int | str, clip_index: int | None = None,
                        arrangement_clip_index: int | None = None,
                        resolution: str = "bar", key: str | None = None,
                        scale: str | None = None) -> str:
        """Name the chords in a MIDI clip: chord per window, key, roman
        numerals, and the chord changes with their lengths.

        - resolution: bar | half_bar | beat | 2_bars (window per chord)
        - key/scale: analyse in this key; by default the key is estimated
          from the notes (the set's Key & Scale is reported alongside)
        The result's `progression` and `bars_per_chord` can be passed to
        generate_bassline / generate_arpeggio / generate_melody /
        generate_chord_progression so a new part follows these chords.
        """
        cp = clip_path(track, clip_index, arrangement_clip_index)
        data = read_notes(cp)
        context = song_context(optional=True)
        bar = context["beats_per_bar"]
        windows = {"bar": bar, "half_bar": bar / 2, "beat": 1.0, "2_bars": bar * 2}
        if resolution not in windows:
            raise ValueError(f"resolution must be one of {', '.join(windows)}")
        result = music.analyze_harmony(data["notes"], bar, windows[resolution],
                                       float(data["length"]), key, scale)
        result["clip"] = data.get("clip")
        result["time_signature"] = context["time_signature"]
        if context.get("key") is not None:
            result["set_key"] = f"{context['key']} {context.get('scale_name') or ''}".strip()
        return to_json(result)

    @tool(destructive=True)
    def import_midi_file(path: str, track: int | str | None = None,
                         clip_index: int | None = 0, arrangement_time: float | None = None,
                         midi_track: int | str | None = None, channel: int | None = None,
                         transpose: int = 0, replace: bool = True,
                         clip_name: str | None = None) -> str:
        """Import the notes of a .mid file into a MIDI clip (created if missing).

        Without `track`, nothing is imported: the file's tracks, channels, note
        counts, tempo and time signature are listed instead.
        - midi_track: index or name of one track in the file (default: all
          tracks merged); channel: 1-16 to take one channel only
        - clip_index (Session slot) or arrangement_time (beats, Live 12+)
        - The clip is rounded up to whole bars of the set's time signature.
          The file's tempo is reported, not applied to the set.
        """
        from . import midi_files

        info = midi_files.read_midi(path)
        if track is None:
            return to_json({k: v for k, v in info.items() if k != "tracks"} | {
                "tracks": [{k: v for k, v in t.items() if k != "notes"}
                           for t in info["tracks"]]})
        notes = midi_files.select_notes(info, midi_track, channel)
        if not notes:
            raise ValueError("No notes found for that track/channel; call import_midi_file "
                             "without `track` to list what the file holds")
        for n in notes:
            n["pitch"] = min(127, max(0, n["pitch"] + transpose))
            n.pop("channel", None)
        bar = song_context()["beats_per_bar"]
        end = max(n["start_time"] + n["duration"] for n in notes)
        length = max(1, -(-end // bar)) * bar
        name = clip_name or Path(info["file"]).stem
        clip = prepare_clip(track, clip_index, length, arrangement_time, name)
        written = write_notes(clip, notes, "replace" if replace else "add")
        return generated_result(clip, notes, written, {
            "file": info["file"], "length_beats": length, "file_tempo_bpm": info["tempo_bpm"],
            "file_time_signature": info["time_signature"]})

    @tool
    def export_clip_to_midi(track: int | str, clip_index: int | None = None,
                            arrangement_clip_index: int | None = None,
                            path: str | None = None, include_muted: bool = False,
                            overwrite: bool = False) -> str:
        """Write a MIDI clip's notes to a .mid file, with the set's tempo and
        time signature.

        path: a .mid file; default ~/Music/Ableton MCP/MIDI/<clip name>.mid
        (numbered, never overwritten). An existing `path` is only replaced
        with overwrite=True. Muted notes are skipped unless include_muted.
        """
        from . import midi_files

        cp = clip_path(track, clip_index, arrangement_clip_index)
        data = read_notes(cp)
        notes = [n for n in data["notes"] if include_muted or not n.get("mute")]
        song = lom_get("live_set", ["tempo", "signature_numerator", "signature_denominator"])
        name = data.get("clip") or "clip"
        if path:
            target = Path(path).expanduser()
            if target.exists() and not overwrite:
                raise ValueError(f"{target} exists; pass overwrite=True to replace it")
        else:
            target = midi_files.free_path(Path.home() / "Music" / "Ableton MCP" / "MIDI", name)
        result = midi_files.write_midi(target, notes, float(song["tempo"]),
                                       int(song["signature_numerator"]),
                                       int(song["signature_denominator"]), name)
        result["clip"] = name
        return to_json(result)

    # ── Devices & racks ──────────────────────────────────────────────────────

    @tool(destructive=True)
    def manage_device(track: int | str, device: int | str, action: str,
                      target_track: int | str | None = None,
                      target_index: int | None = None) -> str:
        """Device operations: info | delete | enable | disable | toggle | select |
        collapse | expand | move (to target_track at target_index; Live 11+).
        """
        action = action.lower()
        dp = device_path(track, device)
        if action == "info":
            info = lom_get(dp, ["name", "class_name", "class_display_name", "type",
                                "is_active", "can_have_chains", "can_have_drum_pads"])
            info["parameters"] = [
                {k: v for k, v in p.items()
                 if not (isinstance(v, dict) and "$error" in v) and k != "$id"}
                for p in lom_get(f"{dp} parameters", ["name", "value", "$display", "min",
                                                      "max", "is_quantized",
                                                      "value_items"])["items"]]
            return to_json(info)
        if action == "delete":
            idx = index_in(f"{track_path(track)} devices", device)
            return to_json(send("lom_call", {"path": track_path(track),
                                             "method": "delete_device", "args": [idx]}))
        if action in ("enable", "disable", "toggle"):
            on = f"{dp} parameters 0"
            if action == "toggle":
                current = lom_get(on, ["value"])["value"]
                value = 0.0 if current else 1.0
            else:
                value = 1.0 if action == "enable" else 0.0
            return to_json(send("lom_set", {"path": on, "property": "value", "value": value}))
        if action == "select":
            return to_json(batch_summary(batch([
                op_call("live_set view", "select_device", ref(dp)),
                op_call("live_app view", "show_view", "Detail/DeviceChain")],
                stop_on_error=False)))
        if action in ("collapse", "expand"):
            return to_json(send("lom_set", {"path": f"{dp} view", "property": "is_collapsed",
                                            "value": action == "collapse"}))
        if action == "move":
            dest = track_path(target_track if target_track is not None else track)
            position = target_index if target_index is not None else -1
            if position < 0:
                position = len(lom_get(f"{dest} devices", ["name"])["items"])
            return to_json(send("lom_call", {"path": "live_set", "method": "move_device",
                                             "args": [ref(dp), ref(dest), position]}))
        raise ValueError(f"Unknown action {action!r}")

    @tool
    def add_device(track: int | str, device: str, position: int | None = None,
                   settings: dict[str, float | str] | None = None) -> str:
        """Insert one of Live's own devices by name, without the browser (Live 12.3+).

        device: the name in Live's browser, e.g. "EQ Eight", "Compressor",
        "Glue Compressor", "Limiter", "Saturator", "Utility", "Reverb",
        "Delay", "Echo", "Auto Filter", "Drum Buss", "Multiband Dynamics",
        "Chorus-Ensemble", "Roar", "Channel EQ", "Gate"; instruments ("Drift",
        "Operator", "Wavetable", "Simpler", "Drum Rack", "Meld"); MIDI effects
        ("Arpeggiator", "Chord", "Scale"); racks ("Audio Effect Rack",
        "Instrument Rack"). Plug-ins, Max devices and presets need
        load_instrument_or_effect with a browser URI.
        - position: index in the device chain (default: the end); MIDI effects
          go before the instrument
        - settings: {"Threshold": "-18 dB", "Ratio": "4", "1 Filter Type A":
          "High Pass 48dB"} as Live displays them, or raw numbers
        """
        tp = track_path(track)
        args = [device] if position is None else [device, int(position)]
        device_id = inserted_device_ids(tp, [args])[0]
        result: dict[str, Any] = {"device": device, "track": track, "id": device_id}
        if settings:
            names = resolve_parameter_names(f"id {device_id}", list(settings))
            result["settings"] = batch_summary(batch(
                setting_ops(f"id {device_id}", settings, names), stop_on_error=False))
        return to_json(result)

    @tool
    def apply_device_chain(track: int | str, chain: str,
                           sidechain_from: int | str | None = None) -> str:
        """Add a ready-made chain of Live's devices to the end of a track
        (Live 12.3+). Starting points to adjust by ear, not presets:

        - vocal: low cut, mud dip, compression, soft saturation, air
        - drum_bus: Drum Buss + parallel Glue Compressor
        - bass: sub cleanup, compression, harmonics, mono below 120 Hz
        - pad: low cut, chorus, long reverb, wider image
        - master: subsonic cut, 2:1 glue, bass mono, true-peak limiter at -1 dB
        - sidechain_duck: compressor keyed from `sidechain_from` (e.g. the kick)
        Use track="master" for the master chain.
        """
        from . import device_recipes

        spec = device_recipes.recipe(chain)
        if spec.get("needs_sidechain") and sidechain_from is None:
            raise ValueError("This chain needs sidechain_from: the track that should duck "
                             "this one (usually the kick or drums)")
        tp = track_path(track)
        ids = inserted_device_ids(tp, [[name] for name, _settings in spec["devices"]])
        ops = []
        for (_name, settings), device_id in zip(spec["devices"], ids):
            names = resolve_parameter_names(f"id {device_id}", list(settings))
            ops += setting_ops(f"id {device_id}", settings, names)
        if sidechain_from is not None:
            source = lom_get(track_path(sidechain_from), ["name"])["name"]
            compressor = f"id {ids[0]}"
            ops.append(op_set(compressor, "input_routing_type", ref(
                f"{compressor} available_input_routing_types {shlex.quote(source)}")))
        applied = batch_summary(batch(ops, stop_on_error=False))
        return to_json({"chain": chain, "description": spec["description"],
                        "devices": [name for name, _settings in spec["devices"]],
                        "ok": applied["ok"], "settings": applied["steps"]})

    @tool(idempotent=True)
    def set_device_parameter_by_name(track: int | str, device: int | str,
                                     parameter: int | str, value: float | None = None,
                                     display_value: str | None = None) -> str:
        """Set a device parameter by name (or index), either as a raw value or as
        the text Live shows: display_value="800 Hz", "-6 dB", "35 %", "120 ms",
        "1.2 kHz", "25L", or a list item like "Lowpass". device="mixer" works
        with parameter "volume" | "pan" | "send A".
        The error message lists valid parameter names when one is not found.
        """
        if str(device).lower() not in ("mixer", "mixer_device") and isinstance(parameter, str) \
                and not parameter.strip().isdigit():
            parameter = resolve_parameter_names(device_path(track, device),
                                                [parameter])[parameter]
        pp = parameter_path(track, device, parameter)
        if display_value is not None:
            return to_json(send("param_set_display", {"path": pp, "value": display_value}))
        if value is None:
            info = lom_get(pp, ["name", "value", "min", "max", "is_quantized", "value_items"])
            return to_json(info)
        return to_json(send("lom_set", {"path": pp, "property": "value", "value": float(value)}))

    @tool(destructive=True)
    def rack_control(track: int | str, device: int | str, action: str = "info",
                     macro: int | str | None = None, value: float | None = None,
                     variation: int | None = None) -> str:
        """Instrument/Audio/MIDI/Drum Rack controls.

        action:
        - info: macros (name, value), chains, variations
        - set_macro: macro (1-16 or name) to value (0-127)
        - randomize_macros | add_macro | remove_macro
        - store_variation | recall_variation (variation index) | delete_variation
        """
        dp = device_path(track, device)
        action = action.lower()
        if action == "info":
            info = lom_get(dp, ["name", "class_name", "visible_macro_count",
                                "variation_count", "selected_variation_index", "chains"])
            params = lom_get(f"{dp} parameters", ["name", "value"])["items"]
            count = info.get("visible_macro_count") or 8
            info["macros"] = [{"macro": i, "name": p["name"], "value": p["value"]}
                              for i, p in enumerate(params[1:count + 1], start=1)]
            return to_json(info)
        if action == "set_macro":
            if macro is None or value is None:
                raise ValueError("macro and value are required")
            pp = f"{dp} parameters {int(macro)}" if str(macro).isdigit() \
                else f"{dp} parameters {selector(macro)}"
            return to_json(send("lom_set", {"path": pp, "property": "value",
                                            "value": float(value)}))
        if action == "recall_variation":
            if variation is None:
                raise ValueError("variation is required")
            return to_json(batch_summary(batch([
                op_set(dp, "selected_variation_index", int(variation)),
                op_call(dp, "recall_selected_variation")])))
        if action == "delete_variation":
            ops = []
            if variation is not None:
                ops.append(op_set(dp, "selected_variation_index", int(variation)))
            ops.append(op_call(dp, "delete_selected_variation"))
            return to_json(batch_summary(batch(ops)))
        if action in ("randomize_macros", "add_macro", "remove_macro", "store_variation"):
            return to_json(send("lom_call", {"path": dp, "method": action}))
        raise ValueError(f"Unknown action {action!r}")

    @tool(read_only=True, ui=ui.DRUMS)
    def get_drum_pads(track: int | str, device: int | str | None = None) -> str:
        """List the filled pads of a Drum Rack: MIDI note, note name, pad name,
        mute/solo. Without `device`, the first Drum Rack on the track is used.
        """
        if device is None:
            devices = lom_get(f"{track_path(track)} devices", ["class_name"])["items"]
            device = next((i for i, d in enumerate(devices)
                           if d.get("class_name") == "DrumGroupDevice"), None)
            if device is None:
                raise ValueError("No Drum Rack on this track")
        pads = lom_get(f"{device_path(track, device)} drum_pads",
                       ["note", "name", "mute", "solo", "chains"])["items"]
        filled = [{"note": p["note"], "note_name": music.note_name(p["note"]),
                   "name": p["name"], "mute": p["mute"], "solo": p["solo"]}
                  for p in pads if p.get("chains")]
        return to_json({"pads": filled, "auto_mapping": drum_mapping_from_rack(track)})

    # ── Browser ──────────────────────────────────────────────────────────────

    def search_browser(query: str, category: str = "all", max_results: int = 25,
                       time_limit: float = 20.0, within: list[str] | None = None) -> dict:
        roots = BROWSER_CATEGORIES.get(category.strip().lower().replace(" ", "_"))
        if roots is None:
            raise ValueError(f"Unknown category {category!r}. Known: "
                             f"{', '.join(BROWSER_CATEGORIES)}")
        words = query.lower().split()

        def rank(entry: dict) -> int:
            """0: every word in the name and one in its folders ("kick" in
            Drum Hits/Kick), 1: every word in the name, 2: matched via folders."""
            name = entry["name"].lower()
            if not all(w in name for w in words):
                return 2
            folders = entry["path"].lower().rsplit("/", 1)[0] if "/" in entry["path"] else ""
            return 0 if any(w in folders for w in words) else 1

        found: list[dict] = []
        walk_id, done, visited = None, False, 0
        deadline = time.monotonic() + time_limit
        while True:
            params: dict[str, Any] = ({"walk_id": walk_id} if walk_id is not None
                                      else {"query": query, "roots": roots,
                                            "within": within or []})
            # ~120 ms of Live's main thread per call, then Live gets a breath
            params.update(slice_ms=120, max_results=500)
            reply = send("browser_search", params)
            walk_id, done, visited = reply["walk_id"], reply["done"], reply["visited"]
            found += reply["results"]
            if done or sum(rank(e) == 0 for e in found) >= max_results \
                    or time.monotonic() > deadline:
                break
        if not done:
            try:
                send("browser_search", {"walk_id": walk_id, "cancel": True})
            except AbletonCommandError as e:  # the walk ended on its own meanwhile
                logger.debug("Browser walk %s already gone: %s", walk_id, e)
        ranked = sorted(found, key=rank)
        return {"query": query, "category": category, "complete": done, "searched": visited,
                "matches": len(found),
                "results": [{"id": e["$id"], "name": e["name"], "kind": e["kind"],
                             "path": f"{e['root']}/{e['path']}", "source": e["source"],
                             "uri": e["uri"]} for e in ranked[:max_results]]}

    def browser_item_id(item: int | str, category: str) -> tuple[int, str]:
        """(id, name) for a browser_search id or a search query."""
        if isinstance(item, int) or re.fullmatch(r"\d+", str(item).strip()):
            name = lom_get(f"id {int(item)}", ["name"]).get("name")
            return int(item), name if isinstance(name, str) else str(item)
        hits = search_browser(str(item), category, max_results=1)["results"]
        if not hits:
            raise ValueError(f"Nothing in the browser ({category}) matches {item!r}")
        return hits[0]["id"], hits[0]["path"]

    def drum_rack_path(track: int | str) -> str | None:
        devices = lom_get(f"{track_path(track)} devices", ["class_name"])["items"]
        index = next((i for i, d in enumerate(devices)
                      if d.get("class_name") == "DrumGroupDevice"), None)
        return None if index is None else f"{track_path(track)} devices {index}"

    def pad_note(pad: int | str) -> int:
        text = str(pad).strip().lower().replace(" ", "_")
        if text in music.GM_DRUMS:
            return music.GM_DRUMS[text]
        note = music.parse_note(pad)
        if not 0 <= note <= 127:
            raise ValueError(f"Pad note {note} is outside 0-127")
        return note

    def selected_track_ref() -> dict | None:
        selected = lom_get("live_set view", ["selected_track"]).get("selected_track")
        return ref(f"id {selected['$id']}") if isinstance(selected, dict) \
            and "$id" in selected else None

    def load_items(track: int | str, loads: list[tuple[int | None, int]]) -> dict:
        """Load browser items (by id) onto a track, or onto Drum Rack pads
        (note), keeping the user's track selection."""
        tp = track_path(track)
        previous = selected_track_ref()
        ops = [op_set("live_set view", "selected_track", ref(tp))]
        rack = None
        for note, item_id in loads:
            if note is not None:
                rack = rack or drum_rack_path(track)
                if rack is None:
                    raise ValueError("No Drum Rack on this track for drum_pad")
                ops.append(op_set(f"{rack} view", "selected_drum_pad",
                                  ref(f"{rack} drum_pads {note}")))
            ops.append(op_call("live_app browser", "load_item", ref(f"id {item_id}")))
        result = batch_summary(batch(ops, stop_on_error=False))
        if previous:
            try:
                send("lom_set", {"path": "live_set view", "property": "selected_track",
                                 "value": previous})
            except AbletonCommandError as e:  # that track was deleted meanwhile
                logger.debug("Could not reselect the previous track: %s", e)
        return result

    @tool(read_only=True)
    def browser_search(query: str, category: str = "all", max_results: int = 25) -> str:
        """Search Live's browser by name: presets, instruments, effects,
        plug-ins, Max devices, clips and samples (yours included).

        Every word must appear in the item's name or the folders above it, so
        "kick 808" finds 808 kicks and "pad warm" warm pads. category: all |
        sounds | drums (kits and one-shots in Drum Hits) | instruments |
        audio_effects | midi_effects | max_for_live | plugins | clips |
        samples | user_library | packs | current_project.
        Results carry an id for browser_load and browser_preview (valid until
        Live restarts) and the uri for load_instrument_or_effect.
        """
        return to_json(search_browser(query, category, max(1, min(int(max_results), 200))))

    @tool
    def browser_load(track: int | str, item: int | str, drum_pad: int | str | None = None,
                     category: str | None = None) -> str:
        """Load a browser item onto a track, as if double-clicked in Live's
        browser: an instrument, effect, preset, plug-in or a sample (which
        becomes a Simpler). item: an id from browser_search, or a search
        query (the best match is loaded). drum_pad: put a sample on that pad
        of the track's Drum Rack — MIDI note (36), note name ("C1") or role
        ("kick", "snare", "closed_hat"…). An instrument replaces the track's
        instrument, as in Live.
        """
        note = pad_note(drum_pad) if drum_pad is not None else None
        item_id, label = browser_item_id(
            item, category or ("drums" if note is not None else "all"))
        result = load_items(track, [(note, item_id)])
        if not result["ok"]:
            raise RuntimeError(f"Live could not load {label}: {result['steps']}")
        devices = lom_get(f"{track_path(track)} devices", ["name", "class_name"])["items"]
        return to_json({"loaded": label, "track": track, "drum_pad": note,
                        "devices": [d["name"] for d in devices]})

    @tool(read_only=True)
    def browser_preview(item: int | str | None = None, stop: bool = False,
                        category: str = "samples") -> str:
        """Let the user hear a sample (or a preset's preview) through Live's
        browser preview, without loading it. item: an id from browser_search
        or a search query. stop=True (or no item) stops the preview. Live's
        preview switch in the browser must be on.
        """
        if stop or item is None:
            send("lom_call", {"path": "live_app browser", "method": "stop_preview"})
            return to_json({"preview": "stopped"})
        item_id, label = browser_item_id(item, category)
        send("lom_call", {"path": "live_app browser", "method": "preview_item",
                          "args": [ref(f"id {item_id}")]})
        return to_json({"previewing": label})

    def drum_sample_score(hit: dict, folder: str, style: str, source: str) -> float:
        """Prefer clean one-shots named after their kind, then the asked source."""
        name = hit["name"].lower()
        score = 0.0
        if folder.split()[0].lower().rstrip("s") in name or (folder == "Hihat" and "hat" in name):
            score += 3
        if style and style.lower() in name:
            score += 2
        if re.search(r"\bbpm\b|\d+\s*bpm|loop|\bfill|\broll|build|reverse|\brev\b", name):
            score -= 6
        origin = (hit.get("source") or "").lower()
        if source == "core":
            score += 2 if "core" in origin else -4
        elif source == "user":
            score += 2 if "user" in origin else -4
        elif "core" in origin:
            score += 0.5
        return score

    @tool(ui=ui.DRUMS)
    def build_drum_kit(track: int | str, roles: list[str] | None = None,
                       sounds: dict[str, int | str] | None = None, style: str = "",
                       source: str = "any", seed: int | None = None,
                       replace: bool = False) -> str:
        """Build a Drum Rack from single samples: for each role a one-shot from
        Live's Drums › Drum Hits (your own samples included) goes onto its
        General MIDI pad, so generate_drum_pattern plays it right away.

        - roles: default kick, snare, clap, closed_hat, open_hat, rim, perc,
          shaker, crash, low_tom, high_tom (also: pedal_hat, mid_tom, ride, cowbell)
        - sounds: per-role override, a browser_search id or a query:
          {"kick": "808 long", "snare": 12345}
        - style: a word added to every search ("808", "909", "acoustic", "lofi");
          roles with no match fall back to any sample of that kind
        - source: any | core (Ableton's Core Library) | user (your own samples)
        - seed: picks among the best dozen matches for variety (default: the best)
        - replace: also refill pads that already hold a sound
        Adds a Drum Rack to the track if it has none (Live 12.3+).
        """
        import random

        roles = roles or DEFAULT_KIT
        if source not in ("any", "core", "user"):
            raise ValueError("source must be any, core or user")
        unknown = [r for r in roles if r not in DRUM_KIT_ROLES]
        if unknown:
            raise ValueError(f"Unknown roles {unknown}. Known: {', '.join(DRUM_KIT_ROLES)}")
        rack = drum_rack_path(track)
        if rack is None:
            inserted_device_ids(track_path(track), [["Drum Rack"]])
            rack = drum_rack_path(track)
        pads = lom_get(f"{rack} drum_pads", ["note", "chains"])["items"]
        filled = {p["note"] for p in pads if p.get("chains")}
        rng = random.Random(seed)
        plan, report, missing = [], [], []
        for role in roles:
            note = music.GM_DRUMS[role]
            if note in filled and not replace:
                report.append({"role": role, "note": note, "kept": "pad already filled"})
                continue
            override = (sounds or {}).get(role)
            if override is not None:
                item_id, label = browser_item_id(override, "drums")
                choice = {"id": item_id, "path": label}
            else:
                folder, words = DRUM_KIT_ROLES[role]
                queries = ([f"{style} {w}" for w in words] + [style] if style else []) \
                    + words + [""]
                choice = None
                for query in queries:
                    try:
                        found = search_browser(query, "drums", max_results=400,
                                               within=["Drum Hits", folder])["results"]
                    except AbletonCommandError:  # no such folder in this Live
                        break
                    hits = sorted((h for h in found if h["kind"] == "sample"),
                                  key=lambda h: -drum_sample_score(h, folder, style, source))
                    if hits:
                        choice = hits[0] if seed is None else rng.choice(hits[:12])
                        break
            if choice is None:
                missing.append(role)
                continue
            plan.append((note, choice["id"]))
            report.append({"role": role, "note": note, "pad": music.note_name(note),
                           "sample": choice["path"]})
        loaded = load_items(track, plan) if plan else {"ok": True, "steps": []}
        result = {"track": track, "pads": report, "ok": loaded["ok"]}
        if missing:
            result["no_match"] = missing
        if not loaded["ok"]:
            result["errors"] = [s for s in loaded["steps"] if isinstance(s, dict) and "error" in s]
        return to_json(result)

    # ── Snapshots (A/B) ──────────────────────────────────────────────────────

    def run_steps(steps: list[dict]) -> list[dict]:
        return batch(steps, stop_on_error=False)["results"] if steps else []

    def current_track_paths(names: list[str | None] | None = None,
                            fallback: list[str] | None = None) -> list[str]:
        """Every track, return and the master; or, given saved track names,
        where those tracks are now (by unique name, else their saved path)."""
        listing = run_steps([
            {"type": "lom_get", "params": {"path": "live_set tracks", "properties": ["name"],
                                           "ids": False}},
            {"type": "lom_get", "params": {"path": "live_set return_tracks",
                                           "properties": ["name"], "ids": False}}])
        paths, labels = [], []
        for collection, step in zip(("live_set tracks", "live_set return_tracks"), listing):
            for index, item in enumerate((step.get("result") or {}).get("items", [])):
                paths.append(f"{collection} {index}")
                labels.append(item.get("name"))
        paths.append("live_set master_track")
        labels.append(None)
        if names is None:
            return paths
        located = []
        for name, saved_path in zip(names, fallback or [None] * len(names)):
            if name and labels.count(name) == 1:
                located.append(paths[labels.index(name)])
            elif saved_path:
                located.append(saved_path)
        return located

    def with_displays(changes: list[dict], limit: int = 60) -> list[dict]:
        shown = changes[:limit]
        lookups = []
        for change in shown:
            if change["property"] == "value":
                for key in ("saved", "now"):
                    lookups.append(op_call(change["path"], "str_for_value", change[key]))
        results = iter(run_steps(lookups))
        rows = []
        for change in shown:
            row = {"track": change["track"], "where": change["where"],
                   "parameter": change["parameter"]}
            if change["property"] == "value":
                for key in ("saved", "now"):
                    step = next(results)
                    text = ((step.get("result") or {}).get("result")
                            if step.get("status") == "success" else None)
                    row[key] = text if isinstance(text, str) else round(change[key], 4)
            else:
                row["saved"], row["now"] = change["saved"], change["now"]
            rows.append(row)
        return rows

    @tool(ui=ui.SNAPSHOTS)
    def snapshot(action: str = "save", name: str = "", tracks: list[int | str] | None = None,
                 compare_to: str = "") -> str:
        """Save and restore how the set sounds: mixer (volume, pan, sends,
        mute) and every device parameter, devices inside racks included.

        action:
        - save: capture `tracks` (default: all tracks, returns and the master)
          under `name` (default: a timestamp)
        - restore: put the saved values back, as one undo step. Automated
          parameters are left alone (setting one would switch its automation off)
        - diff: what changed since the snapshot, with values as Live shows
          them; with compare_to, the difference between two snapshots
        - list | delete
        Use it to try ideas safely and to A/B: save "A", make changes, save
        "B", then restore "A" or "B" while the user listens. Tracks are matched
        by name, so snapshots survive adding or moving tracks.
        """
        from . import snapshots

        action = action.lower()
        if action == "list":
            return to_json({"snapshots": snapshots.list_all(),
                            "folder": str(snapshots.snapshot_dir())})
        if action == "delete":
            return to_json({"deleted": snapshots.delete(name), "name": name})
        if action == "save":
            name = name or time.strftime("snapshot %Y-%m-%d %H.%M.%S")
            paths = [track_path(t) for t in tracks] if tracks else current_track_paths()
            captured = snapshots.capture(run_steps, paths)
            snapshots.save(name, captured)
            return to_json({"saved": name, "tracks": [t["name"] for t in captured],
                            "parameters": sum(snapshots.count_parameters(t) for t in captured)})
        if action not in ("restore", "diff"):
            raise ValueError(f"Unknown action {action!r} (save, restore, diff, list, delete)")
        saved = snapshots.load(name)["tracks"]
        if tracks:
            wanted = {lom_get(track_path(t), ["name"])["name"] for t in tracks}
            saved = [t for t in saved if t.get("name") in wanted]
        if compare_to:
            if action == "restore":
                raise ValueError("compare_to only works with diff")
            current = snapshots.load(compare_to)["tracks"]
        else:
            current = snapshots.capture(run_steps, current_track_paths(
                [t.get("name") for t in saved], [t["path"] for t in saved]))
        report = snapshots.compare(saved, current)
        result: dict[str, Any] = {"snapshot": name, "changed": len(report["changes"])}
        if action == "diff":
            result["compared_to"] = compare_to or "the set now"
            result["changes"] = with_displays(report["changes"])
        else:
            applied = batch_summary(batch(snapshots.restore_ops(report["changes"]),
                                          stop_on_error=False)) if report["changes"] \
                else {"ok": True, "steps": []}
            result["restored"] = len(report["changes"]) - sum(
                1 for s in applied["steps"] if isinstance(s, dict) and "error" in s)
            result["errors"] = [s for s in applied["steps"] if isinstance(s, dict) and "error" in s]
        if report["automated"]:
            result["automated_not_restored" if action == "restore" else "automated"] = [
                f"{c['track']} / {c['where']} / {c['parameter']}" for c in report["automated"]]
        if report["missing"]:
            result["missing_now"] = report["missing"]
        return to_json(result)

    # ── Song structure ───────────────────────────────────────────────────────

    def session_clips() -> tuple[list[str], list[dict[int, dict]], list[str]]:
        """Track names, each track's Session clips by slot, and scene names."""
        listing = run_steps([
            {"type": "lom_get", "params": {"path": "live_set tracks", "properties": ["name"],
                                           "ids": False}},
            {"type": "lom_get", "params": {"path": "live_set scenes", "properties": ["name"],
                                           "ids": False}}])
        names = [t.get("name") for t in listing[0]["result"]["items"]]
        scenes = [s.get("name") for s in listing[1]["result"]["items"]]
        slots = run_steps([{"type": "lom_get", "params": {
            "path": f"live_set tracks {i} clip_slots", "properties": ["has_clip"], "ids": False}}
            for i in range(len(names))])
        wanted = [(i, k) for i, step in enumerate(slots)
                  for k, slot in enumerate((step.get("result") or {}).get("items", []))
                  if slot.get("has_clip")]
        details = run_steps([{"type": "lom_get", "params": {
            "path": f"live_set tracks {i} clip_slots {k} clip", "ids": False,
            "properties": ["name", "length", "loop_start", "is_midi_clip"]}}
            for i, k in wanted])
        clips: list[dict[int, dict]] = [{} for _ in names]
        for (i, k), step in zip(wanted, details):
            if step.get("status") == "success":
                clips[i][k] = step["result"]
        return names, clips, scenes

    @tool(destructive=True)
    def build_arrangement(sections: list[dict], start_bar: int = 1,
                          locators: bool = True) -> str:
        """Lay Session clips out in the Arrangement as a song, in one call.

        sections, in order: {"name": "Intro", "bars": 8, ...} plus what plays —
        - "scene": index or name: every track plays its clip from that scene;
          "except": ["Drums", "Bass"] leaves tracks out (breakdowns, intros)
        - "clips": {"Drums": 0, "Bass": "Bass B"}: per track a Session slot
          index or clip name; tracks not listed are silent
        A clip shorter than its section repeats. MIDI clips are cut exactly at
        the section end; an audio clip that does not fit a whole number of
        times runs over (the next section's clip on that track cuts it).
        locators: a named locator at each section start. Whatever was in the
        Arrangement in those bars is overwritten (one undo step).
        Example: [{"name": "Intro", "bars": 8, "scene": 0, "except": ["Drums"]},
        {"name": "Drop", "bars": 16, "scene": 1}, {"name": "Outro", "bars": 8,
        "clips": {"Pad": 0}}]
        """
        bar = song_context()["beats_per_bar"]
        names, clips, scenes = session_clips()

        def track_index(track: int | str) -> int:
            if isinstance(track, int) or re.fullmatch(r"-?\d+", str(track).strip()):
                index = int(track)
                return index + len(names) if index < 0 else index
            if track in names:
                return names.index(track)
            lowered = [str(n).lower() for n in names]
            if str(track).lower() in lowered:
                return lowered.index(str(track).lower())
            raise ValueError(f"No track named {track!r}. Tracks: {', '.join(map(str, names))}")

        def slot_of(index: int, clip: int | str) -> int:
            if isinstance(clip, int) or re.fullmatch(r"\d+", str(clip).strip()):
                if int(clip) not in clips[index]:
                    raise ValueError(f"Track {names[index]!r} has no clip in slot {clip}")
                return int(clip)
            for slot, info in clips[index].items():
                if info.get("name") == clip:
                    return slot
            raise ValueError(f"Track {names[index]!r} has no clip named {clip!r}")

        place, partial, report, warnings, cues = [], [], [], [], []
        position = (int(start_bar) - 1) * bar
        for number, section in enumerate(sections, start=1):
            label = str(section.get("name") or f"Section {number}")
            length = float(section.get("bars", 8)) * bar
            plays: dict[int, int] = {}
            if section.get("scene") is not None:
                row = section["scene"]
                row = int(row) if re.fullmatch(r"\d+", str(row).strip()) else (
                    scenes.index(row) if row in scenes else None)
                if row is None:
                    raise ValueError(f"{label}: no scene {section['scene']!r}")
                left_out = {track_index(t) for t in section.get("except") or []}
                plays = {i: row for i in range(len(names)) if row in clips[i]
                         and i not in left_out}
            for track, clip in (section.get("clips") or {}).items():
                if clip is None:
                    plays.pop(track_index(track), None)
                else:
                    plays[track_index(track)] = slot_of(track_index(track), clip)
            for i, slot in sorted(plays.items()):
                info = clips[i][slot]
                clip_length = float(info["length"]) or length
                repeats = int(length // clip_length + 1e-9)
                source = f"live_set tracks {i} clip_slots {slot} clip"
                for k in range(repeats):
                    place.append(op_call(f"live_set tracks {i}", "duplicate_clip_to_arrangement",
                                         ref(source), position + k * clip_length))
                rest = length - repeats * clip_length
                if rest > 1e-6:
                    at = position + repeats * clip_length
                    if info.get("is_midi_clip"):
                        partial.append((i, slot, at, rest, float(info.get("loop_start", 0.0))))
                    else:
                        place.append(op_call(f"live_set tracks {i}",
                                             "duplicate_clip_to_arrangement", ref(source), at))
                        warnings.append(f"{label}: {names[i]}'s audio clip runs "
                                        f"{clip_length - rest:g} beats past the section end")
            if locators:
                cues.append((label, position))
            report.append({"section": label, "bar": int(position // bar) + 1,
                           "bars": length / bar,
                           "tracks": {names[i]: clips[i][s].get("name") for i, s in
                                      sorted(plays.items())}})
            position += length
        placed = batch(place, stop_on_error=False) if place else {"results": []}
        errors = [s.get("message") for s in placed["results"] if s.get("status") == "error"]
        if cues:
            errors += [f"locator {c['name']!r}: {c['error']}" for c in place_locators(cues)
                       if "error" in c]
        if partial:
            created = run_steps([op_call(f"live_set tracks {i}", "create_midi_clip", at, rest)
                                 for i, _slot, at, rest, _start in partial])
            writes = []
            for (i, slot, at, rest, loop_start), step in zip(partial, created):
                new_clip = ((step.get("result") or {}).get("result") or {}).get("$id")
                if step.get("status") != "success" or new_clip is None:
                    errors.append(f"{names[i]} at beat {at:g}: {step.get('message')}")
                    continue
                notes = []
                for note in read_notes(f"live_set tracks {i} clip_slots {slot} clip").get(
                        "notes", []):
                    offset = note["start_time"] - loop_start
                    if 0 <= offset < rest:
                        notes.append(dict({k: v for k, v in note.items() if k != "note_id"},
                                          start_time=offset,
                                          duration=min(note["duration"], rest - offset)))
                writes.append({"type": "clip_write_notes", "params": {
                    "clip_path": f"id {new_clip}", "notes": notes, "mode": "replace"}})
                writes.append(op_set(f"id {new_clip}", "name", clips[i][slot].get("name") or ""))
            errors += [s.get("message") for s in run_steps(writes) if s.get("status") == "error"]
        result: dict[str, Any] = {"ok": not errors, "sections": report,
                                  "end_bar": int(position // bar) + 1,
                                  "time_signature": song_context()["time_signature"]}
        if warnings:
            result["warnings"] = warnings
        if errors:
            result["errors"] = errors[:20]
        return to_json(result)

    # ── Song, transport, view ────────────────────────────────────────────────

    @tool(idempotent=True)
    def set_song_properties(tempo: float | None = None, time_signature: str | None = None,
                            swing: float | None = None, metronome: bool | None = None,
                            loop: bool | None = None, loop_start: float | None = None,
                            loop_length: float | None = None, root_note: str | None = None,
                            scale_name: str | None = None, scale_mode: bool | None = None,
                            groove_amount: float | None = None,
                            clip_trigger_quantization: str | None = None,
                            midi_recording_quantization: str | None = None,
                            record_mode: bool | None = None,
                            session_record: bool | None = None,
                            arrangement_overdub: bool | None = None,
                            session_automation_record: bool | None = None) -> str:
        """Set global song settings in one round-trip.

        - time_signature: "7/8"; swing: 0..1 (global swing amount)
        - loop/loop_start/loop_length: Arrangement loop, in beats
        - root_note/scale_name/scale_mode: Live 12 key & scale ("D", "Dorian", True)
        - clip_trigger_quantization: none | 8 bars | 4 bars | 2 bars | 1 bar | 1/2 |
          1/2T | 1/4 | 1/4T | 1/8 | 1/8T | 1/16 | 1/16T | 1/32
        - midi_recording_quantization: none | 1/4 | 1/8 | 1/8T | 1/8+T | 1/16 |
          1/16T | 1/16+T | 1/32
        - record_mode: Arrangement record; session_record: Session record button
        """
        ops = []
        if tempo is not None:
            ops.append(op_set("live_set", "tempo", float(tempo)))
        if time_signature:
            num, den = time_signature.split("/")
            ops.append(op_set("live_set", "signature_numerator", int(num)))
            ops.append(op_set("live_set", "signature_denominator", int(den)))
        simple = {"swing_amount": swing, "metronome": metronome, "loop": loop,
                  "scale_name": scale_name, "scale_mode": scale_mode,
                  "groove_amount": groove_amount, "record_mode": record_mode,
                  "session_record": session_record, "arrangement_overdub": arrangement_overdub,
                  "session_automation_record": session_automation_record}
        if loop_start is not None:
            ops.append(op_set("live_set", "loop_start", float(loop_start)))
        if loop_length is not None:
            ops.append(op_set("live_set", "loop_length", float(loop_length)))
        for prop, value in simple.items():
            if value is not None:
                ops.append(op_set("live_set", prop, value))
        if root_note is not None:
            ops.append(op_set("live_set", "root_note", music.parse_pitch_class(root_note)))
        if clip_trigger_quantization is not None:
            ops.append(op_set("live_set", "clip_trigger_quantization",
                              SONG_QUANTIZATION[clip_trigger_quantization]))
        if midi_recording_quantization is not None:
            grid = {"none": 0, **NOTE_QUANTIZE_GRID}[midi_recording_quantization]
            ops.append(op_set("live_set", "midi_recording_quantization", grid))
        if not ops:
            return "Nothing to change"
        return to_json(batch_summary(batch(ops)))

    @tool(destructive=True)
    def transport(action: str, time: float | None = None, beats: float | None = None) -> str:
        """Transport and edit commands.

        action: play (from start marker) | continue | stop | stop_all_clips |
        jump_to (time in beats) | jump_by (beats, +/-) | next_cue | prev_cue |
        toggle_cue (add/remove a locator at the playhead) | tap_tempo |
        undo | redo | capture_midi | re_enable_automation | record (Arrangement
        record on + play) | stop_record
        """
        action = action.lower()
        simple_calls = {"play": "start_playing", "continue": "continue_playing",
                        "stop": "stop_playing", "stop_all_clips": "stop_all_clips",
                        "next_cue": "jump_to_next_cue", "prev_cue": "jump_to_prev_cue",
                        "toggle_cue": "set_or_delete_cue", "tap_tempo": "tap_tempo",
                        "undo": "undo", "redo": "redo", "capture_midi": "capture_midi",
                        "re_enable_automation": "re_enable_automation"}
        if action in simple_calls:
            return to_json(send("lom_call", {"path": "live_set",
                                             "method": simple_calls[action]}))
        if action == "jump_to":
            if time is None:
                raise ValueError("time is required")
            return to_json(send("lom_set", {"path": "live_set",
                                            "property": "current_song_time", "value": float(time)}))
        if action == "jump_by":
            if beats is None:
                raise ValueError("beats is required")
            return to_json(send("lom_call", {"path": "live_set", "method": "jump_by",
                                             "args": [float(beats)]}))
        if action == "record":
            return to_json(batch_summary(batch([op_set("live_set", "record_mode", True),
                                                op_call("live_set", "continue_playing")])))
        if action == "stop_record":
            return to_json(send("lom_set", {"path": "live_set", "property": "record_mode",
                                            "value": False}))
        raise ValueError(f"Unknown action {action!r}")

    @tool(idempotent=True)
    def select_in_live(view: str | None = None, track: int | str | None = None,
                       scene: int | str | None = None, clip_index: int | None = None,
                       device: int | str | None = None) -> str:
        """Change what the user sees: select a track/scene/clip slot/device and
        optionally show a view (session | arrangement | clip | device | browser).
        """
        ops = []
        if track is not None:
            ops.append(op_set("live_set view", "selected_track", ref(track_path(track))))
        if scene is not None:
            ops.append(op_set("live_set view", "selected_scene",
                              ref(f"live_set scenes {selector(scene)}")))
        if clip_index is not None and track is not None:
            ops.append(op_set("live_set view", "highlighted_clip_slot",
                              ref(f"{track_path(track)} clip_slots {int(clip_index)}")))
        if device is not None and track is not None:
            ops.append(op_call("live_set view", "select_device", ref(device_path(track, device))))
        if view is not None:
            ops.append(op_call("live_app view", "show_view", VIEWS[view.lower()]))
        if not ops:
            return to_json(lom_get("live_set view", ["selected_track", "selected_scene",
                                                     "detail_clip"]))
        return to_json(batch_summary(batch(ops, stop_on_error=False)))

    # ── Diagnostics ──────────────────────────────────────────────────────────

    @tool(read_only=True)
    def doctor() -> str:
        """Diagnose the setup when Live does not respond or tools fail: server
        and Remote Script versions, installed script copies (including an old
        copy inside the Live application that shadows the new one), the
        connection, dialogs open in Live, CPU load, and AbletonMCP errors in
        Live's log. Each problem comes with a fix. Changes nothing.
        """
        import os

        from . import doctor as diagnostics

        host = os.environ.get("ABLETON_HOST", "127.0.0.1")
        port = int(os.environ.get("ABLETON_PORT", "9877"))
        return to_json(diagnostics.run("127.0.0.1" if host == "localhost" else host, port))

    # ── Live events ──────────────────────────────────────────────────────────

    presets = {
        "transport": [("live_set", "is_playing"), ("live_set", "tempo"),
                      ("live_set", "record_mode"), ("live_set", "loop")],
        "selection": [("live_set view", "selected_track"), ("live_set view", "selected_scene"),
                      ("live_set view", "detail_clip")],
        "structure": [("live_set", "tracks"), ("live_set", "scenes"),
                      ("live_set", "return_tracks")],
    }

    @tool(read_only=True)
    def watch_live(path: str | None = None, property: str | None = None,
                   preset: str | None = None) -> str:
        """Start listening to changes the user makes in Live. Events are buffered
        in Live and read with get_live_events.

        Either path+property (any observable LOM property, e.g.
        path='live_set tracks "Bass" mixer_device volume', property="value";
        path="live_set tracks 0 clip_slots 0", property="has_clip"),
        or preset: transport | selection | structure.
        """
        targets = list(presets[preset]) if preset else []
        if path and property:
            targets.append((path, property))
        if not targets:
            raise ValueError("Give path+property or a preset")
        results = [send("observe", {"path": p, "property": prop}) for p, prop in targets]
        latest = send("poll_events", {"since": 10**12})["latest_seq"]
        return to_json({"watching": results, "events_since": latest})

    @tool(read_only=True)
    def unwatch_live(observer_id: int | str = "all") -> str:
        """Stop a watcher (id from watch_live) or all of them ("all")."""
        return to_json(send("unobserve", {"observer_id": observer_id}))

    @tool(read_only=True)
    async def get_live_events(since: int = 0, wait_seconds: float = 0.0,
                              limit: int = 200) -> str:
        """Read events from active watchers newer than `since` (pass the previous
        latest_seq). wait_seconds > 0 waits up to that long for something to
        happen (max 60). Also lists the active watchers.
        """
        deadline = time.monotonic() + max(0.0, min(60.0, wait_seconds))
        while True:
            # Remote Script 2.0 holds the request until an event arrives (in
            # short slices, so other tools are not kept waiting for long);
            # older scripts ignore "wait" and answer at once.
            wait = max(0.0, min(2.0, deadline - time.monotonic()))
            result = await asend("poll_events", {"since": since, "limit": limit, "wait": wait},
                                 timeout=wait + 15.0)
            if result["events"] or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.05 if wait else 0.25)
        result["observers"] = (await asend("list_observers"))["observers"]
        return to_json(result)

    # ── Audio analysis ───────────────────────────────────────────────────────

    @tool(read_only=True, ui=ui.ANALYSIS)
    async def analyze_audio_file(path: str, max_seconds: float = 600.0) -> str:
        """Analyze an audio file (wav, aiff, flac, mp3, ogg): integrated LUFS,
        true peak, loudness range, crest factor, clipping, tonal balance by
        band, spectral centroid, stereo correlation/width (incl. low end), tempo
        and key estimates, plus plain-language mix observations.
        """
        from .audio_analysis import analyze
        return to_json(await asyncio.to_thread(analyze, path, max_seconds))

    @tool(read_only=True, ui=ui.ANALYSIS)
    async def analyze_clip_audio(track: int | str, clip_index: int | None = None,
                                 arrangement_clip_index: int | None = None) -> str:
        """Analyze the audio file behind an audio clip (see analyze_audio_file)."""
        from .audio_analysis import analyze
        cp = clip_path(track, clip_index, arrangement_clip_index)
        info = await asend("lom_get", {"path": cp, "properties": ["is_audio_clip", "file_path",
                                                                   "name"]})
        if not info.get("is_audio_clip"):
            raise ValueError("Not an audio clip — use render_and_analyze to hear MIDI tracks")
        result = await asyncio.to_thread(analyze, info["file_path"])
        result["clip"] = info.get("name")
        return to_json(result)

    async def record_section(sources: list[tuple[str, str]], start_beat: float | None,
                             length_beats: float, keep_tracks: bool = False,
                             scene: int | str | None = None,
                             ctx: Context | None = None) -> dict:
        """Record several inputs at once, in one real-time pass.

        sources: (label, input) pairs, where input is "Resampling" (the master)
        or the name of a track or return as Live lists it among audio inputs.
        Each gets a temporary audio track, fed after the source's mixer.

        What plays: with `scene`, that Session scene is launched and recorded
        into fixed-length Session clips; without scene or start_beat, the
        Session clips already playing are recorded the same way; otherwise the
        Arrangement from start_beat (default: the loop start). Arm states and
        the metronome are restored (and loop, record mode and the playhead in
        the Arrangement); the temporary tracks are deleted unless keep_tracks.
        """
        song = await asend("lom_get", {"path": "live_set", "properties": [
            "tempo", "loop", "loop_start", "metronome", "record_mode", "is_playing",
            "current_song_time"]})
        tracks = (await asend("lom_get", {"path": "live_set tracks", "properties": [
            "name", "can_be_armed", "arm", "playing_slot_index"]}))["items"]
        session_playing = song["is_playing"] and any(
            isinstance(t.get("playing_slot_index"), int) and t["playing_slot_index"] >= 0
            for t in tracks)
        session = scene is not None or (start_beat is None and session_playing)
        scene_index = (await asyncio.to_thread(index_in, "live_set scenes", scene)
                       if scene is not None else None)
        if song["is_playing"] and not session:
            await asend("lom_call", {"path": "live_set", "method": "stop_playing"})
        start = float(start_beat if start_beat is not None else song["loop_start"])
        # Tracks are addressed by id from here on: the user may add or remove
        # tracks while the section plays, which would shift every index.
        armed = [t["$id"] for t in tracks if t.get("can_be_armed") and t.get("arm") is True]
        # In the Session, record into the launched scene's row: a scene launch
        # may itself start recording on armed tracks' empty slots there
        slot = scene_index or 0
        new = "live_set tracks -1"
        temp: dict[str, int] = {}
        try:
            created = await asend("batch", {"commands": [
                step for _source in sources for step in (
                    op_call("live_set", "create_audio_track", -1),
                    {"type": "lom_get", "params": {"path": new, "properties": ["name"]}})],
                "stop_on_error": False})
            steps = created.get("results", [])
            for k, (label, _input) in enumerate(sources):
                got = steps[2 * k + 1] if len(steps) > 2 * k + 1 else {}
                if got.get("status") == "success":
                    temp[label] = got["result"]["$id"]
            if len(temp) < len(sources):
                raise RuntimeError("Render setup failed: could not create a track for "
                                   + ", ".join(lab for lab, _ in sources if lab not in temp))
            # Live fills a new track's input choices only after the call that
            # created it, and only for the first track created in a call, so
            # inputs are set in a separate call (and retried).
            pending = list(sources)
            problems: list[str] = []
            for attempt in range(3):
                if attempt:
                    await asyncio.sleep(0.25)
                ops = []
                for label, input_name in pending:
                    track = f"id {temp[label]}"
                    ops += [op_set(track, "input_routing_type", ref(
                                f"{track} available_input_routing_types "
                                f"{shlex.quote(input_name)}")),
                            op_set(track, "name", f"MCP Rec {label}"[:48]),
                            op_set(track, "current_monitoring_state", MONITORING["off"])]
                    if input_name != "Resampling":
                        # What the source sends on: after its devices, fader and pan
                        ops.append(op_set(track, "input_routing_channel", ref(
                            f'{track} available_input_routing_channels "Post Mixer"')))
                result = await asend("batch", {"commands": ops, "stop_on_error": False})
                steps = result.get("results", [])
                position, failed, problems = 0, [], []
                for label, input_name in pending:
                    step = steps[position] if len(steps) > position else {}
                    if step.get("status") != "success":
                        failed.append((label, input_name))
                        problems.append(f"{label} ({input_name}): "
                                        f"{step.get('message', 'not run')}")
                    position += 3 if input_name == "Resampling" else 4
                pending = failed
                if not pending:
                    break
            if problems:
                raise RuntimeError("Render setup failed: " + "; ".join(problems))
            begin = [op_set(f"id {i}", "arm", False) for i in armed]
            begin += [op_set(f"id {i}", "arm", True) for i in temp.values()]
            begin.append(op_set("live_set", "metronome", False))
            if session:
                if scene_index is not None:
                    begin.append(op_call(f"live_set scenes {scene_index}", "fire"))
                # Fixed-length Session recordings, starting on the next launch
                # quantization boundary together with the scene
                begin += [op_call(f"id {i} clip_slots {slot}", "fire", float(length_beats))
                          for i in temp.values()]
            else:
                begin += [op_set("live_set", "loop", False),
                          op_set("live_set", "current_song_time", start),
                          op_set("live_set", "record_mode", True),
                          op_call("live_set", "continue_playing")]
            seconds = length_beats * 60.0 / float(song["tempo"])
            try:
                started = await asend("batch", {"commands": begin})
                if started.get("failed"):
                    raise RuntimeError("Could not start recording: "
                                       + to_json(batch_summary(started)))
                # Session recordings wait for the next launch boundary (≤ 8 bars)
                began = time.monotonic()
                deadline = began + seconds + (32 * 60.0 / float(song["tempo"])
                                              if session else 0) + 30.0
                label = f"Recording {len(sources)} part{'s' if len(sources) > 1 else ''}"
                while time.monotonic() < deadline:
                    await asyncio.sleep(0.2)
                    elapsed = time.monotonic() - began
                    await report_progress(ctx, min(elapsed, seconds), seconds,
                                          f"{label}: {elapsed:.0f} of {seconds:.0f} s")
                    if session:
                        polled = await asend("batch", {"commands": [
                            {"type": "lom_get", "params": {
                                "path": f"id {i} clip_slots {slot} clip",
                                "properties": ["is_recording"]}} for i in temp.values()],
                            "stop_on_error": False})
                        if all(s.get("status") == "success"
                               and s["result"].get("is_recording") is False
                               for s in polled["results"]):
                            break
                    else:
                        now = await asend("lom_get", {"path": "live_set",
                                                      "properties": ["current_song_time"]})
                        if now["current_song_time"] >= start + length_beats:
                            break
            finally:
                restore = [op_set(f"id {i}", "arm", False) for i in temp.values()]
                restore += [op_set(f"id {i}", "arm", True) for i in armed]
                restore.append(op_set("live_set", "metronome", song["metronome"]))
                if not session or not song["is_playing"]:
                    restore.append(op_call("live_set", "stop_playing"))
                if not session:
                    restore += [op_set("live_set", "record_mode", song["record_mode"]),
                                op_set("live_set", "loop", song["loop"])]
                if not session or not song["is_playing"]:
                    restore.append(op_set("live_set", "current_song_time",
                                          float(song["current_song_time"])))
                await asend("batch", {"commands": restore, "stop_on_error": False})
            files = {label: await recorded_file(f"id {track_id}",
                                                slot if session else None)
                     for label, track_id in temp.items()}
        finally:
            if not keep_tracks:
                for track_id in temp.values():
                    await delete_track_by_id(track_id)
        section: dict[str, Any] = {"mode": "session" if session else "arrangement",
                                   "length_beats": length_beats, "seconds": round(seconds, 2)}
        if session:
            section["scene"] = scene_index if scene_index is not None else "playing clips"
        else:
            section["start_beat"] = start
        return {"files": files, "section": section}

    async def stem_sources(tracks: list[int | str] | None,
                           include_master: bool) -> list[tuple[str, str]]:
        """(label, input name) for each part to record. By default every
        unmuted, non-group track with audio output and every return."""
        if tracks:
            sources = []
            for track in tracks:
                path = track_path(track)
                name = (await asend("lom_get", {"path": path, "properties": ["name"]}))["name"]
                sources.append(("Master", "Resampling") if path == "live_set master_track"
                               else (name, name))
        else:
            listing = await asend("batch", {"commands": [
                {"type": "lom_get", "params": {
                    "path": "live_set tracks", "ids": False,
                    "properties": ["name", "has_audio_output", "is_foldable", "mute"]}},
                {"type": "lom_get", "params": {"path": "live_set return_tracks", "ids": False,
                                               "properties": ["name", "mute"]}}]})
            normal, returns = (step["result"]["items"] for step in listing["results"])
            sources = [(t["name"], t["name"]) for t in normal
                       if t.get("has_audio_output") is True and not t.get("is_foldable")
                       and not t.get("mute")]
            sources += [(t["name"], t["name"]) for t in returns if not t.get("mute")]
        if include_master and ("Master", "Resampling") not in sources:
            sources.append(("Master", "Resampling"))
        labels = [label for label, _ in sources]
        if len(set(labels)) != len(labels):
            raise ValueError("Two tracks share a name; rename them so their outputs can be "
                             "told apart: " + ", ".join(sorted({x for x in labels
                                                               if labels.count(x) > 1})))
        if len(sources) > 24:
            raise ValueError(f"{len(sources)} parts is too many for one pass; pass `tracks` "
                             "to choose up to 24")
        return sources

    @tool(ui=ui.ANALYSIS)
    async def render_and_analyze(start_beat: float | None = None,
                                 length_beats: float = 16.0, source: str = "master",
                                 keep_track: bool = False,
                                 scene: int | str | None = None,
                                 ctx: Context | None = None) -> str:
        """Listen to the set: record a section in real time and analyze it.

        Records `length_beats` of the master output ("master", via Resampling)
        or of one track (track name) on a temporary audio track, then runs
        analyze_audio_file on the recording. What plays:
        - scene: launch this Session scene (index or name) and record it
        - neither scene nor start_beat while Session clips play: record those
        - otherwise the Arrangement from start_beat (default: the loop start)
        Takes as long as the section plays. Arm states, metronome, loop and
        record mode are restored; the temporary track is deleted unless
        keep_track. Undo history will contain the recording steps.
        """
        from .audio_analysis import analyze

        input_name = "Resampling" if source.lower() == "master" else source
        recorded = await record_section([(source, input_name)], start_beat, length_beats,
                                        keep_track, scene, ctx)
        analysis = await asyncio.to_thread(analyze, recorded["files"][source])
        analysis["render"] = dict(recorded["section"], source=source, track_kept=keep_track)
        return to_json(analysis)

    @tool(ui=ui.MIX)
    async def capture_stems(tracks: list[int | str] | None = None,
                            start_beat: float | None = None, length_beats: float = 16.0,
                            include_master: bool = True,
                            scene: int | str | None = None,
                            ctx: Context | None = None) -> str:
        """Record several tracks at once — one real-time pass of the section,
        each on its own temporary audio track fed from that track's output
        (after its devices, fader and pan) — and summarise each stem:
        loudness, peak, energy per band, stereo correlation.

        - tracks: names/indexes (also "master" and returns); default every
          unmuted non-group track with audio output, every return, the master
        - start_beat / length_beats / scene: what plays, as in render_and_analyze
        Takes as long as the section plays. The temporary tracks are deleted;
        the recorded files stay in the project and are listed.
        """
        from .audio_analysis import load_audio
        from .mix_analysis import stem_summary

        sources = await stem_sources(tracks, include_master)
        recorded = await record_section(sources, start_beat, length_beats, scene=scene,
                                        ctx=ctx)

        def summarise() -> dict:
            return {label: dict(stem_summary(*load_audio(path)), file=path)
                    for label, path in recorded["files"].items()}

        stems = await asyncio.to_thread(summarise)
        return to_json({"section": recorded["section"], "stems": stems})

    @tool(ui=ui.MIX)
    async def mix_doctor(start_beat: float | None = None, length_beats: float = 16.0,
                         tracks: list[int | str] | None = None,
                         platform: str = "spotify",
                         scene: int | str | None = None,
                         ctx: Context | None = None) -> str:
        """Diagnose the mix of a section, the way an engineer listening to
        solos would. Records every track and the master in one real-time pass
        (see capture_stems; start_beat / scene choose what plays), then reports:
        - the master: loudness, true peak, dynamics and tonal balance against
          `platform`'s target (spotify, apple_music, youtube, tidal, …, club)
        - masking: which parts fight for the same frequencies at the same
          time, in which band, how often, and what to do about it
        - per stem: stray low end, phase problems, overs, level and main band
        Suggestions only: nothing in the set is changed. Save a snapshot
        before applying fixes, then compare.
        """
        from .audio_analysis import analyze, load_audio
        from .mix_analysis import masking_report, stem_findings, stem_summary
        from .prompts import LOUDNESS_TARGETS

        sources = await stem_sources(tracks, include_master=True)
        recorded = await record_section(sources, start_beat, length_beats, scene=scene,
                                        ctx=ctx)
        files = recorded["files"]

        def diagnose() -> dict:
            master = analyze(files["Master"])
            audio = {label: load_audio(path) for label, path in files.items()
                     if label != "Master"}
            stems = {label: stem_summary(*pair) for label, pair in audio.items()}
            masking = masking_report({label: pair for label, pair in audio.items()
                                      if stems[label]["lufs"] > -60})
            return {"master": master, "stems": stems, "masking": masking,
                    "stem_notes": stem_findings(stems)}

        found = await asyncio.to_thread(diagnose)
        master = found["master"]
        key = platform.strip().lower().replace(" ", "_")
        target_text = LOUDNESS_TARGETS.get(key, LOUDNESS_TARGETS["spotify"])[0]
        findings = list(master["observations"])
        target = re.search(r"-?\d+(?:\.\d+)?", target_text)
        if target and master["integrated_lufs"] > -60:
            gap = master["integrated_lufs"] - float(target.group())
            if abs(gap) >= 2:
                findings.append(f"Master is {abs(gap):.1f} LU {'above' if gap > 0 else 'below'}"
                                f" the {key} target ({target_text}); in this section.")
        findings += [p["advice"] for p in found["masking"]["pairs"] if p["severity"] >= 0.4]
        findings += found["stem_notes"]
        return to_json({
            "section": recorded["section"],
            "findings": findings or ["No clear problems in this section."],
            "master": {k: master[k] for k in ("integrated_lufs", "true_peak_dbtp",
                                              "loudness_range_lu", "crest_factor_db",
                                              "band_energy_percent", "stereo")},
            "masking": found["masking"]["pairs"],
            "stems": {label: {k: s[k] for k in ("lufs", "peak_dbfs", "main_band")}
                      for label, s in found["stems"].items()},
        })

    @tool(ui=ui.REFERENCE)
    async def compare_to_reference(reference_path: str, mix_path: str | None = None,
                                   start_beat: float | None = None,
                                   length_beats: float = 16.0,
                                   whole_reference: bool = False,
                                   scene: int | str | None = None,
                                   ctx: Context | None = None) -> str:
        """Compare the mix with a reference track: loudness, true peak,
        dynamics (crest factor, loudness range), stereo width and tonal balance
        per 1/3 octave after matching levels — plus EQ Eight settings that move
        the mix about 60% of the way (apply them with add_device on the master
        or a bus, ideally after saving a snapshot).

        The mix is mix_path, or else a real-time recording of the master
        (start_beat / scene / length_beats as in render_and_analyze). Unless whole_reference, the
        reference's loudest stretch of the same length is used, so a drop is
        compared with a drop.
        """
        from .audio_analysis import load_audio
        from .mix_analysis import reference_report

        reference = Path(reference_path).expanduser()
        if not reference.is_file():
            raise ValueError(f"Reference file not found: {reference}")
        section = None
        if mix_path is None:
            recorded = await record_section([("Master", "Resampling")], start_beat,
                                            length_beats, scene=scene, ctx=ctx)
            mix_path = recorded["files"]["Master"]
            section = recorded["section"]

        def compare() -> dict:
            return reference_report(load_audio(mix_path), load_audio(reference),
                                    match_section=not whole_reference)

        report = await asyncio.to_thread(compare)
        report["mix"] = section or {"file": str(Path(mix_path).expanduser())}
        report["reference"] = str(reference)
        return to_json(report)

    @tool(ui=ui.LEVELS)
    async def measure_levels(seconds: float = 8.0, tracks: list[int | str] | None = None,
                             start_beat: float | None = None, play: bool = True,
                             ctx: Context | None = None) -> str:
        """Gain staging: meter the set while it plays and report the peak and
        average level of every track, return and the master in dBFS, read
        from Live's own meters. Nothing is recorded or written, so it is much
        quicker than render_and_analyze (but measures level only, no spectrum).

        - seconds: how long to listen (1-60)
        - tracks: names/indexes to meter (default: every track with audio
          output, the returns and the master)
        - play: start playback (from start_beat, default where the playhead
          is) and stop it again afterwards; False meters whatever is playing
        Live's meters show smoothed peaks, -70 dB to +6 dB.
        """
        seconds = max(1.0, min(60.0, float(seconds)))
        targets = await meter_targets(tracks)
        channels = [(label, path, prop) for label, path in targets
                    for prop in ("output_meter_left", "output_meter_right")]
        started = await asend("batch", {"commands": [
            {"type": "observe", "params": {"path": path, "property": prop}}
            for _label, path, prop in channels], "stop_on_error": False})
        meters: dict[int, dict] = {}
        for (label, _path, _prop), step in zip(channels, started["results"]):
            if step["status"] == "success":
                info = step["result"]
                meters[info["observer_id"]] = {
                    "track": label, "ours": not info.get("already_observed"),
                    "readings": [(None, float(info.get("value") or 0.0))]}
        since = (await asend("poll_events", {"since": 10 ** 12}))["latest_seq"]
        song = await asend("lom_get", {"path": "live_set", "properties": [
            "is_playing", "current_song_time"]})
        we_started = play and not song["is_playing"]
        missed = False
        try:
            if we_started:
                ops = [op_call("live_set", "continue_playing")]
                if start_beat is not None:
                    ops.insert(0, op_set("live_set", "current_song_time", float(start_beat)))
                await asend("batch", {"commands": ops})
            deadline = time.monotonic() + seconds
            while True:
                await asyncio.sleep(0.25)
                left = max(0.0, deadline - time.monotonic())
                await report_progress(ctx, seconds - left, seconds,
                                      f"Metering: {seconds - left:.0f} of {seconds:.0f} s")
                polled = await asend("poll_events", {"since": since, "limit": 2000})
                missed = missed or polled.get("missed", False)
                for event in polled["events"]:
                    meter = meters.get(event.get("observer_id"))
                    if meter is not None and isinstance(event.get("value"), (int, float)):
                        meter["readings"].append((event["ts"], float(event["value"])))
                if polled["events"]:
                    since = polled["events"][-1]["seq"]
                if time.monotonic() >= deadline and not polled.get("more"):
                    break
        finally:
            ours = [oid for oid, meter in meters.items() if meter["ours"]]
            cleanup = [{"type": "unobserve", "params": {"observer_id": oid}} for oid in ours]
            if we_started:
                cleanup += [op_call("live_set", "stop_playing"),
                            op_set("live_set", "current_song_time",
                                   float(song["current_song_time"]))]
            if cleanup:
                await asend("batch", {"commands": cleanup, "stop_on_error": False})
        from .audio_analysis import level_report

        report = level_report(meters)
        if missed:
            report["warning"] = "Some meter readings were dropped; levels may be approximate"
        report["seconds"] = seconds
        return to_json(report)

    async def meter_targets(tracks: list[int | str] | None) -> list[tuple[str, str]]:
        """(label, "id N" path) of each track to meter."""
        if tracks:
            found = []
            for track in tracks:
                path = track_path(track)
                info = await asend("lom_get", {"path": path, "properties": ["name"]})
                # Live 12 names the master "Main"; reports call it Master
                label = "Master" if path == "live_set master_track" else info["name"]
                found.append((label, f"id {info['$id']}"))
            return found
        listing = await asend("batch", {"commands": [
            {"type": "lom_get", "params": {"path": f"live_set {kind}",
                                           "properties": ["name", "has_audio_output"]}}
            for kind in ("tracks", "return_tracks")] + [
            {"type": "lom_get", "params": {"path": "live_set master_track",
                                           "properties": ["name"]}}]})
        tracks_info, returns_info, master = (step["result"] for step in listing["results"])
        found = [(t["name"], f"id {t['$id']}") for t in tracks_info["items"]
                 if t.get("has_audio_output") is True]
        found += [(t["name"], f"id {t['$id']}") for t in returns_info["items"]]
        found.append(("Master", f"id {master['$id']}"))
        return found

    async def recorded_file(track: str, slot: int | None = None) -> str:
        """Path of the newest recording on `track` (its Arrangement, or Session
        clip slot `slot`), once Live has finished writing it."""
        for _ in range(50):
            if slot is None:
                clips = (await asend("lom_get", {"path": f"{track} arrangement_clips",
                                                 "properties": ["file_path"]}))["items"]
            else:
                clips = [await asend("lom_get", {"path": f"{track} clip_slots {slot} clip",
                                                 "properties": ["file_path"]})]
            if clips and clips[-1].get("file_path"):
                candidate = Path(clips[-1]["file_path"])
                if candidate.is_file() and candidate.stat().st_size > 0:
                    size = candidate.stat().st_size
                    await asyncio.sleep(0.3)
                    if candidate.stat().st_size == size:
                        return str(candidate)
            await asyncio.sleep(0.2)
        raise RuntimeError("Recording finished but no audio file was found on the "
                           "MCP Render track; check that its input could be set.")

    async def delete_track_by_id(track_id: int) -> None:
        items = (await asend("lom_get", {"path": "live_set tracks",
                                         "properties": ["name"]}))["items"]
        index = next((i for i, t in enumerate(items) if t.get("$id") == track_id), None)
        if index is not None:
            await asend("lom_call", {"path": "live_set", "method": "delete_track",
                                     "args": [index]})

    # ── Resources ────────────────────────────────────────────────────────────

    @mcp.resource("ableton://session/overview", mime_type="application/json")
    def session_overview_resource() -> str:
        """Compact live map of the current Ableton set."""
        return to_json(send("get_session_overview"))

    @mcp.resource("ableton://events", mime_type="application/json")
    def events_resource() -> str:
        """Buffered events from active Live watchers (see watch_live)."""
        return to_json(send("poll_events", {"since": 0}))
