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
import contextlib
import contextvars
import functools
import json
import logging
import os
import re
import shlex
import tempfile
import time
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

from mcp.server.elicitation import AcceptedElicitation, ElicitationResult
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.resolve import Elicit, Resolve

from pydantic import BaseModel, Field

from . import journal, memory as notes_store, music, transitions
from .connection import AbletonCommandError
from .ui import panels as ui
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

# True while a tool runs with preview=True (see previewable): no clip is
# created, no note written, and any other change to the set is refused.
PREVIEW: contextvars.ContextVar[bool] = contextvars.ContextVar("abletonmcp_preview",
                                                                default=False)


def send(command: str, params: dict | None = None, timeout: float | None = None) -> Any:
    if _get_connection is None:
        raise RuntimeError("extended tools not registered")
    if PREVIEW.get() and not journal.is_read_only(command, params):
        raise RuntimeError(f"A preview must not change the set (it tried {command})")
    try:
        if timeout is None:
            return _get_connection().send_command(command, params or {})
        return _get_connection().send_command(command, params or {}, timeout=timeout)
    except Exception as e:
        if command in _NEW_COMMANDS and "Unknown command" in str(e):
            raise RuntimeError(
                "The AbletonMCP Remote Script loaded in Live is older than this server "
                "(missing '%s'). Call install_remote_script (or run "
                "`ableton-mcp-install-script`), then restart Live." % command
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


# Batch commands whose params must name a LOM path
PATH_COMMANDS = {"lom_get", "lom_set", "lom_call", "param_set_display", "observe"}


def check_batch(commands: Any) -> None:
    """Refuse a malformed batch before anything runs, naming the step."""
    if not isinstance(commands, list):
        raise ValueError("commands must be a list of {\"type\": ..., \"params\": {...}}")
    for index, command in enumerate(commands):
        where = f"Step {index}"
        if not isinstance(command, dict) or not isinstance(command.get("type"), str) \
                or not command["type"]:
            raise ValueError(f"{where}: needs {{\"type\": <command name>, \"params\": {{...}}}}, "
                             f"got {json.dumps(command)[:120]}")
        params = command.get("params", {})
        if not isinstance(params, dict):
            raise ValueError(f"{where} ({command['type']}): params must be an object")
        if command["type"] in PATH_COMMANDS and not str(params.get("path") or "").strip():
            raise ValueError(f"{where} ({command['type']}): params.path is missing, e.g. "
                             f"{{\"path\": \"live_set tracks 0 devices 0 parameters 1\"}}")


def op_set(path: str, prop: str, value: Any) -> dict:
    return {"type": "lom_set", "params": {"path": path, "property": prop, "value": value}}


def from_its_start(clip: str, start: float = 0.0, loop_start: float | None = None) -> list[dict]:
    """The steps that put a new audio clip's start where it is to play from
    (start: the file's start, or a source clip's start marker). Live's warp
    analysis moves a new clip's start to the file's first transient (21 ms
    to 3.6 s in, Live 12.4) and keeps it there with warping off; loop_start
    goes first, as it carries start_marker."""
    return [op_set(clip, "loop_start", float(start if loop_start is None else loop_start)),
            op_set(clip, "start_marker", float(start))]


def op_call(path: str, method: str, *args: Any) -> dict:
    return {"type": "lom_call", "params": {"path": path, "method": method, "args": list(args)}}


def op_display(path: str, value: Any) -> dict:
    return {"type": "param_set_display", "params": {"path": path, "value": value}}


def ref(path: str) -> dict:
    return {"$ref": path}


def to_json(value: Any) -> str:
    # Compact: results cost the model tokens on every call (indentation was a
    # quarter to a half of a typical result)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


NOTE_COLUMNS = ["note_id", "pitch", "name", "start_time", "duration", "velocity"]
# Note fields left out of the rows when they hold Live's default
NOTE_DEFAULTS = {"mute": False, "probability": 1.0, "velocity_deviation": 0.0,
                 "release_velocity": 64.0}


def _short(value: Any) -> Any:
    if isinstance(value, float):
        value = round(value, 4)
        return int(value) if value.is_integer() else value
    return value


def note_table(notes: list[dict]) -> dict:
    """Notes as rows under one header instead of one object per note (about a
    third of the size): {"note_columns": [...], "notes": [[...], ...]}, with
    non-default expression (mute, probability, …) in "note_extras" by row."""
    columns = [c for c in NOTE_COLUMNS if c == "name" or any(c in n for n in notes)]
    rows, extras = [], {}
    for index, note in enumerate(notes):
        row = []
        for column in columns:
            if column == "name":
                row.append(music.note_name(int(note["pitch"])))
            else:
                row.append(_short(note.get(column)))
        rows.append(row)
        extra = {k: _short(note[k]) for k in NOTE_DEFAULTS
                 if k in note and note[k] is not None and _short(note[k]) != _short(NOTE_DEFAULTS[k])}
        if extra:
            extras[str(index)] = extra
    table: dict[str, Any] = {"note_columns": columns, "notes": rows}
    if extras:
        table["note_extras"] = extras
    return table


def selector(value: int | str) -> str:
    if isinstance(value, int) or re.fullmatch(r"-?\d+", str(value).strip()):
        return str(int(value))
    return shlex.quote(str(value))


def named(items: str, value: int | str) -> str:
    """The path token of an item of the list at `items` by name or index:
    selector's, but text made only of digits that an item has as its exact
    name (a track called "808", a Simpler renamed so, the routing type of
    such a track) is that item's index: a LOM path takes every number for an
    index. An integer is always an index; digits no item is named are one."""
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        try:
            rows = lom_get(items, ["name", "display_name"])["items"]
        except Exception:  # no Live to ask: as before, an index
            rows = []
        for index, row in enumerate(rows):
            if value.strip() in (row.get("name"), row.get("display_name")):
                return str(index)
    return selector(value)


class TrackId(str):
    """A track pinned by its LOM id ("id N", pin_track): Live renames a track
    still named as it named it ("1-MIDI" → "1-Drift") when an instrument
    lands on it, so a tool that inserts or loads re-addresses it by id."""


def pin_track(track: int | str) -> TrackId:
    if isinstance(track, TrackId):
        return track
    return TrackId(f"id {lom_get(track_path(track), ['name'])['$id']}")


def track_name(track: int | str) -> str:
    """The track's name now."""
    return lom_get(track_path(track), ["name"])["name"]


def track_path(track: int | str) -> str:
    if isinstance(track, TrackId):
        return str(track)
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
        return f"live_set return_tracks {named('live_set return_tracks', text[7:].strip())}"
    return f"live_set tracks {named('live_set tracks', text)}"


def device_path(track: int | str, device: int | str) -> str:
    """A device by index (the track's own chain), by name (the track's own
    devices first, then those inside its racks: find_device) or by LOM path
    ("live_set tracks 0 devices 0 chains 1 devices 0", "id N …") on this
    track."""
    tp = track_path(track)
    if isinstance(device, int):
        return f"{tp} devices {device}"
    text = str(device).strip()
    if re.fullmatch(r"-?\d+", text):
        return digits_device(tp, text)
    if re.fullmatch(r"id \d+( \S+)*|live_set( \S+)+", text):
        # one walk for both: a device named so wins, else the path's device
        try:
            wanted = lom_get(text, ["name"]).get("$id")
        except AbletonCommandError:
            wanted = None
        low = text.lower()
        found, seen = nested_devices(tp, lambda d: str(d.get("name", "")).lower() == low
                                     or (wanted is not None and d.get("$id") == wanted))
        by_name = [d for d in found if str(d.get("name", "")).lower() == low]
        if by_name:
            return pick_named(text, by_name, seen)
        if found:
            return found[0]["path"]
        raise ValueError(f"{text} is not a device on this track (nor inside its racks)")
    return find_device(tp, text)


def digits_device(tp: str, text: str) -> str:
    """Text made only of digits: the track's own device of that name, else
    its device at that index, unless a device inside a rack has that name
    too (an error naming both); else a nested device of that name."""
    found, seen = nested_devices(tp, lambda d: d.get("name") == text)
    if found and found[0]["depth"] == 0:
        return found[0]["path"]
    own = [d for d in seen if d["depth"] == 0]
    index = int(text)
    if found and -len(own) <= index < len(own):
        raise ValueError(f"Device {text!r} is ambiguous: index {index} is "
                         f"{own[index]['name']!r} ({own[index]['path']}), and a device inside "
                         f"a rack is named {text!r} ({'; '.join(d['path'] for d in found)}). "
                         f"Pass the index as a number or one of these paths.")
    if len(found) > 1:
        raise ValueError(f"{len(found)} devices inside racks are named {text!r}: "
                         + "; ".join(d["path"] for d in found))
    return found[0]["path"] if found else f"{tp} devices {index}"


def device_on_track(tp: str, path: str) -> str:
    """A device path given as such, checked to be on the track at tp (or in
    its racks) and written as that track's path."""
    wanted = lom_get(path, ["name"]).get("$id")
    found, _seen = nested_devices(tp, lambda d: d.get("$id") == wanted)
    if not found:
        raise ValueError(f"{path} is not a device on this track (nor inside its racks)")
    return found[0]["path"]


class NoSuchDevice(ValueError):
    pass


def device_owner(tp: str, dp: str) -> tuple[str, int]:
    """(the track or chain holding the device at dp, its index there), for
    delete_device: dp may end in "devices <n>" or be any path to a device."""
    m = re.fullmatch(r"(.+) devices (-?\d+)", dp)
    if m:
        owner, index = m.group(1), int(m.group(2))
        if index < 0:
            index += len(lom_get(f"{owner} devices", ["name"])["items"])
        return owner, index
    return device_owner(tp, device_on_track(tp, dp))


DEVICE_FIELDS = ["name", "class_name", "can_have_chains"]
RACK_DEPTH = 4  # racks inside racks inside racks…


def read_many(paths: list[tuple[str, list[str]]]) -> list[list[dict]]:
    """Several list reads in one round trip (a read-only batch); [] for one
    that fails."""
    if not paths:
        return []
    result = batch([{"type": "lom_get", "params": {"path": p, "properties": props}}
                    for p, props in paths], stop_on_error=False)
    return [step["result"].get("items", []) if step.get("status") != "error"
            and isinstance(step.get("result"), dict) else []
            for step in result["results"]]


def nested_devices(tp: str, wanted: Callable[[dict], bool]) -> tuple[list[dict], list[dict]]:
    """Devices `wanted` accepts, level by level: the track's own devices, then
    those in the chains of its racks (Instrument, Audio/MIDI Effect and Drum
    Racks), then in racks inside those, each level in chain order. Returns the
    matches of the shallowest level that has any and every device seen, each
    {name, class_name, path, depth}."""
    level = [dict(d, path=f"{tp} devices {i}", depth=0)
             for i, d in enumerate(lom_get(f"{tp} devices", DEVICE_FIELDS)["items"])]
    seen: list[dict] = []
    depth = 0
    while True:
        seen += level
        found = [d for d in level if wanted(d)]
        racks = [d["path"] for d in level if d.get("can_have_chains") is True]
        if found or not racks or depth == RACK_DEPTH:
            return found, seen
        depth += 1
        chains = [f"{rack} chains {i}" for rack, items in
                  zip(racks, read_many([(f"{r} chains", ["name"]) for r in racks]))
                  for i in range(len(items))]
        level = [dict(d, path=f"{chain} devices {i}", depth=depth)
                 for chain, items in zip(chains, read_many([(f"{c} devices", DEVICE_FIELDS)
                                                            for c in chains]))
                 for i, d in enumerate(items)]


def find_device(tp: str, name: str) -> str:
    """The path of the device named `name` (exact, else ignoring case) on the
    track at tp or inside its racks. The track's own devices win (the first of
    a name, as Live's paths do); a name several nested devices share is an
    error naming each one's path."""
    text = name.strip()
    found, seen = nested_devices(tp, lambda d: str(d.get("name", "")).lower() == text.lower())
    return pick_named(text, found, seen)


def pick_named(text: str, found: list[dict], seen: list[dict]) -> str:
    """find_device's choice among the devices a walk found by name."""
    name = text
    exact = [d for d in found if d.get("name") == text] or found
    if not exact:
        names = [f"{d['name']}" + (f" ({d['path']})" if d["depth"] else "") for d in seen]
        more = f", … {len(names) - 12} more" if len(names) > 12 else ""
        raise NoSuchDevice(f"No device named {name!r} on this track or in its racks. Devices: "
                         f"{', '.join(names[:12]) or 'none'}{more}")
    if exact[0]["depth"] == 0 or len(exact) == 1:
        return exact[0]["path"]
    raise ValueError(f"{len(exact)} devices inside racks are named {name!r}: "
                     + "; ".join(d["path"] for d in exact) + ". Pass one of these paths as "
                     "the device, or rename one.")


def drum_rack_path(track: int | str, tp: str | None = None) -> str | None:
    """The track's first Drum Rack: its own devices first, then inside its
    racks (a kit preset wrapped in an Instrument Rack). tp: the track's path,
    when known."""
    found, _seen = nested_devices(tp or track_path(track),
                                  lambda d: d.get("class_name") == "DrumGroupDevice")
    return found[0]["path"] if found else None


def clip_path(track: int | str, clip_index: int | None = None,
              arrangement_clip_index: int | None = None) -> str:
    if arrangement_clip_index is not None:
        return f"{track_path(track)} arrangement_clips {int(arrangement_clip_index)}"
    if clip_index is None:
        raise ValueError("Give clip_index (Session slot) or arrangement_clip_index")
    return f"{track_path(track)} clip_slots {int(clip_index)} clip"


def mixer_parameter_path(owner: str, parameter: int | str) -> str:
    """volume | pan | "send A"/"send 0" of a track's or a rack chain's mixer."""
    p = str(parameter).strip().lower()
    if p in ("volume", "vol"):
        return f"{owner} mixer_device volume"
    if p in ("pan", "panning"):
        return f"{owner} mixer_device panning"
    m = re.fullmatch(r"send\s*([a-z]|\d+)", p)
    if m:
        token = m.group(1)
        index = int(token) if token.isdigit() else ord(token) - ord("a")
        return f"{owner} mixer_device sends {index}"
    raise ValueError("Mixer parameter must be volume, pan or 'send A'/'send 0'")


def is_mixer(device: int | str) -> bool:
    return str(device).lower() in ("mixer", "mixer_device")


def parameter_path(track: int | str, device: int | str, parameter: int | str) -> str:
    if is_mixer(device):
        return mixer_parameter_path(track_path(track), parameter)
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
                step_out = {"set": inner.get("name"), "value": inner.get("new")}
                if inner.get("note"):  # what was set instead ("-inf dB", the end)
                    step_out["note"] = inner["note"]
                steps.append(step_out)
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
    if PREVIEW.get():
        return {"written": len(notes)}
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
    if PREVIEW.get():
        chosen_ids = {id(n) for n in chosen}
        result_notes = [n for n in notes if id(n) not in chosen_ids] + changed
        result_notes.sort(key=lambda n: (n["start_time"], n["pitch"]))
        return {"preview": "Nothing was written; call again without preview to apply it.",
                "notes_changed": len(changed), "notes_in_clip": len(result_notes),
                **note_table(result_notes)}
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
    if PREVIEW.get():
        return ""
    if arrangement_time is not None:
        with playhead_put_back():
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
    if PREVIEW.get():
        del payload["clip"]
        payload["preview"] = "Nothing was written; call again without preview to write these notes."
        payload.update(note_table(notes))
    return to_json(payload)


class _Confirm(BaseModel):
    confirm: bool = Field(default=True, description="Go ahead")


# Deletes ask the user first when the client can show a question (MCP
# elicitation). The tools take a parameter that the SDK fills by running one of
# these resolvers before the tool: Elicit asks (on 2025-11-25 as a request in
# the middle of the call, on 2026-07-28 as an input-required round), a ready
# answer skips the question (nothing to confirm, a client that cannot ask, or
# ABLETON_MCP_CONFIRM=off).
# A resolver's plain return value reaches the tool as an accepted answer
PROCEED = _Confirm(confirm=True)
GO_AHEAD = AcceptedElicitation(data=PROCEED)  # the tools' default, outside MCP


def _question(ctx: Context | None, describe: Callable[[], str]) -> Elicit[_Confirm] | _Confirm:
    if os.environ.get("ABLETON_MCP_CONFIRM", "on").strip().lower() in {"0", "off", "false", "no"}:
        return PROCEED
    try:
        capabilities = ctx.client_capabilities if ctx is not None else None
    except Exception:  # no request behind this call (a direct call): nobody to ask
        capabilities = None
    elicitation = getattr(capabilities, "elicitation", None)
    if elicitation is None or (getattr(elicitation, "form", None) is None
                               and getattr(elicitation, "url", None) is not None):
        return PROCEED
    try:
        message = describe()
    except Exception as e:  # the tool will say what is wrong
        logger.debug("No question before the delete: %s", e)
        return PROCEED
    return Elicit(message, _Confirm)


def recording_tracks() -> list[tuple[int, str]]:
    """(id, name) of the tracks record_section made and left ("MCP Rec …").
    Refused while one is armed: a take is running (here or in another
    session; RECORDING only knows this server's)."""
    rows = [t for t in lom_get("live_set tracks", ["name", "arm"])["items"]
            if str(t.get("name", "")).startswith("MCP Rec ")]
    busy = [t["name"] for t in rows if t.get("arm") is True or t.get("$id") in RECORDING]
    if busy:
        raise RuntimeError(f"A recording is running ({', '.join(busy)} armed or in a take); "
                           "delete the recording tracks once it has finished.")
    return [(t["$id"], t["name"]) for t in rows]


def ask_track_delete(action: str, ctx: Context, track: int | str | None = None
                     ) -> Elicit[_Confirm] | _Confirm:
    if action.lower() == "delete_recording_tracks":
        def listing() -> str:
            names = [name for _i, name in recording_tracks()]
            return (f"Delete {len(names)} recording track(s) ({', '.join(names)})? Their "
                    "files stay in Samples/Recorded; Live can undo it.")
        try:
            found = recording_tracks()
        except Exception:  # a take is running: the tool says so
            return PROCEED
        return _question(ctx, listing) if found else PROCEED
    if action.lower() != "delete" or track is None:
        return PROCEED

    def describe() -> str:
        tp = track_path(track)
        devices = len(lom_get(f"{tp} devices", ["name"])["items"])
        clips = sum(1 for s in lom_get(f"{tp} clip_slots", ["has_clip"])["items"]
                    if s.get("has_clip"))
        return (f"Delete the track “{lom_get(tp, ['name'])['name']}” ({clips} clips, "
                f"{devices} devices)? Live can undo it.")
    return _question(ctx, describe)


def ask_scene_delete(action: str, ctx: Context, scene: int | str | None = None
                     ) -> Elicit[_Confirm] | _Confirm:
    if action.lower() != "delete" or scene is None:
        return PROCEED

    def describe() -> str:
        index = index_in("live_set scenes", scene)
        name = lom_get(f"live_set scenes {index}", ["name"])["name"] or f"Scene {index + 1}"
        return f"Delete the scene “{name}” and its clips? Live can undo it."
    return _question(ctx, describe)


def ask_clip_delete(action: str, track: int | str, ctx: Context, clip_index: int | None = None,
                    arrangement_clip_index: int | None = None
                    ) -> Elicit[_Confirm] | _Confirm:
    if action.lower() != "delete":
        return PROCEED

    def describe() -> str:
        name = lom_get(clip_path(track, clip_index, arrangement_clip_index), ["name"])["name"]
        return (f"Delete “{name or 'the clip'}” on {lom_get(track_path(track), ['name'])['name']}? "
                "Live can undo it.")
    return _question(ctx, describe)


def ask_device_delete(track: int | str, device: int | str, action: str, ctx: Context
                      ) -> Elicit[_Confirm] | _Confirm:
    if action.lower() != "delete":
        return PROCEED

    def describe() -> str:
        name = lom_get(device_path(track, device), ["name"])["name"]
        return f"Remove {name} from {lom_get(track_path(track), ['name'])['name']}? Live can undo it."
    return _question(ctx, describe)


def require_go_ahead(answer: Any) -> None:
    if not (isinstance(answer, AcceptedElicitation) and getattr(answer.data, "confirm", False)):
        raise RuntimeError("Not done: the user declined.")


def previewable(fn: Callable) -> Callable:
    """Run the tool with PREVIEW set when it is called with preview=True."""
    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> Any:
        token = PREVIEW.set(bool(kwargs.get("preview")))
        try:
            return fn(*args, **kwargs)
        finally:
            PREVIEW.reset(token)
    return run


def keeping_arms(fn: Callable) -> Callable:
    """Run a tool that creates tracks or lands devices inside arms_put_back:
    Live selects each new track (after the command), so the user's selection
    and arms are read first and put back at the end. The playhead too
    (playhead_put_back), as these tools make Arrangement clips."""
    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> Any:
        with arms_put_back(), playhead_put_back():
            return fn(*args, **kwargs)
    return run


def keeping_playhead(fn: Callable) -> Callable:
    """Run a tool that makes Arrangement clips inside playhead_put_back."""
    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> Any:
        with playhead_put_back():
            return fn(*args, **kwargs)
    return run


# Set while a playhead_put_back is open: one inside it reads and restores nothing
PLAYHEAD_KEPT: contextvars.ContextVar[bool] = contextvars.ContextVar("playhead_kept",
                                                                     default=False)


async def keeping_the_playhead(step: Any) -> Any:
    """Await a step (a coroutine) that makes an Arrangement clip, the
    playhead put back afterwards (playhead_put_back, for async tools)."""
    if PLAYHEAD_KEPT.get():
        return await step
    try:
        before = await asend("lom_get", {"path": "live_set",
                                         "properties": ["current_song_time", "is_playing"]})
    except BaseException:
        if asyncio.iscoroutine(step):
            step.close()  # never started: no "never awaited" warning
        raise
    try:
        return await step
    finally:
        if before.get("is_playing") is False:
            await asend("lom_set", {"path": "live_set", "property": "current_song_time",
                                    "value": float(before["current_song_time"])})


@contextlib.contextmanager
def playhead_put_back():
    """Live 12.4.16 moves the stopped playhead to each clip that
    duplicate_clip_to_arrangement, create_midi_clip or create_audio_clip
    makes in the Arrangement: it goes back where it was (not while Live
    plays; no undo step). Inside another, it does nothing."""
    if PLAYHEAD_KEPT.get():
        yield
        return
    try:
        before = lom_get("live_set", ["current_song_time", "is_playing"])
    except Exception as e:  # no Live to ask (tests, previews)
        logger.debug("No playhead read: %s", e)
        before = None
    token = PLAYHEAD_KEPT.set(True)
    try:
        yield
    finally:
        PLAYHEAD_KEPT.reset(token)
        if isinstance(before, dict) and before.get("is_playing") is False and not PREVIEW.get():
            try:
                send("lom_set", {"path": "live_set", "property": "current_song_time",
                                 "value": float(before["current_song_time"])})
            except Exception as e:
                logger.warning("Could not put the playhead back: %s", e)


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


# Renames of one device (by class_name). They replace the general ones for
# every name they mention: Live 12.3's Limiter "Gain" was its input gain, which
# 12.4 calls "Input Gain" next to an "Output" (measured in 12.4.16, 2026-11-23)
DEVICE_PARAMETER_RENAMES = {"Limiter": [("Gain", "Input Gain")]}


def parameter_aliases(name: str, class_name: str | None = None) -> list[str]:
    """Other names the same parameter has in another Live version, on a
    device of this class_name."""
    own = DEVICE_PARAMETER_RENAMES.get(class_name or "", [])
    mentioned = {n for pair in own for n in pair}
    renames = own + [(old, new) for old, new in PARAMETER_RENAMES
                     if old not in mentioned and new not in mentioned]
    found = [new for old, new in renames if old == name]
    found += [old for old, new in renames if new == name]
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
    mapping, class_name = {}, None
    for name in names:
        if name in present or isinstance(name, int) or str(name).isdigit():
            mapping[name] = name
            continue
        if class_name is None:
            class_name = str(lom_get(device, ["class_name"]).get("class_name"))
        mapping[name] = next((alias for alias in parameter_aliases(name, class_name)
                              if alias in present), name)
    return mapping


def renamed(names: dict[str, str]) -> dict[str, str]:
    """The settings resolve_parameter_names sent to another name: asked → set."""
    return {asked: actual for asked, actual in names.items() if asked != actual}


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


def setting_report(asked: list[str], names: dict[str, str], applied: dict) -> dict:
    """What add_device-style tools say about their settings (one batch step
    each, in order): ok, the failed ones under errors, and those set under
    another parameter name (renamed)."""
    errors = {asked[s["step"]]: s["error"] for s in applied["steps"]
              if isinstance(s, dict) and "error" in s and s.get("step") is not None
              and s["step"] < len(asked)}
    report: dict[str, Any] = {"ok": applied["ok"], "settings": f"{len(asked) - len(errors)} of "
                              f"{len(asked)} applied"}
    if errors:
        report["errors"] = errors
    notes = {asked[i]: s["note"] for i, s in enumerate(applied["steps"])
             if i < len(asked) and isinstance(s, dict) and s.get("note")}
    if notes:
        report["notes"] = notes
    if renamed(names):
        report["renamed"] = renamed(names)
    return report


# Every track's arm and the selected track, read in one batch (ARM_READ)
ARM_READ = [{"type": "lom_get", "params": {"path": "live_set tracks", "properties": ["arm"]}},
            {"type": "lom_get", "params": {"path": "live_set view",
                                           "properties": ["selected_track"]}}]


def arms_and_selection(steps: list[dict]) -> tuple[dict[int, bool], int | None]:
    """({track id: arm}, selected track id) from ARM_READ's two results."""
    rows = steps[0].get("result", {}).get("items", []) if steps[0].get("status") != "error" else []
    arms = {r["$id"]: r["arm"] for r in rows if isinstance(r.get("arm"), bool)}
    view = steps[1].get("result", {}) if steps[1].get("status") != "error" else {}
    selected = view.get("selected_track") if isinstance(view, dict) else None
    return arms, selected.get("$id") if isinstance(selected, dict) else None


def put_back_arms(before: tuple[dict[int, bool], int | None],
                  after: tuple[dict[int, bool], int | None],
                  run: Callable[[list[dict]], Any] | None = None) -> None:
    """Undo what Live did on its own when a device landed on an empty MIDI
    track (Live 12.4.16): it arms that track, drops every other track's arm
    (exclusive arm) and selects it (it selects a track any device lands on,
    and a new track). Arms set through the LOM are not
    exclusive, so tracks Live armed are disarmed first, then the user's arms
    come back, then the selection; one batch (no undo step), read back. The
    selection changes only after the command that landed the device (Live
    defers it), so it is set back whatever `after` read; call this in a
    later command than the one that landed the device."""
    (arms_before, selected_before), (arms_after, _selected_after) = before, after
    ops = [op_set(f"id {t}", "arm", False) for t, armed in arms_after.items()
           if armed and arms_before.get(t) is False]
    ops += [op_set(f"id {t}", "arm", True) for t, armed in arms_after.items()
            if not armed and arms_before.get(t) is True]
    if selected_before is not None:
        ops.append(op_set("live_set view", "selected_track", ref(f"id {selected_before}")))
    if ops:
        (run or (lambda cmds: batch(cmds, stop_on_error=False)))(ops + ARM_READ[:1])


@contextlib.contextmanager
def arms_put_back(select: bool = True, run: Callable[[list[dict]], Any] | None = None):
    """Around steps that create tracks or land devices: every track's arm and
    (select) the selected track are read first and put back afterwards
    (put_back_arms). A failure of the put-back never hides the steps' own."""
    run = run or (lambda cmds: batch(cmds, stop_on_error=False))
    try:
        before = arms_and_selection(run(ARM_READ)["results"])
    except Exception as e:  # an older script: nothing to put back
        logger.debug("No arms read: %s", e)
        before = None
    if before is not None and not select:
        before = (before[0], None)
    failed = True
    try:
        yield
        failed = False
    finally:
        if before is not None:
            try:
                put_back_arms(before, arms_and_selection(run(ARM_READ)["results"]), run)
            except Exception as e:
                if not failed:
                    raise
                logger.warning("Could not put arms and the selection back: %s", e)


def insert_devices(track: str, inserts: list[list[Any]]) -> tuple[list[int], str]:
    """Insert devices with Track.insert_device (Live 12.3+): their ids, in
    order, and the track's name afterwards. Live 12.4.16 selects the track
    and, landing on a MIDI track with no device, arms it and disarms the
    others (put_back_arms); nothing is put back when neither can happen."""
    # by id: Live renames a track named "1-MIDI" as an instrument lands on it
    read = batch([{"type": "lom_get", "params": {"path": track, "properties": [
        "name", "has_midi_input", "devices"]}}] + ARM_READ, stop_on_error=False)["results"]
    if read[0]["status"] == "error":
        raise ValueError(read[0]["message"])
    info = read[0]["result"]
    track = f"id {info['$id']}"
    before = arms_and_selection(read[1:])
    empty_midi = info.get("has_midi_input") is True and not info.get("devices")
    name_read = {"type": "lom_get", "params": {"path": track, "properties": ["name"]}}
    ops = [op_call(track, "insert_device", *args) for args in inserts] + [name_read] \
        + (ARM_READ if empty_midi else [])
    steps = batch(ops)["results"]  # stops at a failed insert
    done = len(steps) == len(ops)
    try:
        if empty_midi:  # armed and the others disarmed (arms read back at once)
            after = steps[-2:] if done else batch(ARM_READ, stop_on_error=False)["results"]
            put_back_arms(before, arms_and_selection(after))
        elif before[1] != info["$id"]:  # only the selection moved (after the command)
            put_back_arms(before, (before[0], None))
    except Exception as e:
        if done:
            raise
        logger.warning("Could not put arms and the selection back: %s", e)  # Live's error first
    name = steps[len(inserts)]["result"].get("name", "") if done else ""
    ids = []
    for args, step in zip(inserts, steps[:len(inserts)]):
        if step["status"] == "error":
            message = step["message"]
            if "has no method 'insert_device'" in message:
                message = ("inserting devices by name needs Live 12.3 or later; find the "
                           "device with browser_search and load it with browser_load")
            raise RuntimeError(f"Could not insert {args[0]!r}: {message}")
        ids.append(step["result"]["result"]["$id"])
    return ids, name


# ── Recording: what may record, and what came out ───────────────────────────

# Every track's arm and implicit arm. Live 12.4.16 (2026-11-23): a track
# with implicit_arm (Push's auto-arm of the selected track; settable
# through the LOM, no undo step) records in the Arrangement like an armed
# one, so both are turned off for a take and put back after it.
REC_ARMS = {"type": "lom_get", "params": {"path": "live_set tracks",
                                          "properties": ["name", "arm", "implicit_arm"]}}
# Grace past a section's end before a take counts as stuck, and how long a
# recording's size must stay put before it is read (Live 12.4.16 had the
# file complete 30 ms after stop_playing)
RECORD_GRACE_S = 30.0
SETTLE_S = 1.0
ANALYSIS_MAX_S = 600.0
# Ids of the temporary tracks of takes running in this server
# (delete_recording_tracks leaves them alone)
RECORDING: set[int] = set()


def rows_of(step: dict) -> list[dict]:
    if step.get("status") == "error":
        return []
    return (step.get("result") or {}).get("items", [])


def arming(rows: list[dict]) -> tuple[list[int], list[int]]:
    """(armed track ids, implicitly armed track ids)."""
    return ([r["$id"] for r in rows if r.get("arm") is True],
            [r["$id"] for r in rows if r.get("implicit_arm") is True])


def disarm_ops(armed: list[int], implicit: list[int], on: bool = False) -> list[dict]:
    """Turn those arms off (on=False) or back on."""
    return ([op_set(f"id {t}", "arm", on) for t in armed]
            + [op_set(f"id {t}", "implicit_arm", on) for t in implicit])


def audio_device_note() -> str:
    """What Live's log says about its audio output, for a recording that
    came back empty or never ran."""
    from .doctor import audio_devices
    try:
        devices = audio_devices()
    except Exception as e:
        logger.debug("No audio device read: %s", e)
        devices = None
    if devices is None:
        return ("Check Live's audio output device (Settings › Audio): without one Live "
                "records nothing.")
    if devices["output"].strip().lower() == "no device":
        return (f"Live's audio output device is \"No Device\" (Log.txt: {devices['line']}), "
                "so its audio engine is off and records nothing. Choose an output device in "
                "Live's Settings › Audio, then record again.")
    return (f"Live's audio output device is {devices['output']} (Log.txt); check that "
            "Live's audio engine runs (Settings › Audio).")


def check_recording(label: str, path: str, beats: float | None,
                    length_beats: float) -> float:
    """The seconds of the file that hold the section, once it does. An
    error for a file with (almost) no audio frames (Live without an audio
    device writes a 92-byte WAV of 2 frames), naming Live's audio device,
    and for a take Live cut short (its clip under the section's beats).
    The clip's beats, not the set's tempo, measure the take: a scene or
    tempo automation may change the tempo. Live records on past the end
    until the stop reaches it; those frames are left out."""
    import soundfile as sf

    try:
        info = sf.info(path)
        got = info.frames / float(info.samplerate)
    except Exception as e:
        raise RuntimeError(f"Could not read the recording of {label} ({path}): {e}") from e
    if got < 0.1:
        raise RuntimeError(f"Live recorded no audio for {label}: {path} holds {got:.2f} s. "
                           + audio_device_note())
    if beats is not None and beats < length_beats - 0.05:
        raise RuntimeError(f"The recording of {label} stopped after {beats:.2f} of "
                           f"{length_beats:g} beats (playback stopped or Live cut the take); "
                           "nothing was analysed.")
    if beats is not None and beats > length_beats:
        return got * length_beats / beats
    return got


def inserted_device_ids(track: str, inserts: list[list[Any]]) -> list[int]:
    """insert_devices' ids."""
    return insert_devices(track, inserts)[0]


# SimplerDevice.replace_sample(path) loads a file by absolute path since
# Live 12.4 (its release notes); before it, samples came through the browser
REPLACE_SAMPLE_SINCE = (12, 4)
PLAYBACK_MODES = {"classic": 0, "one_shot": 1, "slicing": 2}


def live_version() -> tuple[int, ...] | None:
    """(major, minor, bugfix) from Application.get_*_version, or None."""
    try:
        steps = batch([op_call("live_app", f"get_{part}_version")
                       for part in ("major", "minor", "bugfix")])["results"]
        return tuple(int(step["result"]["result"]) for step in steps)
    except Exception as e:  # a step failed (no "result"), or no connection
        logger.debug("Live's version unknown: %s", e)
        return None


def playback_mode(value: str | int) -> int:
    """SimplerDevice.playback_mode: classic 0, one_shot 1, slicing 2."""
    if isinstance(value, int) and not isinstance(value, bool) and value in PLAYBACK_MODES.values():
        return value
    key = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if key not in PLAYBACK_MODES:
        raise ValueError(f"playback_mode {value!r}: use classic, one_shot or slicing")
    return PLAYBACK_MODES[key]


def parse_signature(value: str | list[int] | tuple[int, int]) -> tuple[int, int]:
    """(numerator, denominator) from "7/8" or [7, 8]: 1-99 over 1, 2, 4, 8 or 16."""
    parts = str(value).split("/") if isinstance(value, str) else list(value)
    if len(parts) != 2:
        raise ValueError(f"signature {value!r}: give [numerator, denominator] or \"7/8\"")
    numerator, denominator = int(parts[0]), int(parts[1])
    if not 1 <= numerator <= 99 or denominator not in (1, 2, 4, 8, 16):
        raise ValueError(f"signature {value!r}: 1-99 over 1, 2, 4, 8 or 16")
    return numerator, denominator


def sample_file(where: str, options: dict) -> str | None:
    """A sampler's sample_path, checked: absolute (after ~) and an existing file."""
    path = options.get("sample_path")
    if path is None:
        return None
    file = Path(str(path)).expanduser()
    if not file.is_absolute():
        raise ValueError(f"{where}: sample_path must be absolute, not {path!r}")
    if not file.is_file():
        raise FileNotFoundError(f"{where}: no such file {file}")
    return str(file)


def check_samplers(tracks: list[dict]) -> None:
    """Refuse, before anything is built, Simpler and pad options that cannot
    work: a missing or relative sample_path, an unknown playback_mode, a
    choke group outside 0-16 or a negative start marker."""
    for spec in tracks:
        instrument = spec.get("instrument")
        if not isinstance(instrument, dict):
            continue
        if instrument.get("drum_rack"):
            samplers = [(f"{spec['name']} pad {note}", pad) for note, pad in instrument["drum_rack"].items()]
        else:
            samplers = [(str(spec["name"]), instrument)]
        for where, options in samplers:
            if sample_file(where, options) is None and not options.get("sample"):
                raise ValueError(f"{where}: give sample_path (a file) or sample (a browser name)")
            if options.get("playback_mode") is not None:
                playback_mode(options["playback_mode"])
            group = options.get("choke_group")
            if group is not None and not 0 <= int(group) <= 16:
                raise ValueError(f"{where}: choke_group is 0 (none) to 16, not {group}")
            if options.get("start_marker") is not None and int(options["start_marker"]) < 0:
                raise ValueError(f"{where}: start_marker counts sample frames from 0")


def sampler_ops(device: str, options: dict, chain: str | None = None) -> list[dict]:
    """Batch steps for a Simpler's playback mode and sample start marker, and
    for a drum chain's choke group."""
    ops = []
    if options.get("playback_mode") is not None:
        ops.append(op_set(device, "playback_mode", playback_mode(options["playback_mode"])))
    if options.get("start_marker") is not None:
        ops.append(op_set(f"{device} sample", "start_marker", int(options["start_marker"])))
    if chain is not None and options.get("choke_group") is not None:
        ops.append(op_set(chain, "choke_group", int(options["choke_group"])))
    return ops


def same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


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


ARRANGER_ZOOM_STEPS = 30


@contextlib.contextmanager
def arrangement_zoomed_in(steps: int = ARRANGER_ZOOM_STEPS):
    """The Arrangement in front and zoomed in, for placing cues.

    Live snaps a new cue to the Arrangement's grid, which follows its
    horizontal zoom: at the default zoom beat 9 of a 3/4 song became 12, and
    ~30 `zoom_view` steps in (Live 12.4.16) an eighth of a beat landed
    exactly. The zoom goes out by as many steps afterwards, which restores
    the view only roughly: Live's zoom stops at its limits. Yields the
    errors of showing and zooming (none where it worked): cues placed after
    a zoom that failed may snap, which delete_strays then finds.
    """
    def zoom(direction: int) -> list[dict]:  # 3 in, 2 out
        return [op_call("live_app view", "zoom_view", direction, "Arranger", False)] * steps

    done = batch([op_call("live_app view", "show_view", VIEWS["arrangement"])] + zoom(3), stop_on_error=False)
    errors = sorted({str(step.get("message") or step.get("error") or "failed")
                     for step in (done or {}).get("results", []) if step.get("status") == "error"})
    try:
        yield errors
    finally:
        batch(zoom(2), stop_on_error=False)


def _playhead() -> float:
    return float(lom_get("live_set", ["current_song_time"])["current_song_time"])


def _move_playhead(at: float) -> float | None:
    """Move the playhead in a call of its own and read it back (it is not in
    effect within the call that sets it; Live refuses a time past the
    song's end): None once it is there, else where Live kept it."""
    send("lom_set", {"path": "live_set", "property": "current_song_time", "value": at})
    now = at
    for _ in range(3):
        time.sleep(0.05)
        now = _playhead()
        if abs(now - at) < 1e-3:
            return None
    return now


def delete_strays(placed: list[dict], before: list[float]) -> list[dict]:
    """The locators Live did not make where asked: a cue at a beat neither
    asked for nor in the set before (Live snapped it to the Arrangement's
    grid, which depends on the zoom) is deleted, by moving the playhead
    there (read back) and toggling it in the next call, and listed with the
    beat asked (the nearest failed one) and Live's. A stray whose playhead
    Live keeps elsewhere is left where it is: a toggle at the kept playhead
    would delete another cue or make one. The playhead goes back."""
    asked = [p["time"] for p in placed]
    cues = lom_get("live_set cue_points", ["name", "time"])["items"]
    stray = [c for c in cues if all(abs(c["time"] - t) > 1e-3 for t in asked + list(before))]
    failed = [p for p in placed if p.get("error")]
    out = []
    original = _playhead() if stray else None
    for cue in stray:
        near = min(failed, key=lambda p: abs(p["time"] - cue["time"]), default=None)
        if near is not None:
            failed.remove(near)
        row = {"name": near["name"] if near else cue["name"], "asked": near["time"] if near else None,
               "live": cue["time"]}
        kept = _move_playhead(float(cue["time"]))
        if kept is None:
            send("lom_call", {"path": "live_set", "method": "set_or_delete_cue"})
        else:
            row["playhead"] = kept
        out.append(row)
    out += [{"name": p["name"], "asked": p["time"], "live": None} for p in failed]
    left = lom_get("live_set cue_points", ["name", "time"])["items"] if stray else cues
    for row in out:
        row["deleted"] = row["live"] is not None and all(abs(c["time"] - row["live"]) > 1e-3 for c in left)
    if original is not None:
        send("lom_set", {"path": "live_set", "property": "current_song_time", "value": original})
    return out


def delete_cues(times: list[float]) -> list[dict]:
    """Delete the cues at these beats (each: move the playhead there, read
    back, toggle in the next call); [{name, time}] of those gone. The
    playhead goes back."""
    cues = [c for c in lom_get("live_set cue_points", ["name", "time"])["items"]
            if any(abs(c["time"] - t) < 1e-3 for t in times)]
    if not cues:
        return []
    original = _playhead()
    for cue in cues:
        if _move_playhead(float(cue["time"])) is None:
            send("lom_call", {"path": "live_set", "method": "set_or_delete_cue"})
    left = [c["time"] for c in lom_get("live_set cue_points", ["time"])["items"]]
    send("lom_set", {"path": "live_set", "property": "current_song_time", "value": original})
    return [{"name": c["name"], "time": c["time"]} for c in cues
            if all(abs(c["time"] - t) > 1e-3 for t in left)]


def place_locators(cues: list[tuple[str, float]], zoom: bool = True,
                   remove: list[float] = ()) -> list[dict]:
    """Create or rename named locators at beat positions.

    Song.set_or_delete_cue() toggles a cue at the playhead, and a playhead
    moved in the same call is not in effect yet (Live 12.4), so each cue
    takes two calls: move (read back: Live refuses a time past the song's
    end), then toggle. With zoom, the Arrangement is zoomed in meanwhile
    (arrangement_zoomed_in) and a cue Live still put at another beat is
    deleted (delete_strays), its row's error saying where it went; without,
    the caller does both. Existing cues are only renamed, and the playhead
    goes back where it was. remove: beats of cues to delete first (with
    zoom; delete_cues), reported in a last row {"deleted": [...]}.
    """
    if zoom:
        with arrangement_zoomed_in() as zoom_errors:
            deleted = delete_cues(list(remove)) if remove else []
            before = [float(c["time"]) for c in lom_get("live_set cue_points", ["time"])["items"]]
            result = place_locators(cues, zoom=False)
            moved = delete_strays(result, before) if any("error" in r for r in result) else []
        for row in result:
            if "error" not in row:
                continue
            went = next((m for m in moved if m["asked"] == row["time"] and m["live"] is not None), None)
            if went is not None:
                row["error"] += (f"; Live put it at beat {went['live']:g}"
                                 + (", deleted" if went["deleted"] else ", left there"))
            if zoom_errors:
                row["error"] += f" (zooming the Arrangement in failed: {zoom_errors[0]})"
        return result + ([{"deleted": deleted}] if remove else [])

    def current() -> list[dict]:
        return lom_get("live_set cue_points", ["name", "time"])["items"]

    original = _playhead()
    have = [c["time"] for c in current()]
    kept: dict[float, float] = {}  # beat asked: where Live kept the playhead instead
    for _name, at in cues:
        if any(abs(t - at) < 1e-3 for t in have):
            continue
        stayed = _move_playhead(at)
        if stayed is not None:
            kept[at] = stayed
            continue
        send("lom_call", {"path": "live_set", "method": "set_or_delete_cue"})
        have.append(at)
    placed = current()
    renames, result = [], []
    for name, at in cues:
        cue = next((c for c in placed if abs(c["time"] - at) < 1e-3), None)
        if cue is None:
            error = (f"Live kept the playhead at beat {kept[at]:g} (a time past the song's end is "
                     "refused)" if at in kept else "Live did not create it")
            result.append({"name": name, "time": at, "error": error})
            continue
        renames.append(op_set(f"id {cue['$id']}", "name", name))
        result.append({"name": name, "time": at})
    batch(renames + [op_set("live_set", "current_song_time", original)], stop_on_error=False)
    return result


FILE_EXTENSION = re.compile(r"\.(wav|aiff?|flac|mp3|ogg|m4a|adg|adv|amxd|alc|als)$", re.I)


def _family(word: str) -> str | None:
    """Words a name may carry that change what the sound is."""
    if word == "rev" or word.startswith("revers"):
        return "reverse"
    if word.startswith("loop") or word == "bpm":
        return "loop"
    return None


def name_words(text: str) -> list[str]:
    """A name's words, lower case, without its file extension."""
    return re.findall(r"[^\W_]+", FILE_EXTENSION.sub("", text.strip()).lower())


def same_name(query: str, name: str) -> bool:
    """"impact layered" is "Impact Layered.wav"; "Impact Layered Reversed" is not."""
    return bool(name_words(query)) and name_words(query) == name_words(name)


def by_name_match(query: str, hits: list[dict]) -> list[dict]:
    """Browser results for a name someone asked for, best first: the exact
    name (case and file extension aside), then names that are not reversed or
    loops unless the query says so, have every word asked, fewer other words,
    and are shorter; ties keep the browser's order."""
    words = name_words
    asked = words(query)
    families = {_family(w) for w in asked}

    def key(item: tuple[int, dict]) -> tuple:
        index, hit = item
        name = words(hit["name"])
        return (name != asked,
                any(_family(w) not in families for w in name if _family(w)),
                sum(w not in name for w in asked),
                sum(w not in asked for w in name),
                len(" ".join(name)), index)

    return [hit for _i, hit in sorted(enumerate(hits), key=key)]


def is_reversed(file_path: str) -> bool:
    """A sample Simpler reversed (Live keeps the reversed copy in
    Samples/Processed/Reverse, 12.4.16) or one named reversed."""
    folders = file_path.replace("\\", "/").lower()
    return "/processed/reverse/" in folders or any(
        _family(w) == "reverse" for w in re.findall(r"[^\W_]+", os.path.basename(folders)))


def drum_mapping_from_rack(track: int | str, rack: str | None = None) -> dict[str, int]:
    rack = rack or drum_rack_path(track)
    if rack is None:
        return {}
    pads = lom_get(f"{rack} drum_pads", ["note", "name", "chains"])["items"]
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
    rack = drum_rack_path(track)
    if rack is None:
        return {}
    pads = lom_get(f"{rack} drum_pads", ["note", "name", "chains"])["items"]
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


def set_notes() -> list[dict]:
    """The notes kept in the open Live set (memory.py)."""
    return notes_store.parse(send("lom_call", {"path": "live_set", "method": "get_data",
                                               "args": [notes_store.SET_KEY, ""]})["result"])


def save_set_notes(notes: list[dict]) -> None:
    send("lom_call", {"path": "live_set", "method": "set_data",
                      "args": [notes_store.SET_KEY, notes_store.dumps(notes)]})


def envelope_steps(shape: str | None, points: list[dict] | None, length: float, low: float,
                   high: float, cycles: float, resolution: float, pmin: float, pmax: float,
                   normalized: bool) -> list[dict]:
    """Envelope steps over 0..length (clip time) for clip_write_envelope: a
    shape between low and high, or points; Live plays a step for its duration
    (a zero-length step does nothing)."""
    if shape:
        raw = envelope_points(shape.lower(), 0.0, length, low, high, cycles, resolution)
    elif points:
        raw = interpolate_points(points, resolution)
    else:
        raise ValueError("Give a shape or points")
    scale = (lambda v: pmin + (pmax - pmin) * v) if normalized else (lambda v: v)
    return [{"time": round(t, 6), "value": scale(v), "duration": resolution}
            for t, v in raw if t < length - 1e-9]


# ── Arrangement stretches (see transitions.py for what Live 12.4 allows) ─────

ARRANGEMENT_CLIP_FIELDS = ["name", "start_time", "end_time", "start_marker", "looping",
                           "loop_start", "loop_end", "is_midi_clip", "color_index"]


def bar_range(start_bar: float | None, bars: float | None, start: float | None = None,
              end: float | None = None, default_bars: float = 1.0) -> tuple[float, float]:
    """(start, end) in beats, from 1-based bars in the set's meter or from beats."""
    beats_per_bar = song_context()["beats_per_bar"]
    if start is None:
        if start_bar is None:
            raise ValueError("Give start_bar (1 = the first bar) or start (beats)")
        start = (float(start_bar) - 1) * beats_per_bar
    if end is None:
        end = float(start) + float(bars if bars is not None else default_bars) * beats_per_bar
    if end <= start:
        raise ValueError("The stretch must end after it starts")
    return float(start), float(end)


def arrangement_notes(track: int | str, start: float, end: float) -> tuple[list[dict], list[dict]]:
    """What the track's Arrangement plays between start and end: (the notes, at
    Arrangement times, and the clips there)."""
    tp = track_path(track)
    clips = [dict(c, index=i) for i, c in enumerate(
        lom_get(f"{tp} arrangement_clips", ARRANGEMENT_CLIP_FIELDS)["items"])]
    clips = [c for c in clips if c["start_time"] < end and c["end_time"] > start]
    notes: list[dict] = []
    for clip in clips:
        if clip.get("is_midi_clip"):
            played = read_notes(f"{tp} arrangement_clips {clip['index']}")["notes"]
            notes += transitions.unroll_notes(played, clip, start, end)
    notes.sort(key=lambda n: (n["start_time"], n["pitch"]))
    return notes, clips


@keeping_playhead
def rewrite_range(track: int | str, start: float, end: float, notes: list[dict],
                  envelopes: list[tuple[str, list[dict]]] = (), name: str | None = None,
                  color_index: int | None = None) -> dict:
    """Lay a MIDI clip with `notes` (Arrangement times) and `envelopes`
    [(parameter path, steps)] over the track's Arrangement from start to end.

    Only Session clips take envelopes, so the clip is built in an empty
    Session slot (a scene is added for it when none is free, and removed),
    copied into the Arrangement, and deleted. What the copy lands on is split
    around it, as in Live.
    """
    tp = track_path(track)
    if not lom_get(tp, ["has_midi_input"])["has_midi_input"]:
        raise ValueError("This works on MIDI tracks: Live takes envelopes only on Session clips "
                         "and cannot rebuild audio from notes. For automation on an audio track, "
                         "a return or the main track use record_automation")
    with scratch_slot(tp) as slot:
        scratch = f"{slot} clip"
        send("lom_call", {"path": slot, "method": "create_clip", "args": [float(end - start)]})
        ops = [op_set(scratch, "name", name)] if name else []
        if color_index is not None:
            ops.append(op_set(scratch, "color_index", int(color_index)))
        if ops:
            batch(ops)
        if notes:
            write_notes(scratch, [dict(n, start_time=round(n["start_time"] - start, 6))
                                  for n in notes], "replace")
        for parameter, steps in envelopes:
            send("clip_write_envelope", {"clip_path": scratch, "parameter_path": parameter,
                                         "points": steps})
        send("lom_call", {"path": tp, "method": "duplicate_clip_to_arrangement",
                          "args": [ref(scratch), float(start)]})
    return {"from_beat": start, "to_beat": end, **transitions.summary(notes)}


@contextlib.contextmanager
def scratch_slot(tp: str):
    """An empty Session slot of the track for a clip to be copied into the
    Arrangement (a scene is added when none is free); whatever is in it
    afterwards is deleted, and the scene too."""
    slots = lom_get(f"{tp} clip_slots", ["has_clip"])["items"]
    free = next((i for i, s in enumerate(slots) if not s.get("has_clip")), None)
    added_scene = None
    if free is None:
        send("lom_call", {"path": "live_set", "method": "create_scene", "args": [-1]})
        free = added_scene = len(slots)
    slot = f"{tp} clip_slots {free}"
    failure: BaseException | None = None
    try:
        yield slot
    except BaseException as e:
        failure = e
        raise
    finally:
        problems = []
        try:
            if lom_get(slot, ["has_clip"]).get("has_clip"):
                send("lom_call", {"path": slot, "method": "delete_clip"})
        except Exception as e:
            problems.append(f"the scratch clip in {slot} was not deleted: {e}")
        if added_scene is not None:
            try:
                send("lom_call", {"path": "live_set", "method": "delete_scene",
                                  "args": [added_scene]})
            except Exception as e:
                problems.append(f"the scratch scene {added_scene} was not deleted: {e}")
        if problems:
            if failure is not None:  # both said, the step's failure first
                raise RuntimeError("; ".join([str(failure)] + problems)) from failure
            raise RuntimeError("; ".join(problems))


# Beats a MIDI file's last note may end past a bar line without import_midi_file
# adding a bar for it (a release tail)
MIDI_TAIL_TOLERANCE = 0.25

# A Session clip's fields that size its copy in the Arrangement
SIZE_FIELDS = ["name", "length", "looping", "loop_start", "loop_end", "start_marker",
               "end_marker", "is_midi_clip", "warping"]


def sized_copy(tp: str, source: str, at: float, info: dict,
               length: float | None = None) -> list[dict]:
    """Steps copying the Session clip at `source` (info: its SIZE_FIELDS)
    into the track's Arrangement at `at`, `length` beats long (default its
    own). Live 12.4.16 makes the copy as long as the clip's `length`
    (loop_end - loop_start, looped or not; an unlooped clip's end_marker
    does not shorten it), so loop_end is moved for the copy and put back,
    the markers after it. Not for unwarped audio (trimmed_copies)."""
    dup = op_call(tp, "duplicate_clip_to_arrangement", ref(source), float(at))
    own = float(info["length"])
    if length is None or abs(float(length) - own) < 1e-6 or own <= 0:
        return [dup]
    loop_start, loop_end = float(info["loop_start"]), float(info["loop_end"])
    return [op_set(source, "loop_end", loop_start + float(length)), dup,
            op_set(source, "loop_end", loop_end),
            op_set(source, "start_marker", float(info["start_marker"])),
            op_set(source, "end_marker", float(info["end_marker"]))]


def unwarped(info: dict) -> bool:
    return info.get("is_midi_clip") is False and info.get("warping") is False


def trimmed_copies(jobs: list[tuple[str, str, float, float, float]],
                   tempo: float) -> list[dict]:
    """Copies of unwarped Session audio clips, each (track path, clip path,
    at, length in beats, the clip's own length): such a clip keeps the
    length its warp tempo guess gave it whatever its loop (Live 12.4.16), so
    each is copied past everything on its track (each copy past the one
    before: a copy laid over another replaces it), trimmed there (an
    unlooped Arrangement clip ends at its loop_end, in seconds), copied from
    there, and the scratch copies are deleted whatever fails. Three
    commands: Live updates a trimmed clip's span only after the command that
    trims it. One result per job, in order: the final copy's duplicate step,
    or an error step; never raises."""
    if not jobs:
        return []
    scratch: list[tuple[int, str, int]] = []
    failed: list[dict | None] = [None] * len(jobs)
    out: list[dict] = []
    try:
        cursor: dict[str, float | None] = {}
        for tp in dict.fromkeys(tp for tp, *_rest in jobs):  # one read per track
            try:
                cursor[tp] = past_the_end(tp, 0.0)
            except Exception as e:
                cursor[tp] = None
                for k, job in enumerate(jobs):
                    if job[0] == tp:
                        failed[k] = {"status": "error", "message": f"no scratch place: {e}"}
        places: list[float | None] = []
        for tp, _source, _at, _length, own in jobs:
            places.append(cursor[tp])
            if cursor[tp] is not None:
                cursor[tp] += (int(max(float(own), 0.0) // 16) + 2) * 16
        todo = [k for k, at in enumerate(places) if at is not None]
        made = batch([op_call(jobs[k][0], "duplicate_clip_to_arrangement", ref(jobs[k][1]),
                              places[k]) for k in todo], stop_on_error=False)["results"] \
            if todo else []
        first: list[dict] = [failed[k] or {} for k in range(len(jobs))]
        for k, step in zip(todo, made):
            first[k] = step
        ids: list[int | None] = []
        for k, ((tp, *_rest), step) in enumerate(zip(jobs, first)):
            cid = ((step.get("result") or {}).get("result") or {}).get("$id") \
                if step.get("status") == "success" else None
            ids.append(cid)
            if cid is None:
                failed[k] = failed[k] or step or {"status": "error", "message": "no copy made"}
            else:
                scratch.append((k, tp, cid))
        rows = lom_get_many([f"id {cid}" for cid in ids if cid is not None], ["loop_start"])
        trims, live = [], iter(rows)
        for k, ((_tp, _source, _at, length, _own), cid) in enumerate(zip(jobs, ids)):
            if cid is None:
                continue
            loop_start = next(live).get("loop_start")
            if not isinstance(loop_start, (int, float)) or isinstance(loop_start, bool):
                failed[k] = {"status": "error", "message": f"Live lost the scratch copy: {loop_start}"}
                ids[k] = None
                continue
            trims.append((k, op_set(f"id {cid}", "loop_end",
                                    float(loop_start) + float(length) * 60.0 / tempo)))
        if trims:
            for (k, _op), step in zip(trims, batch([op for _k, op in trims],
                                                   stop_on_error=False)["results"]):
                if step.get("status") == "error":
                    failed[k], ids[k] = step, None
        wanted = [(k, op_call(tp, "duplicate_clip_to_arrangement", ref(f"id {ids[k]}"), float(at)))
                  for k, (tp, _source, at, *_rest) in enumerate(jobs) if ids[k] is not None]
        done = batch([op for _k, op in wanted], stop_on_error=False)["results"] if wanted else []
        for (k, _op), step in zip(wanted, done):
            failed[k] = step
        out = [step or {"status": "error", "message": "no copy made"} for step in failed]
    except Exception as e:  # a lost connection, say: each job not done says so
        out = [step if step and step.get("status") == "error"
               else {"status": "error", "message": str(e)} for step in failed]
    # the scratch copies go whatever happened; one left behind is an error of its job
    try:
        gone = batch([op_call(tp, "delete_clip", ref(f"id {cid}")) for _k, tp, cid in scratch],
                     stop_on_error=False)["results"] if scratch else []
    except Exception as e:
        gone = [{"status": "error", "message": str(e)}] * len(scratch)
    for (k, _tp, cid), step in zip(scratch, gone):
        if step.get("status") == "error":
            out[k] = {"status": "error", "message": f"a scratch copy (id {cid}) is left past "
                      f"the end of the track: {step.get('message')}"}
    return out


def lom_get_many(paths: list[str], properties: list[str]) -> list[dict]:
    """Several objects' properties in one round trip ({} for one that
    fails; a property Live could not read is left out: Live answers a
    deleted object's properties with {"$error": …})."""
    if not paths:
        return []
    result = batch([{"type": "lom_get", "params": {"path": p, "properties": properties}}
                    for p in paths], stop_on_error=False)
    return [{k: v for k, v in step["result"].items()
             if not (isinstance(v, dict) and "$error" in v)}
            if step.get("status") != "error" and isinstance(step.get("result"), dict) else {}
            for step in result["results"]]


def past_the_end(tp: str, at: float) -> float:
    """A beat after every clip in the track's Arrangement and after `at`
    (whole bars of 4/4 beyond), for a scratch copy (sized_copy)."""
    ends = [c["end_time"] for c in lom_get(f"{tp} arrangement_clips", ["end_time"])["items"]]
    return float((int(max(ends + [at]) // 16) + 64) * 16)


def copy_warnings(info: dict, tempo: float, length: float | None = None,
                  takes_length: bool = True) -> list[str]:
    """What a Session clip's copy will get wrong, from its SIZE_FIELDS: an
    unlooped clip whose end_marker is before its loop_end (Live copies to
    loop_end), an unwarped audio clip whose length in beats (Live's warp
    tempo guess) is not its loop's seconds at the set's tempo. takes_length:
    the caller has a length to pass (duplicate_to_arrangement)."""
    passing = "pass the length or " if takes_length else ""
    out = []
    name = info.get("name") or "The clip"
    span = float(info["loop_end"]) - float(info["loop_start"])
    if info.get("is_midi_clip") is False and info.get("warping") is False:
        real = span * tempo / 60.0  # unwarped: loop times are seconds
        if length is None and abs(real - float(info["length"])) > max(0.05, 0.005 * real):
            out.append(f"{name} is unwarped: Live copies it as {float(info['length']):g} beats "
                       f"(its warp tempo guess), but it plays {real:g} beats at {tempo:g} BPM; "
                       f"{passing}recreate it with create_audio_clip(warping=false)")
    elif info.get("looping") is False and length is None \
            and float(info["end_marker"]) < float(info["loop_end"]) - 1e-6:
        out.append(f"{name} is unlooped with end_marker {float(info['end_marker']):g} before "
                   f"loop_end {float(info['loop_end']):g}: Live copies it to loop_end "
                   f"({span:g} beats); {passing}set loop_end")
    return out


def duplicate_session_clip(track: int | str, clip_index: int, at: float,
                           length: float | None = None) -> dict:
    """Copy a Session clip into the track's Arrangement at `at` (sized_copy)
    and say where it landed: start_time, end_time, and warnings when that is
    not what was asked or the copy is not what the clip plays (copy_warnings)."""
    if length is not None and float(length) <= 0:
        raise ValueError(f"length must be more than 0 beats (got {length})")
    tp = track_path(track)
    source = f"{tp} clip_slots {int(clip_index)} clip"
    if not lom_get(f"{tp} clip_slots {int(clip_index)}", ["has_clip"]).get("has_clip"):
        raise ValueError(f"No clip in slot {clip_index}")
    info = lom_get(source, SIZE_FIELDS)
    tempo = float(lom_get("live_set", ["tempo"])["tempo"])
    with playhead_put_back():
        if unwarped(info) and length is not None:
            steps = trimmed_copies([(tp, source, float(at), float(length),
                                     float(info["length"]))], tempo)
        else:  # the loop is put back whatever happens
            steps = batch(sized_copy(tp, source, float(at), info, length),
                          stop_on_error=False)["results"]
    dup = next((s for s in steps if s.get("type") == "lom_call" and isinstance(s.get("result"), dict)
                and s["result"].get("method") == "duplicate_clip_to_arrangement"), None)
    if dup is None or any(s.get("status") == "error" for s in steps):
        raise RuntimeError("; ".join(s.get("message", "") for s in steps
                                     if s.get("status") == "error") or "Live made no copy")
    held = lom_get(f"id {dup['result']['result']['$id']}", ["name", "start_time", "end_time"])
    want = float(at) + float(length if length is not None else info["length"])
    result: dict[str, Any] = {"clip": held.get("name"), "start_time": held["start_time"],
                              "end_time": held["end_time"]}
    warnings = copy_warnings(info, tempo, length)
    if abs(held["start_time"] - float(at)) > 1e-6 or abs(held["end_time"] - want) > 1e-6:
        warnings.append(f"Live placed it at beats {held['start_time']:g}-{held['end_time']:g}, "
                        f"not {float(at):g}-{want:g}")
    if warnings:
        result["warnings"] = warnings
    return result


@keeping_playhead
def cut_ranges(track: int | str, ranges: list[tuple[float, float]]) -> dict:
    """Leave nothing in the track's Arrangement between each (start, end):
    clips inside go (Track.delete_clip), clips across a boundary are split
    there first by laying a short copy over it and deleting the copy (a copy
    laid over a clip splits it, MIDI and audio alike, Live 12.4.16). The
    copy comes from a scratch Session clip: an empty MIDI clip, or an audio
    clip of a file a clip there plays, looped to its first beat.
    {"clips_removed": n, "clips_trimmed": n (cut at an edge), "left": [...]}
    (left: clips still in a range)."""
    tp = track_path(track)
    ranges = [(float(a), float(b)) for a, b in ranges if b - a > 1e-6]

    def clips() -> list[dict]:
        return lom_get(f"{tp} arrangement_clips", ["start_time", "end_time", "is_midi_clip"])["items"]

    def inside(c: dict) -> bool:
        return any(c["start_time"] >= a - 1e-6 and c["end_time"] <= b + 1e-6 for a, b in ranges)

    def across(c: dict) -> bool:
        return any(c["start_time"] < b - 1e-6 and c["end_time"] > a + 1e-6 for a, b in ranges) \
            and not inside(c)

    now = clips()
    removed = sum(1 for c in now if inside(c))
    trimmed = sum(1 for c in now if across(c))
    if not removed and not trimmed:
        return {"clips_removed": 0, "clips_trimmed": 0}
    width = min([1.0] + [b - a for a, b in ranges])

    def over(c: dict, edge: float) -> bool:  # across the edge, not just touching it
        return c["start_time"] < edge - 1e-6 and c["end_time"] > edge + 1e-6

    cuts = [(a, c) for a, b in ranges for c in now if over(c, a)] \
        + [(b - width, c) for a, b in ranges for c in now if over(c, b)]
    at = sorted({t for t, _c in cuts})
    if at:
        audio = next((c for _t, c in cuts if c.get("is_midi_clip") is False), None)
        with scratch_slot(tp) as slot:
            if audio is None:  # the clips are MIDI, so is the track
                send("lom_call", {"path": slot, "method": "create_clip", "args": [width]})
            else:
                file = lom_get(f"id {audio['$id']}", ["file_path"])["file_path"]
                send("lom_call", {"path": slot, "method": "create_audio_clip", "args": [file]})
                # Live may leave a new clip unwarped (Auto-Warp Long Samples off):
                # warped again, its loop is in beats (Live 12.4.16)
                batch([op_set(f"{slot} clip", "warping", True),
                       op_set(f"{slot} clip", "looping", True),
                       op_set(f"{slot} clip", "loop_start", 0.0),
                       op_set(f"{slot} clip", "loop_end", width)])
            batch([op_call(tp, "duplicate_clip_to_arrangement", ref(f"{slot} clip"), t)
                   for t in at])
        now = clips()
    doomed = [c for c in now if inside(c)]
    if doomed:
        batch([op_call(tp, "delete_clip", ref(f"id {c['$id']}")) for c in doomed],
              stop_on_error=False)
    result: dict[str, Any] = {"clips_removed": removed, "clips_trimmed": trimmed}
    left = [c for c in clips() if inside(c) or across(c)]
    if left:
        result["left"] = [[c["start_time"], c["end_time"]] for c in left]
    return result


def audio_seconds(info: dict) -> float | None:
    """A clip's file length in seconds from sample_length and sample_rate."""
    frames, rate = info.get("sample_length"), info.get("sample_rate")
    if isinstance(frames, (int, float)) and isinstance(rate, (int, float)) and rate > 0:
        return float(frames) / float(rate)
    return None


def clip_read(step: dict) -> bool:
    """Whether a batch step read a clip (a missing clip answers with errors)."""
    result = step.get("result")
    return step.get("status") != "error" and isinstance(result, dict) \
        and isinstance(result.get("length"), (int, float))


def check_fit(warping: bool | None, bars: float | None) -> None:
    """fit_audio_clip's options, refused before a clip is made."""
    if bars is not None and warping is False:
        raise ValueError("bars warps the file to that length: it cannot go with warping=false "
                         "(unwarped, a file plays as long as it is)")
    if bars is not None and float(bars) <= 0:
        raise ValueError(f"bars must be more than 0 (got {bars})")


AUDIO_FIELDS = ["length", "warping", "looping", "loop_start", "loop_end", "end_marker",
                "sample_length", "sample_rate", "is_arrangement_clip", "start_time", "end_time"]


def fit_audio_clip(clip: str, warping: bool | None = None, bars: float | None = None,
                   start: float = 0.0, loop_start: float | None = None) -> dict:
    """Settle a new audio clip: its start back at `start` (from_its_start),
    and what it lasts checked against the file at the set's tempo.

    Live 12.4.16 warps a new clip at the tempo its analysis guesses (a 40 s
    file made at 120 BPM came in as 96 BPM, 64 beats instead of 80), and an
    unwarped Session clip keeps that guess as its length. With `bars` (in the
    set's meter) the file is warped straight to that many bars; with
    warping=False it is first warped to its length at the set's tempo, then
    unwarped, so copies of it last as long as it plays. Each: inner warp
    markers removed, the last moved (move_warp_marker), end_marker and
    loop_end set (an Arrangement clip unlooped, so it ends there).
    Returns what Live holds: length_beats, warping, warp_markers,
    live_guess_bpm (Live's first reading), warp_bpm (warped clips: the
    file's tempo by its warp markers after the fit; the set's tempo when
    it plays as recorded),
    set_bpm, errors (failed steps), notes (what was left
    undone, and why), and a warning when the length is not what
    was asked (bars, warping=False) or, the file lasting whole bars at the
    set's tempo (rendered from a song), not that."""
    check_fit(warping, bars)
    song = lom_get("live_set", ["tempo", "signature_numerator", "signature_denominator"])
    tempo = float(song["tempo"])
    bar = music.beats_per_bar(song["signature_numerator"], song["signature_denominator"])
    read = batch([{"type": "lom_get", "params": {"path": clip, "properties": AUDIO_FIELDS}},
                  {"type": "lom_get", "params": {"path": f"{clip} warp_markers",
                                                 "properties": ["beat_time", "sample_time"]}}],
                 stop_on_error=False)["results"]
    if not clip_read(read[0]):
        return {"errors": [f"Live did not answer for the clip: "
                           f"{read[0].get('message') or read[0].get('result')}"]}
    info = read[0]["result"]
    markers = read[1].get("result", {}).get("items", []) if read[1].get("status") != "error" else []
    notes: list[str] = []
    errors: list[str] = []
    left_unwarped = bars is not None and info.get("warping") is False
    if left_unwarped:
        # Live left it unwarped (Auto-Warp Long Samples off). Warped again, a
        # clip keeps no marker at the file's end (Live 12.4.16: only the start
        # and the shadow marker, end_marker moved), so there is nothing to fit
        # by, and the clip would be changed for nothing: it is left as it is
        notes.append("Live has it unwarped, and warping it again would drop its warp marker "
                     "at the file's end, so it was not fitted to bars; it is left unwarped")
    real = markers[:-1]  # the last is Live's shadow marker, 1/32 beat on
    seconds = audio_seconds(info)
    live_guess = None
    if len(real) >= 2 and real[-1]["sample_time"] > real[0]["sample_time"]:
        live_guess = 60.0 * (real[-1]["beat_time"] - real[0]["beat_time"]) / (
            real[-1]["sample_time"] - real[0]["sample_time"])
    at_tempo = seconds * tempo / 60.0 if seconds else None
    target = float(bars) * bar if bars is not None and warping is not False else at_tempo \
        if warping is False else None
    ops: list[dict] = []
    warped, arranged = info.get("warping") is True, info.get("is_arrangement_clip") is True
    if target and warped and len(real) >= 2 \
            and abs(real[-1]["beat_time"] - real[0]["beat_time"] - target) > 1e-6:
        ops += [op_call(clip, "remove_warp_marker", m["beat_time"]) for m in reversed(real[1:-1])]
        ops.append(op_call(clip, "move_warp_marker", real[-1]["beat_time"],
                           real[0]["beat_time"] + target - real[-1]["beat_time"]))
    if target and warped and len(real) < 2:  # no marker at the file's end to fit by
        errors.append("its warp markers could not be read, so it was not fitted")
    elif target and warped:
        end = real[0]["beat_time"] + target
        if arranged:
            ops.append(op_set(clip, "looping", False))
        markers_first = end > float(info["end_marker"])
        pair = [op_set(clip, "end_marker", end), op_set(clip, "loop_end", end)]
        ops += pair if markers_first else pair[::-1]
    elif target and not notes:
        notes.append("Live had it unwarped already, so it was not fitted")
    if warping is False:  # the start back first: the length it has now is what it keeps
        ops += from_its_start(clip, 0.0)
    if warping is not None and not left_unwarped:  # one left unwarped stays as it is
        ops.append(op_set(clip, "warping", bool(warping)))
    ops += from_its_start(clip, start, loop_start)
    done = batch(ops, stop_on_error=False)
    errors += [s.get("message") for s in done["results"] if s.get("status") == "error"]
    after = batch([{"type": "lom_get", "params": {"path": clip, "properties": AUDIO_FIELDS}},
                   {"type": "lom_get", "params": {"path": f"{clip} warp_markers",
                                                  "properties": ["beat_time", "sample_time"]}}],
                  stop_on_error=False)["results"]
    if not clip_read(after[0]):
        return {"errors": errors + [f"Live did not answer for the clip afterwards: "
                                    f"{after[0].get('message') or after[0].get('result')}"],
                **({"notes": notes} if notes else {})}
    held = after[0]["result"]
    count = len(after[1].get("result", {}).get("items", [])) if after[1].get("status") != "error" \
        else None
    length = float(held["end_time"] - held["start_time"]) if arranged \
        else float(held["length"])
    report: dict[str, Any] = {"length_beats": round(length, 4), "warping": held.get("warping"),
                              "warp_markers": count, "set_bpm": tempo}
    if seconds:
        report["file_seconds"] = round(seconds, 4)
    if live_guess:  # Live's first reading of the file's tempo, before any fit
        report["live_guess_bpm"] = round(live_guess, 2)
    now_real = (after[1].get("result") or {}).get("items", [])[:-1] \
        if after[1].get("status") != "error" else []
    if held.get("warping") is True and len(now_real) >= 2 \
            and now_real[-1]["sample_time"] > now_real[0]["sample_time"]:
        # the file's tempo by its warp markers now: the set's, the file plays as recorded
        report["warp_bpm"] = round(60.0 * (now_real[-1]["beat_time"] - now_real[0]["beat_time"])
                                   / (now_real[-1]["sample_time"] - now_real[0]["sample_time"]), 2)
    expected = float(bars) * bar if bars is not None else at_tempo
    # Without bars, Live's own reading stands (a 90 BPM loop warped to 120 is
    # right) unless the file lasts whole bars at the set's tempo, as a file
    # rendered from a song does: then a length that differs is Live's guess
    # gone wrong (a 20-bar file read at 104 BPM)
    whole = bool(at_tempo) and abs(at_tempo - round(at_tempo / bar) * bar) <= 0.125 \
        and round(at_tempo / bar) >= 1
    checked = bars is not None or warping is False or whole
    if expected and checked and start == 0.0 \
            and abs(length - expected) > max(0.05, 0.005 * expected):
        what = f"{float(bars):g} bars ({expected:g} beats)" if bars is not None else \
            f"{expected:g} beats, the file's {seconds:g} s at {tempo:g} BPM"
        hint = "" if bars is not None or warping is False else \
            (f" (Live warped it at {live_guess:.1f} BPM)" if live_guess else "") + \
            f", {round(at_tempo / bar):g} bars at the set's tempo; pass bars to fit it, " \
            "or warping=false to play it as recorded"
        if held.get("warping") is False:  # it plays the file; its length is what copies take
            plays = f"; the file plays {at_tempo:g} beats" if at_tempo else ""
            report["warning"] = (f"Live keeps it {length:g} beats long (copies last that), "
                                 f"not {what}{plays}")
        else:
            report["warning"] = f"Live made it {length:g} beats, not {what}{hint}"
    if bars is not None and warping is not False and at_tempo and "warning" not in report \
            and abs(at_tempo / expected - 1) > 0.02:
        report["warning"] = (f"{float(bars):g} bars play the file at {at_tempo / expected:.2f}x "
                             f"its speed at {tempo:g} BPM")
    if errors:
        report["errors"] = errors
    if notes:  # what was left undone on purpose, and why
        report["notes"] = notes
    return report


# ── Registration ─────────────────────────────────────────────────────────────

def register(mcp, get_connection: Callable[[], Any]) -> None:
    global _get_connection
    _get_connection = get_connection

    def tool(fn: Callable | None = None, **hints: Any) -> Callable:
        """@tool, or @tool(read_only=True | destructive=True | idempotent=True).

        Hints become MCP tool annotations; see tooling.register_tool.
        """
        def decorate(f: Callable) -> Callable:
            options = dict(hints)
            if options.pop("preview", False):
                f = previewable(f)
            return register_tool(mcp, f, **options)
        return decorate(fn) if fn is not None else decorate

    # ── Generic LOM access ───────────────────────────────────────────────────

    @tool(read_only=True)
    def lom_get_object(path: str, properties: list[str] | None = None) -> str:
        """Read anything in the Live Object Model by path. Without `properties`: the
        object's properties, children (with lengths) and methods; with them, only those
        (on a list, for each item).

        Paths are space separated, as in Max for Live: "live_set", "live_app",
        "live_set view", "live_set tracks 0 devices 1 parameters 2", by name
        'live_set tracks "Bass"', negative indexes "live_set tracks -1", or "id 12345"
        for a "$id" from an earlier result. For anything without a dedicated tool.
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
        Arguments that must be Live objects are written {"$ref": "<path>"}
        or {"$id": n} (an id from an earlier result).
        """
        return to_json(send("lom_call", {"path": path, "method": method,
                                         "args": args or [], "kwargs": kwargs or {}}))

    @tool(destructive=True)
    def run_batch(commands: list[dict], stop_on_error: bool = True) -> str:
        """Run several Remote Script commands in one round trip, on Live's main
        thread; each modifying command is one undo step (Live keeps parameter changes and
        structural edits as separate steps).

        commands: [{"type": <command>, "params": {...}}] — lom_get, lom_set, lom_call,
        param_set_display, clip_write_notes, clip_write_envelope, create_clip,
        set_device_parameter, load_browser_item, fire_clip, …
        e.g. [{"type": "lom_call", "params": {"path": "live_set", "method":
        "create_midi_track", "args": [-1]}}, {"type": "lom_set", "params": {"path":
        "live_set tracks -1", "property": "name", "value": "Pad"}},
        {"type": "param_set_display", "params": {"path": "live_set tracks 0 devices 0
        parameters 1", "value": "-6 dB"}}]
        A moved playhead takes effect after the batch (set_or_delete_cue toggles
        at the old one).
        """
        check_batch(commands)
        return to_json(batch(commands, stop_on_error))

    @tool(read_only=True, ui=ui.SESSION)
    def get_session_overview() -> str:
        """Compact map of the whole set: every track (type, group, mute/solo/arm,
        volume, pan, sends, devices, Session clips), returns, master, scenes,
        cue points, tempo, loop, key/scale, and the notes kept about this set
        and the user (see memory). Cheaper than get_session_snapshot — call
        this first to orient yourself.
        """
        overview = send("get_session_overview")
        try:
            in_set = set_notes()
        except AbletonCommandError:  # a Live without Song.get_data (before 11)
            in_set = []
        kept = {"set": notes_store.brief(in_set), "user": notes_store.brief(notes_store.load_user())}
        if kept["set"] or kept["user"]:
            overview["memory"] = {k: v for k, v in kept.items() if v}
        return to_json(overview)

    @tool
    def memory(action: str = "recall", text: str | None = None, scope: str = "set",
               key: str | None = None) -> str:
        """Notes that outlast the conversation. scope="set" keeps them inside
        this Live set, saved with it: what the song is, references, decisions
        ("kick at -6 dB"). scope="user" keeps them for every set: the user's
        taste and habits. action: recall (both) | remember (text; a key
        replaces that key's earlier note) | forget (key, or words of the
        note). get_session_overview shows them too; none enter Live's undo
        history. Remember what the user states as a preference or decision.
        """
        action, scope = action.strip().lower(), scope.strip().lower()
        if scope not in ("set", "user"):
            raise ValueError("scope must be set or user")
        if action == "recall":
            return to_json({"set": set_notes(), "user": notes_store.load_user()})
        notes = set_notes() if scope == "set" else notes_store.load_user()
        if action == "remember":
            notes, outcome = notes_store.add(notes, text or "", key)
            result: dict[str, Any] = {outcome: text, "scope": scope, "notes": len(notes)}
        elif action == "forget":
            notes, removed = notes_store.forget(notes, key, text)
            if not removed:
                raise ValueError(f"No {scope} note matches; recall lists them")
            result = {"forgot": [n["text"] for n in removed], "scope": scope}
        else:
            raise ValueError("action must be recall, remember or forget")
        if scope == "set":
            save_set_notes(notes)
            result["kept_in"] = "this Live set: saved with it when the set is saved"
        else:
            notes_store.save_user(notes)
            result["kept_in"] = str(notes_store.user_file())
        return to_json(result)

    # ── Tracks & mixer ───────────────────────────────────────────────────────

    @tool(idempotent=True)
    def set_track_mixer(track: int | str, volume_db: float | None = None,
                        pan: float | str | None = None, mute: bool | None = None,
                        solo: bool | None = None, arm: bool | None = None,
                        sends_db: dict[str, float] | None = None,
                        monitoring: str | None = None) -> str:
        """Set mixer state for a track, return track ("return A") or "master".

        - volume_db: fader in dB, -inf .. +6 (0 = unity; below Live's lowest
          level, about -70, it is -inf; 6.1 sets 6.0 with a note, more is
          refused)
        - pan: -1.0 (left) .. 1.0 (right), or Live text like "25L", "C", "40R"
        - sends_db: {"A": -12, "B": -6} (letters or 0-based indexes)
        - monitoring: "in", "auto" or "off"
        Applied in one round-trip; undoing it takes one Cmd+Z (two if a name
        or color change is mixed in — Live files those separately).
        """
        auto_snapshot("set_track_mixer", [track])
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
        summary = batch_summary(batch(ops, stop_on_error=False))
        if not summary["ok"]:
            # a part that failed is an error, not a success with ok: false
            # in its text (a send set to "-80 dB" was refused that way)
            failed = [s["error"] for s in summary["steps"] if isinstance(s, dict) and "error" in s]
            applied = [s for s in summary["steps"] if not (isinstance(s, dict) and "error" in s)]
            raise RuntimeError(f"{len(failed)} of {len(ops)} mixer changes failed: "
                               f"{'; '.join(map(str, failed))}. Applied: {to_json(applied)}")
        return to_json(summary)

    @tool(destructive=True)
    def manage_track(action: Literal["create_midi", "create_audio", "create_return", "delete",
                                     "duplicate", "rename", "set_color", "fold", "unfold",
                                     "delete_recording_tracks"],
                     track: int | str | None = None, name: str | None = None,
                     color: str | int | None = None, index: int = -1,
                     go_ahead: Annotated[ElicitationResult[_Confirm],
                                        Resolve(ask_track_delete)] = GO_AHEAD) -> str:
        """Create, delete, duplicate, rename, recolor or fold tracks.

        - index: insert position for create_* (-1 = end)
        - color: hex "#RRGGBB" or a Live color index (0-69)
        - delete_recording_tracks: every "MCP Rec …" track renders kept
          (refused while one records)
        """
        require_go_ahead(go_ahead)
        if action == "delete_recording_tracks":
            deleted = []
            for track_id, name in recording_tracks():
                # by id: each index is looked up right before its delete
                ids = [t.get("$id") for t in lom_get("live_set tracks", ["name"])["items"]]
                if track_id in ids:
                    send("lom_call", {"path": "live_set", "method": "delete_track",
                                      "args": [ids.index(track_id)]})
                    deleted.append(name)
            return to_json({"deleted": deleted,
                            "note": "Their recordings stay in the project's Samples/Recorded."})
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
                ops.append(op_set(tp, prop, ref(f"{tp} available_{prop}s {named(f'{tp} available_{prop}s', value)}")))
        return to_json(batch_summary(batch(ops)))

    # ── Scenes ───────────────────────────────────────────────────────────────

    @tool(destructive=True)
    def manage_scene(action: str, scene: int | str | None = None, name: str | None = None,
                     tempo: float | None = None, index: int = -1,
                     go_ahead: Annotated[ElicitationResult[_Confirm],
                                        Resolve(ask_scene_delete)] = GO_AHEAD) -> str:
        """Scenes: create | delete | duplicate | fire | rename | set_tempo |
        clear_tempo | stop_all_clips | capture (capture playing clips into a new scene).
        scene: index or name. index: insert position for create (-1 = end).
        """
        require_go_ahead(go_ahead)
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
                    length: float = 4.0, name: str | None = None,
                    go_ahead: Annotated[ElicitationResult[_Confirm],
                                       Resolve(ask_clip_delete)] = GO_AHEAD) -> str:
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
        require_go_ahead(go_ahead)
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
        Times are clip-relative beats. Session clips only (Live's API refuses
        envelopes on Arrangement clips): write_arrangement_automation and
        record_automation put automation into the Arrangement.
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
        """Read the notes of a Session or Arrangement MIDI clip, optionally within
        a time window (beats) and pitch range (MIDI number or "C3"). Notes come
        as rows under "note_columns"; mute, probability, velocity_deviation and
        release_velocity that differ from Live's defaults are in "note_extras"
        by row index.
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
        result.update(note_table(result["notes"]))
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
        start_time is in beats from the clip's start, also for an Arrangement
        clip that starts later in the song: notes past the clip's end are silent.
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

    @tool(destructive=True, ui=ui.NOTES, preview=True)
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
                             time_end: float | None = None,
                             preview: bool = False) -> str:
        """Transform a clip's MIDI notes (or only those in a pitch and/or time range)
        as one edit; note ids and MPE are kept (Live 11+).

        operation:
        - transpose (semitones) | transpose_in_scale (steps, key, scale) |
          constrain_to_scale (key, scale)
        - quantize (grid in beats, 0.25 = 1/16; strength 0..1) | humanize (timing,
          velocity_amount, seed)
        - velocity_scale (factor, offset) | velocity_ramp (start_velocity → end_velocity)
        - reverse | invert (axis pitch) | legato | double_speed | half_speed
        - set_probability (value 0..1) | set_velocity_deviation (value) | dedupe | delete
        key/scale default to the set's Key & Scale.
        preview=True shows the result without writing it.
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

    @tool(destructive=True, ui=ui.NOTES, preview=True)
    def apply_midi_tool(track: int | str, tool: str, clip_index: int | None = None,
                        arrangement_clip_index: int | None = None,
                        settings: dict[str, Any] | None = None,
                        pitch_low: int | str | None = None,
                        pitch_high: int | str | None = None,
                        time_start: float | None = None,
                        time_end: float | None = None,
                        preview: bool = False) -> str:
        """Live 12's MIDI Tools (Transformations) on a clip's notes, or only those
        in a pitch/time range; one undo step, note ids and MPE kept.

        tool → settings (defaults):
        - strum: amount (0.04 beats), direction up|down|alternate
        - ornament: kind flam|grace, spacing (0.0625), velocity (0.6×), step (-1 scale steps)
        - chop: parts (2), velocity_decay (0) — ratchets
        - connect: rate (0.25), max_notes (8) — scale passing notes between notes
        - time_warp: mode accelerando|ritardando, amount (0.5)
        - velocity_shaper: shape ramp_up|ramp_down|swell|dip|sine|random|accent,
          low (40), high (120), cycles (1), seed
        - recombine: mode rotate|reverse|shuffle, part pitch|velocity|duration, amount (1), seed
        - span: mode percent|fixed|legato|scale, value (0.5)
        - arpeggiate: style up|down|updown|downup|random|played, rate (0.25), gate (0.9),
          octaves (1), seed
        e.g. tool="strum", settings={"amount": 0.03, "direction": "alternate"}
        preview=True shows the result without writing it.
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

    @tool(destructive=True, ui=ui.NOTES, preview=True)
    def generate_chord_progression(track: int | str, progression: str | None = None,
                                   key: str | None = None, scale: str | None = None,
                                   clip_index: int | None = 0,
                                   arrangement_time: float | None = None,
                                   bars_per_chord: float = 1.0, octave: int = 3,
                                   voicing: str = "smooth", rhythm: str = "sustain",
                                   add_bass_note: bool = False, velocity: int = 90,
                                   replace: bool = True, clip_name: str | None = None,
                                   preview: bool = False) -> str:
        """Write a chord progression into a MIDI clip (created if missing).

        - progression: roman numerals ("ii7 V7 Imaj7", "i iv bVII III") or chord symbols
          ("Am F C G", "C/E"); default four of the scale's triads
        - key/scale: default the set's Key & Scale; bars follow its meter
        - voicing: smooth (voice-led) | close | open | drop2 | spread
        - rhythm: sustain | half | quarters | eighths | offbeat | syncopated | charleston | stabs
        - octave: 3 centres close chords on middle C (C3 = 60 in Live)
        - arrangement_time: a new Arrangement clip at this beat instead of a Session slot
        preview=True shows the result without writing it.
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

    @tool(destructive=True, ui=ui.NOTES, preview=True)
    def generate_drum_pattern(track: int | str, style: str = "house", bars: int = 2,
                              clip_index: int | None = 0,
                              arrangement_time: float | None = None,
                              swing: float = 0.0, humanize: float = 0.3, fill: bool = True,
                              mapping: dict[str, int | str] | None = None,
                              auto_map_drum_rack: bool = True, seed: int | None = None,
                              velocity: int = 100, replace: bool = True,
                              clip_name: str | None = None,
                              preview: bool = False) -> str:
        """Write a drum groove into a MIDI clip on a Drum Rack track.

        style: house | techno | trap | drill | boom_bap | lofi | dnb | breakbeat | uk_garage |
        reggaeton | afrobeats | rock | funk | disco | halftime (fitted to the set's meter)
        - swing 0..1 (off-beat 16ths); humanize 0..1; fill: a snare fill on the last beat
        - auto_map_drum_rack: find pads by their names; otherwise General MIDI (kick 36,
          snare 38, clap 39, closed hat 42, open hat 46)
        - mapping: {"kick": 36, "closed_hat": "F#1"}
        preview=True shows the result without writing it.
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

    @tool(destructive=True, ui=ui.NOTES, preview=True)
    def generate_euclidean(track: int | str, voices: list[dict], bars: int = 2,
                           step: float | str = "1/16", clip_index: int | None = 0,
                           arrangement_time: float | None = None, replace: bool = True,
                           clip_name: str | None = None,
                           preview: bool = False) -> str:
        """Euclidean rhythms (Live 12's Euclidean generator): each voice spreads
        `pulses` hits evenly over `steps`; cycles of different lengths drift apart.

        voices: [{"pad": "kick", "pulses": 3, "steps": 8, "rotation": 0, "velocity": 100,
        "accent": 120}, …]; pad: a Drum Rack role, a note name ("C1") or a MIDI number;
        accent: the velocity of each cycle's first hit.
        step: "1/16" (default), "1/8", "1/8T" or beats.
        Classics: E(3,8) tresillo, E(5,8) cinquillo, E(5,16) bossa, E(4,9) aksak (2+2+2+3).
        preview=True shows the result without writing it.
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
        """Turkish makams (Arel-Ezgi-Uzdilek, 53 commas per octave) and usuls. No
        name: all of them. With a name (rast, uşşak, hicaz, hüseynî, kürdî, saba, segâh,
        hüzzam, nihavend, karcığar, …): steps in commas, degrees in cents, karar, güçlü
        and the 12-tone approximation on `tonic` (default its karar). Every generator
        takes a makam as its scale (scale="hicaz", key="A").
        tuning_file=True writes an .ascl tuning to User Library › Tunings with the true
        comma pitches; the user double-clicks it in Live's browser.
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

    @tool(destructive=True, ui=ui.NOTES, preview=True)
    def generate_usul(track: int | str, usul: str = "aksak", cycles: int = 4,
                      clip_index: int | None = 0, arrangement_time: float | None = None,
                      mapping: dict[str, int | str] | None = None, velocity: int = 110,
                      ornament: float = 0.0, seed: int | None = None,
                      set_meter: bool = False, replace: bool = True,
                      clip_name: str | None = None,
                      preview: bool = False) -> str:
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
        preview=True shows the result without writing it.
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
        if set_meter and not PREVIEW.get():
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

    @tool(destructive=True, ui=ui.NOTES, preview=True)
    def generate_bassline(track: int | str, progression: str | None = None,
                          key: str | None = None, scale: str | None = None,
                          clip_index: int | None = 0,
                          arrangement_time: float | None = None,
                          bars_per_chord: float = 1.0, octave: int = 1,
                          style: str = "root", velocity: int = 100,
                          seed: int | None = None, replace: bool = True,
                          clip_name: str | None = None,
                          preview: bool = False) -> str:
        """Write a bassline that follows a chord progression.

        style: root (whole notes) | octave (8th-note octave jumps) | offbeat
        (house) | syncopated | walking (scale steps approaching the next root) |
        pulse (16ths) | 808 (long notes). octave=1 → roots around C1 (36).
        key/scale default to the set's Key & Scale (Live 12); the progression
        to four chords from the scale's own triads. To follow chords already
        in a clip, run analyze_harmony and pass its progression and
        bars_per_chord.
        preview=True shows the result without writing it.
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

    @tool(destructive=True, ui=ui.NOTES, preview=True)
    def generate_arpeggio(track: int | str, progression: str | None = None,
                          key: str | None = None, scale: str | None = None,
                          clip_index: int | None = 0, arrangement_time: float | None = None,
                          bars_per_chord: float = 1.0, octave: int = 4,
                          pattern: str = "up", rate: float = 0.25, octaves: int = 1,
                          gate: float = 0.8, velocity: int = 90, seed: int | None = None,
                          replace: bool = True, clip_name: str | None = None,
                          preview: bool = False) -> str:
        """Write an arpeggio over a chord progression.

        pattern: up | down | updown | downup | random | converge | pinky
        rate: note length in beats (0.25 = 1/16, 0.5 = 1/8, 1/3 = 1/8 triplet)
        octaves: how many octaves the arpeggio spans; gate: note length fraction
        key/scale default to the set's Key & Scale (Live 12); the progression
        to four chords from the scale's own triads.
        preview=True shows the result without writing it.
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

    @tool(destructive=True, ui=ui.NOTES, preview=True)
    def generate_melody(track: int | str, key: str | None = None, scale: str | None = None,
                        bars: int = 4, progression: str | None = None,
                        bars_per_chord: float = 1.0, clip_index: int | None = 0,
                        arrangement_time: float | None = None, low: str = "C4",
                        high: str = "C6", density: float = 0.6, seed: int | None = None,
                        velocity: int = 95, replace: bool = True,
                        clip_name: str | None = None,
                        preview: bool = False) -> str:
        """Write a phrase-structured melody (A A' B A'') in a key/scale.

        If a progression is given, strong beats land on chord tones. density
        0..1 controls how busy it is; low/high bound the range; vary `seed` for
        alternatives and keep it to reproduce one. key/scale default to the
        set's Key & Scale (Live 12); bars follow the set's time signature.
        preview=True shows the result without writing it.
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
                         clip_name: str | None = None,
                         length_bars: float | None = None) -> str:
        """Import the notes of a .mid file into a MIDI clip (created if missing).

        Without `track`, nothing is imported: the file's tracks, channels, note
        counts, tempo and time signature are listed instead.
        - midi_track: index or name of one track in the file (default: all
          tracks merged); channel: 1-16 to take one channel only
        - clip_index (Session slot) or arrangement_time (beats, Live 12+)
        - length_bars: the clip's length (default: whole bars to the last
          note). The file's tempo is reported, not applied to the set.
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
        if length_bars is not None:
            if float(length_bars) <= 0:
                raise ValueError("length_bars must be more than 0")
            length = float(length_bars) * bar
        else:  # a release tail just past a bar line is not another bar; a note in it is
            end = max(n["start_time"] + n["duration"] for n in notes)
            last = max(n["start_time"] for n in notes)
            length = max(1, -(-(end - MIDI_TAIL_TOLERANCE) // bar),
                         -(-(last + 1e-6) // bar)) * bar
        dropped = sum(1 for n in notes if n["start_time"] >= length - 1e-9)
        notes = [dict(n, duration=min(n["duration"], length - n["start_time"]))
                 for n in notes if n["start_time"] < length - 1e-9]
        name = clip_name or Path(info["file"]).stem
        clip = prepare_clip(track, clip_index, length, arrangement_time, name)
        written = write_notes(clip, notes, "replace" if replace else "add")
        extra = {"file": info["file"], "length_beats": length, "file_tempo_bpm": info["tempo_bpm"],
                 "file_time_signature": info["time_signature"]}
        if dropped:
            extra["notes_after_the_end"] = dropped
        return generated_result(clip, notes, written, extra)

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

    # ── Live Set files (.als) ────────────────────────────────────────────────

    @tool(read_only=True)
    def read_live_set(path: str, detail: str = "summary", compare_to: str | None = None) -> str:
        """Read a Live Set file (.als) without opening it in Live: the Live
        version that saved it, tempo, meter, key, arrangement length, locators,
        scenes, every track with its mixer, devices (racks, third-party
        plug-ins, Max for Live) and clips, and the samples it uses, including
        missing ones.

        detail: summary (devices as one line each, clip counts) | full (rack
        chains and drum pads, every clip with its position, every sample).
        compare_to: another .als, e.g. one from the project's Backup folder:
        report what changed from that one to `path` instead.
        """
        from . import als

        if compare_to:
            return to_json(als.compare(path, compare_to))
        return to_json(als.read_set(path, detail))

    @tool(read_only=True)
    def find_live_sets(folder: str | None = None, query: str | None = None,
                       include_backups: bool = False, limit: int = 30) -> str:
        """Find Live Sets (.als) in a folder and its subfolders, newest first,
        with tempo, meter, key, length, track counts, third-party plug-ins and
        missing samples.

        folder: default ~/Music/Ableton and ~/Documents (Ableton folders
        first). query: matches file names, track names and plug-in names, so
        "Serum" finds the sets that use Serum. Backup copies are skipped
        unless include_backups.
        """
        from . import als

        return to_json(als.find_sets(folder, query, include_backups, max(1, min(int(limit), 200))))

    @tool(destructive=True)
    def import_from_live_set(path: str, track: int | str, clip_index: int | None = None,
                             arrangement_clip_index: int | None = None,
                             to_track: int | str | None = None, to_clip_index: int | None = 0,
                             arrangement_time: float | None = None,
                             midi_path: str | None = None, replace: bool = True,
                             clip_name: str | None = None) -> str:
        """Take a MIDI clip's notes from a Live Set file (.als) on disk.

        - track and clip_index (Session slot) or arrangement_clip_index pick the
          clip in the file (read_live_set with detail="full" lists them). The
          notes are those you hear: the loop, or start to end marker.
        - to_track: write them into the open set, in Session slot
          to_clip_index or in the Arrangement at arrangement_time (beats); the
          new clip gets the source clip's length.
        - midi_path: write a .mid file (a folder gives a free name in it),
          with the file's tempo and time signature.
        Without to_track or midi_path the notes are only listed.
        """
        from . import als, midi_files

        source = als.clip(path, track, clip_index, arrangement_clip_index)
        notes = [{k: v for k, v in n.items()} for n in source["notes"]]
        if not notes:
            raise ValueError(f"The clip {source['clip']!r} has no notes in its played region")
        if to_track is None and midi_path is None:
            return to_json({**source, **note_table(source["notes"])})
        name = clip_name or source["clip"] or Path(source["file"]).stem
        result: dict[str, Any] = {"source": {k: source[k] for k in ("file", "track", "clip",
                                                                     "length", "notes_outside")}}
        if midi_path:
            target = Path(midi_path).expanduser()
            if target.is_dir() or not target.suffix:
                target = midi_files.free_path(target, name)
            elif target.exists():
                raise ValueError(f"{target} exists; choose another midi_path")
            numerator, denominator = source["set_time_signature"]
            result["midi_file"] = midi_files.write_midi(
                target, notes, float(source["set_tempo"] or 120.0), numerator, denominator, name)
        if to_track is not None:
            length = max(float(source["length"]), 1.0)
            clip = prepare_clip(to_track, None if arrangement_time is not None else to_clip_index,
                                length, arrangement_time, name)
            written = write_notes(clip, notes, "replace" if replace else "add")
            result.update(json.loads(generated_result(clip, notes, written, {
                "length_beats": length})))
        return to_json(result)

    # ── Machine learning (run by ml.py in their own environments) ────────────

    async def audio_source(path: str | None, track: int | str | None, clip_index: int | None,
                           arrangement_clip_index: int | None) -> dict:
        """The audio file to work on: `path`, or the file behind an audio clip
        (with what decides where its sound plays: warping, start marker, loop)."""
        if path:
            file = Path(path).expanduser().resolve()  # jobs run in another folder
            if not file.is_file():
                raise FileNotFoundError(f"No such audio file: {file}")
            return {"file": str(file), "name": file.stem}
        if track is None:
            raise ValueError("Give path (an audio file), or track with clip_index or "
                             "arrangement_clip_index (an audio clip)")
        cp = clip_path(track, clip_index, arrangement_clip_index)
        info = await asend("lom_get", {"path": cp, "properties": [
            "is_audio_clip", "file_path", "name", "warping", "warp_mode", "start_time",
            "start_marker", "loop_start", "looping", "sample_rate"]})
        if not info.get("is_audio_clip"):
            raise ValueError("That is a MIDI clip; these tools work on audio")
        source = {"file": info["file_path"], "name": info.get("name") or Path(info["file_path"]).stem,
                  "track": track, "warping": info.get("warping"), "warp_mode": info.get("warp_mode"),
                  "clip": f"id {info['$id']}", "start_marker": info.get("start_marker"),
                  "loop_start": info.get("loop_start"), "looping": info.get("looping"),
                  "sample_rate": info.get("sample_rate")}
        if arrangement_clip_index is not None:
            source["start_time"] = info.get("start_time")
        else:
            source["clip_index"] = clip_index
        return source

    def ml_progress(ctx: Context | None):
        async def report(fraction: float, message: str) -> None:
            await report_progress(ctx, round(fraction * 100, 1), 100, message)
        return report

    @tool
    async def separate_stems(path: str | None = None, track: int | str | None = None,
                             clip_index: int | None = None,
                             arrangement_clip_index: int | None = None,
                             two_stems: str | None = None, model: str = "htdemucs",
                             out_dir: str | None = None, to_tracks: bool = False,
                             overwrite: bool = False,
                             ctx: Context | None = None) -> str:
        """Split a mix into drums, bass, vocals and other with Demucs (htdemucs_6s adds
        guitar and piano), from an audio file or the file behind an audio clip.

        - two_stems: "vocals" (or drums, bass, other) gives that stem plus the rest
        - model: htdemucs | htdemucs_ft (cleaner, ~4× slower) | htdemucs_6s
        - out_dir: default ~/Music/Ableton MCP/Stems/<name> ("<name> 2" when
          another file's stems are there); one holding another file's (or
          unknown) stems is refused unless overwrite
        - to_tracks: one audio track per stem in the set, placed where the source clip is
        Uses the GPU if there is one; the first use downloads ~500 MB.
        """
        from . import ml
        from .audio_analysis import analyze

        source = await audio_source(path, track, clip_index, arrangement_clip_index)
        stem = re.sub(r'[\\/:*?"<>|]+', "_", source["name"]).strip() or "stems"
        # Validated now, chosen when this job's turn comes (a queued call
        # must not pick the folder a running one is about to fill)
        ml.stem_folder(out_dir, stem, source["file"], overwrite)

        def claim(job: dict) -> None:
            target = ml.stem_folder(out_dir, stem, source["file"], overwrite)
            target.mkdir(parents=True, exist_ok=True)
            ml.mark_stems(target, source["file"])
            job["out_dir"] = str(target)

        job = {"path": source["file"], "model": model}
        if two_stems:
            job["two_stems"] = two_stems
        result = await ml.run("separate", job, ml_progress(ctx), prepare=claim)

        def loudness(file: str) -> dict:
            info = analyze(file, max_seconds=600)
            lufs = info.get("integrated_lufs")
            return {"file": file, "lufs": lufs, "peak_dbtp": info.get("true_peak_dbtp"),
                    **({"silent": True} if lufs is None or lufs < -50 else {})}

        stems = {name: await asyncio.to_thread(loudness, file)
                 for name, file in result["stems"].items()}
        payload: dict[str, Any] = {"source": source["file"], "stems": stems,
                                   "model": result["model"], "device": result["device"],
                                   "seconds": result["seconds"]}
        empty = [name for name, s in stems.items() if s.get("silent")]
        if empty:
            payload["observations"] = [f"Nothing in: {', '.join(empty)} (the source has no "
                                       "such part, or very little of it)."]
        if to_tracks:
            payload["tracks"] = await place_stems(source, result["stems"])
        return to_json(payload)

    async def place_stems(source: dict, stems: dict[str, str]) -> list[dict]:
        """One audio track per stem after the source track, each stem where the source is."""
        index = -1
        if source.get("track") is not None:
            index = await asyncio.to_thread(index_in, "live_set tracks", source["track"]) + 1
        placed = []
        for offset, (name, file) in enumerate(stems.items()):
            made = await asend("lom_call", {"path": "live_set", "method": "create_audio_track",
                                            "args": [index if index < 0 else index + offset]})
            tp = f"id {made['result']['$id']}"
            label = f"{source['name']} {name.replace('_', ' ')}"
            await asend("lom_set", {"path": tp, "property": "name", "value": label})
            if source.get("start_time") is not None:
                clip = await keeping_the_playhead(asend("lom_call", {
                    "path": tp, "method": "create_audio_clip",
                    "args": [file, float(source["start_time"])]}))
                where = {"arrangement_time": source["start_time"]}
            else:
                row = source.get("clip_index") or 0
                clip = await asend("lom_call", {"path": f"{tp} clip_slots {int(row)}",
                                                "method": "create_audio_clip", "args": [file]})
                where = {"clip_index": row}
            clip_ref = clip.get("result") or {}
            row = {"track": label, **where}
            if clip_ref.get("$id") and source.get("warping") is not True:
                # The source plays the file at its own speed (unwarped): so must its stems,
                # from where it plays (seconds), not where Live's warp analysis put the start.
                # A warped source's beats are its own: its stems are left as Live made them
                unwarped = source.get("warping") is False
                fitted = await asyncio.to_thread(
                    fit_audio_clip, f"id {clip_ref['$id']}", False if unwarped else None, None,
                    float(source.get("start_marker") or 0.0) if unwarped else 0.0,
                    source.get("loop_start") if unwarped else None)
                row.update({k: fitted[k] for k in ("warning", "errors", "notes") if k in fitted})
            placed.append(row)
        return placed

    async def warped_beats(source: dict, seconds: list[float]) -> dict[float, float] | None:
        """Live's own beat for each time in a warped clip's file
        (Clip.sample_to_beat_time takes sample frames), or None if Live
        cannot say."""
        rate = source.get("sample_rate")
        if not isinstance(rate, (int, float)) or isinstance(rate, bool) or rate <= 0:
            try:
                import soundfile as sf
                rate = sf.info(source["file"]).samplerate
            except Exception:
                return None
        beats: dict[float, float] = {}
        for first in range(0, len(seconds), 1000):
            chunk = seconds[first:first + 1000]
            try:
                done = await asend("batch", {"commands": [
                    op_call(source["clip"], "sample_to_beat_time", t * rate) for t in chunk]})
            except Exception as e:
                logger.debug("sample_to_beat_time failed: %s", e)
                return None
            if done.get("failed"):
                return None
            for t, step in zip(chunk, done["results"]):
                beats[t] = float(step["result"]["result"])
        return beats

    async def clip_timing(source: dict, events: list[dict], tempo: float, tempo_given: bool,
                          offset_beats: float) -> tuple[Callable[[float], float], dict]:
        """How a time in the file becomes beats from the source clip's start,
        and what the result says about it. A warped clip follows Live's warp
        (unless a tempo is given); an unwarped one plays its file in real
        time from its start marker (seconds); a file plays from second 0."""
        per_second = tempo / 60.0
        marker = source.get("start_marker")
        start = float(marker) if isinstance(marker, (int, float)) and not isinstance(marker, bool) else 0.0
        timing: dict[str, Any] = {"offset_beats": offset_beats}
        if source.get("looping"):
            timing["note"] = "The clip loops; the notes follow its file once."
        if "clip" not in source:
            timing.update(method="tempo", tempo=tempo)
            return (lambda t: t * per_second), timing
        if source.get("warping"):
            if not tempo_given:
                times = sorted({float(e["start"]) for e in events} | {float(e["end"]) for e in events})
                mapped = await warped_beats(source, times)
                if mapped is not None:
                    timing.update(method="warp", start_marker_beats=start)
                    return (lambda t: mapped[t] - start), timing
                timing["warning"] = ("Live did not map this warped clip's times (sample_to_beat_time); "
                                     "they assume its file plays straight at the set's tempo")
            timing.update(method="tempo", tempo=tempo, start_marker_beats=start)
            return (lambda t: t * per_second - start), timing
        timing.update(method="tempo", tempo=tempo, start_marker_seconds=start)
        return (lambda t: (t - start) * per_second), timing

    def note_velocities(file: str, events: list[dict]) -> tuple[list[int] | None, dict]:
        """Velocities from each note's level in the audio (audio_analysis.note_levels)."""
        from . import ml
        from .audio_analysis import load_audio, note_levels

        try:
            data, rate = load_audio(file, max_seconds=None)
        except Exception as e:
            return None, {"from": "amplitude", "warning": f"Could not read the audio for levels: {e}"}
        levels = note_levels(data.mean(axis=1), rate, events).tolist()
        measured = [v for v in levels if v == v]
        return ml.level_velocities(levels), {
            "from": "level", "loudest_db": round(max(measured), 1) if measured else None}

    @tool(destructive=True)
    async def audio_to_midi(path: str | None = None, track: int | str | None = None,
                            clip_index: int | None = None,
                            arrangement_clip_index: int | None = None,
                            to_track: int | str | None = None, to_clip_index: int | None = None,
                            arrangement_time: float | None = None, midi_path: str | None = None,
                            preset: Literal["melodic", "chords", "legacy"] = "melodic",
                            tempo: float | None = None, offset_beats: float = 0.0,
                            onset_threshold: float | None = None,
                            frame_threshold: float | None = None,
                            minimum_note_ms: float | None = None, melodia_trick: bool | None = None,
                            lowest: int | str | None = None, highest: int | str | None = None,
                            bends: bool = False, replace: bool = True, clip_name: str | None = None,
                            ctx: Context | None = None) -> str:
        """Turn audio into MIDI notes with Spotify's Basic Pitch (melodies, bass lines,
        chords; not drums), from an audio file or the file behind an audio clip.

        - to_track: write a MIDI clip there (Session slot to_clip_index, or
          arrangement_time; default where the source clip is); midi_path: a .mid file;
          with neither, the notes are listed
        - preset: melodic (onset 0.6, frame 0.3, 35 ms; melodia only below MIDI 52,
          165 Hz, where 808s and sub basses show no onsets) | chords (melodia at every
          pitch: soft pad onsets and sine leads, more ghost notes) | legacy (0.5, 0.3,
          127.7 ms, melodia on, velocity 127 × Basic Pitch's confidence); onset_threshold,
          frame_threshold (0..1, higher: fewer notes), minimum_note_ms and melodia_trick
          (then at every pitch) override it
        - lowest/highest: Basic Pitch's minimum/maximum_frequency, a note ("E1", 40) or
          "82.4 Hz"; for a melody over an 808, set lowest just above the 808
        - Timing: a warped clip follows Live's warp (unless tempo is given); else
          seconds × tempo (default the set's) from an unwarped clip's start marker or a
          file's start; offset_beats moves every note; "timing" in the result says which
        - Velocity follows each note's level at its pitch (loudest 127, 12 dB down 64)
        - bends=true adds each bent note's pitch bend in cents, one value per 11.6 ms
          (corrected: Basic Pitch reads 33 cents sharp); returned only, as Live's API
          has no per-note pitch bend
        The first use downloads Basic Pitch (~150 MB).
        """
        from . import midi_files, ml

        source = await audio_source(path, track, clip_index, arrangement_clip_index)
        settings = ml.transcription_settings(
            preset, onset_threshold=onset_threshold, frame_threshold=frame_threshold,
            minimum_note_ms=minimum_note_ms, melodia_trick=melodia_trick)
        velocity_from = settings.pop("velocity")
        job: dict[str, Any] = {"path": source["file"], **settings}
        for key, value in (("minimum_frequency", lowest), ("maximum_frequency", highest)):
            if value is not None:
                job[key] = music.frequency(value)
        result = await ml.run("transcribe", job, ml_progress(ctx))
        events = ml.drop_octave_ghosts(result["notes"], job.get("melodia_below"))
        if not events:
            raise ValueError("No notes found; try preset=\"chords\" (melodia at every pitch, for "
                             "notes without an attack) or a lower frame_threshold")
        context = await asyncio.to_thread(song_context, True)
        tempo_used = tempo
        if tempo_used is None:
            try:
                tempo_used = float((await asend("lom_get", {"path": "live_set",
                                                            "properties": ["tempo"]}))["tempo"])
            except Exception:
                tempo_used = 120.0
        beat_of, timing = await clip_timing(source, events, float(tempo_used), tempo is not None,
                                            offset_beats)
        velocities, velocity = None, {"from": "amplitude"}
        if velocity_from == "level":
            velocities, velocity = await asyncio.to_thread(note_velocities, source["file"], events)
        notes, kept = ml.place_notes(events, beat_of, velocities, offset_beats)
        if not notes:
            raise ValueError("No notes start after the clip's start marker")
        if len(kept) < len(events):
            timing["notes_before_clip_start"] = len(events) - len(kept)
        bar = context["beats_per_bar"]
        end = max(n["start_time"] + n["duration"] for n in notes)
        length = max(1, -(-end // bar)) * bar
        pitches = sorted({n["pitch"] for n in notes})
        payload: dict[str, Any] = {
            "source": source["file"], "notes_found": len(notes), "preset": preset,
            "settings": {k: v for k, v in job.items() if k != "path"}, "timing": timing,
            "velocity": velocity, "tempo_used": tempo_used,
            "pitch_range": [music.note_name(pitches[0]), music.note_name(pitches[-1])],
            "length_beats": length}
        if bends:
            bent = []
            for note, index in zip(notes, kept):
                cents = ml.bend_cents(events[index].get("bends") or [])
                if any(cents):
                    bent.append({"pitch": note["pitch"], "start_time": note["start_time"], "cents": cents})
            payload["bends"] = {"frame_s": round(float(result.get("frame_s") or ml.BEND_FRAME_S), 6),
                                "notes": bent}
        name = clip_name or f"{source['name']} MIDI"
        if midi_path:
            target = Path(midi_path).expanduser()
            if target.is_dir() or not target.suffix:
                target = midi_files.free_path(target, name)
            elif target.exists():
                raise ValueError(f"{target} exists; choose another midi_path")
            payload["midi_file"] = midi_files.write_midi(target, notes, tempo_used, name=name)
        if to_track is not None:
            if arrangement_time is None and to_clip_index is None:
                if source.get("start_time") is not None:
                    arrangement_time = source["start_time"]
                else:
                    to_clip_index = source.get("clip_index") or 0

            def write() -> str:
                clip = prepare_clip(to_track, to_clip_index if arrangement_time is None else None,
                                    length, arrangement_time, name)
                written = write_notes(clip, notes, "replace" if replace else "add")
                return generated_result(clip, notes, written)

            payload.update(json.loads(await asyncio.to_thread(write)))
        elif not midi_path:
            payload.update(note_table(notes))
        return to_json(payload)

    def sound_folders() -> list[Path]:
        from . import als
        folders = [r / "Core Library" / "Samples" / "One Shots" for r in als._live_resource_dirs()]
        folders += [lib / "Samples" for lib in als._user_libraries()]
        return [f for f in folders if f.is_dir()]

    @tool(read_only=True)
    async def find_similar_sounds(text: str | None = None, like: str | None = None,
                                  folder: str | None = None, top: int = 10,
                                  min_score: float = 0.0, min_seconds: float | None = None,
                                  max_seconds: float | None = None,
                                  ctx: Context | None = None) -> str:
        """Find sounds by description or by example with CLAP (LAION's
        audio-text model): text="dusty vinyl kick", "airy female vocal
        chop", or like=<an audio file> for the closest ones; both together
        mean "like this, but …".

        folder: where to look (default: the Core Library's one-shots and the
        User Library's Samples folder). The first search in a folder indexes
        its files (about 10 a second, at most 1,500 per call; later calls go
        on) and keeps the index in ~/.ableton-mcp/sound-index. The defaults
        are one-shots: for sustained sounds give a folder of them and
        min_seconds (max_seconds for one-shots); min_score drops weak
        matches. Put a result on a track with browser_load(item=<file name>)
        or on a pad with build_drum_kit(sounds={"kick": <file name>}).
        """
        import numpy as np

        from . import ml

        if not text and not like:
            raise ValueError("Give text (a description), like (an audio file), or both")
        folders = [Path(folder).expanduser()] if folder else sound_folders()
        missing = [str(f) for f in folders if not f.is_dir()]
        if missing or not folders:
            raise FileNotFoundError(f"No such folder: {', '.join(missing) or 'none to search'}")
        files = await asyncio.to_thread(ml.audio_files, folders)
        if not files:
            raise ValueError(f"No audio files in {', '.join(str(f) for f in folders)}")
        index = await asyncio.to_thread(ml.SoundIndex, ml.CLAP_MODEL)
        stale = index.stale(files)
        batch = stale[:ml.INDEX_PER_CALL]
        example = Path(like).expanduser() if like else None
        if example is not None and not example.is_file():
            raise FileNotFoundError(f"No such audio file: {example}")
        embed_files = ([example] if example is not None else []) + batch
        with tempfile.TemporaryDirectory() as scratch:
            out = Path(scratch) / "vectors.npz"
            run = await ml.run("embed", {"texts": [text] if text else [],
                                         "files": [str(f) for f in embed_files],
                                         "out": str(out), "model": ml.CLAP_MODEL},
                               ml_progress(ctx))
            # np.load keeps the file open; Windows cannot delete the folder until it closes
            with np.load(out) as data:
                vectors = (np.array(data["files"]) if "files" in data
                           else np.zeros((0, 512), np.float32))
                text_vector = np.array(data["texts"][0]) if text else None
        if batch:
            index.add(batch, vectors[len(embed_files) - len(batch):])
            await asyncio.to_thread(index.save)
        parts = []
        if example is not None:
            if not np.all(np.isfinite(vectors[0])):
                raise ValueError(f"Could not read {example}")
            parts.append(vectors[0])
        if text_vector is not None:
            parts.append(text_vector)
        query = np.mean(parts, axis=0)
        top = max(1, min(int(top), 50))
        filtered = min_seconds is not None or max_seconds is not None
        ranked = index.search(query, files, len(files) if filtered else top,
                              exclude=str(example) if example else None)
        results = await asyncio.to_thread(ml.keep_results, ranked, top, float(min_score),
                                          min_seconds, max_seconds)
        payload: dict[str, Any] = {"query": {"text": text, "like": like}, "results": results,
                                   "folders": [str(f) for f in folders],
                                   "indexed": len(files) - (len(stale) - len(batch)),
                                   "sounds": len(files)}
        if len(stale) > len(batch):
            payload["not_indexed_yet"] = len(stale) - len(batch)
            payload["note"] = ("Only part of the folder is indexed so far; call again to index "
                               "more, or give a smaller folder.")
        if run.get("failed"):
            payload["unreadable"] = len(run["failed"])
        weak = ml.WEAK_SCORE["both" if text and like else "text" if text else "like"]
        best = ranked[0]["score"] if ranked else None
        if best is None or best < weak:
            payload["warning"] = (
                (f"Weak matches: the best scores {best:.2f} (good ones score {weak:g} or more)"
                 if best is not None else "Nothing indexed matched")
                + "; these folders may hold no such sound. Try another folder, or "
                "min_seconds for sustained sounds.")
        elif not results:
            payload["warning"] = "No sound passed min_score / min_seconds / max_seconds."
        return to_json(payload)

    # ── Devices & racks ──────────────────────────────────────────────────────

    @tool(destructive=True)
    def manage_device(track: int | str, device: int | str, action: str,
                      target_track: int | str | None = None,
                      target_index: int | None = None,
                      go_ahead: Annotated[ElicitationResult[_Confirm],
                                         Resolve(ask_device_delete)] = GO_AHEAD) -> str:
        """Device operations: info | delete | enable | disable | toggle | select |
        collapse | expand | move (to target_track at target_index; Live 11+).
        """
        require_go_ahead(go_ahead)
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
            owner, idx = device_owner(track_path(track), dp)  # a track or a rack's chain
            # Live selects the track of a deleted device when another device
            # stays (12.4.16, after the command; inside racks unmeasured): the
            # user's selection goes back in any case (no undo step)
            selected = lom_get("live_set view", ["selected_track"]).get("selected_track")
            deleted = send("lom_call", {"path": owner, "method": "delete_device", "args": [idx]})
            if isinstance(selected, dict) and "$id" in selected:
                send("lom_set", {"path": "live_set view", "property": "selected_track",
                                 "value": ref(f"id {selected['$id']}")})
            return to_json(deleted)
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
        """Insert one of Live's own devices by its browser name (Live 12.3+): effects
        ("EQ Eight", "Compressor", "Glue Compressor", "Limiter", "Saturator", "Utility",
        "Reverb", "Delay", "Echo", "Auto Filter", "Drum Buss", "Roar", …), instruments
        ("Drift", "Operator", "Wavetable", "Simpler", "Drum Rack", "Meld"), MIDI effects
        ("Arpeggiator", "Chord", "Scale") and racks. Plug-ins, Max devices and presets
        load with browser_load.

        - position: index in the chain (default the end)
        - settings: {"Threshold": "-18 dB", "1 Filter Type A": "High Pass 48dB"} as Live
          displays them, or raw numbers; `notes` says where one landed
          instead (the end of its range, -inf)
        """
        args = [device] if position is None else [device, int(position)]
        ids, name = insert_devices(track_path(track), [args])
        device_id = ids[0]
        result: dict[str, Any] = {"ok": True, "device": device, "track": name, "id": device_id}
        if settings:
            names = resolve_parameter_names(f"id {device_id}", list(settings))
            applied = batch_summary(batch(
                setting_ops(f"id {device_id}", settings, names), stop_on_error=False))
            result.update(setting_report(list(settings), names, applied))
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
        Use track="master" for the master chain. Only Compressor exposes its
        sidechain input to scripts (input_routing_type); Gate's cannot be set.
        """
        from . import device_recipes

        spec = device_recipes.recipe(chain)
        if spec.get("needs_sidechain") and sidechain_from is None:
            raise ValueError("This chain needs sidechain_from: the track that should duck "
                             "this one (usually the kick or drums)")
        source = track_name(sidechain_from) if sidechain_from is not None else None
        ids = inserted_device_ids(track_path(track), [[name] for name, _settings in spec["devices"]])
        ops, asked, names = [], [], {}
        for (name, settings), device_id in zip(spec["devices"], ids):
            mapping = resolve_parameter_names(f"id {device_id}", list(settings))
            ops += setting_ops(f"id {device_id}", settings, mapping)
            asked += [f"{name}: {s}" for s in settings]
            names.update({f"{name}: {k}": f"{name}: {v}" for k, v in mapping.items()})
        if sidechain_from is not None:
            compressor = f"id {ids[0]}"
            ops.append(op_set(compressor, "input_routing_type", ref(
                f"{compressor} available_input_routing_types "
                f"{named(f'{compressor} available_input_routing_types', source)}")))
        if sidechain_from is not None:
            asked.append("sidechain")
        applied = batch_summary(batch(ops, stop_on_error=False))
        return to_json(dict({"chain": chain, "description": spec["description"],
                             "devices": [name for name, _settings in spec["devices"]]},
                            **setting_report(asked, names, applied)))

    def find_sample(query: str, wait_s: float = 0.0) -> tuple[int, str]:
        """(id, path) of a browser sample: an exact file name when the query has
        an audio extension (waiting up to wait_s for Live to list a file just
        written to the User Library), else the best search match."""
        if not re.search(r"\.(wav|aif|aiff|flac|mp3|ogg)$", query, re.I):
            try:
                return browser_item_id(query, "samples")
            except ValueError:
                return browser_item_id(query, "drums")  # Drums › Drum Hits
        stem = query.rsplit(".", 1)[0]
        deadline = time.monotonic() + wait_s
        while True:
            hits = [h for category in ("samples", "drums")
                    for h in search_browser(stem, category, max_results=20)["results"]]
            exact = [h for h in hits if h["name"] == query]
            if exact:
                return exact[0]["id"], exact[0]["path"]
            if time.monotonic() >= deadline:
                raise ValueError(f"Live's browser does not list {query!r}")
            time.sleep(1.0)

    def confirm_sample(device: str, path: str, wait_s: float = 2.0) -> None:
        """Wait until the Simpler holds the file at `path` (Sample.file_path);
        raise with what it holds instead."""
        deadline = time.monotonic() + wait_s
        while True:
            try:
                held = lom_get(f"{device} sample", ["file_path"]).get("file_path")
            except AbletonCommandError:
                held = None
            if isinstance(held, str) and same_file(held, path):
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(f"the Simpler holds {held if isinstance(held, str) else 'no sample'}")
            time.sleep(0.1)

    def simpler_from_path(tp: str, path: str) -> int:
        """A Simpler at the front of the track holding the file at `path`
        (replace_sample, Live 12.4+), read back; its id. A Simpler that did
        not take the file is deleted again."""
        simpler = inserted_device_ids(tp, [["Simpler", 0]])[0]
        try:
            send("lom_call", {"path": f"id {simpler}", "method": "replace_sample", "args": [path]})
            confirm_sample(f"id {simpler}", path)
        except Exception:
            send("lom_call", {"path": tp, "method": "delete_device", "args": [0]})
            raise
        return simpler

    def first_error(result: dict) -> str:
        return next((str(step.get("message")) for step in result.get("results", [])
                     if step.get("status") == "error"), "no error reported")

    def pads_from_paths(rack: str, files: dict[int, str]) -> tuple[dict[int, tuple[str, str]], dict[int, str]]:
        """Drum Rack pads without the browser (Live 12.4+): per note a new
        chain (insert_chain, in_note), a Simpler on it and the note's file in
        that (replace_sample), read back. {note: (chain, simpler)} for the
        pads that loaded, {note: why} for the rest, whose chains are deleted.
        Raises when Live refuses a chain or its Simpler (the rack is then
        replaced: see fresh_rack)."""
        before = {c["$id"] for c in lom_get(f"{rack} chains", ["name"])["items"]}
        made = batch([op_call(rack, "insert_chain") for _ in files])
        fresh = [c["$id"] for c in lom_get(f"{rack} chains", ["name"])["items"] if c["$id"] not in before]
        notes = list(files)
        if made.get("failed") or len(fresh) != len(notes):
            raise RuntimeError(f"insert_chain gave {len(fresh)} new chains for {len(notes)} pads"
                               + (f" ({first_error(made)})" if made.get("failed") else ""))
        steps = batch([step for note, chain in zip(notes, fresh) for step in (
            op_set(f"id {chain}", "in_note", note), op_call(f"id {chain}", "insert_device", "Simpler"))])
        if steps.get("failed"):
            raise RuntimeError(f"a chain's in_note or Simpler: {first_error(steps)}")
        simplers = [step["result"]["result"]["$id"] for step in steps["results"][1::2]]
        batch([op_call(f"id {simpler}", "replace_sample", files[note])
               for note, simpler in zip(notes, simplers)], stop_on_error=False)
        placed, failed = {}, {}
        for note, chain, simpler in zip(notes, fresh, simplers):
            try:
                confirm_sample(f"id {simpler}", files[note])
                placed[note] = (f"id {chain}", f"id {simpler}")
            except RuntimeError as e:
                failed[note] = f"replace_sample: {e}"
                send("lom_call", {"path": f"{rack} drum_pads {note}", "method": "delete_all_chains"})
        return placed, failed

    def fresh_rack(tp: str, rack: str) -> str:
        """Delete the track's Drum Rack and put an empty one in its place, its
        id: a build by path that Live refused part-way leaves chains on no pad
        or on all notes, and no call deletes a single rack chain. A rack no
        longer on the track gets a new one at the front."""
        devices = lom_get(f"{tp} devices", ["name"])["items"]
        index = next((i for i, d in enumerate(devices) if f"id {d.get('$id')}" == rack), None)
        if index is not None:
            send("lom_call", {"path": tp, "method": "delete_device", "args": [index]})
        return f"id {inserted_device_ids(tp, [['Drum Rack', index or 0]])[0]}"

    @tool(destructive=True)
    @keeping_arms
    def build_tracks(tracks: list[dict], replace: bool = False, sample_wait_s: float = 40.0,
                     signature: str | list[int] | None = None) -> str:
        """Build tracks from a spec in one call, e.g. to rebuild a set the Trial
        cannot save from a JSON file. A MIDI track (default):
          {"name": "Kick",
           "instrument": "Operator"                     # a device by name, or
                       | {"sample_path": "/abs/808.wav", "warping": false}   # a Simpler
                       | {"sample": "kick 808"}         # a Simpler, from the browser
                       | {"drum_rack": {"36": {"sample_path": "/abs/kick.wav",
                                               "choke_group": 1, "settings": {...}}}},
           "settings": {"Volume": "0.0 dB", "Detune": "-12 ct"},  # on the instrument
           "effects": [{"device": "Compressor", "settings": {...},
                        "sidechain_from": "Kick"}],     # Compressor only
           "volume_db": -6.0, "pan": 0.0, "mute": false,
           "clips": [{"start": 0, "length": 64, "name": "A",
                      "notes": [{"pitch": 36, "start_time": 0, "duration": 1,
                                 "velocity": 100}]}]}   # times from the clip start
        An audio track: {"name": "REF drums", "kind": "audio",
          "audio_clips": [{"file": "/abs/drums.wav", "start": 0, "warping": false}]}
          (the clip starts at the file's start: Live's warp analysis otherwise
          moves it to the first transient; with "warping": true, "bars" fits it).
        Simplers and pads also take playback_mode (classic | one_shot | slicing)
        and start_marker (sample frames); pads take choke_group (0 none, 1-16).
        sample_path loads the file itself (Live 12.4+, read back); on older Live,
        or when that fails, the browser loads "sample" if given, else the file's
        name ("loaded_by_name" says why), and the call fails if neither works.
        A name with a file extension is looked up exactly, waiting up to
        sample_wait_s for Live to list a new file. Settings take display text
        ("-12 dB", "-12 ct") or raw numbers (`setting_notes`: where one landed
        instead). Sidechains are routed once every
        track exists. signature: [7, 8] or "7/8", set first. replace=True first
        deletes tracks with these names.
        """
        names = [str(t["name"]) for t in tracks]
        if len(set(names)) != len(names):
            raise ValueError("Track names must be unique")
        check_samplers(tracks)
        for spec in tracks:  # before anything is built
            for clip in spec.get("audio_clips") or []:
                if clip.get("bars") is not None and clip.get("warping") is not True:
                    raise ValueError(f"{spec['name']}: an audio clip's bars needs "
                                     "\"warping\": true (unwarped, a file plays as long as it is)")
                try:
                    check_fit(clip.get("warping"), clip.get("bars"))
                except ValueError as e:
                    raise ValueError(f"{spec['name']}: {e}") from e
        if signature is not None:
            numerator, denominator = parse_signature(signature)
            batch([op_set("live_set", "signature_numerator", numerator),
                   op_set("live_set", "signature_denominator", denominator)])
        if replace:
            existing = lom_get("live_set tracks", ["name"])["items"]
            for index in sorted((i for i, t in enumerate(existing) if t.get("name") in names), reverse=True):
                send("lom_call", {"path": "live_set", "method": "delete_track", "args": [index]})
        report, sidechains = [], []
        no_path_loading: list[str | None] = []  # why replace_sample cannot be used, once asked

        def path_loading_off() -> str | None:
            """Why files cannot be loaded by path in this Live, or None."""
            if not no_path_loading:
                version = live_version()
                if version is None:
                    no_path_loading.append("Live did not report its version")
                elif version[:2] < REPLACE_SAMPLE_SINCE:
                    no_path_loading.append("replace_sample needs Live 12.4; this is "
                                           + ".".join(map(str, version)))
                else:
                    no_path_loading.append(None)
            return no_path_loading[0]

        def by_name(where: str, options: dict, why: str | None) -> tuple[int, str]:
            """The browser item for a sampler without its file loaded: its
            sample name, else its file's name. Raises when neither works."""
            query = options.get("sample") or Path(sample_file(where, options)).name
            try:
                return find_sample(query, sample_wait_s)
            except ValueError as e:
                if why is None:
                    raise
                raise RuntimeError(f"Could not load {options['sample_path']} ({where}): {why}; "
                                   f"browser: {e}") from e

        def index_of(track_id: int) -> int:
            items = lom_get("live_set tracks", ["name"])["items"]
            return next(i for i, t in enumerate(items) if t.get("$id") == track_id)

        def no_warp(sample_path: str) -> None:
            try:
                if lom_get(sample_path, ["warping"]).get("warping"):
                    send("lom_set", {"path": sample_path, "property": "warping", "value": False})
            except Exception:  # an instrument without a sample
                pass

        for spec in tracks:
            kind = spec.get("kind", "midi")
            created = send("lom_call", {"path": "live_set", "method": f"create_{kind}_track", "args": [-1]})
            track_id = created["result"]["$id"]
            tp = f"id {track_id}"
            ops = [op_set(tp, "name", spec["name"])]
            row: dict[str, Any] = {"track": spec["name"], "problems": []}
            chain: list[tuple[str, dict]] = []
            instrument = spec.get("instrument")
            if isinstance(instrument, dict) and instrument.get("drum_rack"):
                rack = f"id {inserted_device_ids(tp, [['Drum Rack', 0]])[0]}"
                pads = {int(n): p for n, p in instrument["drum_rack"].items()}
                files = {n: sample_file(f"pad {n}", p) for n, p in pads.items() if p.get("sample_path")}
                placed: dict[int, tuple[str, str]] = {}
                why: dict[int, str] = {}
                if files:
                    off = path_loading_off()
                    if off is None:
                        try:
                            placed, why = pads_from_paths(rack, files)
                        except Exception as e:  # every pad by name, on a new rack
                            rack = fresh_rack(tp, rack)
                            why = {n: f"pads by path: {e}" for n in files}
                    else:
                        why = {n: off for n in files}
                labels = {n: files[n] for n in placed}
                loads = []
                for note, pad in pads.items():
                    if note not in placed:
                        item_id, labels[note] = by_name(f"pad {note}", pad, why.get(note))
                        loads.append((note, item_id))
                if loads:
                    loaded = load_items(index_of(track_id), loads)
                    if not loaded["ok"]:
                        row["problems"].append(f"drum pads: {loaded['steps']}")
                if why:
                    row["loaded_by_name"] = {str(n): reason for n, reason in why.items()}
                for note, pad in pads.items():
                    pad_chain, device = placed.get(note) or (f"{rack} drum_pads {note} chains 0",
                                                             f"{rack} drum_pads {note} chains 0 devices 0")
                    chain.append((device, pad.get("settings") or {}))
                    ops += sampler_ops(device, pad, pad_chain)
                    if pad.get("warping") is False:
                        no_warp(f"{device} sample")
                row["instrument"] = {str(n): labels[n] for n in pads}
            elif isinstance(instrument, dict):
                device, why_not = None, None
                file = sample_file(spec["name"], instrument)
                if file:
                    why_not = path_loading_off()
                    if why_not is None:
                        try:
                            device, row["instrument"] = f"id {simpler_from_path(tp, file)}", file
                        except Exception as e:
                            why_not = f"replace_sample: {e}"
                if device is None:
                    item_id, row["instrument"] = by_name(spec["name"], instrument, why_not)
                    loaded = load_items(index_of(track_id), [(None, item_id)])
                    if not loaded["ok"]:
                        row["problems"].append(f"could not load {row['instrument']}")
                    if why_not:
                        row["loaded_by_name"] = why_not
                    first = lom_get(f"{tp} devices", ["name"])["items"][:1]
                    device = f"id {first[0]['$id']}" if first else None
                if device:
                    chain.append((device, spec.get("settings") or {}))
                    ops += sampler_ops(device, instrument)
                    if instrument.get("warping") is False:
                        no_warp(f"{device} sample")
            elif instrument:
                device_id = inserted_device_ids(tp, [[str(instrument), 0]])[0]
                chain.append((f"id {device_id}", spec.get("settings") or {}))
                row["instrument"] = str(instrument)
            effects = spec.get("effects") or []
            if effects:
                ids = inserted_device_ids(tp, [[fx["device"]] for fx in effects])
                for fx, device_id in zip(effects, ids):
                    chain.append((f"id {device_id}", fx.get("settings") or {}))
                    if fx.get("sidechain_from"):
                        sidechains.append((device_id, str(fx["sidechain_from"]), spec["name"]))
            for device, settings in chain:
                if settings:
                    ops += setting_ops(device, settings, resolve_parameter_names(device, list(settings)))
            if spec.get("volume_db") is not None:
                ops.append(op_display(f"{tp} mixer_device volume", f"{float(spec['volume_db']):.1f} dB"))
            if spec.get("pan") is not None:
                ops.append(op_set(f"{tp} mixer_device panning", "value", float(spec["pan"])))
            if spec.get("mute") is not None:
                ops.append(op_set(tp, "mute", bool(spec["mute"])))
            applied = batch_summary(batch(ops, stop_on_error=False))
            row["problems"] += [step for step in applied["steps"] if isinstance(step, dict) and "error" in step]
            setting_notes = [step for step in applied["steps"] if isinstance(step, dict) and step.get("note")]
            if setting_notes:
                row["setting_notes"] = setting_notes
            row["devices"] = [d["name"] for d in lom_get(f"{tp} devices", ["name"])["items"]]
            row["notes"] = 0
            for clip in spec.get("clips") or []:
                created_clip = send("lom_call", {"path": tp, "method": "create_midi_clip",
                                                 "args": [float(clip["start"]), float(clip["length"])]})
                cp = f"id {created_clip['result']['$id']}"
                if clip.get("name"):
                    send("lom_set", {"path": cp, "property": "name", "value": clip["name"]})
                notes = [dict(n, pitch=music.parse_note(n["pitch"])) for n in clip.get("notes") or []]
                for a in range(0, len(notes), 500):
                    write_notes(cp, notes[a:a + 500], "replace" if a == 0 else "add")
                row["notes"] += len(notes)
            for clip in spec.get("audio_clips") or []:
                made = send("lom_call", {"path": tp, "method": "create_audio_clip",
                                         "args": [str(clip["file"]), float(clip.get("start", 0.0))]})
                cp = f"id {made['result']['$id']}"
                # back to the file's start, its length checked (and fitted: warping, bars)
                fitted = fit_audio_clip(cp, False if clip.get("warping", False) is False
                                        else None, clip.get("bars"))
                row["problems"] += [f"audio clip {Path(str(clip['file'])).name}: {error}"
                                    for error in fitted.get("errors", [])]
                if fitted.get("notes"):
                    row.setdefault("notes", []).extend(
                        f"audio clip {Path(str(clip['file'])).name}: {n}" for n in fitted["notes"])
                if fitted.get("warning"):
                    row.setdefault("warnings", []).append(
                        f"audio clip {Path(str(clip['file'])).name}: {fitted['warning']}")
                held = lom_get(cp, ["start_time", "end_time", "start_marker", "warping"])
                held["warp_markers"] = fitted.get("warp_markers")
                if clip.get("warping", False) is False and held.get("warping"):
                    row["problems"].append(f"audio clip {Path(str(clip['file'])).name}: Live left it warped")
                if abs(float(held.get("start_time", 0.0)) - float(clip.get("start", 0.0))) > 1e-6:
                    row["problems"].append(f"audio clip {Path(str(clip['file'])).name}: starts at beat "
                                           f"{held.get('start_time')}, not {clip.get('start', 0.0)}")
                row.setdefault("audio_clips", []).append(held)
            report.append(row)
        for device_id, source, owner in sidechains:
            device = f"id {device_id}"
            try:
                send("lom_set", {"path": device, "property": "input_routing_type", "value": ref(
                    f"{device} available_input_routing_types "
                    f"{named(f'{device} available_input_routing_types', source)}")})
            except Exception as e:  # Gate and most devices have no sidechain input here
                next(r for r in report if r["track"] == owner)["problems"].append(
                    f"sidechain from {source}: {e}")
        return to_json({"ok": not any(r["problems"] for r in report), "tracks": report,
                        "sidechains": len(sidechains)})

    # ── Reference songs: reference-dissector jobs and blueprints ─────────────
    # (reference_jobs.py runs refdissect in its own uv environment, detached;
    # blueprint.py holds the format; the full profile only)

    @tool(tier="full")
    async def dissect_reference(path: str | None = None, track: int | str | None = None,
                                clip_index: int | None = None,
                                arrangement_clip_index: int | None = None,
                                model: str = "htdemucs", melodic: bool = True,
                                slides: bool = False, steps: list[str] | None = None) -> str:
        """Start analysing a reference song with reference-dissector, as a job:
        stems, tempo grid, sections, drum hits matched to the user's samples,
        the 808, melodic notes, and a blueprint to build it with. Returns a job
        id at once: follow it with reference_job, then build_from_blueprint.
        About 4 minutes for a 2-minute song on Apple Silicon (the first run
        about 7, as it sets up ~2 GB); index_sound_library must have run once.

        - path: an audio file, or track + clip_index / arrangement_clip_index
        - model: Demucs model (htdemucs | htdemucs_ft | htdemucs_6s)
        - melodic: transcribe the tonal stems too; slides: the 808's slides
        - steps: run part of the chain again on the song's folder, e.g.
          ["match", "plan", "blueprint"] after a new index (separate, analyze,
          melodic, match, fit-808, plan, blueprint)
        """
        from . import reference_jobs as jobs

        if not re.fullmatch(r"[A-Za-z0-9_]+", model or ""):
            raise ValueError(f"model {model!r}: a Demucs model name such as htdemucs")
        source = await audio_source(path, track, clip_index, arrangement_clip_index)
        chosen = jobs.resolve_steps(steps, melodic)
        work = jobs.work_folder(source["name"], source["file"])
        planned = await asyncio.to_thread(jobs.dissect_steps, source["file"], work, chosen, model,
                                          slides)
        await asyncio.to_thread(jobs.claim_folder, work, source["file"])
        record = await asyncio.to_thread(jobs.start, "dissect", source["name"], work, planned, {
            "source": source["file"], "model": model, "melodic": melodic, "slides": slides})
        expected = sum(step["expected_s"] for step in planned)
        return to_json({
            "job": record["id"], "steps": chosen, "work": str(work), "refdissect": record["refdissect"],
            "estimate": f"about {max(1, round(expected / 60))} min once set up; the first run also "
                        "downloads its environments and models (~2 GB)",
            "next": f'reference_job(job_id="{record["id"]}") until it is done'})

    @tool(tier="full")
    async def index_sound_library(folders: list[str] | None = None) -> str:
        """Index the user's samples for dissect_reference to match drum sounds
        and 808s against, as a job (refdissect inventory, index-library and its
        808 index): once, and again when the library changes. About 35 s for
        12,000 sounds on Apple Silicon after the first run's setup. Returns
        a job id: follow it with reference_job.

        folders: default the User Library and Live's Core Library samples.
        """
        from . import reference_jobs as jobs

        chosen = ([Path(f).expanduser().resolve() for f in folders] if folders  # the job runs elsewhere
                  else jobs.default_sound_folders())
        missing = [str(f) for f in chosen if not f.is_dir()]
        if missing or not chosen:
            raise FileNotFoundError(f"No such folder: {', '.join(missing) or 'no User Library or Core Library found; pass folders'}")
        library = jobs.library_dir()
        planned = await asyncio.to_thread(jobs.index_steps, chosen)
        record = await asyncio.to_thread(jobs.start, "index", "sound library", library, planned,
                                         {"folders": [str(f) for f in chosen]})
        return to_json({"job": record["id"], "folders": [str(f) for f in chosen],
                        "index": str(library), "refdissect": record["refdissect"],
                        "next": f'reference_job(job_id="{record["id"]}") until it is done'})

    @tool(tier="full")
    async def reference_job(job_id: str | None = None, wait: float = 20.0, cancel: bool = False,
                            ctx: Context | None = None) -> str:
        """A dissect_reference or index_sound_library job: waits up to `wait`
        seconds (at most 60) for its next step, with progress notifications,
        then gives its state. Done, it gives a summary: tempo, meter, sections,
        pads with their match verdicts, 808 notes, files. Without job_id: the
        recent jobs. cancel=True stops the job and all it started.
        """
        from . import reference_jobs as jobs

        if job_id is None:
            if cancel:
                raise ValueError("cancel needs the job_id")
            rows = await asyncio.to_thread(jobs.recent, 10)
            return to_json({"jobs": [jobs.brief(r) for r in rows]} if rows else
                           {"jobs": [], "note": "No reference jobs yet: dissect_reference starts one"})
        if cancel:
            return to_json(jobs.status(await asyncio.to_thread(jobs.cancel, job_id)))
        record = await asyncio.to_thread(jobs.load, job_id)
        mark = (record.get("state"), record.get("step"))
        deadline = time.monotonic() + max(0.0, min(float(wait), 60.0))
        shown = -1.0
        while record.get("state") in jobs.ACTIVE and time.monotonic() < deadline:
            progress = float(record.get("progress") or 0.0)
            if progress > shown:
                shown = progress
                steps = [s["name"] for s in record.get("steps", [])]
                where = (f"{record['step']} ({steps.index(record['step']) + 1}/{len(steps)})"
                         if record.get("step") in steps else "starting")
                await report_progress(ctx, round(progress * 100, 1), 100,
                                      f"{record.get('name')}: {where}")
            await asyncio.sleep(1.0)
            record = await asyncio.to_thread(jobs.load, job_id)
            if (record.get("state"), record.get("step")) != mark:
                break
        return to_json(jobs.status(record))

    def load_blueprint(value: str | dict) -> tuple[dict, Path | None, str]:
        """(blueprint, its folder or None, where it came from): a job id, a
        file or its folder, JSON text or the object itself."""
        from . import reference_jobs as jobs

        if isinstance(value, dict):
            return value, None, "the blueprint given"
        text = str(value).strip()
        if text.startswith("{"):
            return json.loads(text), None, "the blueprint given"
        if re.fullmatch(r"[A-Za-z0-9._-]+", text) and jobs.record_path(text).is_file():
            record = jobs.load(text)
            file = Path(record["work"]) / "blueprint.json"
            if record.get("kind") != "dissect":
                raise ValueError(f"Job {text} indexed the sound library: give a dissect_reference job")
            if record.get("state") != "done":
                raise RuntimeError(f"Job {text} is {record.get('state')} (step {record.get('step')}): "
                                   "wait for it with reference_job")
            if not file.is_file():
                raise FileNotFoundError(f"Job {text} wrote no blueprint.json in {record['work']}: "
                                        "its refdissect has no `blueprint` command")
        else:
            file = Path(text).expanduser().resolve()  # Live gets absolute paths
            if file.is_dir():
                file = file / "blueprint.json"
            if not file.is_file():
                raise FileNotFoundError(f"No blueprint at {file} (give a job id, a blueprint.json "
                                        "or its folder)")
        data = json.loads(file.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            from . import blueprint as bp

            stale = bp.plan_changed(data, file.parent)
            if stale:
                raise ValueError(f"{file}: {stale}")
        return data, file.parent, str(file)

    def read_back(blueprint: dict, base: Path | None, placed: dict[str, Path],
                  tracks: dict[str, int], checks: dict[str, bool]) -> dict:
        """What Live holds after the build, against the blueprint: the time
        signature and tempo, each Simpler's file (by content), numeric Volumes (sent
        again as display text where the number did not hold), choke groups
        (set again where they differ) and voices."""
        from . import blueprint as bp

        out: dict[str, Any] = {}
        song = lom_get("live_set", ["signature_numerator", "signature_denominator", "tempo"])
        if checks["time_signature"]:
            live = f"{song['signature_numerator']}/{song['signature_denominator']}"
            out["time_signature"] = {"want": blueprint["time_signature"], "live": live,
                                     "same": live == blueprint["time_signature"]}
        if checks["tempo"]:
            want = float(blueprint["tempo"])
            out["tempo"] = {"want": want, "live": song.get("tempo"),
                            "same": abs(float(song.get("tempo") or 0.0) - want) <= bp.TEMPO_BPM}
        samples, volumes, chokes = {}, {}, {}
        for _i, name, note, options in bp.samplers(blueprint):
            if name not in tracks:
                continue
            key = f"{name} pad {note}" if note else name
            rack = f"live_set tracks {tracks[name]} devices 0"
            chain = f"{rack} drum_pads {note} chains 0" if note else None
            device = f"{chain} devices 0" if note else rack
            want = (placed.get(options["file"]) if options.get("file")
                    else Path(options["sample_path"]).expanduser() if options.get("sample_path") else None)
            if checks["samples"] and want is not None:
                try:
                    held = lom_get(f"{device} sample", ["file_path"]).get("file_path")
                    same = bool(held) and Path(held).is_file() and bp.sha1(Path(held)) == bp.sha1(want)
                    samples[key] = {"want": str(want), "live": held, "same": same}
                except Exception as e:  # no Simpler there, or one without a sample
                    samples[key] = {"want": str(want), "live": None, "same": False, "error": str(e)}
            volume = (options.get("settings") or {}).get("Volume") if note else (
                (next(t for t in blueprint["tracks"] if t["name"] == name).get("settings") or {}).get("Volume"))
            if checks["volumes"] and isinstance(volume, (int, float)):
                parameter = f"{device} parameters {selector('Volume')}"

                def reading(parameter: str = parameter) -> dict:
                    try:
                        got = lom_get(parameter, ["value", "$display"])
                        return {"value": got.get("value"), "display": got.get("$display")}
                    except Exception as e:
                        return {"error": str(e)}

                got = reading()
                row = {"want": float(volume), **got,
                       "held": bp.volume_held(float(volume), got.get("value"), got.get("display"))}
                if not row["held"] and "error" not in got:
                    try:  # Live's way before: a display step under the text (-6.6 holds -6.605)
                        batch([op_display(parameter, f"{float(volume):.1f} dB")])
                        row["then"] = then = reading()
                        row["by_text"] = (isinstance(then.get("value"), (int, float))
                                          and abs(float(then["value"]) - float(volume)) <= bp.VOLUME_TEXT_DB)
                    except Exception as e:
                        row["text_error"] = str(e)
                volumes[key] = row
            group = options.get("choke_group")
            if checks["choke_groups"] and note and group is not None:
                try:
                    live = lom_get(chain, ["choke_group"]).get("choke_group")
                    if live != int(group):
                        send("lom_set", {"path": chain, "property": "choke_group", "value": int(group)})
                        live = lom_get(chain, ["choke_group"]).get("choke_group")
                    chokes[key] = {"want": int(group), "live": live, "same": live == int(group)}
                except Exception as e:
                    chokes[key] = {"want": int(group), "live": None, "same": False, "error": str(e)}
        if samples:
            out["samples"] = {"checked": len(samples),
                              "wrong": {k: v for k, v in samples.items() if not v["same"]}}
        if volumes:
            out["volumes"] = {"checked": len(volumes), "held": sum(v["held"] for v in volumes.values())}
            by_text = [k for k, v in volumes.items() if v.get("by_text")]
            if by_text:
                out["volumes"]["held_by_text"] = by_text
            missed = {k: v for k, v in volumes.items() if not v["held"] and not v.get("by_text")}
            if missed:
                out["volumes"]["not_held"] = missed
        if chokes:
            out["choke_groups"] = {"checked": len(chokes),
                                   "wrong": {k: v for k, v in chokes.items() if not v["same"]}}
        return out

    @tool(tier="full")
    def build_from_blueprint(blueprint: str | dict[str, Any], bars: str | None = None) -> str:
        """Build a blueprint (a beat as JSON: tempo, meter, locators, tracks
        and their sample files; docs/blueprint-v1.md) in the open, empty Live
        set, then read back what Live holds: every Simpler's file, pad Volumes,
        choke groups, locators Live moved, the time signature.

        - blueprint: a finished dissect_reference job's id, a blueprint.json
          (or its folder), or the blueprint itself
        - bars: "9-16" builds only those bars (from 1), moved to the start,
          for a quick listen
        """
        from . import als, blueprint as bp, ml

        data, base, origin = load_blueprint(blueprint)
        bp.check(data)
        problems = bp.check_sources(data, base)
        if problems:
            raise FileNotFoundError("The blueprint's files: " + "; ".join(problems[:10]))
        report: dict[str, Any] = {"blueprint": data.get("name") or origin}
        if bars:
            a, b = bp.parse_bars(bars)
            from . import reference_jobs as jobs
            data = bp.excerpt(data, (a, b), base, (base or jobs.data_dir()) / f"excerpt-{a}-{b}")
            report["excerpt"] = data["meta"]["excerpt"]
        overview = send("get_session_overview")
        busy = [t["name"] for t in overview["tracks"]
                if t.get("devices") or t.get("session_clips") or t.get("arrangement_clip_count")]
        # a locator too: the build would rename one at a beat it asks for
        busy += [f"locator {c.get('name') or '(unnamed)'!r} at beat {float(c['time']):g}"
                 for c in overview.get("cue_points") or []]
        if busy:
            raise RuntimeError(f"The set is not empty ({', '.join(busy)}): open a new Live set first")
        before = len(overview["tracks"])
        numerator, denominator = bp.signature(data["time_signature"])
        # the meter before any locator: Live snaps a new cue to the Arrangement's grid
        batch([op_set("live_set", "tempo", float(data["tempo"])),
               op_set("live_set", "signature_numerator", numerator),
               op_set("live_set", "signature_denominator", denominator)])
        folder = ml.generated_folder(als._user_libraries())
        placed, files = bp.put_files(data, base, folder)
        spec = bp.build_spec(data, placed, base)
        try:
            built = json.loads(build_tracks(spec, sample_wait_s=40.0))
        except Exception as e:
            try:
                made = send("get_session_overview")["tracks"][before:]
                left = ", ".join(f"{t['name']!r} ({', '.join(t.get('devices') or []) or 'no device'})"
                                 for t in made) or "no track"
            except Exception as again:  # Live is gone too
                left = f"unknown tracks ({again})"
            why = str(e).rstrip().rstrip(".")  # Live's own messages end in a period
            raise RuntimeError(f"build_tracks stopped: {why}. It left {left} in the set: delete "
                               "them, or open a new set, before building again") from e
        tracks = {t["name"]: t["index"] for t in send("get_session_overview")["tracks"][before:]}
        checks = bp.checks_on(data)
        report.update(tempo=data["tempo"], time_signature=data["time_signature"], tracks=[
            {"track": row["track"], "devices": row.get("devices"), "notes": row.get("notes", 0),
             **({"audio_clips": len(row["audio_clips"])} if row.get("audio_clips") else {}),
             **({"problems": row["problems"]} if row.get("problems") else {}),
             **({"loaded_by_name": row["loaded_by_name"]} if row.get("loaded_by_name") else {})}
            for row in built["tracks"]])
        report["files"] = {"folder": str(folder), **{
            action: sum(f["action"] == action for f in files)
            for action in ("copied", "scaled", "kept", "replaced") if any(f["action"] == action for f in files)}}
        if any(f["action"] == "replaced" for f in files):
            report["files"]["note"] = ("A file of another content had this name: Live may keep "
                                       "playing the old one where it loaded it by name, until it restarts")
        voices = {}
        for spec_track in data["tracks"]:
            if spec_track.get("voices") is None or spec_track["name"] not in tracks:
                continue
            device = f"live_set tracks {tracks[spec_track['name']]} devices 0"
            want = int(spec_track["voices"])
            try:
                send("lom_set", {"path": device, "property": "voices", "value": want})
                live = lom_get(device, ["voices"]).get("voices") if checks["voices"] else want
                voices[spec_track["name"]] = {"want": want, "live": live, "same": live == want}
            except Exception as e:
                voices[spec_track["name"]] = {"want": want, "live": None, "same": False, "error": str(e)}
        if data.get("locators"):
            # zoomed in until the strays are deleted too: on the default grid a
            # toggle at a stray's beat could snap elsewhere and make a new cue
            with arrangement_zoomed_in() as zoom_errors:
                placed_cues = place_locators([(c["name"], float(c["beat"])) for c in data["locators"]],
                                             zoom=False)
                moved = delete_strays(placed_cues, []) if checks["locators"] else [
                    {"name": p["name"], "asked": p["time"], "live": None} for p in placed_cues
                    if p.get("error")]
            report["locators"] = sum("error" not in p for p in placed_cues)
            if moved:
                report["locators_moved"] = moved
            if zoom_errors:
                report["zoom_errors"] = zoom_errors
        report["checks"] = read_back(data, base, placed, tracks, checks)
        if voices:
            report["checks"]["voices"] = voices
        batch([op_set("live_set", "current_song_time", 0.0),
               op_call("live_app view", "show_view", VIEWS["arrangement"])], stop_on_error=False)
        found = report["checks"]
        report["ok"] = not (
            any("problems" in t for t in report["tracks"]) or report.get("locators_moved")
            or not found.get("time_signature", {"same": True})["same"]
            or not found.get("tempo", {"same": True})["same"]
            or found.get("samples", {}).get("wrong") or found.get("choke_groups", {}).get("wrong")
            or found.get("volumes", {}).get("not_held") or any(not v["same"] for v in voices.values()))
        return to_json(report)

    @tool(idempotent=True)
    def set_device_parameter_by_name(track: int | str, device: int | str,
                                     parameter: int | str, value: float | None = None,
                                     display_value: str | None = None,
                                     pad: int | str | None = None) -> str:
        """Set a device parameter by name (or index), either as a raw value or as
        the text Live shows: display_value="800 Hz", "-6 dB", "35 %", "120 ms",
        "1.2 kHz", "25L", or a list item like "Lowpass" (a bare number is in
        display units: "0.5" on a pan is 1R). device: index or name
        (devices inside racks are found by name too); device="mixer" works
        with parameter "volume" | "pan" | "send A" (a pad's sends go to its
        Drum Rack's own returns). pad (note, "C1" or role
        like "kick"): the device (or "mixer": the pad chain's mixer) on that
        pad of the track's Drum Rack.
        The error message lists valid parameter names when one is not found.
        """
        auto_snapshot("set_device_parameter_by_name", [track])
        asked = parameter
        if pad is not None:
            rack = drum_rack_path(track)
            if rack is None:
                raise ValueError("No Drum Rack on this track (nor inside its racks) for pad")
            note = pad_note(pad)
            if not lom_get(f"{rack} drum_pads {note}", ["chains"]).get("chains"):
                raise ValueError(f"Pad {note} ({music.note_name(note)}) is empty")
            chain = f"{rack} drum_pads {note} chains 0"
            dp = None if is_mixer(device) else \
                f"{chain} devices {named(f'{chain} devices', device)}"
        else:
            chain = None
            dp = None if is_mixer(device) else device_path(track, device)
        if dp is not None and isinstance(parameter, str) and not parameter.strip().isdigit():
            parameter = resolve_parameter_names(dp, [parameter])[parameter]
        if dp is not None:
            pp = f"{dp} parameters {selector(parameter)}"
        else:
            pp = mixer_parameter_path(chain or track_path(track), parameter)
            send_index = re.search(r" sends (\d+)$", pp)
            if chain and send_index and int(send_index.group(1)) >= len(
                    lom_get(f"{rack} return_chains", ["name"])["items"]):
                raise ValueError(f"This Drum Rack has no return chain {send_index.group(1)}: "
                                 "pad sends go to the rack's own returns, not the set's")
        if display_value is not None:
            result = send("param_set_display", {"path": pp, "value": display_value})
        elif value is None:
            result = lom_get(pp, ["name", "value", "min", "max", "is_quantized", "value_items"])
        else:
            result = send("lom_set", {"path": pp, "property": "value", "value": float(value)})
        if parameter != asked and isinstance(result, dict):
            result = dict(result, asked=asked, parameter=parameter)
        return to_json(result)

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
        if action.lower() != "info":
            auto_snapshot("rack_control", [track])
        dp = device_path(track, device)
        action = action.lower()
        if action == "info":
            info = lom_get(dp, ["name", "class_name", "visible_macro_count",
                                "variation_count", "selected_variation_index", "chains"])
            params = lom_get(f"{dp} parameters", ["name", "value"])["items"]
            # an unreadable property comes back as an error dict, not a number
            count = info.get("visible_macro_count")
            count = count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else 8
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
        mute/solo. Without `device`, the track's first Drum Rack (also one
        inside an Instrument Rack) is used.
        """
        rack = drum_rack_path(track) if device is None else device_path(track, device)
        if rack is None:
            raise ValueError("No Drum Rack on this track (nor inside its racks)")
        pads = lom_get(f"{rack} drum_pads", ["note", "name", "mute", "solo", "chains"])["items"]
        filled = [{"note": p["note"], "note_name": music.note_name(p["note"]),
                   "name": p["name"], "mute": p["mute"], "solo": p["solo"]}
                  for p in pads if p.get("chains")]
        result: dict[str, Any] = {"pads": filled,
                                  "auto_mapping": drum_mapping_from_rack(track, rack)}
        if " chains " in rack:
            result["rack"] = rack  # inside another rack
        return to_json(result)

    # ── Browser ──────────────────────────────────────────────────────────────

    def search_browser(query: str, category: str = "all", max_results: int = 25,
                       time_limit: float = 20.0, within: list[str] | None = None,
                       enough: Callable[[dict], bool] | None = None,
                       patience: float | None = None) -> dict:
        """enough: an item that ends the walk once found (listed first).
        patience: time_limit counts from the first strong hit (every word of
        the query in its name); without one the walk goes on up to patience
        seconds."""
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
        started = time.monotonic()
        first_strong: float | None = None  # when the first strong hit came
        rested = 0.0  # time spent waiting for Live, not counted against the limits
        while True:
            params: dict[str, Any] = ({"walk_id": walk_id} if walk_id is not None
                                      else {"query": query, "roots": roots,
                                            "within": within or []})
            # ~60 ms of Live's main thread per call, then as long for Live
            # (script 2.2 says how long: rest_ms; a call sooner gets less)
            params.update(slice_ms=60, max_results=500)
            reply = send("browser_search", params)
            walk_id, done, visited = reply["walk_id"], reply["done"], reply["visited"]
            found += reply["results"]
            now = time.monotonic() - rested
            elapsed = now - started
            if first_strong is None and any(rank(e) < 2 for e in found):
                first_strong = now
            if done or sum(rank(e) == 0 for e in found) >= max_results \
                    or (patience is None and elapsed > time_limit) \
                    or (patience is not None and first_strong is not None
                        and now - first_strong > time_limit) \
                    or (patience is not None and elapsed > patience) \
                    or (enough is not None and any(enough(e) for e in found)):
                break
            pause = max(0.0, min(float(reply.get("rest_ms") or 0), 500.0) / 1000.0)
            time.sleep(pause)
            rested += pause
        if not done:
            try:
                send("browser_search", {"walk_id": walk_id, "cancel": True})
            except AbletonCommandError as e:  # the walk ended on its own meanwhile
                logger.debug("Browser walk %s already gone: %s", walk_id, e)
        ranked = sorted(found, key=lambda e: (enough is None or not enough(e), rank(e)))
        return {"query": query, "category": category, "complete": done, "searched": visited,
                "seconds": round(time.monotonic() - started, 1), "matches": len(found),
                "results": [{"id": e["$id"], "name": e["name"], "kind": e["kind"],
                             "path": f"{e['root']}/{e['path']}", "source": e["source"],
                             "uri": e["uri"]} for e in ranked[:max_results]]}

    def browser_item_id(item: int | str, category: str) -> tuple[int, str]:
        """(id, name) for a browser_search id or a search query."""
        if isinstance(item, int) or re.fullmatch(r"\d+", str(item).strip()):
            name = lom_get(f"id {int(item)}", ["name"]).get("name")
            return int(item), name if isinstance(name, str) else str(item)
        # an exact name ends the walk; otherwise 25 strong hits or 3 s after
        # the first (a whole large User Library walked kept Live busy for
        # minutes); with nothing found yet it goes on for up to 20 s
        search = search_browser(str(item), category, max_results=25, time_limit=3.0,
                                patience=20.0,
                                enough=lambda e: same_name(str(item), e["name"]))
        hits = search["results"]
        if not hits:
            raise ValueError(f"Nothing in the browser ({category}) matches {item!r}"
                             + incomplete(search))
        best = by_name_match(str(item), hits)[0]
        return best["id"], best["path"]

    def incomplete(search: dict) -> str:
        """Why a search found nothing, when its walk did not finish."""
        if search["complete"]:
            return ""
        return (f": searched {search['searched']} items in {search['seconds']} s without "
                "finishing; pass an id from browser_search or a narrower category")

    def pad_note(pad: int | str) -> int:
        text = str(pad).strip().lower().replace(" ", "_")
        if text in music.GM_DRUMS:
            return music.GM_DRUMS[text]
        note = music.parse_note(pad)
        if not 0 <= note <= 127:
            raise ValueError(f"Pad note {note} is outside 0-127")
        return note

    def load_items(track: int | str, loads: list[tuple[int | None, int]],
                   rack: str | None = None) -> dict:
        """Load browser items (by id) onto a track, or onto Drum Rack pads
        (note), keeping the user's track selection. Live's browser loads onto
        whatever is selected, so the track (and its Drum Rack) is found before
        anything loads, and an item loads only once its track reads back as
        selected (and its pad's selection was accepted). Then reads back what
        the track holds; a pad that holds no chain afterwards is an error.
        rack: the Drum Rack's path when the caller found it (by the track's id)."""
        target = lom_get(track_path(track), ["name"])  # raises for a missing track
        tid = f"id {target['$id']}"  # its name can change as it loads (Live names it)
        if rack is None and any(note is not None for note, _item in loads):
            rack = drum_rack_path(track, tid)
            if rack is None:
                raise ValueError("No Drum Rack on this track (nor inside its racks) for drum_pad")
        before = [d["$id"] for d in lom_get(f"{tid} devices", ["name"])["items"]]
        steps: list[dict] = []
        # the user's selection, and arms Live changes as an item lands
        with arms_put_back():
            for note, item_id in loads:
                ops = [op_set("live_set view", "selected_track", ref(tid))]
                if note is not None:
                    ops.append(op_set(f"{rack} view", "selected_drum_pad",
                                      ref(f"{rack} drum_pads {note}")))
                ops.append({"type": "lom_get", "params": {"path": "live_set view",
                                                          "properties": ["selected_track"]}})
                chosen = batch(ops)
                failed = next((r for r in chosen["results"] if r["status"] == "error"), None)
                selected = (chosen["results"][-1].get("result") or {}).get("selected_track") \
                    if failed is None else None
                if failed is None and not (isinstance(selected, dict)
                                           and f"id {selected.get('$id')}" == tid):
                    failed = {"message": "another track stayed selected"}
                if failed is not None:
                    if note is None:
                        raise RuntimeError(f"Nothing loaded: could not select the track "
                                           f"({failed['message']})")
                    steps.append({"item": item_id, "drum_pad": note,
                                  "error": f"not loaded, the pad was not selected: "
                                           f"{failed['message']}"})
                    continue
                done = batch_summary(batch([op_call("live_app browser", "load_item",
                                                    ref(f"id {item_id}"))]))
                step: dict[str, Any] = {"item": item_id}
                if note is not None:
                    step["drum_pad"] = note
                if not done["ok"]:
                    step["error"] = done["steps"][0].get("error")
                steps.append(step)
        # what the track holds now
        now = lom_get(tid, ["name"])["name"]
        devices = lom_get(f"{tid} devices", ["name"])["items"]
        result: dict[str, Any] = {"track": now, "devices": [d["name"] for d in devices]}
        notes = [s["drum_pad"] for s in steps if "drum_pad" in s and "error" not in s]
        if notes:
            pads = lom_get(f"{rack} drum_pads", ["note", "name", "chains"])["items"]
            by_note = {p["note"]: p for p in pads}
            result["pads"] = {}
            for s in steps:
                pad = by_note.get(s.get("drum_pad"))
                if pad is None or "error" in s:
                    continue
                if pad.get("chains"):
                    result["pads"][str(pad["note"])] = pad.get("name")
                else:
                    s["error"] = "the pad is still empty"
        if any("drum_pad" not in s and "error" not in s for s in steps) \
                and [d["$id"] for d in devices] == before:
            result["warning"] = "the track's devices did not change"
        result["ok"] = not any("error" in s for s in steps)
        result["steps"] = steps
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
        Live restarts).
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
        track = pin_track(track)  # a missing track fails before the search
        rack = drum_rack_path(track) if note is not None else None
        if note is not None and rack is None:
            raise ValueError("No Drum Rack on this track (nor inside its racks) for drum_pad")
        item_id, label = browser_item_id(
            item, category or ("drums" if note is not None else "all"))
        result = load_items(track, [(note, item_id)], rack)
        if not result["ok"]:
            raise RuntimeError(f"Live could not load {label}: {result['steps'][0]['error']}")
        loaded = {"loaded": label, "track": result["track"], "drum_pad": note,
                  "devices": result["devices"]}
        if note is not None:
            loaded["pad_name"] = result["pads"].get(str(note))
        if result.get("warning"):
            loaded["warning"] = result["warning"]
        return to_json(loaded)

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
        """Build a Drum Rack from one-shots in Live's Drums › Drum Hits (the user's
        included), each role on its General MIDI pad, so generate_drum_pattern plays it at
        once. Adds a Drum Rack if the track has none (Live 12.3+).

        - roles: default kick, snare, clap, closed_hat, open_hat, rim, perc, shaker, crash,
          low_tom, high_tom (also pedal_hat, mid_tom, ride, cowbell)
        - sounds: per role a browser_search id or query, {"kick": "808 long"}
        - style: a word added to every search ("808", "909", "lofi")
        - source: any | core | user; seed: another of the best matches; replace: refill
          pads that already have a sound
        """
        import random

        roles = roles or DEFAULT_KIT
        if source not in ("any", "core", "user"):
            raise ValueError("source must be any, core or user")
        unknown = [r for r in roles if r not in DRUM_KIT_ROLES]
        if unknown:
            raise ValueError(f"Unknown roles {unknown}. Known: {', '.join(DRUM_KIT_ROLES)}")
        track = pin_track(track)  # Live renames "1-MIDI" as the Drum Rack lands
        rack = drum_rack_path(track)
        if rack is None:
            inserted_device_ids(track_path(track), [["Drum Rack"]])
            rack = drum_rack_path(track)
        pads = lom_get(f"{rack} drum_pads", ["note", "chains"])["items"]
        filled = {p["note"] for p in pads if p.get("chains")}
        rng = random.Random(seed)
        plan, report, missing = [], [], []
        unfinished: dict[str, str] = {}
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
                        search = search_browser(query, "drums", max_results=400,
                                                within=["Drum Hits", folder])
                    except AbletonCommandError:  # no such folder in this Live
                        break
                    found = search["results"]
                    if not search["complete"]:
                        unfinished[role] = incomplete(search)[2:]
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
        result = {"track": loaded.get("track") or track_name(track), "pads": report,
                  "ok": loaded["ok"]}
        if missing:
            result["no_match"] = missing
            if any(r in unfinished for r in missing):
                result["searches_unfinished"] = {r: unfinished[r] for r in missing
                                                 if r in unfinished}
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
        """Save and restore how the set sounds: mixer (volume, pan, sends, mute) and
        every device parameter, racks included.

        action:
        - save: `tracks` (default all, returns and the master) under `name` (default a
          timestamp)
        - restore: one undo step; automated parameters are left alone
        - diff: what changed since the snapshot (or against compare_to), in Live's units
        - list | delete
        A/B: save "A", change, save "B", restore either while the user listens. Tracks
        match by name.
        """
        from . import snapshots

        action = action.lower()
        if action == "list":
            every = snapshots.list_all()
            automatic = [s for s in every if str(s.get("name", "")).startswith("auto ")]
            return to_json({"snapshots": [s for s in every if s not in automatic],
                            "automatic": [{"name": s["name"], "created": s["created"]}
                                          for s in automatic],
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
            if report["changes"] and not compare_to:
                # The state being replaced, so the restore itself can be taken back
                keep_auto_snapshot(f"restoring {name}", current)
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

    AUTO_SNAPSHOTS = 10

    def keep_auto_snapshot(reason: str, captured: list[dict]) -> None:
        from . import snapshots

        name = f"auto {time.strftime('%H.%M.%S')} before {reason}"
        snapshots.save(name, captured)
        journal.attach_snapshot(name)
        autos = sorted(snapshots.snapshot_dir().glob("auto *.json"), key=lambda p: p.stat().st_mtime)
        for old in autos[:-AUTO_SNAPSHOTS]:
            old.unlink(missing_ok=True)

    def auto_snapshot(reason: str, tracks: list[int | str] | None) -> None:
        """Save how `tracks` sound before a tool changes their parameters. Unlike
        undo, restoring it still works after the user has edited the set."""
        from . import snapshots

        if PREVIEW.get():
            return
        try:
            paths = [track_path(t) for t in tracks] if tracks else current_track_paths()
            keep_auto_snapshot(reason, snapshots.capture(run_steps, paths))
        except Exception as e:  # never stands in the way of the change itself
            logger.debug("No automatic snapshot before %s: %s", reason, e)

    @tool(destructive=True)
    def agent_changes(undo: int = 0) -> str:
        """What the agent changed in Live this session, newest first: tool,
        arguments, time, undo steps, and the automatic snapshot taken before
        parameter changes. undo=N takes back the last N changes with Live's
        undo, exactly as many steps as they made. Live's undo history is the
        user's too: if they edited the set since, undo would take their edits
        back first; then restore the change's snapshot_before instead, or let
        them undo in Live.
        """
        if undo <= 0:
            return to_json({"changes": [e.describe() for e in journal.recent(30)]})
        entries = journal.undoable(int(undo))
        if not entries:
            raise ValueError("Nothing of the agent's to undo (or it was undone already)")
        steps = sum(e.steps for e in entries)
        with journal.not_recorded():
            if not lom_get("live_set", ["can_undo"]).get("can_undo"):
                raise RuntimeError("Live has nothing to undo")
            done = 0
            for _ in range(steps):
                send("lom_call", {"path": "live_set", "method": "undo"})
                done += 1
        for entry in entries:
            entry.undone = True
        return to_json({"undone": [e.describe() for e in entries], "undo_steps": done,
                        "redo": "transport(action=\"redo\") steps forward again"})

    # ── Song structure ───────────────────────────────────────────────────────

    def session_clips() -> tuple[list[str], list[dict[int, dict]], list[str], set[int]]:
        """Track names, each track's Session clips by slot, scene names and
        the indexes of group tracks (they hold no clips)."""
        listing = run_steps([
            {"type": "lom_get", "params": {"path": "live_set tracks",
                                           "properties": ["name", "is_foldable"], "ids": False}},
            {"type": "lom_get", "params": {"path": "live_set scenes", "properties": ["name"],
                                           "ids": False}}])
        names = [t.get("name") for t in listing[0]["result"]["items"]]
        groups = {i for i, t in enumerate(listing[0]["result"]["items"])
                  if t.get("is_foldable") is True}
        scenes = [s.get("name") for s in listing[1]["result"]["items"]]
        slots = run_steps([{"type": "lom_get", "params": {
            "path": f"live_set tracks {i} clip_slots", "properties": ["has_clip"], "ids": False}}
            for i in range(len(names))])
        wanted = [(i, k) for i, step in enumerate(slots)
                  for k, slot in enumerate((step.get("result") or {}).get("items", []))
                  if slot.get("has_clip")]
        details = run_steps([{"type": "lom_get", "params": {
            "path": f"live_set tracks {i} clip_slots {k} clip", "ids": False,
            "properties": SIZE_FIELDS}}
            for i, k in wanted])
        clips: list[dict[int, dict]] = [{} for _ in names]
        for (i, k), step in zip(wanted, details):
            if step.get("status") == "success":
                clips[i][k] = step["result"]
        return names, clips, scenes, groups

    @tool(destructive=True)
    @keeping_playhead
    def build_arrangement(sections: list[dict], start_bar: int = 1,
                          locators: bool = True, clear_others: bool = True,
                          clear_locators: bool = True) -> str:
        """Lay Session clips out in the Arrangement as a song, in one call.

        sections, in order, each {"name", "bars"} plus what plays:
        - "scene": index or name, every track plays its clip there; "except": ["Drums"]
          leaves tracks out
        - "clips": {"Drums": 0, "Bass": "Bass B"}: slot index or clip name per track
        Clips repeat, the last copy cut to fit. A track not playing in a section is
        cleared there (clear_others); one with no Session clips only where a section
        names it ({"Vox": None}, except). locators: one per section start;
        clear_locators deletes others in the built bars. e.g. [{"name": "Intro",
        "bars": 8, "scene": 0, "except": ["Drums"]}, {"name": "Drop", "bars": 16, "scene": 1}]
        """
        bar = song_context()["beats_per_bar"]
        tempo = float(lom_get("live_set", ["tempo"])["tempo"])
        names, clips, scenes, groups = session_clips()

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

        place, report, warnings, cues, spans = [], [], [], [], []
        place_where: list[str] = []  # each step's section and track, for its error
        trim_where: list[str] = []
        warned: set[tuple[int, int]] = set()
        first = position = (int(start_bar) - 1) * bar
        trims: list[tuple[str, str, float, float, float]] = []
        for number, section in enumerate(sections, start=1):
            label = str(section.get("name") or f"Section {number}")
            named: set[int] = set()  # the tracks this section names (clips, except)
            length = float(section.get("bars", 8)) * bar
            plays: dict[int, int] = {}
            if section.get("scene") is not None:
                row = section["scene"]
                row = int(row) if re.fullmatch(r"\d+", str(row).strip()) else (
                    scenes.index(row) if row in scenes else None)
                if row is None:
                    raise ValueError(f"{label}: no scene {section['scene']!r}")
                left_out = {track_index(t) for t in section.get("except") or []}
                named |= left_out
                plays = {i: row for i in range(len(names)) if row in clips[i]
                         and i not in left_out}
            for track, clip in (section.get("clips") or {}).items():
                named.add(track_index(track))
                if clip is None:
                    plays.pop(track_index(track), None)
                else:
                    plays[track_index(track)] = slot_of(track_index(track), clip)
            for i, slot in sorted(plays.items()):
                info = clips[i][slot]
                clip_length = float(info["length"]) or length
                repeats = int(length // clip_length + 1e-9)
                tp, source = f"live_set tracks {i}", f"live_set tracks {i} clip_slots {slot} clip"
                where = f"{label}: {names[i]}"
                for k in range(repeats):
                    place.append(op_call(tp, "duplicate_clip_to_arrangement",
                                         ref(source), position + k * clip_length))
                rest = length - repeats * clip_length
                if rest > 1e-6 and unwarped(info):
                    trims.append((tp, source, position + repeats * clip_length, rest,
                                  clip_length))
                    trim_where.append(where)
                elif rest > 1e-6:  # the rest at its own length, envelopes and all
                    place += sized_copy(tp, source, position + repeats * clip_length, info, rest)
                place_where += [where] * (len(place) - len(place_where))
                if (i, slot) not in warned:
                    warned.add((i, slot))
                    warnings += [f"{label}: {names[i]}: {w}" for w in copy_warnings(info, tempo,
                                                                            takes_length=False)]
            if locators:
                cues.append((label, position))
            spans.append((position, position + length, set(plays), named))
            report.append({"section": label, "bar": int(position // bar) + 1,
                           "bars": length / bar,
                           "tracks": {names[i]: clips[i][s].get("name") for i, s in
                                      sorted(plays.items())}})
            position += length
        errors: list[str] = []
        cleared: dict[str, dict] = {}
        kept: dict[str, int] = {}
        if clear_others:  # what a track does not play in a section is not heard there
            for i, name in enumerate(names):
                if i in groups:  # a group track has no clips of its own
                    continue
                # a track without Session clips (not part of the layout) is cleared
                # only in the sections that name it, and left alone in the others
                ranges = [(a, b) for a, b, playing, named in spans
                          if i not in playing and (clips[i] or i in named)]
                alone = [(a, b) for a, b, playing, named in spans
                         if i not in playing and not clips[i] and i not in named]
                if ranges:
                    try:
                        cut = cut_ranges(i, ranges)
                    except Exception as e:  # said, and the rest of the build still runs
                        errors.append(f"{name}: not cleared: {e}")
                        continue
                    done = {k: cut[f"clips_{k}"] for k in ("removed", "trimmed")
                            if cut.get(f"clips_{k}")}
                    if done:
                        cleared[name] = done
                    if cut.get("left"):
                        errors.append(f"{name}: clips left at {cut['left']}")
                if alone:  # what is left there after the cuts
                    try:
                        there = [c for c in lom_get(f"live_set tracks {i} arrangement_clips",
                                                    ["start_time", "end_time"])["items"]
                                 if any(c["start_time"] < b - 1e-6 and c["end_time"] > a + 1e-6
                                        for a, b in alone)]
                    except Exception as e:
                        logger.debug("No Arrangement read on %s: %s", name, e)
                        there = []
                    if there:
                        kept[name] = len(there)
        placed = batch(place, stop_on_error=False) if place else {"results": []}
        errors += [f"{place_where[k]}: {s.get('message')}"
                   for k, s in enumerate(placed["results"]) if s.get("status") == "error"]
        if trims:
            errors += [f"{trim_where[k]}: {s.get('message')}"
                       for k, s in enumerate(trimmed_copies(trims, tempo))
                       if s.get("status") == "error"]
        result: dict[str, Any] = {"sections": report, "end_bar": int(position // bar) + 1,
                                  "time_signature": song_context()["time_signature"]}
        if cleared:
            result["cleared"] = cleared
        if kept:  # Arrangement clips of tracks with no Session clips, not named
            result["kept"] = kept
        if cues:
            starts = [at for _label, at in cues]
            remove = [c["time"] for c in lom_get("live_set cue_points", ["time"])["items"]
                      if first - 1e-3 < c["time"] < position - 1e-3
                      and all(abs(c["time"] - t) > 1e-3 for t in starts)] if clear_locators else []
            rows = place_locators(cues, remove=remove)
            if remove:
                result["locators_deleted"] = rows.pop()["deleted"]
            errors += [f"locator {c['name']!r}: {c['error']}" for c in rows if "error" in c]
        if warnings:
            result["warnings"] = warnings
        if errors:
            result["errors"] = errors[:20]
        return to_json({"ok": not errors, **result})

    # ── Generated audio (MusicGen) ───────────────────────────────────────────

    @tool
    async def generate_audio(prompt: str, seconds: float = 8.0, variations: int = 1,
                             seed: int | None = None, track: int | str | None = None,
                             clip_index: int | None = None,
                             arrangement_time: float | None = None,
                             drum_pad: int | str | None = None, follow_set: bool = True,
                             ctx: Context | None = None) -> str:
        """Make audio from a text prompt with MusicGen (small) on this computer
        ("dusty lo-fi drum loop", "warm analog pad swell", "metallic snare hit")
        and put it in the set:
        - an audio track: a clip in clip_index (Session; default the first free
          slot) or at arrangement_time (beats)
        - a MIDI track: on its Drum Rack's drum_pad ("snare", "D1", 38), else
          in a Simpler
        - no track: the files only
        Up to 30 s; variations makes that many takes (the first goes into the
        set); follow_set adds the set's tempo and key to the prompt. Files go
        to the User Library (Samples › AbletonMCP Generated). The first use
        downloads ~2.5 GB. MusicGen's weights are CC-BY-NC 4.0: what it makes is
        for non-commercial use, which the user should know.
        """
        from . import als, ml

        if not 1.0 <= float(seconds) <= ml.MAX_GENERATED_SECONDS:
            raise ValueError(f"seconds must be between 1 and {ml.MAX_GENERATED_SECONDS:g}")
        if not 1 <= int(variations) <= 4:
            raise ValueError("variations must be between 1 and 4")
        text = prompt
        if follow_set:
            try:
                context = await asyncio.to_thread(song_context, True)
                tempo = (await asend("lom_get", {"path": "live_set",
                                                 "properties": ["tempo"]}))["tempo"]
                text = ml.generation_prompt(prompt, tempo, context.get("key"),
                                            context.get("scale_name"))
            except Exception as e:  # no Live to ask: the prompt as given
                logger.debug("Prompt without the set's tempo and key: %s", e)
        folder = ml.generated_folder(als._user_libraries())
        job = {"prompt": text, "seconds": float(seconds), "variations": int(variations),
               "seed": int(seed) if seed is not None else int(time.time()) % 100000,
               "out_dir": str(folder), "stem": ml.file_stem(prompt, time.strftime("%Y%m%d-%H%M%S")),
               "model": ml.MUSICGEN_MODEL}
        result = await ml.run("generate", job, ml_progress(ctx))
        payload: dict[str, Any] = {"files": result["files"], "prompt": text,
                                   "seconds": result["seconds"], "seed": job["seed"],
                                   "device": result["device"], "license": ml.MUSICGEN_LICENSE}
        if track is not None:
            payload["placed"] = await place_generated(track, result["files"][0], clip_index,
                                                      arrangement_time, drum_pad)
        return to_json(payload)

    async def from_the_files_start(made: dict) -> dict:
        """The new clip's start back at the file's, its length checked
        (fit_audio_clip): its warning, if any."""
        clip_id = ((made or {}).get("result") or {}).get("$id")
        if not clip_id:
            return {}
        fitted = await asyncio.to_thread(fit_audio_clip, f"id {clip_id}")
        return {k: fitted[k] for k in ("warning",) if k in fitted}

    async def place_generated(track: int | str, file: str, clip_index: int | None,
                              arrangement_time: float | None, drum_pad: int | str | None) -> dict:
        track = await asyncio.to_thread(pin_track, track)  # a load can rename it
        tp = track_path(track)
        info = await asend("lom_get", {"path": tp, "properties": ["name", "has_audio_input"]})
        if info.get("has_audio_input"):
            if arrangement_time is not None:
                made = await keeping_the_playhead(asend("lom_call", {
                    "path": tp, "method": "create_audio_clip",
                    "args": [file, float(arrangement_time)]}))
                return {"track": info["name"], "arrangement_time": arrangement_time,
                        **await from_the_files_start(made)}
            slots = (await asend("lom_get", {"path": f"{tp} clip_slots",
                                             "properties": ["has_clip"]}))["items"]
            row = clip_index if clip_index is not None else next(
                (i for i, s in enumerate(slots) if not s.get("has_clip")), None)
            if row is None:
                raise ValueError("No free Session slot on that track; give clip_index or "
                                 "arrangement_time")
            if slots[row].get("has_clip"):
                raise ValueError(f"Slot {row} already holds a clip")
            made = await asend("lom_call", {"path": f"{tp} clip_slots {int(row)}",
                                            "method": "create_audio_clip", "args": [file]})
            return {"track": info["name"], "clip_index": row, **await from_the_files_start(made)}
        # A MIDI track: through Live's browser, which lists the file after a moment
        name = Path(file).stem
        for _attempt in range(30):
            try:
                item_id, _label = await asyncio.to_thread(browser_item_id, name, "user_library")
                break
            except ValueError:
                await asyncio.sleep(1.0)
        else:
            raise RuntimeError(f"Live's browser does not list {name} yet; it is in "
                               f"{Path(file).parent}")
        note = pad_note(drum_pad) if drum_pad is not None else None
        if note is not None and await asyncio.to_thread(drum_rack_path, track) is None:
            raise ValueError("That track has no Drum Rack for drum_pad")
        loaded = await asyncio.to_thread(load_items, track, [(note, item_id)])
        if not loaded.get("ok"):
            raise RuntimeError(f"Live could not load it: {loaded.get('steps')}")
        return {"track": loaded["track"], **({"drum_pad": note} if note is not None
                                          else {"device": "Simpler"})}

    # ── Arrangement automation and transitions ──────────────────────────────

    @tool(destructive=True)
    def write_arrangement_automation(track: int | str, parameter: int | str,
                                     device: int | str = "mixer",
                                     start_bar: float | None = None, bars: float | None = None,
                                     start: float | None = None, end: float | None = None,
                                     shape: str | None = None, points: list[dict] | None = None,
                                     low: float = 0.0, high: float = 1.0, cycles: float = 1.0,
                                     normalized: bool = True, resolution: float = 0.25) -> str:
        """Automation over a stretch of the Arrangement on a MIDI track, at
        once: the notes there go into a Session clip with the envelope (Live's
        API draws envelopes only on those), laid back over the same stretch.
        Settings of the clips it replaces there (transpose, their own
        envelopes) are not kept; after the stretch the parameter is back where
        it was. Audio tracks, returns, the main track: record_automation.

        start_bar/bars (1-based) or start/end in beats; device, parameter,
        shape, low/high and normalized as in write_clip_automation, or points
        [{"time": beats from the start, "value"}].
        """
        s, e = bar_range(start_bar, bars, start, end)
        pp = parameter_path(track, device, parameter)
        info = lom_get(pp, ["min", "max", "name"])
        steps = envelope_steps(shape, points, e - s, low, high, cycles, resolution,
                               float(info["min"]), float(info["max"]), normalized)
        notes, clips = arrangement_notes(track, s, e)
        first = clips[0] if clips else {}
        result = rewrite_range(track, s, e, notes, [(pp, steps)],
                               name=first.get("name") or info["name"],
                               color_index=first.get("color_index"))
        result.update(parameter=info["name"], replaced_clips=len(clips))
        return to_json(result)

    @tool(destructive=True)
    async def record_automation(track: int | str, parameter: int | str,
                                device: int | str = "mixer",
                                start_bar: float | None = None, bars: float | None = None,
                                start: float | None = None, end: float | None = None,
                                shape: str | None = None, points: list[dict] | None = None,
                                low: float = 0.0, high: float = 1.0, cycles: float = 1.0,
                                normalized: bool = True, hold: bool = False,
                                ctx: Context | None = None) -> str:
        """Track automation in the Arrangement on any track (audio, a return,
        the main track): plays the stretch while Live records and moves the
        parameter in time. Takes the stretch's length in real time, out loud;
        armed tracks are disarmed and loop and punch off meanwhile (put back
        after). Replaces that parameter's automation there; hold=False returns
        to the value from before right after. Points about every 50 ms: smooth
        for sweeps and fades. Arguments as in write_arrangement_automation.
        """
        s, e = await asyncio.to_thread(bar_range, start_bar, bars, start, end)
        pp = parameter_path(track, device, parameter)
        info = await asend("lom_get", {"path": pp, "properties": ["min", "max", "value", "name"]})
        pmin, pmax, before = float(info["min"]), float(info["max"]), float(info["value"])
        if not shape and not points:
            raise ValueError("Give a shape or points")

        def value_at(position: float) -> float:
            if shape:
                v = transitions.curve(shape.lower(), (position - s) / (e - s), low, high, cycles)
            else:
                v = transitions.points_value(points, position - s)
            v = pmin + (pmax - pmin) * v if normalized else v
            return min(pmax, max(pmin, v))

        keys = ["is_playing", "loop", "punch_in", "punch_out", "record_mode",
                "session_automation_record", "current_song_time", "tempo"]
        song = await asend("lom_get", {"path": "live_set", "properties": keys})
        if song["is_playing"]:
            raise ValueError("Stop playback first: recording plays the stretch from its start")
        # Armed and implicitly armed tracks would record too (by id: the
        # user may add tracks meanwhile)
        armed, implicit = arming((await asend("lom_get", {
            "path": "live_set tracks", "properties": ["arm", "implicit_arm"]}))["items"])
        seconds = (e - s + 1) * 60.0 / float(song["tempo"])
        set_song = lambda prop, value: asend("lom_set", {  # noqa: E731
            "path": "live_set", "property": prop, "value": value})
        # Live 12.4 keeps an undo step for every value set while it records,
        # plus one (measured with 2 to 48 values, spaced 10 ms to 300 ms): the
        # first undo takes the automation, the rest are empty. Counted here,
        # values sent only when they move.
        written, steps, last = 0, 0, None
        smallest = (pmax - pmin) * 0.002
        # Progress twice a second, without waiting on it: the values must keep time
        reports: set[asyncio.Task] = set()
        reported = 0.0
        with journal.not_recorded():
            try:
                ready = await asend("batch", {"commands": [
                    op_set("live_set", "loop", False), op_set("live_set", "punch_in", False),
                    op_set("live_set", "punch_out", False),
                    op_set("live_set", "session_automation_record", True)]
                    + disarm_ops(armed, implicit) + [REC_ARMS],
                    "stop_on_error": False})
                # Nothing may record notes or audio over the stretch: read back
                arms_read = ready["results"][-1]
                if arms_read.get("status") == "error":
                    raise RuntimeError("Not recording: the tracks' arms could not be read back ("
                                       + arms_read.get("message", "") + ")")
                strays = [r.get("name") or f"id {r['$id']}" for r in rows_of(arms_read)
                          if r.get("arm") is True or r.get("implicit_arm") is True]
                if strays:
                    raise RuntimeError("Not recording: " + ", ".join(map(str, strays))
                                       + " stayed armed and would record too.")
                await asend("lom_call", {"path": "live_set", "method": "start_playing"})
                await set_song("current_song_time", max(0.0, s - 1.0))
                deadline = time.monotonic() + seconds + 10
                recording = False
                while True:
                    if time.monotonic() > deadline:
                        raise RuntimeError("Live did not play through the stretch in time")
                    position = (await asend("lom_get", {
                        "path": "live_set", "properties": ["current_song_time"]}))["current_song_time"]
                    if not recording and position >= s - 0.05:
                        await set_song("record_mode", True)
                        recording, steps = True, 1
                    if recording:
                        at = min(position, e)
                        value = value_at(at)
                        if last is None or abs(value - last) >= smallest or position >= e:
                            await asend("lom_set", {"path": pp, "property": "value",
                                                    "value": value})
                            written, steps, last = written + 1, steps + 1, value
                        if time.monotonic() - reported >= 0.5:
                            task = asyncio.create_task(report_progress(
                                ctx, at - s, e - s, "Recording automation"))
                            reports.add(task)
                            task.add_done_callback(reports.discard)
                            reported = time.monotonic()
                        if position >= e:
                            if not hold:
                                await asend("lom_set", {"path": pp, "property": "value",
                                                        "value": before})
                                steps += 1
                            break
                    await asyncio.sleep(0.02)
            finally:
                await set_song("record_mode", False)
                await asend("lom_call", {"path": "live_set", "method": "stop_playing"})
                if reports:
                    await asyncio.wait(reports, timeout=2)
                await asend("batch", {"commands": [
                    op_set("live_set", key, song[key]) for key in
                    ("loop", "punch_in", "punch_out", "session_automation_record",
                     "current_song_time")]
                    + disarm_ops(armed, implicit, on=True),
                    "stop_on_error": False})
        journal.add_steps(steps)
        return to_json({"parameter": info["name"], "from_beat": s, "to_beat": e,
                        "points": written, "held": hold, "seconds": round(seconds, 1),
                        "undo_steps": steps})

    def track_named(name: str, create: bool = True) -> int | None:
        names = [t["name"] for t in lom_get("live_set tracks", ["name"])["items"]]
        if name in names:
            return names.index(name)
        if not create:
            return None
        send("lom_call", {"path": "live_set", "method": "create_midi_track", "args": [-1]})
        send("lom_set", {"path": f"live_set tracks {len(names)}", "property": "name",
                         "value": name})
        return len(names)

    def device_parameters(device: str) -> dict[str, dict]:
        items = lom_get(f"{device} parameters", ["name", "min", "max", "is_quantized",
                                                 "value_items"])["items"]
        return {p["name"]: dict(p, index=i) for i, p in enumerate(items)}

    def choose_item(parameter: dict, *wanted: str) -> float | None:
        items = parameter.get("value_items") if parameter.get("is_quantized") else None
        if not isinstance(items, list):
            return None
        for word in wanted:
            for i, item in enumerate(items):
                if word in str(item).lower():
                    return float(parameter["min"]) + i
        return None

    def midi_tracks_playing(start: float, end: float) -> list[int]:
        found = []
        for i, t in enumerate(lom_get("live_set tracks", ["has_midi_input"])["items"]):
            if t.get("has_midi_input") and arrangement_notes(i, start, end)[0]:
                found.append(i)
        return found

    @tool(destructive=True)
    @keeping_arms
    def design_transition(kind: str, at_bar: float, bars: float | None = None,
                          track: int | str | None = None,
                          tracks: list[int | str] | None = None,
                          fill: str = "snare_roll") -> str:
        """Build a transition into the section that starts at `at_bar` (1-based)
        in the Arrangement:

        - riser: a climbing line in the set's key over the last `bars` (4) on a
          "Riser" track (Operator, saw, filter opening, level rising)
        - reverse_cymbal: a crash from Drum Hits, reversed in Simpler on a
          "Reverse Cymbal" track, swelling into the downbeat (`bars`, 2, at most)
        - drum_fill: the drum track's last `bars` (1) become a fill: fill =
          snare_roll | toms | build; track: default the first with a Drum Rack
        - gap: silence for the last `bars` (1/4 bar) on `tracks` (default:
          MIDI tracks with notes there; audio tracks are cut); returns the
          clips around it
        - filter_sweep: a high-pass Auto Filter on each MIDI track in `tracks`
          (default: those playing) sweeping up over the last `bars` (4), open
          again on the downbeat (record_automation for audio tracks or the
          main track)

        Built as Arrangement clips (see write_arrangement_automation); undo with
        agent_changes.
        """
        kind = kind.strip().lower().replace(" ", "_").replace("-", "_")
        context = song_context()
        bar = context["beats_per_bar"]
        defaults = {"riser": 4, "reverse_cymbal": 2, "drum_fill": 1, "gap": 0.25,
                    "filter_sweep": 4}
        if kind not in defaults:
            raise ValueError("kind must be " + ", ".join(defaults))
        at = (float(at_bar) - 1) * bar
        span = float(bars if bars is not None else defaults[kind]) * bar
        start = at - span
        if start < 0:
            raise ValueError(f"at_bar {at_bar} leaves no room for {span:g} beats before it")
        result: dict[str, Any] = {"kind": kind, "from_beat": start, "to_beat": at}

        if kind == "riser":
            index = track_named("Riser")
            tp = track_path(index)
            if not lom_get(f"{tp} devices", ["name"])["items"]:
                inserted_device_ids(tp, [["Operator"]])
            operator = f"{tp} devices 0"
            params = device_parameters(operator)
            ops = []
            wave = params.get("Osc-A Wave")
            saw = choose_item(wave, "saw 16", "saw") if wave else None
            if saw is not None:
                ops.append(op_set(f"{operator} parameters {wave['index']}", "value", saw))
            if "Filter On" in params:
                ops.append(op_set(f"{operator} parameters {params['Filter On']['index']}",
                                  "value", 1.0))
            if ops:
                batch(ops, stop_on_error=False)
            root = music.NOTE_NAMES.index(context["key"]) if context.get("key") else 0
            notes = [dict(n, start_time=n["start_time"] + start) for n in
                     transitions.riser_notes(span, 48 + root, context.get("scale"))]
            envelopes = []
            if "Filter Freq" in params:
                p = params["Filter Freq"]
                envelopes.append((f"{operator} parameters {p['index']}", envelope_steps(
                    "exp_up", None, span, 0.3, 1.0, 1, 0.25, float(p["min"]), float(p["max"]),
                    True)))
            volume = f"{tp} mixer_device volume"
            envelopes.append((volume, envelope_steps("ramp_up", None, span, 0.55, 0.85, 1, 0.25,
                                                     0.0, 1.0, True)))
            result.update(track="Riser", **rewrite_range(index, start, at, notes, envelopes,
                                                         name="Riser"))
        elif kind == "reverse_cymbal":
            index = track_named("Reverse Cymbal")
            tp = track_path(index)
            devices = lom_get(f"{tp} devices", ["class_name"])["items"]
            simplers = [i for i, d in enumerate(devices) if d.get("class_name") == "OriginalSimpler"]
            if simplers:  # a Simpler holding something else than a crash goes
                try:
                    held = str(lom_get(f"{tp} devices {simplers[0]} sample",
                                       ["file_path"]).get("file_path") or "")
                except AbletonCommandError:  # an empty Simpler has no sample
                    held = ""
                # Live's reversed copy is this transition's own, whatever its name
                if "crash" not in os.path.basename(held).lower() and not is_reversed(held):
                    for i in reversed(simplers):
                        send("lom_call", {"path": tp, "method": "delete_device", "args": [i]})
                    devices = lom_get(f"{tp} devices", ["class_name"])["items"]
            if not any(d.get("class_name") == "OriginalSimpler" for d in devices):
                found = [r for r in search_browser("crash", "drums", 40)["results"]
                         if r["kind"] == "sample"]
                if not found:
                    raise ValueError("No crash sample in the browser's Drums")
                # a plain crash (Drum Hits/Cymbal), not a loop or one already reversed
                sample = sorted(by_name_match("crash", found),
                                key=lambda r: "/cymbal/" not in r["path"].lower())[0]
                loaded = load_items(index, [(None, sample["id"])])
                if not loaded["ok"]:
                    raise RuntimeError(f"Could not load {sample['path']}: {loaded['steps']}")
                devices = lom_get(f"{tp} devices", ["class_name"])["items"]
            simpler = f"{tp} devices " + str(next(
                i for i, d in enumerate(devices) if d.get("class_name") == "OriginalSimpler"))
            # Simpler.reverse() toggles; Live writes the reversed copy to
            # Samples/Processed/Reverse/<name> R.wav (12.4.16), so a sample
            # already there, or named reversed, stays as it is
            path = str(lom_get(f"{simpler} sample", ["file_path"]).get("file_path") or "")
            if not is_reversed(path):
                send("lom_call", {"path": simpler, "method": "reverse"})
            info = lom_get(f"{simpler} sample", ["length", "sample_rate", "file_path"])
            seconds = float(info["length"]) / float(info["sample_rate"] or 44100.0)
            tempo = float(lom_get("live_set", ["tempo"])["tempo"])
            length = min(span, seconds * tempo / 60.0)
            if length < seconds * tempo / 60.0:  # start later in the reversed sample
                params = device_parameters(simpler)
                if "S Start" in params:
                    p = params["S Start"]
                    skip = 1 - length / (seconds * tempo / 60.0)
                    send("lom_set", {"path": f"{simpler} parameters {p['index']}",
                                     "property": "value", "value": float(p["min"]) + skip * (
                                         float(p["max"]) - float(p["min"]))})
            note = music._note(60, at - length, length, 110)
            result.update(track="Reverse Cymbal", sample=os.path.basename(info["file_path"]),
                          **rewrite_range(index, at - length, at, [note],
                                          name="Reverse Cymbal"))
        elif kind == "drum_fill":
            if track is None:
                tracks_info = lom_get("live_set tracks", ["name"])["items"]
                track = next((i for i in range(len(tracks_info)) if drum_rack_path(i)), None)
                if track is None:
                    raise ValueError("No track with a Drum Rack; say which track")
            rack = drum_rack_path(track)
            pads = {}
            if rack:
                filled = {p["note"] for p in lom_get(f"{rack} drum_pads", ["note", "chains"])[
                    "items"] if p.get("chains")}
                if fill == "toms" and not {45, 47, 48, 50} & filled:
                    fill = "snare_roll"  # no toms in this kit
                    result["note"] = "No toms on the kit's pads: a snare roll instead"
            notes, clips = arrangement_notes(track, start, at)
            new = [dict(n, start_time=n["start_time"] + start)
                   for n in transitions.fill_notes(span, fill, pads)]
            first = clips[0] if clips else {}
            result.update(track=lom_get(track_path(track), ["name"])["name"], fill=fill,
                          **rewrite_range(track, start, at,
                                          transitions.replace_span(notes, start, at, new),
                                          name=first.get("name") or "Fill",
                                          color_index=first.get("color_index")))
        elif kind == "gap":
            chosen = list(tracks) if tracks else midi_tracks_playing(start, at)
            names, around = [], {}
            for t in chosen:
                tp = track_path(t)
                info = lom_get(tp, ["name", "has_midi_input"])
                if info.get("has_midi_input"):
                    rewrite_range(t, start, at, [], name="Gap")
                else:  # audio: the clips there are cut out (cut_ranges)
                    cut_ranges(t, [(start, at)])
                name = lom_get(tp, ["name"])["name"]  # Live may rename it as clips go
                names.append(name)
                around[name] = [
                    {"arrangement_clip_index": i, "name": c.get("name"),
                     "start_time": c["start_time"], "end_time": c["end_time"]}
                    for i, c in enumerate(lom_get(f"{tp} arrangement_clips", [
                        "name", "start_time", "end_time"])["items"])
                    if c["end_time"] > start - bar and c["start_time"] < at + bar]
            result["tracks"] = names
            result["clips"] = around
        else:  # filter_sweep
            chosen = list(tracks) if tracks else midi_tracks_playing(start, at)
            if not chosen:
                raise ValueError("No MIDI track plays there; name tracks (record_automation for "
                                 "audio tracks or the main track)")
            names = []
            for t in chosen:
                tp = track_path(t)
                devices = [d["name"] for d in lom_get(f"{tp} devices", ["name"])["items"]]
                if "Auto Filter" in devices:
                    index = devices.index("Auto Filter")
                else:
                    inserted_device_ids(tp, [["Auto Filter"]])
                    index = len(devices)
                device = f"{tp} devices {index}"
                params = device_parameters(device)
                kind_param, frequency = params.get("Filter Type"), params.get("Frequency")
                if frequency is None:
                    raise ValueError("This Auto Filter has no Frequency parameter")
                ops = [op_set(f"{device} parameters {frequency['index']}", "value",
                              float(frequency["min"]))]
                high_pass = choose_item(kind_param, "high") if kind_param else None
                if high_pass is not None:
                    ops.append(op_set(f"{device} parameters {kind_param['index']}", "value",
                                      high_pass))
                batch(ops, stop_on_error=False)
                notes, clips = arrangement_notes(t, start, at)
                first = clips[0] if clips else {}
                steps = envelope_steps("exp_up", None, span, 0.0, 0.6, 1, 0.25,
                                       float(frequency["min"]), float(frequency["max"]), True)
                rewrite_range(t, start, at, notes,
                              [(f"{device} parameters {frequency['index']}", steps)],
                              name=first.get("name"), color_index=first.get("color_index"))
                names.append(lom_get(tp, ["name"])["name"])
            result["tracks"] = names
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
        connection, dialogs open in Live, CPU load, AbletonMCP errors in
        Live's log and Live's audio output device. Each problem comes with a
        fix. Changes nothing.
        """
        import os

        from . import doctor as diagnostics

        from .connection import live_port

        host = os.environ.get("ABLETON_HOST", "127.0.0.1")
        return to_json(diagnostics.run("127.0.0.1" if host == "localhost" else host, live_port()))

    @tool(idempotent=True)
    def install_remote_script() -> str:
        """Install or update the AbletonMCP Remote Script (the part that runs
        inside Live) in every User Library's Remote Scripts folder, as
        `ableton-mcp-install-script` does from a terminal. Use it when doctor
        reports the script missing or outdated. A copy that differs from this
        server's version is backed up first.

        Live loads Remote Scripts when it starts: afterwards the user must
        restart Live and, the first time, choose AbletonMCP under Settings →
        Link, Tempo & MIDI → Control Surface.
        """
        from .remote_script_install import install_remote_script as install

        results = install(force=True)
        if all(r["status"] == "error" for r in results):
            raise RuntimeError("; ".join(r["detail"] for r in results))
        changed = any(r["status"] in ("installed", "updated") for r in results)
        return to_json({
            "results": results,
            "next": ("Restart Live (it loads Remote Scripts at launch), then choose AbletonMCP "
                     "under Settings → Link, Tempo & MIDI → Control Surface if it is not "
                     "selected yet." if changed else
                     "Already up to date. If Live still reports an older version, restart "
                     "Live, or run doctor to find a copy inside the Live application."),
        })

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
        Arrangement from start_beat (default: the loop start).

        Only the temporary tracks may record: every other track's arm and
        implicit arm go off for the take (read back before it starts) and
        come back after the transport stopped and record mode is off, in a
        later command, with the selected track. The metronome is restored
        (and loop, record mode and the playhead in the Arrangement); the
        temporary tracks are deleted unless keep_tracks. Each file must hold
        the section (check_recording); the take must reach its end.
        """
        song = await asend("lom_get", {"path": "live_set", "properties": [
            "tempo", "loop", "loop_start", "metronome", "record_mode", "is_playing",
            "current_song_time", "start_time", "back_to_arranger",
            "signature_numerator", "signature_denominator"]})
        tracks = (await asend("lom_get", {"path": "live_set tracks", "properties": [
            "name", "can_be_armed", "arm", "implicit_arm", "playing_slot_index"]}))["items"]
        view = await asend("lom_get", {"path": "live_set view", "properties": ["selected_track"]})
        selected = (view.get("selected_track") or {}).get("$id") \
            if isinstance(view.get("selected_track"), dict) else None
        session_playing = song["is_playing"] and any(
            isinstance(t.get("playing_slot_index"), int) and t["playing_slot_index"] >= 0
            for t in tracks)
        session = scene is not None or (start_beat is None and session_playing)
        scene_index = (await asyncio.to_thread(index_in, "live_set scenes", scene)
                       if scene is not None else None)
        # The take's pace: a scene with a tempo of its own sets the set's
        slowest = float(song["tempo"])
        if scene_index is not None:
            own = await asend("lom_get", {"path": f"live_set scenes {scene_index}",
                                          "properties": ["tempo", "tempo_enabled"]})
            if own.get("tempo_enabled") is True and isinstance(own.get("tempo"), (int, float)) \
                    and own["tempo"] > 0:
                slowest = min(slowest, float(own["tempo"]))
        if song["is_playing"] and not session:
            await asend("lom_call", {"path": "live_set", "method": "stop_playing"})
        start = float(start_beat if start_beat is not None else song["loop_start"])
        # Tracks are addressed by id from here on: the user may add or remove
        # tracks while the section plays, which would shift every index.
        armed, implicit = arming(tracks)
        names = {t["$id"]: t.get("name") for t in tracks}
        # In the Session, record into the launched scene's row: a scene launch
        # may itself start recording on armed tracks' empty slots there
        slot = scene_index or 0
        new = "live_set tracks -1"
        temp: dict[str, int] = {}
        tempo_back = False
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
            RECORDING.update(temp.values())
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
                                f"{named(f'{track} available_input_routing_types', input_name)}")),
                            op_set(track, "name", f"MCP Rec {label}"[:48]),
                            op_set(track, "current_monitoring_state", MONITORING["off"])]
                result = await asend("batch", {"commands": ops, "stop_on_error": False})
                steps = result.get("results", [])
                position, failed, problems = 0, [], []
                for label, input_name in pending:
                    step = steps[position] if len(steps) > position else {}
                    if step.get("status") != "success":
                        failed.append((label, input_name))
                        problems.append(f"{label} ({input_name}): "
                                        f"{step.get('message', 'not run')}")
                    position += 3
                pending = failed
                if not pending:
                    break
            if problems:
                raise RuntimeError("Render setup failed: " + "; ".join(problems))
            await post_mixer([temp[label] for label, input_name in sources
                              if input_name != "Resampling"])
            # Only the temporary tracks may record: the others' arms and
            # implicit arms go off, read back before anything plays
            ready = await asend("batch", {"commands": disarm_ops(armed, implicit)
                                          + [op_set(f"id {i}", "arm", True)
                                             for i in temp.values()]
                                          + [op_set("live_set", "metronome", False), REC_ARMS],
                                          "stop_on_error": False})
            if ready["results"][-1].get("status") == "error":
                raise RuntimeError("Not recording: the tracks' arms could not be read back ("
                                   + ready["results"][-1].get("message", "") + ")")
            rows = rows_of(ready["results"][-1])
            now_armed, now_implicit = arming(rows)
            mine = set(temp.values())
            strays = [names.get(t) or r.get("name") or f"id {t}" for r in rows
                      for t in [r["$id"]] if t not in mine and (t in now_armed or t in now_implicit)]
            if strays:
                raise RuntimeError("Not recording: " + ", ".join(map(str, strays))
                                   + " stayed armed and would record too.")
            if not mine <= set(now_armed):
                raise RuntimeError("Render setup failed: Live did not arm the recording track")
            begin: list[dict] = []
            if session:
                if scene_index is not None:
                    begin.append(op_call(f"live_set scenes {scene_index}", "fire"))
                # Fixed-length Session recordings, starting on the next launch
                # quantization boundary together with the scene
                begin += [op_call(f"id {i} clip_slots {slot}", "fire", float(length_beats))
                          for i in temp.values()]
            else:
                # Where playback starts (Live 12.4.2, measured): continue_playing
                # resumes where Live last stopped, whatever current_song_time
                # was set to while stopped; start_playing starts at start_time,
                # which takes effect within the same batch.
                begin += [op_set("live_set", "loop", False),
                          op_set("live_set", "start_time", start),
                          op_set("live_set", "record_mode", True),
                          op_call("live_set", "start_playing")]
            try:
                started = await asend("batch", {"commands": begin})
                if started.get("failed"):
                    raise RuntimeError("Could not start recording: "
                                       + to_json(batch_summary(started)))
                # Session takes start on the next launch boundary: 8 bars at most
                bar = (float(song.get("signature_numerator") or 4) * 4.0
                       / float(song.get("signature_denominator") or 4))
                await take(session, temp, slot, start, length_beats, slowest,
                           len(sources), ctx, launch_beats=8 * bar)
            finally:
                # The transport stops and record mode goes off first; arms come
                # back in a later command (Crank: arms restored in the same
                # batch as the stop recorded on those tracks and split clips)
                stop = [op_set(f"id {i}", "arm", False) for i in temp.values()]
                if not session or not song["is_playing"]:
                    stop.insert(0, op_call("live_set", "stop_playing"))
                if not session:
                    stop.insert(1, op_set("live_set", "record_mode", song["record_mode"]))
                    stop += [op_set("live_set", "loop", song["loop"]),
                             op_set("live_set", "start_time", float(song.get("start_time", 0.0)))]
                if session and not song["is_playing"]:
                    # Live 12.4.16 left the launched clips playing on a stopped
                    # transport, overriding the Arrangement (Back to Arrangement lit)
                    stop.append(op_call("live_set", "stop_all_clips", False))
                    if isinstance(song.get("back_to_arranger"), bool):
                        stop.append(op_set("live_set", "back_to_arranger",
                                           song["back_to_arranger"]))
                if not session or not song["is_playing"]:
                    stop.append(op_set("live_set", "current_song_time",
                                       float(song["current_song_time"])))
                # A scene with a tempo of its own changes the set's
                stop.append({"type": "lom_get", "params": {"path": "live_set",
                                                           "properties": ["tempo"]}})
                stopped = await asend("batch", {"commands": stop, "stop_on_error": False})
                after = (stopped.get("results") or [{}])[-1].get("result") or {}
                if scene_index is not None and isinstance(after.get("tempo"), (int, float)) \
                        and abs(after["tempo"] - float(song["tempo"])) > 1e-6:
                    tempo_back = True
            recorded = await recorded_files(temp, slot if session else None, length_beats)
        finally:
            try:
                if not keep_tracks:
                    for track_id in temp.values():
                        await delete_track_by_id(track_id)
            finally:
                RECORDING.difference_update(temp.values())
                # Live selects each new track and a neighbour of a deleted one
                # kept temporary tracks too: a setup that stopped early left them armed
                back = [op_set(f"id {i}", "arm", False) for i in temp.values()
                        if keep_tracks] + disarm_ops(armed, implicit, on=True)
                back.append(op_set("live_set", "metronome", song["metronome"]))
                if tempo_back:
                    back.append(op_set("live_set", "tempo", float(song["tempo"])))
                if selected is not None:
                    back.append(op_set("live_set view", "selected_track", ref(f"id {selected}")))
                await asend("batch", {"commands": back, "stop_on_error": False})
        files = {label: path for label, (path, _s) in recorded.items()}
        played = {label: s for label, (_p, s) in recorded.items()}
        # Analyses read the section, at most ANALYSIS_MAX_S of it (as before)
        reads = {label: min(ANALYSIS_MAX_S, s) for label, s in played.items()}
        section: dict[str, Any] = {"mode": "session" if session else "arrangement",
                                   "length_beats": length_beats,
                                   "seconds": round(next(iter(played.values())), 2)}
        if session:
            section["scene"] = scene_index if scene_index is not None else "playing clips"
            section["played"] = (f"Session scene {scene_index + 1}" if scene_index is not None
                                 else "the Session clips playing")
        else:
            section["start_beat"] = start
            section["played"] = f"Arrangement, beats {start:g} to {start + length_beats:g}"
        if armed or implicit:
            section["disarmed_for_take"] = [names.get(t) for t in dict.fromkeys(armed + implicit)]
        if tempo_back:
            section["tempo_put_back"] = float(song["tempo"])
        return {"files": files, "section": section, "seconds": reads}

    async def post_mixer(track_ids: list[int]) -> None:
        """Each recording track takes its source after devices, fader and
        pan. Live 12.4.16 sets a track input's channel to "Post Mixer" with
        the type; in the batch that sets the types, the later tracks still
        listed Ext. In's channels (1/2, 1, 2), so setting the channel there
        failed (Live's log). Read it in a later command; set it where it
        is not."""
        if not track_ids:
            return
        read = await asend("batch", {"commands": [
            {"type": "lom_get", "params": {"path": f"id {t}",
                                           "properties": ["input_routing_channel"]}}
            for t in track_ids], "stop_on_error": False})
        wrong = [t for t, step in zip(track_ids, read.get("results", []))
                 if ((step.get("result") or {}).get("input_routing_channel") or {})
                 .get("display_name") != "Post Mixer"]
        if not wrong:
            return
        fixed = await asend("batch", {"commands": [
            op_set(f"id {t}", "input_routing_channel",
                   ref(f'id {t} available_input_routing_channels "Post Mixer"'))
            for t in wrong], "stop_on_error": False})
        failed = [s.get("message", "") for s in fixed.get("results", [])
                  if s.get("status") == "error"]
        if failed:
            raise RuntimeError("Render setup failed: could not take the source after its "
                               "mixer: " + "; ".join(failed))

    async def take(session: bool, temp: dict[str, int], slot: int, start: float,
                   length_beats: float, tempo: float, parts: int,
                   ctx: Context | None, launch_beats: float = 32.0) -> None:
        """Wait until the section has been recorded; raise if Live stops
        playing first or never gets there. tempo: the slowest the take can
        play at (the set's, or a fired scene's own)."""
        began = time.monotonic()
        seconds = length_beats * 60.0 / tempo
        # Session recordings wait for the next launch boundary (≤ 8 bars)
        deadline = (began + seconds + (launch_beats * 60.0 / tempo if session else 0)
                    + RECORD_GRACE_S)
        label = f"Recording {parts} part{'s' if parts > 1 else ''}"
        end = start + length_beats
        position, playing_seen = None, False
        while True:
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
                    return
            else:
                now = await asend("lom_get", {"path": "live_set", "properties": [
                    "current_song_time", "is_playing"]})
                position = float(now["current_song_time"])
                if position >= end:
                    return
                if now.get("is_playing") is False and (playing_seen or elapsed > 3.0):
                    raise RuntimeError(f"Playback stopped at beat {position:g}, before the "
                                       f"section's end ({end:g}); nothing was analysed.")
                playing_seen = playing_seen or now.get("is_playing") is True
            if time.monotonic() > deadline:
                where = f" (at beat {position:g} of {end:g})" if position is not None else ""
                raise RuntimeError(f"Live did not finish recording the section in time{where}"
                                   f" ({time.monotonic() - began:.0f} s, {seconds:.0f} s "
                                   "expected); nothing was analysed.")

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
        Takes as long as the section plays; only the temporary track records
        (other arms are off meanwhile), and the file must hold the section.
        Arms, selection, metronome, loop and record mode are restored; the
        temporary track is deleted unless keep_track (remove kept ones with
        manage_track(action="delete_recording_tracks")). The recording stays
        in the project's Samples/Recorded (path in file).
        """
        from .audio_analysis import analyze

        input_name = "Resampling" if source.lower() == "master" else source
        recorded = await record_section([(source, input_name)], start_beat, length_beats,
                                        keep_track, scene, ctx)
        file = recorded["files"][source]
        # The section only: Live records on until the stop reaches it
        analysis = await asyncio.to_thread(analyze, file, recorded["seconds"][source])
        render = dict(recorded["section"], source=source, track_kept=keep_track)
        return to_json({"render": render, **analysis})

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
        the recorded files stay in the project's Samples/Recorded and are
        listed. They are at the audio device's sample rate (often 48 kHz):
        resample before lining them up with 44.1 kHz material.
        """
        from .audio_analysis import load_audio
        from .mix_analysis import stem_summary

        sources = await stem_sources(tracks, include_master)
        recorded = await record_section(sources, start_beat, length_beats, scene=scene,
                                        ctx=ctx)

        def summarise() -> dict:
            return {label: dict(stem_summary(*load_audio(path, recorded["seconds"][label])),
                                file=path)
                    for label, path in recorded["files"].items()}

        stems = await asyncio.to_thread(summarise)
        result: dict[str, Any] = {"section": recorded["section"], "stems": stems}
        clipped = [label for label, s in stems.items() if s.get("clipped_samples")]
        if clipped:
            result["clipped"] = clipped
        return to_json(result)

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
        files, span = recorded["files"], recorded["seconds"]

        def diagnose() -> dict:
            master = analyze(files["Master"], span["Master"])
            audio = {label: load_audio(path, span[label]) for label, path in files.items()
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
            "files": files,
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
            section = dict(recorded["section"], file=mix_path)
            span = recorded["seconds"]["Master"]

        def compare() -> dict:
            mix = load_audio(mix_path, span) if section else load_audio(mix_path)
            return reference_report(mix, load_audio(reference),
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

    async def recorded_files(temp: dict[str, int], slot: int | None,
                             length_beats: float) -> dict[str, tuple[str, float]]:
        """{label: (path, seconds holding the section)} of the newest
        recording on each temporary track (its Arrangement, or Session clip
        slot `slot`), once Live has written it: known to Live, its size
        unchanged for SETTLE_S, and holding the section (check_recording)."""
        props = ["file_path"] + (["start_time", "end_time"] if slot is None else ["length"])
        found: dict[str, tuple[str, float | None]] = {}
        deadline = time.monotonic() + 15.0
        while len(found) < len(temp) and time.monotonic() < deadline:
            pending = [label for label in temp if label not in found]
            polled = await asend("batch", {"commands": [
                {"type": "lom_get", "params": {
                    "path": (f"id {temp[label]} arrangement_clips" if slot is None
                             else f"id {temp[label]} clip_slots {slot} clip"),
                    "properties": props}} for label in pending], "stop_on_error": False})
            for label, step in zip(pending, polled.get("results", [])):
                if step.get("status") != "success":
                    continue
                clip = (step["result"].get("items") or [{}])[-1] if slot is None \
                    else step["result"]
                path = clip.get("file_path")
                if path and Path(path).is_file():
                    span = (clip.get("end_time"), clip.get("start_time")) if slot is None \
                        else (clip.get("length"), 0.0)
                    beats = (float(span[0]) - float(span[1])
                             if all(isinstance(v, (int, float)) for v in span) else None)
                    found[label] = (path, beats)
            if len(found) < len(temp):
                await asyncio.sleep(0.2)
        if len(found) < len(temp):
            raise RuntimeError("Recording finished but Live made no audio file for "
                               + ", ".join(label for label in temp if label not in found)
                               + ". " + audio_device_note())
        # Settled: no file grew for SETTLE_S
        sizes = {label: -1 for label in found}
        steady_since, settle_by = time.monotonic(), time.monotonic() + 20.0
        while True:
            now = {label: Path(path).stat().st_size for label, (path, _b) in found.items()}
            if now != sizes:
                sizes, steady_since = now, time.monotonic()
            elif time.monotonic() - steady_since >= SETTLE_S:
                break
            if time.monotonic() > settle_by:
                raise RuntimeError("Live was still writing the recordings after 20 s: "
                                   + ", ".join(path for path, _b in found.values()))
            await asyncio.sleep(0.1)
        return {label: (path, await asyncio.to_thread(
                    check_recording, label, path, beats, length_beats))
                for label, (path, beats) in found.items()}

    async def delete_track_by_id(track_id: int) -> None:
        items = (await asend("lom_get", {"path": "live_set tracks",
                                         "properties": ["name"]}))["items"]
        index = next((i for i, t in enumerate(items) if t.get("$id") == track_id), None)
        if index is not None:
            await asend("lom_call", {"path": "live_set", "method": "delete_track",
                                     "args": [index]})

    # ── Extensions SDK companion (Live 12.4.5+ Suite, optional) ──────────────

    @tool(read_only=True)
    async def get_live_selection() -> str:
        """What the user shared from Live with "Share … with AbletonMCP" (the
        AbletonMCP Companion extension, Live 12.4.5+ Suite): the selected bars
        and tracks, clips or a track, for "this part" or "the selection".
        """
        from . import companion

        shared = await asyncio.to_thread(companion.request, "get_selection")
        if not shared:
            raise ValueError("Nothing shared yet: in Live, right-click the selection, a clip or "
                             "a track and choose \"Share … with AbletonMCP\".")
        if shared.get("kind") == "arrangement":
            try:
                bar = (await asyncio.to_thread(song_context, True))["beats_per_bar"]
                shared["start_bar"] = round(shared["start_beat"] / bar + 1, 2)
                shared["bars"] = round((shared["end_beat"] - shared["start_beat"]) / bar, 2)
            except Exception as e:  # the Remote Script is not connected: beats only
                logger.debug("No meter for the selection: %s", e)
        return to_json(shared)

    @tool(read_only=True)
    async def confirm_in_live(message: str, title: str = "AbletonMCP",
                              confirm_label: str = "OK", cancel_label: str = "Cancel") -> str:
        """Ask a yes/no question in a dialog inside Live (AbletonMCP Companion
        extension, Live 12.4.5+ Suite); {"confirmed": false} if closed. Waits up
        to 5 minutes.
        """
        from . import companion

        return to_json(await asyncio.to_thread(
            companion.request, "confirm",
            {"title": title, "message": message, "confirm_label": confirm_label,
             "cancel_label": cancel_label}, 300.0))

    @tool(read_only=True)
    async def render_track_audio(track: int | str, start_beat: float, end_beat: float,
                                 analyze: bool = True) -> str:
        """Render an audio track's clips between two beats to a WAV, before its
        devices and without playing (AbletonMCP Companion extension, Live
        12.4.5+ Suite); analyze: also measure it. With devices and mix:
        render_and_analyze.
        """
        from . import companion
        from .audio_analysis import analyze as measure

        result = await asyncio.to_thread(
            companion.request, "render_audio",
            {"track": track, "start_beat": start_beat, "end_beat": end_beat}, 600.0)
        if analyze and result.get("file"):
            result["analysis"] = await asyncio.to_thread(measure, result["file"])
        return to_json(result)

    # ── Max for Live analyzer ────────────────────────────────────────────────

    def analyzer_file() -> Path:
        from . import als, m4l_device
        libraries = als._user_libraries()
        if not libraries:
            raise RuntimeError("No User Library found to put the analyzer device in")
        return (libraries[0] / "Presets" / "Audio Effects" / "Max Audio Effect"
                / f"{m4l_device.NAME}.amxd")

    @tool(idempotent=True)
    def install_midi_tools() -> str:
        """Install the AbletonMCP MIDI Tools into Live 12's clip view (Max for
        Live: Suite, or Standard with the add-on): Usul and Makam Melody under
        Generate, Makam Snap under Transform. They run inside Live, without
        this server: pick one in a MIDI clip's tool tabs, set it, Apply.
        """
        from . import als, midi_tools

        libraries = als._user_libraries()
        if not libraries:
            raise RuntimeError("No User Library found to put the MIDI Tools in")
        paths = midi_tools.install(libraries[:1])
        return to_json({"installed": [str(p) for p in paths],
                        "use": "In a MIDI clip's view: Generate › Usul or Makam Melody, "
                               "Transform › Makam Snap (Live lists new files after a moment)"})

    @tool(idempotent=True)
    async def install_analyzer(track: int | str = "master") -> str:
        """Put the AbletonMCP Analyzer on a track (default the master): a Max
        for Live audio effect that passes audio through and sends this server
        octave-band levels, stereo correlation and width, levels and an
        approximate loudness ten times a second, without recording. Read them
        with read_analyzer. Needs Max for Live (Suite, or Standard with the
        add-on; the Trial has it). The device file goes into the User Library
        (Presets › Audio Effects › Max Audio Effect) the first time.
        """
        from . import analyzer, m4l_device

        target = analyzer_file()
        device = m4l_device.analyzer_device()
        if not target.is_file() or target.read_bytes() != device:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(device)
        listening = analyzer.listener()
        track = await asyncio.to_thread(pin_track, track)  # a load can rename it
        tp = track_path(track)

        async def on_track() -> list[dict]:
            items = (await asend("lom_get", {"path": f"{tp} devices",
                                             "properties": ["name"]}))["items"]
            return [d for d in items if d.get("name") == m4l_device.NAME]

        async def load() -> None:
            # Live notices a new file in the User Library after a moment
            for _attempt in range(30):
                try:
                    item_id, _label = await asyncio.to_thread(browser_item_id, m4l_device.NAME,
                                                              "user_library")
                    break
                except ValueError:
                    await asyncio.sleep(1.0)
            else:
                raise RuntimeError(f"Live's browser does not list {target.name} yet; open the "
                                   "User Library once in Live's browser, then try again.")
            result = await asyncio.to_thread(load_items, track, [(None, item_id)])
            if not result["ok"]:
                raise RuntimeError(f"Live could not load the analyzer: {result['steps']}")

        async def receiving_from(name: str) -> bool:
            for _attempt in range(40):  # Max starts a device in a second or two
                if time.monotonic() - listening.last_seen().get(name, -1e9) < 1.0:
                    return True
                await asyncio.sleep(0.25)
            return False

        replaced = False
        found = await on_track()
        if not found:
            await load()
            found = await on_track()
        name = (await asend("lom_get", {"path": tp, "properties": ["name"]}))["name"]
        receiving = await receiving_from(name)
        if not receiving and time.monotonic() - listening.outdated_at < 2.0:
            # A copy from an older build of the device: Live keeps the file it loaded
            devices = (await asend("lom_get", {"path": f"{tp} devices",
                                               "properties": ["name"]}))["items"]
            for index in reversed([i for i, d in enumerate(devices)
                                   if d.get("name") == m4l_device.NAME]):
                await asend("lom_call", {"path": tp, "method": "delete_device",
                                         "args": [index]})
            await load()
            found = await on_track()
            replaced = True
            receiving = await receiving_from(name)
        payload = {"device": str(target), "track": name, "on_track": len(found),
                   "receiving": receiving}
        if replaced:
            payload["replaced"] = "an older version of the device"
        if not receiving:
            payload["note"] = ("No data from the device yet. If it shows an error in Live, Max "
                               "for Live may be unavailable; otherwise call read_analyzer while "
                               "the set plays.")
        return to_json(payload)

    @tool(read_only=True)
    async def read_analyzer(seconds: float = 3.0, play: bool = False) -> str:
        """Listen to every AbletonMCP Analyzer (see install_analyzer) for a few
        seconds while the set plays, without recording: per track,
        octave-band levels (31 Hz–16 kHz), approximate loudness (LUFS, about
        ±1 LU; render_and_analyze measures exactly), peak and RMS, stereo
        correlation (1 mono, 0 wide, below 0 cancels in mono) and side/mid
        balance, with notes on what stands out. play: start playback for
        the reading if the set is stopped (and stop again).
        """
        from . import analyzer

        seconds = max(0.5, min(float(seconds), 30.0))
        listening = analyzer.listener()
        started_playing = False
        if play and not (await asend("lom_get", {"path": "live_set",
                                                 "properties": ["is_playing"]}))["is_playing"]:
            await asend("lom_call", {"path": "live_set", "method": "start_playing"})
            started_playing = True
            await asyncio.sleep(0.3)
        start = time.monotonic()
        try:
            await asyncio.sleep(seconds)
        finally:
            if started_playing:
                await asend("lom_call", {"path": "live_set", "method": "stop_playing"})
        frames = listening.since(start)
        if not frames:
            raise RuntimeError("No analyzer sent anything: put one on a track with "
                               "install_analyzer (needs Max for Live), and check that the "
                               "device is on.")
        tracks = {name: await asyncio.to_thread(analyzer.summary, device_frames)
                  for name, device_frames in frames.items()}
        return to_json({"seconds": seconds, "tracks": tracks})

    # ── Resources ────────────────────────────────────────────────────────────

    @mcp.resource("ableton://session/overview", mime_type="application/json")
    def session_overview_resource() -> str:
        """Compact live map of the current Ableton set."""
        return to_json(send("get_session_overview"))

    @mcp.resource("ableton://events", mime_type="application/json")
    def events_resource() -> str:
        """Buffered events from active Live watchers (see watch_live)."""
        return to_json(send("poll_events", {"since": 0}))
