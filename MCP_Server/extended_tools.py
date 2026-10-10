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

from . import music
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

_NEW_COMMANDS = {"lom_get", "lom_set", "lom_call", "batch", "observe", "unobserve",
                 "list_observers", "poll_events", "clip_get_notes", "clip_write_notes",
                 "clip_write_envelope", "param_set_display", "get_session_overview"}


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


def setting_ops(device: str, settings: dict[str, Any]) -> list[dict]:
    """Batch steps setting device parameters: text by display, numbers raw."""
    ops = []
    for name, value in settings.items():
        parameter = f"{device} parameters {selector(name)}"
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

    @tool(read_only=True)
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

    @tool(read_only=True)
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

    @tool(destructive=True)
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
        if chosen and all("note_id" in n for n in notes):
            if op in ("dedupe", "delete"):
                survivors = {n["note_id"] for n in changed}
                doomed = [n["note_id"] for n in chosen if n["note_id"] not in survivors]
                if doomed:
                    write_notes(cp, [], "remove_ids", note_ids=doomed)
                summary["notes_removed"] = len(doomed)
            else:
                result = write_notes(cp, changed, "modify")
                summary["notes_modified"] = result.get("modified")
            summary["notes_in_clip"] = len(notes) - summary.get("notes_removed", 0)
            return to_json(summary)
        # No note ids (Live 10): rewrite the clip
        chosen_ids = {id(n) for n in chosen}
        final = [n for n in notes if id(n) not in chosen_ids] + changed
        for n in final:
            n.pop("note_id", None)
        result = write_notes(cp, final, "replace")
        summary["notes_in_clip"] = result.get("written")
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

    @tool(destructive=True)
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

    @tool(destructive=True)
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

    @tool(destructive=True)
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

    @tool(destructive=True)
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

    @tool(destructive=True)
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
            result["settings"] = batch_summary(batch(setting_ops(f"id {device_id}", settings),
                                                     stop_on_error=False))
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
            ops += setting_ops(f"id {device_id}", settings)
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

    @tool(read_only=True)
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

    @tool(read_only=True)
    async def analyze_audio_file(path: str, max_seconds: float = 600.0) -> str:
        """Analyze an audio file (wav, aiff, flac, mp3, ogg): integrated LUFS,
        true peak, loudness range, crest factor, clipping, tonal balance by
        band, spectral centroid, stereo correlation/width (incl. low end), tempo
        and key estimates, plus plain-language mix observations.
        """
        from .audio_analysis import analyze
        return to_json(await asyncio.to_thread(analyze, path, max_seconds))

    @tool(read_only=True)
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

    @tool
    async def render_and_analyze(start_beat: float | None = None,
                                 length_beats: float = 16.0, source: str = "master",
                                 keep_track: bool = False) -> str:
        """Listen to the set: record a section in real time and analyze it.

        Records `length_beats` from `start_beat` (default: Arrangement loop
        start) of the master output ("master", via Resampling) or of one track
        (track name), on a temporary audio track in the Arrangement, then runs
        analyze_audio_file on the recording. Takes as long as the section
        plays. Other tracks' arm states, loop, metronome and record mode are
        restored afterwards; the temporary track is deleted unless keep_track.
        Undo history will contain the recording steps.
        """
        from .audio_analysis import analyze

        song = await asend("lom_get", {"path": "live_set", "properties": [
            "tempo", "loop", "loop_start", "metronome", "record_mode", "is_playing",
            "current_song_time"]})
        if song["is_playing"]:
            await asend("lom_call", {"path": "live_set", "method": "stop_playing"})
        start = float(start_beat if start_beat is not None else song["loop_start"])
        tracks = (await asend("lom_get", {"path": "live_set tracks",
                                          "properties": ["name", "can_be_armed", "arm"]}))["items"]
        # Tracks are addressed by id from here on: the user may add or remove
        # tracks while the section plays, which would shift every index.
        armed = [t["$id"] for t in tracks if t.get("can_be_armed") and t.get("arm") is True]
        input_name = "Resampling" if source.lower() == "master" else source
        new = "live_set tracks -1"
        setup = [op_call("live_set", "create_audio_track", -1),
                 {"type": "lom_get", "params": {"path": new, "properties": ["name"]}},
                 op_set(new, "name", "MCP Render"),
                 op_set(new, "input_routing_type",
                        ref(f"{new} available_input_routing_types {shlex.quote(input_name)}")),
                 op_set(new, "current_monitoring_state", MONITORING["off"])]
        setup += [op_set(f"id {i}", "arm", False) for i in armed]
        setup += [op_set(new, "arm", True), op_set("live_set", "loop", False),
                  op_set("live_set", "metronome", False),
                  op_set("live_set", "current_song_time", start),
                  op_set("live_set", "record_mode", True),
                  op_call("live_set", "continue_playing")]
        render_id = None
        try:
            result = await asend("batch", {"commands": setup})
            steps = result.get("results", [])
            if len(steps) > 1 and steps[1].get("status") == "success":
                render_id = steps[1]["result"]["$id"]
            try:
                if result.get("failed"):
                    raise RuntimeError("Render setup failed: "
                                       + to_json(batch_summary(result)))
                seconds = length_beats * 60.0 / float(song["tempo"])
                deadline = time.monotonic() + seconds + 30.0
                while time.monotonic() < deadline:
                    await asyncio.sleep(0.2)
                    now = await asend("lom_get", {"path": "live_set",
                                                  "properties": ["current_song_time"]})
                    if now["current_song_time"] >= start + length_beats:
                        break
            finally:
                restore = [op_call("live_set", "stop_playing"),
                           op_set("live_set", "record_mode", song["record_mode"])]
                if render_id is not None:
                    restore.append(op_set(f"id {render_id}", "arm", False))
                restore += [op_set(f"id {i}", "arm", True) for i in armed]
                restore += [op_set("live_set", "loop", song["loop"]),
                            op_set("live_set", "metronome", song["metronome"]),
                            op_set("live_set", "current_song_time",
                                   float(song["current_song_time"]))]
                await asend("batch", {"commands": restore, "stop_on_error": False})

            file_path = await recorded_file(f"id {render_id}")
            analysis = await asyncio.to_thread(analyze, file_path)
        finally:
            if render_id is not None and not keep_track:
                await delete_track_by_id(render_id)
        analysis["render"] = {"source": source, "start_beat": start,
                              "length_beats": length_beats, "track_kept": keep_track}
        return to_json(analysis)

    @tool
    async def measure_levels(seconds: float = 8.0, tracks: list[int | str] | None = None,
                             start_beat: float | None = None, play: bool = True) -> str:
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

    async def recorded_file(track: str) -> str:
        """Path of the newest recording on `track`, once Live has finished writing it."""
        for _ in range(50):
            clips = (await asend("lom_get", {"path": f"{track} arrangement_clips",
                                             "properties": ["file_path"]}))["items"]
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
