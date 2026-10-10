# AbletonMCP/init.py
from __future__ import absolute_import, print_function, unicode_literals

from _Framework.ControlSurface import ControlSurface
import binascii
import collections
import errno
import hmac
import os
import socket
import json
import threading
import time
import traceback

# Constants for socket communication
DEFAULT_PORT = 9877
# Loopback, not 0.0.0.0. This socket takes unauthenticated commands that import
# arbitrary absolute paths, load browser items and drive the transport, so
# anything that can reach the port controls the DAW. Binding every interface
# exposed that to the whole local network — and to any network the machine is
# routable from.
#
# Running Live and the MCP server on different machines is the one legitimate
# reason to widen this; set ABLETON_MCP_HOST to do so deliberately, and put it
# behind a trusted network or an explicit relay.
HOST = os.environ.get("ABLETON_MCP_HOST", "127.0.0.1")

# Log every command to Live's Log.txt (off by default: it is one line per
# tool step).
VERBOSE = os.environ.get("ABLETON_MCP_VERBOSE", "").lower() in ("1", "true", "yes")

# A command that never completes must not grow the buffer without bound.
# Generous enough for a large add_notes_to_clip payload.
MAX_REQUEST_BYTES = 16 * 1024 * 1024

# Bumped whenever the TCP command surface changes; the MCP server compares
# this to EXPECTED_REMOTE_SCRIPT_VERSION.
SCRIPT_VERSION = "2.0.0"
# 2: newline-terminated JSON, request ids echoed, hello with a session token.
# Requests without a newline (protocol 1 clients) are still understood.
PROTOCOL_VERSION = 2

# Where the script publishes its port and a token made fresh at every launch.
# A client must send {"type": "hello", "params": {"token": ...}} before any
# other command; only this user can read the file. Set ABLETON_MCP_AUTH=0 in
# Live's environment to accept clients that cannot send hello.
SESSION_FILE = os.environ.get("ABLETON_MCP_SESSION_FILE") or os.path.join(
    os.path.expanduser("~"), ".ableton-mcp", "live-session.json")
AUTH_REQUIRED = os.environ.get("ABLETON_MCP_AUTH", "1").lower() not in ("0", "false", "no")

# Sockets are polled on Live's main thread: by a Live.Base.Timer every
# TIMER_INTERVAL_MS, and on every update_display (~100 ms) as a fallback.
# A command is read, run and answered in the same callback, instead of
# waiting for a socket thread that Live only lets run on its 100 ms tick.
TIMER_INTERVAL_MS = 10
# Stop starting new commands this long into one poll, so Live's UI keeps up
# with a client that floods the socket.
POLL_BUDGET_SECONDS = 0.05
MAX_CLIENTS = 8
# A client that stops reading its responses is dropped past this backlog.
MAX_RESPONSE_BACKLOG = 64 * 1024 * 1024
# Longest a poll_events request may wait for an event
MAX_EVENT_WAIT_SECONDS = 30.0

SCRIPT_CAPABILITIES = [
    "hello",
    "get_session_info",
    "get_track_info",
    "get_script_info",
    "get_clip_notes",
    "get_device_parameters",
    "get_session_snapshot",
    "set_device_parameter",
    "drain_passive_events",
    "create_midi_track",
    "create_audio_track",
    "create_clip",
    "create_audio_clip",
    "add_notes_to_clip",
    "load_instrument_or_effect",
    "get_arrangement_clips",
    "duplicate_session_clip_to_arrangement",
    "create_locator",
    "delete_clip",
    "clear_notes_from_clip",
    "lom_get",
    "lom_set",
    "lom_call",
    "batch",
    "observe",
    "unobserve",
    "list_observers",
    "poll_events",
    "clip_get_notes",
    "clip_write_notes",
    "clip_write_envelope",
    "param_set_display",
    "get_session_overview",
    "browser_search",
]

# ── Generic Live Object Model bridge ──────────────────────────────────────────
#
# Instead of one hand-written command per feature, these commands address any
# object in the Live Object Model by path, the same way Max for Live does:
#
#   "live_set tracks 2 devices 0 parameters 3"
#   "live_set tracks \"Lead Synth\" mixer_device volume"   (select by name)
#   "live_set tracks -1"                                   (negative index)
#   "id 140234"                                            (an id returned earlier)
#
# Paths may also be JSON lists: ["live_set", "tracks", 2]. Values that must be
# Live objects (a routing type, a device parameter passed to a method) are
# written as {"$ref": "<path>"} or {"$id": <id>}.

try:
    string_types = basestring  # noqa: F821  (Python 2)
except NameError:
    string_types = str

LOM_MAX_LIST_ITEMS = 512
LOM_REGISTRY_MAX = 4096
LOM_EVENT_BUFFER_MAX = 2000
# Generous enough for any real clip; Live clamps note windows itself.
NOTE_TIME_SPAN = 1000000.0


class LomError(Exception):
    pass


# Errors that mean "the request was wrong", not "the script is broken"
EXPECTED_ERRORS = (LomError, ValueError, IndexError)


def lom_tokenize(path):
    if isinstance(path, (list, tuple)):
        return list(path)
    if path is None:
        return []
    import shlex
    return shlex.split(str(path))


def lom_is_int_token(token):
    if isinstance(token, bool):
        return False
    if isinstance(token, int):
        return True
    try:
        int(str(token))
        return True
    except ValueError:
        return False


def lom_is_live_object(value):
    if hasattr(value, "_live_ptr"):
        return True
    module = getattr(type(value), "__module__", "") or ""
    return module == "Live" or module.startswith("Live.")


def lom_is_sequence(value):
    # Live returns some lists as its own vector types (module "Live.*", no
    # _live_ptr), so only objects with a pointer are ruled out here.
    if isinstance(value, (list, tuple)):
        return True
    if isinstance(value, (string_types, bytes, dict)) or hasattr(value, "_live_ptr"):
        return False
    return hasattr(value, "__len__") and hasattr(value, "__getitem__")


def lom_is_value_object(value):
    """Objects Live defines outside the Live module (BrowserItem, RoutingType,
    TuningSystem): no _live_ptr, but still worth describing and referencing."""
    if value is None or isinstance(value, (bool, int, float, string_types, bytes, list,
                                           tuple, dict)):
        return False
    return getattr(type(value), "__module__", "builtins") not in ("builtins", "__builtin__")


def lom_check_name(name):
    """Attribute names reachable through the bridge: public LOM names only.

    Blocks dunder/private access so a path can never climb out of the Live API
    into Python internals (__class__, __globals__, ...).
    """
    if not isinstance(name, string_types) or not name:
        raise LomError("Expected a property name, got %r" % (name,))
    if name.startswith("_"):
        raise LomError("Private attribute '%s' is not accessible" % name)
    return str(name)


def parse_display_number(text):
    """Number from a Live display string, normalised to the base unit.

    "800 Hz" → 800, "1.20 kHz" → 1200, "-6.0 dB" → -6, "-inf dB" → -inf,
    "350 ms" → 350, "1.50 s" → 1500, "50L" → -50, "C" → 0, "25R" → 25.
    Returns None when the text holds no number.
    """
    import re
    if text is None:
        return None
    s = str(text).strip()
    low = s.lower()
    if "inf" in low:
        return float("-inf") if "-" in low else float("inf")
    if low == "c":
        return 0.0
    pan = re.match(r"^(\d+(?:\.\d+)?)\s*([lr])$", low)
    if pan:
        value = float(pan.group(1))
        return -value if pan.group(2) == "l" else value
    match = re.search(r"-?(?:\d+(?:\.\d+)?|\.\d+)", s)
    if not match:
        return None
    value = float(match.group(0))
    unit = low[match.end():].strip()
    if unit.startswith("k"):
        value *= 1000.0
    elif unit == "s" or unit.startswith("s "):
        value *= 1000.0
    return value


class LomMixin(object):
    """Generic LOM access, batching, observers and note/envelope editing."""

    def _lom_init(self):
        self._lom_registry = collections.OrderedDict()
        self._observers = {}
        self._observer_seq = 0
        self._events = []
        self._event_seq = 0
        self._events_lock = threading.Lock()
        self._browser_walks = collections.OrderedDict()
        self._browser_walk_seq = 0

    def _lom_command_handlers(self):
        return {
            "lom_get": self._cmd_lom_get,
            "lom_set": self._cmd_lom_set,
            "lom_call": self._cmd_lom_call,
            "batch": self._cmd_batch,
            "observe": self._cmd_observe,
            "unobserve": self._cmd_unobserve,
            "list_observers": self._cmd_list_observers,
            "poll_events": self._cmd_poll_events,
            "clip_get_notes": self._cmd_clip_get_notes,
            "clip_write_notes": self._cmd_clip_write_notes,
            "clip_write_envelope": self._cmd_clip_write_envelope,
            "param_set_display": self._cmd_param_set_display,
            "get_session_overview": self._cmd_get_session_overview,
            "browser_search": self._cmd_browser_search,
        }

    # ── Path resolution ───────────────────────────────────────────────────────

    def _lom_root(self, name):
        if name in ("live_set", "song"):
            return self._song
        if name in ("live_app", "app"):
            return self.application()
        if name == "browser":
            return self.application().browser
        raise LomError(
            "Unknown root '%s' (use live_set, live_app, browser or 'id N')" % name)

    def _lom_resolve(self, path):
        tokens = lom_tokenize(path)
        if not tokens:
            raise LomError("Empty path")
        head = str(tokens.pop(0))
        if head == "id":
            if not tokens:
                raise LomError("'id' must be followed by a number")
            obj = self._lom_lookup_id(tokens.pop(0))
        else:
            obj = self._lom_root(head)

        walked = [head]
        while tokens:
            token = tokens.pop(0)
            if lom_is_sequence(obj):
                obj = self._lom_select(obj, token, " ".join(walked))
            else:
                name = lom_check_name(token)
                if not hasattr(obj, name):
                    raise LomError("'%s' has no property '%s'" % (" ".join(walked), name))
                obj = getattr(obj, name)
                if callable(obj) and not lom_is_live_object(obj):
                    raise LomError("'%s' is a method — use lom_call" % name)
            walked.append(str(token))
        return obj

    def _lom_select(self, seq, token, where):
        if lom_is_int_token(token):
            index = int(token)
            size = len(seq)
            if index < -size or index >= size:
                raise LomError("Index %d out of range for '%s' (length %d)"
                               % (index, where, size))
            return seq[index]
        wanted = str(token)
        for exact in (True, False):
            for item in seq:
                for attr in ("name", "display_name"):
                    label = getattr(item, attr, None)
                    if not isinstance(label, string_types):
                        continue
                    if (label == wanted) if exact else (label.lower() == wanted.lower()):
                        return item
        names = []
        for item in seq:
            label = getattr(item, "name", None) or getattr(item, "display_name", None)
            if label:
                names.append(label)
        raise LomError("No item named '%s' in '%s'. Available: %s"
                       % (wanted, where, ", ".join(names[:40])))

    def _lom_lookup_id(self, token):
        try:
            key = int(token)
        except (TypeError, ValueError):
            raise LomError("Invalid id %r" % (token,))
        if key not in self._lom_registry:
            raise LomError("Unknown id %d — read the object again to get a fresh id" % key)
        return self._lom_registry[key]

    def _lom_register(self, obj):
        key = getattr(obj, "_live_ptr", None)
        if not isinstance(key, int) or isinstance(key, bool):
            key = id(obj)
        registry = self._lom_registry
        if key in registry:
            registry.pop(key)
        registry[key] = obj
        while len(registry) > LOM_REGISTRY_MAX:
            registry.popitem(last=False)
        return key

    def _lom_resolve_value(self, value):
        """Turn {"$ref": path} / {"$id": n} (recursively) into Live objects."""
        if isinstance(value, dict):
            if "$ref" in value:
                return self._lom_resolve(value["$ref"])
            if "$id" in value:
                return self._lom_lookup_id(value["$id"])
            if "$enum" in value:
                return self._lom_enum(value["$enum"], value.get("value"))
            return dict((k, self._lom_resolve_value(v)) for k, v in value.items())
        if isinstance(value, list):
            return [self._lom_resolve_value(v) for v in value]
        return value

    @staticmethod
    def _enum_member(enum_type, value):
        """Member of a Boost.Python enum type by int value or name."""
        if isinstance(value, string_types):
            members = getattr(enum_type, "names", {}) or {}
            if value not in members:
                raise LomError("'%s' is not one of %s" % (value, ", ".join(sorted(members))))
            return members[value]
        members = getattr(enum_type, "values", {}) or {}
        if int(value) not in members:
            raise LomError("%r is not a valid %s value" % (value, enum_type.__name__))
        return members[int(value)]

    def _lom_enum(self, dotted, value):
        """{"$enum": "Live.Song.RecordingQuantization", "value": 5 | "rec_q_sixtenth"}"""
        parts = str(dotted).split(".")
        if parts[0] != "Live" or any(p.startswith("_") for p in parts):
            raise LomError("$enum must name a type inside the Live module")
        import Live
        enum_type = Live
        for part in parts[1:]:
            enum_type = getattr(enum_type, part)
        return self._enum_member(enum_type, value)

    # ── Serialisation ─────────────────────────────────────────────────────────

    def _lom_ref(self, obj):
        ref = {"$type": type(obj).__name__, "$id": self._lom_register(obj)}
        for attr in ("name", "display_name"):
            try:
                label = getattr(obj, attr)
            except Exception:
                continue
            if isinstance(label, string_types):
                ref[attr] = label
        return ref

    def _lom_value(self, value, depth=0):
        if value is None or isinstance(value, (bool, float)):
            return value
        if isinstance(value, int):
            if type(value) is int:
                return value
            # Boost.Python enums subclass int
            return {"$enum": str(value).split(".")[-1], "value": int(value)}
        if isinstance(value, string_types):
            return value
        if lom_is_live_object(value) and not lom_is_sequence(value):
            return self._lom_ref(value)
        if lom_is_sequence(value):
            if depth > 1:
                return {"$len": len(value)}
            items = []
            for index, item in enumerate(value):
                if index >= LOM_MAX_LIST_ITEMS:
                    items.append({"$truncated": len(value) - LOM_MAX_LIST_ITEMS})
                    break
                items.append(self._lom_value(item, depth + 1))
            return items
        # Value objects Live defines outside the "Live" module (e.g.
        # Track.RoutingType) are still needed as references for lom_set.
        if getattr(type(value), "__module__", "builtins") not in ("builtins", "__builtin__"):
            return self._lom_ref(value)
        return str(value)

    def _lom_describe(self, obj):
        info = self._lom_ref(obj) if (lom_is_live_object(obj) or lom_is_value_object(obj)) \
            else {"$type": type(obj).__name__}
        properties, children, methods = {}, {}, []
        for name in sorted(dir(obj)):
            if name.startswith("_") or "listener" in name:
                continue
            try:
                value = getattr(obj, name)
            except Exception as e:
                properties[name] = {"$error": str(e)[:160]}
                continue
            if callable(value) and not lom_is_live_object(value):
                methods.append(name)
            elif lom_is_sequence(value):
                children[name] = {"$len": len(value)}
            elif lom_is_live_object(value):
                children[name] = self._lom_ref(value)
            else:
                properties[name] = self._lom_value(value)
        info["properties"] = properties
        info["children"] = children
        info["methods"] = methods
        return info

    def _lom_pick(self, obj, properties, register=True):
        picked = {}
        for prop in properties:
            if prop == "$display":
                # A parameter's value as Live shows it ("-6.0 dB", "800 Hz")
                try:
                    picked[prop] = obj.str_for_value(obj.value)
                except Exception as e:
                    picked[prop] = {"$error": str(e)[:160]}
                continue
            name = lom_check_name(prop)
            try:
                picked[name] = self._lom_value(getattr(obj, name))
            except Exception as e:
                picked[name] = {"$error": str(e)[:160]}
        if register and lom_is_live_object(obj):
            picked["$id"] = self._lom_register(obj)
        return picked

    # ── lom_get / lom_set / lom_call ──────────────────────────────────────────

    def _cmd_lom_get(self, params):
        target = self._lom_resolve(params.get("path"))
        properties = params.get("properties")
        # "ids": false leaves "$id" out, so a bulk read (every parameter of a
        # set) does not push other objects out of the id registry
        register = params.get("ids", True) is not False
        if lom_is_sequence(target):
            if properties:
                return {"items": [self._lom_pick(item, properties, register) for item in
                                  list(target)[:LOM_MAX_LIST_ITEMS]],
                        "length": len(target)}
            return {"items": self._lom_value(target), "length": len(target)}
        if properties:
            return self._lom_pick(target, properties, register)
        if lom_is_live_object(target) or lom_is_value_object(target):
            return self._lom_describe(target)
        return {"value": self._lom_value(target)}

    def _cmd_lom_set(self, params):
        target = self._lom_resolve(params.get("path"))
        name = lom_check_name(params.get("property"))
        if not hasattr(target, name):
            raise LomError("Object has no property '%s'" % name)
        old = getattr(target, name)
        value = self._lom_resolve_value(params.get("value"))
        is_enum = isinstance(old, int) and type(old) is not int and not isinstance(old, bool)
        if is_enum and isinstance(value, (string_types, int)) and not isinstance(value, bool) \
                and type(value) in (int, str):
            value = self._enum_member(type(old), value)
        elif isinstance(old, float) and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        setattr(target, name, value)
        return {"property": name, "old_value": self._lom_value(old),
                "value": self._lom_value(getattr(target, name))}

    def _cmd_lom_call(self, params):
        target = self._lom_resolve(params.get("path"))
        name = lom_check_name(params.get("method"))
        method = getattr(target, name, None)
        if method is None or not callable(method):
            raise LomError("Object has no method '%s'" % name)
        args = self._lom_resolve_value(params.get("args") or [])
        kwargs = self._lom_resolve_value(params.get("kwargs") or {})
        result = method(*args, **kwargs)
        return {"method": name, "result": self._lom_value(result)}

    # ── Batch ─────────────────────────────────────────────────────────────────

    def _cmd_batch(self, params):
        commands = params.get("commands") or []
        stop_on_error = params.get("stop_on_error", True)
        handlers = self._command_handlers()
        # The whole batch runs inside one main-thread callback and the
        # dispatcher closes the undo step afterwards. Live 12.3 still files
        # parameter changes (volume, tempo, mute) as a separate step from
        # structural ones (names, colors, new tracks/clips), so a mixed batch
        # takes two Cmd+Z; begin_undo_step/end_undo_step does not merge them.
        results = []
        for index, command in enumerate(commands):
            command_type = command.get("type", "")
            handler = handlers.get(command_type)
            if handler is None or command_type in ("batch",) \
                    or command_type in self.UNBATCHED_COMMANDS:
                outcome = {"index": index, "type": command_type, "status": "error",
                           "message": "Command not allowed in a batch: " + command_type}
            else:
                try:
                    outcome = {"index": index, "type": command_type, "status": "success",
                               "result": handler(command.get("params") or {})}
                except Exception as e:
                    self.log_message("Batch step %d (%s) failed: %s"
                                     % (index, command_type, e))
                    outcome = {"index": index, "type": command_type,
                               "status": "error", "message": str(e)}
            results.append(outcome)
            if outcome["status"] == "error" and stop_on_error:
                break
        failed = [r for r in results if r["status"] == "error"]
        return {"results": results, "completed": len(results) - len(failed),
                "failed": len(failed), "total": len(commands)}

    # ── Observers ─────────────────────────────────────────────────────────────

    def _push_event(self, event):
        with self._events_lock:
            self._event_seq += 1
            event["seq"] = self._event_seq
            event["ts"] = time.time()
            self._events.append(event)
            if len(self._events) > LOM_EVENT_BUFFER_MAX:
                self._events = self._events[-LOM_EVENT_BUFFER_MAX:]

    def _cmd_observe(self, params):
        path = params.get("path")
        name = lom_check_name(params.get("property"))
        target = self._lom_resolve(path)
        add = getattr(target, "add_%s_listener" % name, None)
        if add is None:
            raise LomError("Property '%s' is not observable on '%s'" % (name, path))
        for oid, (obj, prop, _cb, opath) in self._observers.items():
            if opath == path and prop == name:
                return {"observer_id": oid, "path": path, "property": name,
                        "already_observed": True}
        self._observer_seq += 1
        oid = self._observer_seq

        def callback(*_args):
            try:
                value = self._lom_value(getattr(target, name))
            except Exception as e:
                value = {"$error": str(e)[:160]}
            self._push_event({"observer_id": oid, "path": path,
                              "property": name, "value": value})

        add(callback)
        self._observers[oid] = (target, name, callback, path)
        return {"observer_id": oid, "path": path, "property": name,
                "value": self._lom_value(getattr(target, name))}

    def _remove_observer(self, oid):
        target, name, callback, _path = self._observers.pop(oid)
        try:
            remove = getattr(target, "remove_%s_listener" % name)
            has = getattr(target, "%s_has_listener" % name, None)
            if has is None or has(callback):
                remove(callback)
        except Exception as e:
            self.log_message("Observer %d removal failed: %s" % (oid, e))

    def _cmd_unobserve(self, params):
        oid = params.get("observer_id")
        if oid in (None, "all"):
            removed = list(self._observers.keys())
            for key in removed:
                self._remove_observer(key)
            return {"removed": removed}
        oid = int(oid)
        if oid not in self._observers:
            raise LomError("Unknown observer %d" % oid)
        self._remove_observer(oid)
        return {"removed": [oid]}

    def _cmd_list_observers(self, params):
        return {"observers": [{"observer_id": oid, "path": entry[3], "property": entry[1]}
                              for oid, entry in sorted(self._observers.items())]}

    def _cmd_poll_events(self, params):
        since = int(params.get("since", 0) or 0)
        limit = int(params.get("limit", 200) or 200)
        with self._events_lock:
            pending = [e for e in self._events if e["seq"] > since]
            oldest = self._events[0]["seq"] if self._events else self._event_seq + 1
            latest = self._event_seq
        return {"events": pending[:limit], "latest_seq": latest,
                "more": len(pending) > limit,
                "missed": since + 1 < oldest and since < latest}

    def _teardown_observers(self):
        for oid in list(getattr(self, "_observers", {}).keys()):
            self._remove_observer(oid)

    # ── Notes (Live 11+ extended note API, with a Live 10 fallback) ──────────

    def _resolve_clip(self, params):
        if params.get("clip_path"):
            clip = self._lom_resolve(params["clip_path"])
        else:
            track = self._lom_resolve(["live_set", "tracks", int(params.get("track_index", 0))])
            slot = self._lom_select(track.clip_slots, int(params.get("clip_index", 0)),
                                    "clip_slots")
            if not slot.has_clip:
                raise LomError("No clip in that slot")
            clip = slot.clip
        if not getattr(clip, "is_midi_clip", False):
            raise LomError("Target is not a MIDI clip")
        return clip

    def _note_to_dict(self, note):
        entry = {}
        for attr in ("note_id", "pitch", "start_time", "duration", "velocity", "mute",
                     "probability", "velocity_deviation", "release_velocity"):
            if hasattr(note, attr):
                value = getattr(note, attr)
                entry[attr] = bool(value) if attr == "mute" else (
                    int(value) if attr in ("note_id", "pitch") else float(value))
        return entry

    def _cmd_clip_get_notes(self, params):
        clip = self._resolve_clip(params)
        from_time = float(params.get("from_time", 0.0))
        time_span = float(params.get("time_span", NOTE_TIME_SPAN))
        from_pitch = int(params.get("from_pitch", 0))
        pitch_span = int(params.get("pitch_span", 128))
        if hasattr(clip, "get_notes_extended"):
            raw = clip.get_notes_extended(from_pitch, pitch_span, from_time, time_span)
            notes = [self._note_to_dict(n) for n in raw]
        else:
            raw = clip.get_notes(from_time, from_pitch, time_span, pitch_span)
            notes = [{"pitch": int(n[0]), "start_time": float(n[1]),
                      "duration": float(n[2]), "velocity": float(n[3]),
                      "mute": bool(n[4])} for n in raw]
        notes.sort(key=lambda n: (n["start_time"], n["pitch"]))
        return {"clip": clip.name, "length": float(clip.length),
                "loop_start": float(clip.loop_start), "loop_end": float(clip.loop_end),
                "notes": notes, "note_count": len(notes)}

    @staticmethod
    def _clean_note(note):
        pitch = int(note["pitch"])
        if pitch < 0 or pitch > 127:
            raise LomError("Pitch %d outside 0..127" % pitch)
        duration = float(note.get("duration", 0.25))
        if duration <= 0:
            raise LomError("Note duration must be positive")
        cleaned = {
            "pitch": pitch,
            "start_time": max(0.0, float(note.get("start_time", 0.0))),
            "duration": duration,
            "velocity": min(127.0, max(1.0, float(note.get("velocity", 100)))),
            "mute": bool(note.get("mute", False)),
        }
        if "probability" in note:
            cleaned["probability"] = min(1.0, max(0.0, float(note["probability"])))
        if "velocity_deviation" in note:
            cleaned["velocity_deviation"] = min(127.0, max(-127.0,
                                                          float(note["velocity_deviation"])))
        if "release_velocity" in note:
            cleaned["release_velocity"] = min(127.0, max(0.0, float(note["release_velocity"])))
        return cleaned

    def _add_notes(self, clip, notes):
        cleaned = [self._clean_note(n) for n in notes]
        spec_class = None
        try:
            import Live
            spec_class = getattr(Live.Clip, "MidiNoteSpecification", None)
        except ImportError:
            pass
        if spec_class is not None and hasattr(clip, "add_new_notes"):
            ids = clip.add_new_notes(tuple(spec_class(**n) for n in cleaned))
            return [int(i) for i in ids] if ids is not None else []
        clip.set_notes(tuple((n["pitch"], n["start_time"], n["duration"],
                              n["velocity"], n["mute"]) for n in cleaned))
        return []

    def _remove_notes(self, clip, from_time, time_span, from_pitch=0, pitch_span=128):
        if hasattr(clip, "remove_notes_extended"):
            clip.remove_notes_extended(from_pitch, pitch_span, from_time, time_span)
        else:
            clip.remove_notes(from_time, from_pitch, time_span, pitch_span)

    def _cmd_clip_write_notes(self, params):
        """mode: add | replace | modify | remove_ids."""
        clip = self._resolve_clip(params)
        mode = params.get("mode", "add")
        notes = params.get("notes") or []
        if mode == "replace":
            self._remove_notes(clip, float(params.get("from_time", 0.0)),
                               float(params.get("time_span", NOTE_TIME_SPAN)),
                               int(params.get("from_pitch", 0)),
                               int(params.get("pitch_span", 128)))
            ids = self._add_notes(clip, notes)
            return {"mode": mode, "written": len(notes), "note_ids": ids}
        if mode == "add":
            ids = self._add_notes(clip, notes)
            return {"mode": mode, "written": len(notes), "note_ids": ids}
        if mode == "modify":
            if not hasattr(clip, "apply_note_modifications"):
                raise LomError("Note modification needs Live 11 or later")
            updates = dict((int(n["note_id"]), n) for n in notes)
            vector = clip.get_notes_extended(0, 128, 0.0, NOTE_TIME_SPAN)
            touched = 0
            for live_note in vector:
                change = updates.get(int(live_note.note_id))
                if change is None:
                    continue
                merged = self._clean_note(dict(self._note_to_dict(live_note), **change))
                for attr, value in merged.items():
                    if hasattr(live_note, attr):
                        setattr(live_note, attr, value)
                touched += 1
            clip.apply_note_modifications(vector)
            return {"mode": mode, "modified": touched,
                    "missing_ids": sorted(set(updates) - set(
                        int(n.note_id) for n in vector))}
        if mode == "remove_ids":
            if not hasattr(clip, "remove_notes_by_id"):
                raise LomError("Removing notes by id needs Live 11 or later")
            ids = [int(i) for i in params.get("note_ids") or []]
            clip.remove_notes_by_id(ids)
            return {"mode": mode, "removed": len(ids)}
        raise LomError("Unknown mode '%s' (add, replace, modify, remove_ids)" % mode)

    # ── Clip automation envelopes ────────────────────────────────────────────

    def _cmd_clip_write_envelope(self, params):
        clip = self._lom_resolve(params.get("clip_path"))
        param = self._lom_resolve(params.get("parameter_path"))
        if not hasattr(clip, "automation_envelope"):
            raise LomError("Target is not a clip")
        if params.get("clear", True) and hasattr(clip, "clear_envelope"):
            clip.clear_envelope(param)
        points = params.get("points") or []
        if not points:
            return {"cleared": True, "written": 0}
        envelope = clip.automation_envelope(param)
        if envelope is None:
            envelope = clip.create_automation_envelope(param)
        if envelope is None:
            raise LomError("Live refused to create an envelope for this parameter "
                           "(it may not be automatable from a clip)")
        low, high = float(param.min), float(param.max)
        for point in points:
            value = min(high, max(low, float(point["value"])))
            envelope.insert_step(float(point["time"]), float(point.get("duration", 0.0)), value)
        return {"written": len(points), "parameter": param.name,
                "min": low, "max": high}

    # ── Parameters by display value ("800 Hz", "-6 dB", "50L") ───────────────

    def _cmd_param_set_display(self, params):
        param = self._lom_resolve(params.get("path"))
        if not hasattr(param, "str_for_value"):
            raise LomError("Target is not a device parameter")
        target = params.get("value")
        old_display = param.str_for_value(param.value)

        if getattr(param, "is_quantized", False):
            items = [str(i) for i in getattr(param, "value_items", [])]
            wanted = str(target).strip().lower()
            for index, item in enumerate(items):
                if item.lower() == wanted:
                    param.value = float(param.min) + index
                    return {"name": param.name, "old": old_display,
                            "new": param.str_for_value(param.value)}
            if items:
                raise LomError("'%s' is not one of: %s" % (target, ", ".join(items)))

        goal = parse_display_number(target) if isinstance(target, string_types) \
            else float(target)
        if goal is None:
            raise LomError("Could not read a number from %r" % (target,))
        low, high = float(param.min), float(param.max)
        read = lambda v: parse_display_number(param.str_for_value(v))
        at_low, at_high = read(low), read(high)
        if at_low is None or at_high is None:
            raise LomError("Parameter display '%s' is not numeric" % old_display)
        increasing = at_high >= at_low
        if (goal <= min(at_low, at_high)):
            best = low if increasing else high
        elif goal >= max(at_low, at_high):
            best = high if increasing else low
        else:
            lo, hi = low, high
            for _ in range(48):
                mid = (lo + hi) / 2.0
                shown = read(mid)
                if shown is None:
                    break
                if (shown < goal) == increasing:
                    lo = mid
                else:
                    hi = mid
            # Displays are rounded, so the crossing point can show one step
            # off ("-6.1 dB" for -6). Keep whichever side reads closest.
            candidates = []
            for value in (hi, lo, (lo + hi) / 2.0):
                shown = read(value)
                if shown is not None:
                    candidates.append((abs(shown - goal), value))
            best = min(candidates, key=lambda c: c[0])[1] if candidates else (lo + hi) / 2.0
        param.value = min(high, max(low, best))
        return {"name": param.name, "old": old_display,
                "new": param.str_for_value(param.value), "raw_value": float(param.value)}

    # ── Compact session overview ──────────────────────────────────────────────

    # ── Browser search ───────────────────────────────────────────────────────

    BROWSER_ROOTS = ("sounds", "drums", "instruments", "audio_effects", "midi_effects",
                     "max_for_live", "plugins", "clips", "samples", "packs",
                     "user_library", "current_project")
    SAMPLE_EXTENSIONS = ("wav", "aif", "aiff", "flac", "mp3", "ogg", "m4a")
    PRESET_EXTENSIONS = ("adg", "adv", "amxd", "agr", "alc", "als")

    def _cmd_browser_search(self, params):
        """Find browser items whose name, or the folders above it, contain every
        word of the query. Walks in slices (slice_ms) so Live's UI keeps up;
        while "done" is false, call again with the returned walk_id."""
        walks = self._browser_walks
        if params.get("walk_id") is not None:
            walk = walks.get(int(params["walk_id"]))
            if walk is None:
                raise LomError("Unknown or finished browser walk %s" % params["walk_id"])
            if params.get("cancel"):
                walks.pop(walk["id"], None)
                return {"walk_id": walk["id"], "results": [], "visited": walk["visited"],
                        "done": True}
        else:
            words = [w for w in str(params.get("query") or "").lower().split() if w]
            # Folder names to step into first, e.g. ["Drum Hits", "Kick"]
            within = [str(w).lower() for w in params.get("within") or []]
            if not words and not within:
                raise LomError("browser_search needs a query")
            browser = self.application().browser
            walk = {"words": words, "queue": collections.deque(), "current": None,
                    "visited": 0,
                    "max_depth": min(int(params.get("max_depth") or 6), 20),
                    "loadable_only": params.get("loadable_only", True) is not False}
            roots = params.get("roots") or ("sounds", "drums", "instruments", "audio_effects",
                                            "midi_effects", "max_for_live", "plugins",
                                            "samples")
            for root in roots:
                if root not in self.BROWSER_ROOTS:
                    raise LomError("Unknown browser root '%s' (use %s)"
                                   % (root, ", ".join(self.BROWSER_ROOTS)))
                item = getattr(browser, root)
                trail = [item.name]
                for folder in within:
                    item = next((c for c in item.children if c.name.lower() == folder), None)
                    if item is None:
                        break
                    trail.append(item.name)
                if item is not None:
                    walk["queue"].append((item, trail, 0))
            if not walk["queue"]:
                raise LomError("No folder '%s' in %s" % ("/".join(within), ", ".join(roots)))
            self._browser_walk_seq += 1
            walk["id"] = self._browser_walk_seq
            walks[walk["id"]] = walk
            while len(walks) > 4:
                walks.popitem(last=False)
        deadline = time.time() + min(float(params.get("slice_ms") or 100), 2000) / 1000.0
        limit = min(int(params.get("max_results") or 100), 500)
        results = []
        visited_before = walk["visited"]
        # Always make some progress, however short the slice
        while len(results) < limit and (time.time() < deadline
                                        or walk["visited"] == visited_before):
            if walk["current"] is None:
                if not walk["queue"]:
                    break
                folder, trail, depth = walk["queue"].popleft()
                try:
                    children = list(folder.children)
                except Exception:
                    continue
                walk["current"] = [children, 0, trail, depth]
            children, position, trail, depth = walk["current"]
            while (position < len(children) and len(results) < limit
                   and (time.time() < deadline or walk["visited"] == visited_before)):
                child = children[position]
                position += 1
                walk["visited"] += 1
                try:
                    name = child.name
                    is_folder = child.is_folder
                    loadable = child.is_loadable
                except Exception:
                    continue
                if is_folder and depth + 1 < walk["max_depth"]:
                    walk["queue"].append((child, trail + [name], depth + 1))
                if walk["loadable_only"] and not loadable:
                    continue
                text = (" ".join(trail[1:]) + " " + name).lower()
                if all(word in text for word in walk["words"]):
                    results.append(self._browser_entry(child, trail))
            walk["current"] = None if position >= len(children) else [
                children, position, trail, depth]
        done = walk["current"] is None and not walk["queue"]
        if done:
            walks.pop(walk["id"], None)
        return {"walk_id": walk["id"], "results": results, "visited": walk["visited"],
                "done": done}

    def _browser_entry(self, item, trail):
        name = item.name
        extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if item.is_folder:
            kind = "folder"
        elif item.is_device:
            kind = "device"
        elif extension in self.SAMPLE_EXTENSIONS:
            kind = "sample"
        elif extension == "alc":
            kind = "clip"
        elif extension in self.PRESET_EXTENSIONS:
            kind = "preset"
        else:
            kind = "item"
        return {"name": name, "root": trail[0], "path": "/".join(trail[1:] + [name]),
                "kind": kind, "source": getattr(item, "source", ""),
                "uri": getattr(item, "uri", ""), "$id": self._lom_register(item)}

    def _overview_track(self, track, kind, index):
        mixer = track.mixer_device
        entry = {
            "kind": kind,
            "index": index,
            "id": self._lom_register(track),
            "name": track.name,
            "color_index": self._safe_attr(track, "color_index", int),
            "mute": self._safe_attr(track, "mute", bool),
            "solo": self._safe_attr(track, "solo", bool),
            "volume": mixer.volume.str_for_value(mixer.volume.value),
            "pan": mixer.panning.str_for_value(mixer.panning.value),
            "devices": [d.name for d in track.devices],
        }
        if kind == "track":
            entry["type"] = ("group" if getattr(track, "is_foldable", False)
                             else "midi" if track.has_midi_input else "audio")
            entry["arm"] = self._safe_arm(track)
            if getattr(track, "is_grouped", False):
                entry["group"] = track.group_track.name
            entry["sends"] = [s.str_for_value(s.value) for s in mixer.sends]
            clips = []
            for slot_index, slot in enumerate(track.clip_slots):
                if slot.has_clip:
                    clips.append({"slot": slot_index, "name": slot.clip.name,
                                  "length": float(slot.clip.length),
                                  "playing": bool(slot.clip.is_playing)})
            entry["session_clips"] = clips
            try:
                entry["arrangement_clip_count"] = len(track.arrangement_clips)
            except Exception:
                pass
        return entry

    def _cmd_get_session_overview(self, params):
        song = self._song
        view = song.view
        selected = getattr(view, "selected_track", None)
        result = {
            "tempo": float(song.tempo),
            "time_signature": "%d/%d" % (song.signature_numerator, song.signature_denominator),
            "is_playing": bool(song.is_playing),
            "current_song_time": float(song.current_song_time),
            "loop": {"on": bool(song.loop), "start": float(song.loop_start),
                     "length": float(song.loop_length)},
            "selected_track": selected.name if selected is not None else None,
            "tracks": [self._overview_track(t, "track", i) for i, t in enumerate(song.tracks)],
            "return_tracks": [self._overview_track(t, "return", i)
                              for i, t in enumerate(song.return_tracks)],
            "master_track": self._overview_track(song.master_track, "master", 0),
            "scenes": [{"index": i, "name": s.name} for i, s in enumerate(song.scenes)],
            "cue_points": [{"name": c.name, "time": float(c.time)} for c in song.cue_points],
        }
        for attr in ("root_note", "scale_name", "scale_mode", "swing_amount", "metronome",
                     "record_mode", "session_record", "arrangement_overdub"):
            if hasattr(song, attr):
                result[attr] = self._lom_value(getattr(song, attr))
        return result


def _would_block(error):
    """True when a non-blocking socket call simply had nothing to do."""
    return getattr(error, "errno", None) in (
        errno.EAGAIN, errno.EWOULDBLOCK, getattr(errno, "WSAEWOULDBLOCK", 10035))


def split_messages(buffer):
    """Split received bytes into complete requests: (list of bytes, rest).

    Protocol 2 clients end every request with a newline. A protocol 1 client
    sends one bare JSON object, complete once it ends with "}" and parses (no
    prefix of a JSON object is itself a complete object, so this cannot fire
    early).
    """
    messages = []
    while True:
        newline = buffer.find(b"\n")
        if newline < 0:
            break
        line, buffer = buffer[:newline].strip(), buffer[newline + 1:]
        if line:
            messages.append(line)
    tail = buffer.strip()
    if tail.endswith(b"}"):
        try:
            json.loads(tail.decode("utf-8"))
        except ValueError:  # includes UnicodeDecodeError: not all here yet
            pass
        else:
            messages.append(tail)
            buffer = b""
    return messages, buffer


class _Client(object):
    """One connection. Everything about it happens on Live's main thread."""

    def __init__(self, sock, address):
        self.sock = sock
        self.address = address
        self.inbox = b""
        self.outbox = bytearray()
        self.requests = []
        self.checked_first_byte = False
        self.authenticated = False
        self.eof = False
        self.closing = False
        # A poll_events that waits for an event: (request id, params, deadline)
        self.waiting = None


def create_instance(c_instance):
    """Create and return the AbletonMCP script instance"""
    return AbletonMCP(c_instance)

class AbletonMCP(LomMixin, ControlSurface):
    """AbletonMCP Remote Script for Ableton Live"""
    
    def __init__(self, c_instance):
        """Initialize the control surface"""
        ControlSurface.__init__(self, c_instance)
        self.log_message(
            "AbletonMCP Remote Script initializing... (script v%s)"
            % SCRIPT_VERSION
        )
        
        # Socket server for communication
        self._transport_init()
        
        # Cache the song reference for easier access
        self._song = self.song()
        self._lom_init()

        # Passive human-UI event queue (drained by MCP → Supabase)
        self._passive_events = []
        self._passive_lock = threading.Lock()
        self._passive_max = 500
        self._passive_track_count = None
        self._passive_track_bindings = []  # (track, [(add_name, callback), ...]) for cleanup
        self._song_passive_callbacks = []
        
        # Start the socket server
        self.start_server()

        # Register LOM listeners for passive capture (after song is ready)
        try:
            self._setup_passive_listeners()
        except Exception as e:
            self.log_message("Passive listener setup failed: " + str(e))
            self.log_message(traceback.format_exc())
        
        self.log_message("AbletonMCP initialized")
        
        # Show a message in Ableton
        self.show_message("AbletonMCP: Listening for commands on port " + str(DEFAULT_PORT))
    
    def disconnect(self):
        """Called when Ableton closes or the control surface is removed"""
        self.log_message("AbletonMCP disconnecting...")
        self.running = False
        self._stop_timer()

        try:
            self._teardown_passive_listeners()
        except Exception as e:
            self.log_message("Passive listener teardown error: " + str(e))

        try:
            self._teardown_observers()
        except Exception as e:
            self.log_message("Observer teardown error: " + str(e))

        for client in list(self._clients):
            self._drop_client(client)
        if self.server:
            try:
                self.server.close()
            except Exception:
                pass
            self.server = None
        self._withdraw_session()

        ControlSurface.disconnect(self)
        self.log_message("AbletonMCP disconnected")

    # ── Transport: non-blocking sockets polled on Live's main thread ─────────
    #
    # Live's Python lets a background thread run only on its ~100 ms tick, so
    # the old design (socket thread → schedule_message → main thread → queue →
    # socket thread) spent three or four ticks, about 410 ms, on every command.
    # Here the main thread itself accepts, reads, runs and answers, every
    # TIMER_INTERVAL_MS. No threads, so no locks around the Live Object Model.

    def _transport_init(self):
        self.server = None
        self.running = False
        self._clients = []
        self._polling = False
        self._timer = None
        self._session_token = None

    def start_server(self):
        """Listen on the port and start polling it from Live's main thread."""
        try:
            self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server.bind((HOST, DEFAULT_PORT))
            self.server.listen(5)
            self.server.setblocking(False)
            self.running = True
            self.log_message("Server started on port " + str(DEFAULT_PORT))
        except Exception as e:
            self.log_message("Error starting server: " + str(e))
            self.show_message("AbletonMCP: Error starting server - " + str(e))
            return
        self._session_token = self._publish_session()
        self._start_timer()

    def _start_timer(self):
        """Poll every TIMER_INTERVAL_MS with Live's own timer (Live 12 has one;
        Ableton's scripts use it). Without it, update_display polls every ~100 ms."""
        self._timer = None
        try:
            import Live
            timer = Live.Base.Timer(callback=self._on_timer, interval=TIMER_INTERVAL_MS,
                                    repeat=True)
            timer.start()
        except Exception as e:
            self.log_message("No Live.Base.Timer (%s); polling every ~100 ms" % e)
            return
        self._timer = timer
        self.log_message("Polling the socket every %d ms" % TIMER_INTERVAL_MS)

    def _stop_timer(self):
        timer, self._timer = getattr(self, "_timer", None), None
        if timer is not None:
            try:
                timer.stop()
            except Exception as e:
                self.log_message("Timer stop failed: " + str(e))

    def _on_timer(self, *_args):
        self._poll_sockets()

    def update_display(self):
        """Live calls this every ~100 ms: keeps _Framework's tasks running and
        polls the sockets in case the timer is missing."""
        ControlSurface.update_display(self)
        self._poll_sockets()

    def _poll_sockets(self):
        """Accept, read, run and answer whatever is ready, without blocking."""
        if self._polling or not self.running:
            return
        self._polling = True
        try:
            self._accept_clients()
            deadline = time.time() + POLL_BUDGET_SECONDS
            for client in list(self._clients):
                self._service_client(client, deadline)
        except Exception as e:
            self.log_message("Socket poll failed: " + str(e))
            self.log_message(traceback.format_exc())
        finally:
            self._polling = False

    def _accept_clients(self):
        while self.server is not None:
            try:
                sock, address = self.server.accept()
            except socket.error as e:
                if not _would_block(e):
                    self.log_message("Accept failed: " + str(e))
                return
            if len(self._clients) >= MAX_CLIENTS:
                self.log_message("Refusing a connection: %d clients already connected"
                                 % len(self._clients))
                sock.close()
                continue
            # Whether accept() hands back a non-blocking socket is OS-dependent
            sock.setblocking(False)
            self._clients.append(_Client(sock, address))
            self.log_message("Connection accepted from " + str(address))

    def _service_client(self, client, deadline):
        if not (client.eof or client.closing):
            self._receive(client)
        if client.sock is None:
            return
        if client.waiting is not None:
            self._answer_waiting_poll(client)
        while (client.requests and client.waiting is None and not client.closing
               and time.time() < deadline):
            self._handle_request(client, client.requests.pop(0))
        if client.closing:
            client.requests = []  # nothing more runs on a connection being closed
        self._flush(client)
        if client.sock is None:
            return
        finished = not (client.outbox or client.requests or client.waiting)
        if (client.closing or client.eof) and finished:
            self._drop_client(client)

    def _receive(self, client):
        """Read what has arrived and queue every complete request."""
        while True:
            try:
                data = client.sock.recv(65536)
            except socket.error as e:
                if not _would_block(e):
                    self._drop_client(client)
                    return
                break
            if not data:
                # The client is gone: run what it sent, then close
                client.eof = True
                client.waiting = None
                break
            client.inbox += data
            if not client.checked_first_byte and client.inbox.strip():
                client.checked_first_byte = True
                if not client.inbox.lstrip().startswith(b"{"):
                    # Not this protocol (e.g. an HTTP request a web page sent
                    # to localhost): run nothing from it.
                    self.log_message("Dropped a connection that does not send JSON")
                    self._drop_client(client)
                    return
            if b"\n" in data or data.rstrip().endswith(b"}"):
                messages, client.inbox = split_messages(client.inbox)
                client.requests.extend(messages)
            if len(client.inbox) > MAX_REQUEST_BYTES:
                client.inbox = b""
                self._reply(client, None, {
                    "status": "error",
                    "message": "Request exceeded %d bytes without a complete JSON command"
                               % MAX_REQUEST_BYTES})
                client.closing = True
                return

    # Answered before a client has authenticated
    OPEN_COMMANDS = frozenset(["hello", "get_script_info"])

    def _handle_request(self, client, raw):
        try:
            command = json.loads(raw.decode("utf-8"))
            if not isinstance(command, dict):
                raise ValueError("a request must be a JSON object")
        except ValueError as e:
            # Out of step with the client: say why, then close
            self._reply(client, None, {"status": "error", "message": "Invalid request: " + str(e)})
            client.closing = True
            return
        request_id = command.get("id")
        command_type = command.get("type", "")
        params = command.get("params") or {}
        if VERBOSE:
            self.log_message("Received command: " + str(command_type))
        if command_type == "hello":
            response = self._hello(client, params)
        elif (self._session_token and not client.authenticated
              and command_type not in self.OPEN_COMMANDS):
            response = {"status": "error", "message": (
                "Authentication required: send hello with the token from %s first "
                "(the ableton-mcp server does this from version 1.7)." % SESSION_FILE)}
        elif command_type == "poll_events" and params.get("wait"):
            self._wait_for_events(client, request_id, params)
            return
        else:
            response = self._process_command(command)
        self._reply(client, request_id, response)

    def _hello(self, client, params):
        """Authenticate the connection with the token from SESSION_FILE."""
        token = params.get("token")
        if self._session_token is not None:
            if not isinstance(token, string_types) or not hmac.compare_digest(
                    token.encode("utf-8"), self._session_token.encode("utf-8")):
                client.closing = True
                return {"status": "error", "message": (
                    "Invalid session token. Live writes a new one to %s at every "
                    "launch; read it again." % SESSION_FILE)}
        client.authenticated = True
        return {"status": "success", "result": self._get_script_info()}

    def _wait_for_events(self, client, request_id, params):
        """poll_events with wait > 0: answer as soon as there is an event."""
        try:
            wait = min(max(float(params.get("wait") or 0), 0.0), MAX_EVENT_WAIT_SECONDS)
        except (TypeError, ValueError):
            wait = 0.0
        client.waiting = (request_id, params, time.time() + wait)
        self._answer_waiting_poll(client)

    def _answer_waiting_poll(self, client):
        request_id, params, deadline = client.waiting
        response = self._invoke_handler(self._cmd_poll_events, params)
        if (response["status"] != "success" or response["result"]["events"]
                or time.time() >= deadline):
            client.waiting = None
            self._reply(client, request_id, response)

    def _reply(self, client, request_id, response):
        if request_id is not None:
            response = dict(response, id=request_id)
        try:
            data = json.dumps(response)
        except (TypeError, ValueError) as e:
            data = json.dumps({"status": "error", "id": request_id,
                               "message": "The result could not be encoded: " + str(e)})
        client.outbox += data.encode("utf-8") + b"\n"

    def _flush(self, client):
        while client.outbox:
            try:
                sent = client.sock.send(client.outbox[:262144])
            except socket.error as e:
                if not _would_block(e):
                    self._drop_client(client)
                    return
                break
            if not sent:
                break
            del client.outbox[:sent]
        if len(client.outbox) > MAX_RESPONSE_BACKLOG:
            self.log_message("Dropped a client that stopped reading its responses")
            self._drop_client(client)

    def _drop_client(self, client):
        if client.sock is not None:
            try:
                client.sock.close()
            except Exception:
                pass
            client.sock = None
            self.log_message("Client disconnected")
        if client in self._clients:
            self._clients.remove(client)

    def _publish_session(self):
        """Write the port and a fresh token to SESSION_FILE, readable only by
        this user. Returns the token, or None when clients need none."""
        if not AUTH_REQUIRED:
            self.log_message("ABLETON_MCP_AUTH=0: accepting clients without a token")
            return None
        token = binascii.hexlify(os.urandom(16)).decode("ascii")
        payload = {"port": DEFAULT_PORT, "token": token, "pid": os.getpid(),
                   "script_version": SCRIPT_VERSION, "protocol_version": PROTOCOL_VERSION}
        temp = SESSION_FILE + ".tmp"
        try:
            folder = os.path.dirname(SESSION_FILE)
            if folder and not os.path.isdir(folder):
                os.makedirs(folder, 0o700)
            handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(handle, json.dumps(payload).encode("utf-8"))
            finally:
                os.close(handle)
            os.chmod(temp, 0o600)
            replace = getattr(os, "replace", None)
            if replace is None:  # Python 2
                if os.path.exists(SESSION_FILE):
                    os.remove(SESSION_FILE)
                replace = os.rename
            replace(temp, SESSION_FILE)
        except Exception as e:
            self.log_message("Could not write %s (%s): accepting clients without a token"
                             % (SESSION_FILE, e))
            return None
        return token

    def _withdraw_session(self):
        """Remove SESSION_FILE if it still holds this instance's token."""
        token = getattr(self, "_session_token", None)
        if not token:
            return
        try:
            with open(SESSION_FILE) as handle:
                if json.load(handle).get("token") == token:
                    os.remove(SESSION_FILE)
        except Exception:
            pass
    
    # Commands that may not run inside a batch
    UNBATCHED_COMMANDS = frozenset([
        "batch", "hello", "get_script_info", "drain_passive_events", "poll_events",
    ])

    # Commands that never change the set; everything else closes its undo step.
    READ_ONLY_COMMANDS = frozenset([
        "get_session_info", "get_track_info", "get_browser_item", "get_browser_tree",
        "get_browser_items_at_path", "get_arrangement_clips", "get_clip_notes",
        "get_device_parameters", "get_session_snapshot", "inspect_rack", "lom_get",
        "clip_get_notes", "get_session_overview", "list_observers", "observe", "unobserve",
        "hello", "get_script_info", "drain_passive_events", "poll_events", "browser_search",
    ])

    def _command_handlers(self):
        """Command name → callable(params). Built once per instance."""
        handlers = getattr(self, "_handlers_cache", None)
        if handlers is not None:
            return handlers

        def track_clip(fn):
            return lambda p: fn(p.get("track_index", 0), p.get("clip_index", 0))

        handlers = {
            "get_script_info": lambda p: self._get_script_info(),
            "get_session_info": lambda p: self._get_session_info(),
            "get_track_info": lambda p: self._get_track_info(p.get("track_index", 0)),
            "create_midi_track": lambda p: self._create_midi_track(p.get("index", -1)),
            "create_audio_track": lambda p: self._create_audio_track(p.get("index", -1)),
            "set_track_name": lambda p: self._set_track_name(
                p.get("track_index", 0), p.get("name", "")),
            "create_clip": lambda p: self._create_clip(
                p.get("track_index", 0), p.get("clip_index", 0), p.get("length", 4.0)),
            "create_audio_clip": lambda p: self._create_audio_clip(
                p.get("track_index", 0), p.get("clip_index", 0), p.get("path", "")),
            "add_notes_to_clip": lambda p: self._add_notes_to_clip(
                p.get("track_index", 0), p.get("clip_index", 0), p.get("notes", [])),
            "clear_notes_from_clip": track_clip(self._clear_notes_from_clip),
            "set_clip_name": lambda p: self._set_clip_name(
                p.get("track_index", 0), p.get("clip_index", 0), p.get("name", "")),
            "set_arrangement_clip_name": lambda p: self._set_arrangement_clip_name(
                p.get("track_index", 0), p.get("clip_index", 0), p.get("name", "")),
            "set_tempo": lambda p: self._set_tempo(p.get("tempo", 120.0)),
            "fire_clip": track_clip(self._fire_clip),
            "stop_clip": track_clip(self._stop_clip),
            "delete_clip": track_clip(self._delete_clip),
            "start_playback": lambda p: self._start_playback(),
            "stop_playback": lambda p: self._stop_playback(),
            "load_instrument_or_effect": lambda p: self._load_instrument_or_effect(
                p.get("track_index", 0), p.get("uri", "")),
            "load_browser_item": lambda p: self._load_browser_item(
                p.get("track_index", 0), p.get("item_uri", "")),
            "switch_to_arrangement_view": lambda p: self._switch_to_arrangement_view(),
            "set_current_song_time": lambda p: self._set_current_song_time(p.get("time", 0.0)),
            "duplicate_session_clip_to_arrangement": lambda p: (
                self._duplicate_session_clip_to_arrangement(
                    p.get("track_index", 0), p.get("clip_index", 0),
                    p.get("destination_time", 0.0))),
            "inspect_rack": lambda p: self._inspect_rack(
                p.get("track_index", 0), p.get("device_index", 0)),
            "create_locator": lambda p: self._create_locator(
                p.get("name", ""), p.get("time", 0.0)),
            "get_browser_item": lambda p: self._get_browser_item(
                p.get("uri", None), p.get("path", None)),
            "get_browser_tree": lambda p: self.get_browser_tree(p.get("category_type", "all")),
            "get_browser_items_at_path": lambda p: self.get_browser_items_at_path(
                p.get("path", "")),
            "get_arrangement_clips": lambda p: self._get_arrangement_clips(
                p.get("track_index", 0)),
            "get_clip_notes": track_clip(self._get_clip_notes),
            "get_device_parameters": lambda p: self._get_device_parameters(
                p.get("track_index", 0), p.get("device_index", 0)),
            "get_session_snapshot": lambda p: self._get_session_snapshot(
                include_notes=p.get("include_notes", True),
                include_params=p.get("include_params", True)),
            "drain_passive_events": lambda p: self._drain_passive_events(),
            "set_device_parameter": lambda p: self._set_device_parameter(
                p.get("track_index", 0), p.get("device_index", 0),
                p.get("parameter_index", 0), p.get("value", 0.0)),
        }
        handlers.update(self._lom_command_handlers())
        self._handlers_cache = handlers
        return handlers

    def _process_command(self, command):
        """Run one command and return its response. Called on Live's main
        thread, inside the component guard like any script callback."""
        command_type = command.get("type", "")
        params = command.get("params") or {}

        handler = self._command_handlers().get(command_type)
        if handler is None:
            return {"status": "error", "message": "Unknown command: " + command_type}

        modifies = not self._is_read_only(command_type, params)
        if modifies:
            self._undo_step("begin")
        guard = getattr(self, "component_guard", None)
        if guard is None:
            response = self._invoke_handler(handler, params)
        else:
            with guard():
                response = self._invoke_handler(handler, params)
        if modifies:
            self._undo_step("end")
        return response

    # LOM methods that only read: calling them must not touch the undo history
    READ_ONLY_METHODS = frozenset([
        "str_for_value", "available_main_views", "value_at_time", "events_in_range",
        "note_number_to_name", "find_device_position", "beat_to_sample_time",
        "sample_to_beat_time", "seconds_to_sample_time",
    ])

    def _is_read_only(self, command_type, params):
        if command_type in self.READ_ONLY_COMMANDS:
            return True
        if command_type == "lom_call":
            method = str(params.get("method") or "")
            return (method in self.READ_ONLY_METHODS
                    or method.startswith("get_") or method.startswith("is_"))
        if command_type == "batch":
            commands = params.get("commands") or []
            return bool(commands) and all(
                isinstance(c, dict) and c.get("type") != "batch"
                and self._is_read_only(c.get("type", ""), c.get("params") or {})
                for c in commands)
        return False

    def _undo_step(self, edge):
        """Open ("begin") or seal ("end") the undo step of one command.

        Live otherwise keeps appending every script-made change to one open
        undo step until the user does something, so a single Cmd+Z would wipe
        all agent edits since then. Measured on Live 12.3.8: from a
        Live.Base.Timer callback, end_undo_step() alone does not close the
        step (later commands still merge into it); begin_undo_step() before
        and end_undo_step() after each command gives every command its own
        step(s).
        """
        try:
            method = getattr(self._song, edge + "_undo_step", None)
            if method is not None:
                method()
        except Exception as e:
            self.log_message("%s_undo_step failed: %s" % (edge, e))

    def _invoke_handler(self, handler, params):
        try:
            return {"status": "success", "result": handler(params)}
        except EXPECTED_ERRORS as e:
            # A bad path or value from the client: worth a line, not a traceback
            if VERBOSE:
                self.log_message("Command refused: " + str(e))
            return {"status": "error", "message": str(e)}
        except Exception as e:
            self.log_message("Error processing command: " + str(e))
            self.log_message(traceback.format_exc())
            return {"status": "error", "message": str(e)}

    # Command implementations

    def _get_script_info(self):
        """Handshake payload for MCP server version / capability checks."""
        return {
            "name": "AbletonMCP",
            "script_version": SCRIPT_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "port": DEFAULT_PORT,
            "capabilities": list(SCRIPT_CAPABILITIES),
            "snapshot_schema": "ableton_mcp_snapshot_v2",
            "passive_listeners": True,
            "transport": "timer" if getattr(self, "_timer", None) else "update_display",
            "auth_required": bool(getattr(self, "_session_token", None)),
        }
    
    def _safe_song_property(self, attr, cast, default):
        """Read self._song.<attr> with cast, returning default on common failures.
        Catches only narrow exceptions so genuine bugs still surface."""
        try:
            return cast(getattr(self._song, attr))
        except (AttributeError, TypeError, ValueError):
            return default

    def _get_session_info(self):
        """Get information about the current session"""
        try:
            result = {
                "tempo": self._song.tempo,
                "signature_numerator": self._song.signature_numerator,
                "signature_denominator": self._song.signature_denominator,
                "track_count": len(self._song.tracks),
                "return_track_count": len(self._song.return_tracks),
                "master_track": {
                    "name": "Master",
                    "volume": self._song.master_track.mixer_device.volume.value,
                    "panning": self._song.master_track.mixer_device.panning.value
                },
                # Read via _safe_song_property so an attribute missing on a
                # given Live version falls back to its default.
                "is_playing":        self._safe_song_property("is_playing",        bool,  False),
                "current_song_time": self._safe_song_property("current_song_time", float, 0.0),
                "song_length":       self._safe_song_property("song_length",       float, 0.0),
                "loop":              self._safe_song_property("loop",              bool,  False),
                "loop_start":        self._safe_song_property("loop_start",        float, 0.0),
                "loop_length":       self._safe_song_property("loop_length",       float, 0.0),
            }
            return result
        except Exception as e:
            self.log_message("Error getting session info: " + str(e))
            raise
    
    def _get_track_info(self, track_index):
        """Get information about a track"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            # Get clip slots
            clip_slots = []
            for slot_index, slot in enumerate(track.clip_slots):
                clip_info = None
                if slot.has_clip:
                    clip = slot.clip
                    clip_info = {
                        "name": clip.name,
                        "length": clip.length,
                        "is_playing": clip.is_playing,
                        "is_recording": clip.is_recording
                    }
                
                clip_slots.append({
                    "index": slot_index,
                    "has_clip": slot.has_clip,
                    "clip": clip_info
                })
            
            # Get devices
            devices = []
            for device_index, device in enumerate(track.devices):
                devices.append({
                    "index": device_index,
                    "name": device.name,
                    "class_name": device.class_name,
                    "type": self._get_device_type(device)
                })
            
            result = {
                "index": track_index,
                "name": track.name,
                "is_audio_track": track.has_audio_input,
                "is_midi_track": track.has_midi_input,
                "mute": track.mute,
                "solo": track.solo,
                "arm": self._safe_arm(track),
                "volume": track.mixer_device.volume.value,
                "panning": track.mixer_device.panning.value,
                "clip_slots": clip_slots,
                "devices": devices
            }
            return result
        except Exception as e:
            self.log_message("Error getting track info: " + str(e))
            raise
    
    def _safe_arm(self, track):
        """Read track.arm, returning False for tracks that have no arm state.

        Live raises RuntimeError("Master and Return Tracks have no 'Arm'
        state!") for group tracks as well as return and main tracks. A
        `getattr(track, "arm", False)` does not guard this: the attribute
        exists, so getattr's default never applies -- reading it is what
        throws, and the error is a RuntimeError rather than an AttributeError.

        Check can_be_armed first so the common path does not rely on raising,
        and keep a narrow catch for Live versions that do not expose that
        property on every track type.
        """
        try:
            if not getattr(track, "can_be_armed", False):
                return False
            return bool(track.arm)
        except (AttributeError, RuntimeError):
            return False

    def _create_midi_track(self, index):
        """Create a new MIDI track at the specified index"""
        try:
            # Create the track
            self._song.create_midi_track(index)
            
            # Get the new track
            new_track_index = len(self._song.tracks) - 1 if index == -1 else index
            new_track = self._song.tracks[new_track_index]
            
            result = {
                "index": new_track_index,
                "name": new_track.name
            }
            return result
        except Exception as e:
            self.log_message("Error creating MIDI track: " + str(e))
            raise

    def _create_audio_track(self, index):
        """Create a new audio track at the specified index"""
        try:
            # Create the track
            self._song.create_audio_track(index)

            # Get the new track
            new_track_index = len(self._song.tracks) - 1 if index == -1 else index
            new_track = self._song.tracks[new_track_index]

            result = {
                "index": new_track_index,
                "name": new_track.name
            }
            return result
        except Exception as e:
            self.log_message("Error creating audio track: " + str(e))
            raise


    def _set_track_name(self, track_index, name):
        """Set the name of a track"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            # Set the name
            track = self._song.tracks[track_index]
            track.name = name
            
            result = {
                "name": track.name
            }
            return result
        except Exception as e:
            self.log_message("Error setting track name: " + str(e))
            raise
    
    def _create_clip(self, track_index, clip_index, length):
        """Create a new MIDI clip in the specified track and clip slot"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            # Check if the clip slot already has a clip
            if clip_slot.has_clip:
                raise Exception("Clip slot already has a clip")
            
            # Create the clip
            clip_slot.create_clip(length)
            
            result = {
                "name": clip_slot.clip.name,
                "length": clip_slot.clip.length
            }
            return result
        except Exception as e:
            self.log_message("Error creating clip: " + str(e))
            raise

    def _create_audio_clip(self, track_index, clip_index, path):
        """Create an audio clip in the specified audio track clip slot by importing a file.

        Requires Ableton Live 12.0.5 or newer (the underlying
        ClipSlot.create_audio_clip Live API was introduced in 12.0.5 — it is
        not available in earlier 12.0.x releases).
        """
        try:
            if not path:
                raise ValueError("Audio file path is required")

            if not os.path.isabs(path):
                raise ValueError("Audio file path must be absolute (got: %s)" % path)

            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]

            if getattr(track, "has_midi_input", False) or not getattr(track, "has_audio_input", True):
                raise ValueError("Track %d is not an audio track" % track_index)

            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")

            clip_slot = track.clip_slots[clip_index]

            if clip_slot.has_clip:
                raise Exception("Clip slot already has a clip")

            if not hasattr(clip_slot, "create_audio_clip"):
                raise Exception(
                    "ClipSlot.create_audio_clip is unavailable in this Ableton Live "
                    "version. Requires Live 12.0.5 or newer."
                )

            clip_slot.create_audio_clip(path)

            result = {
                "name": clip_slot.clip.name,
                "length": clip_slot.clip.length,
                "is_audio_clip": clip_slot.clip.is_audio_clip
            }
            return result
        except Exception as e:
            self.log_message("Error creating audio clip: " + str(e))
            raise

    def _add_notes_to_clip(self, track_index, clip_index, notes):
        """Add MIDI notes to a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            if not clip_slot.has_clip:
                raise Exception("No clip in slot")
            
            clip = clip_slot.clip
            
            # Convert note data to Live's format
            live_notes = []
            for note in notes:
                pitch = note.get("pitch", 60)
                start_time = note.get("start_time", 0.0)
                duration = note.get("duration", 0.25)
                velocity = note.get("velocity", 100)
                mute = note.get("mute", False)
                
                live_notes.append((pitch, start_time, duration, velocity, mute))
            
            # Add the notes
            clip.set_notes(tuple(live_notes))
            
            result = {
                "note_count": len(notes)
            }
            return result
        except Exception as e:
            self.log_message("Error adding notes to clip: " + str(e))
            raise
    
    def _set_clip_name(self, track_index, clip_index, name):
        """Set the name of a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            if not clip_slot.has_clip:
                raise Exception("No clip in slot")
            
            clip = clip_slot.clip
            clip.name = name
            
            result = {
                "name": clip.name
            }
            return result
        except Exception as e:
            self.log_message("Error setting clip name: " + str(e))
            raise

    def _set_arrangement_clip_name(self, track_index, clip_index, name):
        """Set the name of a clip placed in the Arrangement timeline.

        clip_index indexes into track.arrangement_clips, in the same order
        as returned by _get_arrangement_clips (i.e. ordered by start_time).
        """
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]
            arrangement_clips = list(track.arrangement_clips)

            if clip_index < 0 or clip_index >= len(arrangement_clips):
                raise IndexError("Clip index out of range")

            clip = arrangement_clips[clip_index]
            clip.name = name

            result = {
                "name": clip.name
            }
            return result
        except Exception as e:
            self.log_message("Error setting arrangement clip name: " + str(e))
            raise

    def _set_tempo(self, tempo):
        """Set the tempo of the session"""
        try:
            self._song.tempo = tempo
            
            result = {
                "tempo": self._song.tempo
            }
            return result
        except Exception as e:
            self.log_message("Error setting tempo: " + str(e))
            raise
    
    def _fire_clip(self, track_index, clip_index):
        """Fire a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            if not clip_slot.has_clip:
                raise Exception("No clip in slot")
            
            clip_slot.fire()
            
            result = {
                "fired": True
            }
            return result
        except Exception as e:
            self.log_message("Error firing clip: " + str(e))
            raise
    
    def _stop_clip(self, track_index, clip_index):
        """Stop a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            clip_slot.stop()
            
            result = {
                "stopped": True
            }
            return result
        except Exception as e:
            self.log_message("Error stopping clip: " + str(e))
            raise

    def _delete_clip(self, track_index, clip_index):
        """Delete the clip in the given clip slot, freeing the slot for reuse."""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]

            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")

            clip_slot = track.clip_slots[clip_index]

            if not clip_slot.has_clip:
                return {"deleted": False, "reason": "Clip slot was already empty"}

            clip_slot.delete_clip()

            return {"deleted": True}
        except Exception as e:
            self.log_message("Error deleting clip: " + str(e))
            raise


    def _start_playback(self):
        """Start playing the session"""
        try:
            self._song.start_playing()
            
            result = {
                "playing": self._song.is_playing
            }
            return result
        except Exception as e:
            self.log_message("Error starting playback: " + str(e))
            raise
    
    def _stop_playback(self):
        """Stop playing the session"""
        try:
            self._song.stop_playing()
            
            result = {
                "playing": self._song.is_playing
            }
            return result
        except Exception as e:
            self.log_message("Error stopping playback: " + str(e))
            raise
    
    # ── Arrangement view implementations ──────────────────────────────────────

    def _switch_to_arrangement_view(self):
        """Switch Ableton's main window to the Arrangement view"""
        try:
            self.application().view.show_view("Arranger")
            return {"view": "Arranger"}
        except Exception as e:
            self.log_message("Error switching to arrangement view: " + str(e))
            raise

    def _set_current_song_time(self, time_val):
        """Move the arrangement playhead to a position in beats"""
        try:
            self._song.current_song_time = float(time_val)
            return {"current_song_time": self._song.current_song_time}
        except Exception as e:
            self.log_message("Error setting current song time: " + str(e))
            raise

    def _get_arrangement_clips(self, track_index):
        """Return all clips placed in the Arrangement timeline for a track.

        Each clip dict contains:
          name, start_time, end_time, length, color,
          is_midi_clip, is_audio_clip, is_playing
        """
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]
            clips = []

            # track.arrangement_clips is available in Live 11 / 12
            for clip in track.arrangement_clips:
                clips.append({
                    "name": clip.name,
                    "start_time": clip.start_time,
                    "end_time": clip.end_time,
                    "length": clip.length,
                    "color": clip.color,
                    "is_midi_clip": clip.is_midi_clip,
                    "is_audio_clip": clip.is_audio_clip,
                    "is_playing": clip.is_playing
                })

            return {
                "track_index": track_index,
                "track_name": track.name,
                "clip_count": len(clips),
                "clips": clips
            }
        except Exception as e:
            self.log_message("Error getting arrangement clips: " + str(e))
            raise

    def _clear_notes_from_clip(self, track_index, clip_index):
        """Remove all MIDI notes from a Session clip.

        Pairs with _add_notes_to_clip to make a real replace (clear, then add),
        which the write-only API otherwise can't do. Counts notes first so the
        result can report how many were removed.
        """
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]

            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")

            clip_slot = track.clip_slots[clip_index]

            if not clip_slot.has_clip:
                raise Exception("No clip in slot")

            clip = clip_slot.clip

            if not clip.is_midi_clip:
                raise Exception("Clip is not a MIDI clip; no notes to clear")

            length = clip.length

            # Count existing notes for the report (best-effort; never fatal).
            cleared = 0
            try:
                getter = getattr(clip, "get_notes_extended", None)
                if getter is not None:
                    cleared = len(list(getter(0, 128, 0.0, length)))
                else:
                    cleared = len(list(clip.get_notes(0.0, 0, length, 128)))
            except Exception:
                cleared = 0

            # Remove every note across the full pitch/time range. Prefer the
            # modern API (Live 11+); fall back to the legacy signature. Argument
            # order mirrors the get/remove _extended family:
            #   remove_notes_extended(from_pitch, pitch_span, from_time, time_span)
            # vs the legacy remove_notes(from_time, from_pitch, time_span, pitch_span).
            remover = getattr(clip, "remove_notes_extended", None)
            if remover is not None:
                remover(0, 128, 0.0, length)
            else:
                clip.remove_notes(0.0, 0, length, 128)

            return {
                "track_index": track_index,
                "clip_index": clip_index,
                "clip_name": clip.name,
                "cleared_count": cleared,
            }
        except Exception as e:
            self.log_message("Error clearing notes from clip: " + str(e))
            raise

    def _duplicate_session_clip_to_arrangement(self, track_index, clip_index, destination_time):
        """Copy a Session-view clip into the Arrangement timeline.

        Uses the real Live API:
          track.duplicate_clip_to_arrangement(clip, destination_time)

        Available in Live 11 / 12.  destination_time is in beats from the
        start of the arrangement.
        """
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]

            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip slot index out of range")

            clip_slot = track.clip_slots[clip_index]

            if not clip_slot.has_clip:
                raise Exception(
                    "No clip in slot " + str(clip_index) +
                    " on track " + str(track_index)
                )

            clip = clip_slot.clip

            # Duplicate to arrangement at the requested beat position
            track.duplicate_clip_to_arrangement(clip, float(destination_time))

            return {
                "success": True,
                "track_index": track_index,
                "track_name": track.name,
                "clip_name": clip.name,
                "destination_time": destination_time
            }
        except Exception as e:
            self.log_message("Error duplicating clip to arrangement: " + str(e))
            raise

    def _create_locator(self, name, time_val):
        """Create (or rename) a named locator at the given beat position.

        Live only has Song.set_or_delete_cue(), which toggles a cue at the
        playhead — and a playhead moved in this same callback is not in effect
        yet (measured on Live 12.4: the cue lands at the old position, or an
        existing cue there is deleted). So the playhead moves now and the
        toggle and rename run on the next update; the reply says so.
        """
        song = self._song
        target_time = float(time_val)
        tolerance = 1e-3

        def cue_at():
            for cue in song.cue_points:
                if abs(cue.time - target_time) < tolerance:
                    return cue
            return None

        existing = cue_at()
        if existing is not None:
            if name:
                existing.name = str(name)
            return {"success": True, "time": existing.time, "name": existing.name}

        original_time = song.current_song_time
        song.current_song_time = target_time

        def finish():
            try:
                if cue_at() is None:
                    song.set_or_delete_cue()
                cue = cue_at()
                if cue is None:
                    self.log_message("No locator appeared at beat " + str(target_time))
                elif name:
                    cue.name = str(name)
                song.current_song_time = original_time
            except Exception as e:
                self.log_message("Error creating locator: " + str(e))

        self.schedule_message(1, finish)
        return {"success": True, "scheduled": True, "time": target_time, "name": name}

    # ── Browser implementations ───────────────────────────────────────────────

    def _get_browser_item(self, uri, path):
        """Get a browser item by URI or path"""
        try:
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            if not app:
                raise RuntimeError("Could not access Live application")
                
            result = {
                "uri": uri,
                "path": path,
                "found": False
            }
            
            # Try to find by URI first if provided
            if uri:
                item = self._find_browser_item_by_uri(app.browser, uri)
                if item:
                    result["found"] = True
                    result["item"] = {
                        "name": item.name,
                        "is_folder": item.is_folder,
                        "is_device": item.is_device,
                        "is_loadable": item.is_loadable,
                        "uri": item.uri
                    }
                    return result
            
            # If URI not provided or not found, try by path
            if path:
                # Parse the path and navigate to the specified item
                path_parts = path.split("/")
                
                # Determine the root based on the first part
                current_item = None
                if path_parts[0].lower() == "instruments":
                    current_item = app.browser.instruments
                elif path_parts[0].lower() == "sounds":
                    current_item = app.browser.sounds
                elif path_parts[0].lower() == "drums":
                    current_item = app.browser.drums
                elif path_parts[0].lower() == "audio_effects":
                    current_item = app.browser.audio_effects
                elif path_parts[0].lower() == "midi_effects":
                    current_item = app.browser.midi_effects
                else:
                    # Default to instruments if not specified
                    current_item = app.browser.instruments
                    # Don't skip the first part in this case
                    path_parts = ["instruments"] + path_parts
                
                # Navigate through the path
                for i in range(1, len(path_parts)):
                    part = path_parts[i]
                    if not part:  # Skip empty parts
                        continue
                    
                    found = False
                    for child in current_item.children:
                        if child.name.lower() == part.lower():
                            current_item = child
                            found = True
                            break
                    
                    if not found:
                        result["error"] = "Path part '{0}' not found".format(part)
                        return result
                
                # Found the item
                result["found"] = True
                result["item"] = {
                    "name": current_item.name,
                    "is_folder": current_item.is_folder,
                    "is_device": current_item.is_device,
                    "is_loadable": current_item.is_loadable,
                    "uri": current_item.uri
                }
            
            return result
        except Exception as e:
            self.log_message("Error getting browser item: " + str(e))
            self.log_message(traceback.format_exc())
            raise   
    
    
    
    def _load_instrument_or_effect(self, track_index, uri):
        """Load an instrument or effect onto a track by its browser URI.

        The command dispatcher above calls this method, but it was never
        defined — and "load_instrument_or_effect" was missing from the list of
        main-thread commands as well, so the command fell through to the final
        "Unknown command" branch. Loading a device is exactly what
        _load_browser_item does, so delegate to it; the only difference is the
        parameter name the MCP server uses ("uri" vs "item_uri").
        """
        return self._load_browser_item(track_index, uri)

    def _load_browser_item(self, track_index, item_uri):
        """Load a browser item onto a track by its URI"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            
            # Find the browser item by URI
            item = self._find_browser_item_by_uri(app.browser, item_uri)
            
            if not item:
                raise ValueError("Browser item with URI '{0}' not found".format(item_uri))
            
            # Select the track
            self._song.view.selected_track = track
            
            # Load the item
            app.browser.load_item(item)
            
            result = {
                "loaded": True,
                "item_name": item.name,
                "track_name": track.name,
                "uri": item_uri
            }
            return result
        except Exception as e:
            self.log_message("Error loading browser item: {0}".format(str(e)))
            self.log_message(traceback.format_exc())
            raise
    
    # Substring markers that point a URI at a likely root. Unmatched URIs fall
    # back to the default search order.
    _URI_ROOT_HINTS = (
        ('plugins',       ('vst:', 'vst3:', 'au:', 'query:plugins', 'plugin#')),
        ('max_for_live',  ('max for live', 'maxforlive', 'm4l', 'query:max')),
        ('user_library',  ('user library', 'userlibrary', 'query:user library', 'query:user-library')),
        ('packs',         ('query:packs', '/packs/')),
        ('samples',       ('query:samples', 'sample:', '/samples/')),
        ('drums',         ('query:drums', '/drums/')),
        ('instruments',   ('query:instruments', '/instruments/')),
        ('sounds',        ('query:sounds', '/sounds/')),
        ('audio_effects', ('query:audio effects', 'audioeffects', '/audio_effects/')),
        ('midi_effects',  ('query:midi effects', 'midieffects', '/midi_effects/')),
    )

    def _order_roots_by_uri(self, roots, uri):
        """Reorder ``roots`` so the URI's likely root is walked first."""
        if not isinstance(uri, (bytes, str)) or not uri:
            return roots
        lowered = uri.lower()
        for attr, markers in self._URI_ROOT_HINTS:
            if any(m in lowered for m in markers):
                head = [(a, r) for (a, r) in roots if a == attr]
                tail = [(a, r) for (a, r) in roots if a != attr]
                return head + tail
        return roots

    def _find_browser_item_by_uri(self, browser_or_item, uri, max_depth=10, current_depth=0):
        """Find a browser item by its URI.

        Top-level lookups are memoised on ``self._uri_cache`` so repeated
        loads of the same URI don't re-walk the entire browser tree.
        """
        if current_depth == 0:
            cache = getattr(self, '_uri_cache', None)
            if cache is None:
                self._uri_cache = cache = {}
            if uri in cache:
                return cache[uri]
            result = self._walk_browser_for_uri(browser_or_item, uri, max_depth, 0)
            if result is not None:
                cache[uri] = result
            return result
        return self._walk_browser_for_uri(browser_or_item, uri, max_depth, current_depth)

    def _walk_browser_for_uri(self, browser_or_item, uri, max_depth, current_depth):
        """Recursive walk used by :py:meth:`_find_browser_item_by_uri`."""
        try:
            # Check if this is the item we're looking for
            if hasattr(browser_or_item, 'uri') and browser_or_item.uri == uri:
                return browser_or_item

            # Stop recursion if we've reached max depth
            if current_depth >= max_depth:
                return None

            # Check if this is a browser with root categories
            if hasattr(browser_or_item, 'instruments'):
                roots = [
                    ('instruments', browser_or_item.instruments),
                    ('sounds', browser_or_item.sounds),
                    ('drums', browser_or_item.drums),
                    ('audio_effects', browser_or_item.audio_effects),
                    ('midi_effects', browser_or_item.midi_effects),
                ]
                for extra_attr in ('plugins', 'max_for_live', 'user_library', 'packs', 'samples'):
                    if hasattr(browser_or_item, extra_attr):
                        try:
                            roots.append((extra_attr, getattr(browser_or_item, extra_attr)))
                        except (AttributeError, RuntimeError) as e:
                            self.log_message("Could not access browser.{0}: {1}".format(extra_attr, str(e)))

                for _attr, category in self._order_roots_by_uri(roots, uri):
                    item = self._find_browser_item_by_uri(category, uri, max_depth, current_depth + 1)
                    if item:
                        return item

                return None

            # Check if this item has children
            if hasattr(browser_or_item, 'children') and browser_or_item.children:
                for child in browser_or_item.children:
                    item = self._find_browser_item_by_uri(child, uri, max_depth, current_depth + 1)
                    if item:
                        return item

            return None
        except Exception as e:
            self.log_message("Error finding browser item by URI: {0}".format(str(e)))
            return None
    
    # Helper methods

    def _find_blend_parameter(self, device):
        """Find Dry/Wet, Mix, or Amount on a device for Magnitude mapping."""
        preferred = ("Dry/Wet", "Dry Wet", "Mix", "Amount")
        by_name = {}
        for param in device.parameters:
            try:
                by_name[param.name] = param
            except Exception:
                continue
        for name in preferred:
            if name in by_name:
                return by_name[name], name
        # Case-insensitive fallback
        lowered = dict((k.lower(), (v, k)) for k, v in by_name.items())
        for name in preferred:
            hit = lowered.get(name.lower())
            if hit:
                return hit[0], hit[1]
        return None, None

    def _inspect_rack(self, track_index, device_index=0):
        """Inspect a rack's nested devices and blend parameters."""
        if track_index < 0 or track_index >= len(self._song.tracks):
            raise IndexError("Track index out of range")
        track = self._song.tracks[track_index]
        if device_index < 0 or device_index >= len(track.devices):
            raise IndexError("Device index out of range")
        rack = track.devices[device_index]
        if not getattr(rack, "can_have_chains", False):
            raise ValueError("Device '{0}' is not a rack".format(rack.name))

        devices_info = []
        for chain_index, chain in enumerate(rack.chains):
            for nested in chain.devices:
                blend, blend_name = self._find_blend_parameter(nested)
                param_names = []
                try:
                    param_names = [p.name for p in nested.parameters]
                except Exception:
                    pass
                devices_info.append({
                    "chain_index": chain_index,
                    "name": nested.name,
                    "class_name": nested.class_name,
                    "blend_param": blend_name,
                    "parameters": param_names,
                })

        return {
            "track_index": track_index,
            "device_index": device_index,
            "rack_name": rack.name,
            "has_macro_map": hasattr(rack, "macro_map"),
            "has_rename_macro": hasattr(rack, "rename_macro"),
            "macros_mapped": list(getattr(rack, "macros_mapped", [])),
            "devices": devices_info,
        }

    def _get_device_type(self, device):
        """Get the type of a device"""
        try:
            # Simple heuristic - in a real implementation you'd look at the device class
            if device.can_have_drum_pads:
                return "drum_machine"
            elif device.can_have_chains:
                return "rack"
            elif "instrument" in device.class_display_name.lower():
                return "instrument"
            elif "audio_effect" in device.class_name.lower():
                return "audio_effect"
            elif "midi_effect" in device.class_name.lower():
                return "midi_effect"
            else:
                return "unknown"
        except:
            return "unknown"

    # ── Passive human-UI listeners ──────────────────────────────────────────────

    def _enqueue_passive(self, event_type, detail=None, track_index=None, clip_index=None):
        """Append a coarse human-UI event (capped FIFO)."""
        evt = {
            "type": event_type,
            "ts": time.time(),
            "track_index": track_index,
            "clip_index": clip_index,
            "detail": detail if detail is not None else {},
        }
        with self._passive_lock:
            self._passive_events.append(evt)
            if len(self._passive_events) > self._passive_max:
                self._passive_events = self._passive_events[-self._passive_max:]

    def _drain_passive_events(self):
        """Return and clear the passive event queue (called by MCP poller)."""
        with self._passive_lock:
            events = list(self._passive_events)
            self._passive_events = []
        return {"events": events, "count": len(events)}

    def _safe_add_listener(self, obj, add_name, callback):
        try:
            if obj is not None and hasattr(obj, add_name):
                getattr(obj, add_name)(callback)
                return True
        except Exception as e:
            self.log_message("add listener %s failed: %s" % (add_name, str(e)))
        return False

    def _safe_remove_listener(self, obj, remove_name, callback):
        try:
            if obj is not None and hasattr(obj, remove_name):
                getattr(obj, remove_name)(callback)
        except Exception:
            pass

    def _setup_passive_listeners(self):
        """Register high-signal LOM listeners for Mode C / assisted human edits."""
        song = self._song

        def on_tempo():
            try:
                self._enqueue_passive("tempo_changed", {"tempo": float(song.tempo)})
            except Exception:
                self._enqueue_passive("tempo_changed")

        def on_sig_num():
            try:
                self._enqueue_passive(
                    "time_signature_changed",
                    {
                        "signature_numerator": int(song.signature_numerator),
                        "signature_denominator": int(song.signature_denominator),
                    },
                )
            except Exception:
                self._enqueue_passive("time_signature_changed")

        def on_sig_den():
            on_sig_num()

        def on_is_playing():
            try:
                self._enqueue_passive(
                    "playback_changed",
                    {"is_playing": bool(song.is_playing)},
                )
            except Exception:
                self._enqueue_passive("playback_changed")

        def on_tracks():
            count = len(song.tracks)
            previous = getattr(self, "_passive_track_count", None)
            self._passive_track_count = count
            detail = {"track_count": count}
            if previous is not None:
                detail["previous_count"] = previous
                detail["removed"] = count < previous
                detail["added"] = count > previous
            self._enqueue_passive("tracks_changed", detail)
            try:
                self._rebind_track_listeners()
            except Exception as e:
                self.log_message("rebind track listeners failed: " + str(e))

        self._safe_add_listener(song, "add_tempo_listener", on_tempo)
        self._safe_add_listener(song, "add_signature_numerator_listener", on_sig_num)
        self._safe_add_listener(song, "add_signature_denominator_listener", on_sig_den)
        self._safe_add_listener(song, "add_is_playing_listener", on_is_playing)
        self._safe_add_listener(song, "add_tracks_listener", on_tracks)

        self._song_passive_callbacks = [
            ("tempo_listener", on_tempo),
            ("signature_numerator_listener", on_sig_num),
            ("signature_denominator_listener", on_sig_den),
            ("is_playing_listener", on_is_playing),
            ("tracks_listener", on_tracks),
        ]

        self._rebind_track_listeners()
        self.log_message("Passive LOM listeners registered")

    def _teardown_passive_listeners(self):
        song = getattr(self, "_song", None)
        for suffix, cb in getattr(self, "_song_passive_callbacks", []):
            self._safe_remove_listener(song, "remove_" + suffix, cb)
        self._clear_track_listeners()

    def _clear_track_listeners(self):
        for track, bindings in getattr(self, "_passive_track_bindings", []):
            for add_name, callback in bindings:
                remove_name = "remove_" + add_name[len("add_"):]
                target = getattr(callback, "_passive_target", None)
                if target is not None:
                    self._safe_remove_listener(target, remove_name, callback)
                else:
                    self._safe_remove_listener(track, remove_name, callback)
        self._passive_track_bindings = []

    def _rebind_track_listeners(self):
        self._clear_track_listeners()
        song = self._song
        for track_index, track in enumerate(song.tracks):
            bindings = []

            def make_track_cb(kind, t_index):
                def _cb():
                    detail = {}
                    try:
                        t = song.tracks[t_index]
                        if kind == "name_changed":
                            detail["name"] = t.name
                        elif kind == "mute_changed":
                            detail["mute"] = bool(t.mute)
                        elif kind == "solo_changed":
                            detail["solo"] = bool(t.solo)
                        elif kind == "arm_changed":
                            detail["arm"] = self._safe_arm(t)
                        elif kind == "devices_changed":
                            detail["device_count"] = len(t.devices)
                        elif kind == "volume_changed":
                            detail["volume"] = float(t.mixer_device.volume.value)
                        elif kind == "panning_changed":
                            detail["panning"] = float(t.mixer_device.panning.value)
                    except Exception:
                        pass
                    self._enqueue_passive(kind, detail, track_index=t_index)
                return _cb

            pairs = [
                ("add_name_listener", "name_changed"),
                ("add_mute_listener", "mute_changed"),
                ("add_solo_listener", "solo_changed"),
                ("add_arm_listener", "arm_changed"),
                ("add_devices_listener", "devices_changed"),
            ]
            for add_name, kind in pairs:
                cb = make_track_cb(kind, track_index)
                if self._safe_add_listener(track, add_name, cb):
                    bindings.append((add_name, cb))

            try:
                mixer = track.mixer_device
                vol_cb = make_track_cb("volume_changed", track_index)
                vol_cb._passive_target = mixer.volume
                if self._safe_add_listener(mixer.volume, "add_value_listener", vol_cb):
                    bindings.append(("add_value_listener", vol_cb))
                pan_cb = make_track_cb("panning_changed", track_index)
                pan_cb._passive_target = mixer.panning
                if self._safe_add_listener(mixer.panning, "add_value_listener", pan_cb):
                    bindings.append(("add_value_listener", pan_cb))
            except Exception as e:
                self.log_message("mixer listeners failed on track %d: %s" % (track_index, str(e)))

            try:
                for clip_index, slot in enumerate(track.clip_slots):
                    def make_slot_cb(t_index, c_index):
                        def _cb():
                            has = False
                            try:
                                has = bool(song.tracks[t_index].clip_slots[c_index].has_clip)
                            except Exception:
                                pass
                            self._enqueue_passive(
                                "clip_slot_changed",
                                {"has_clip": has},
                                track_index=t_index,
                                clip_index=c_index,
                            )
                            try:
                                self._bind_clip_listeners(t_index, c_index)
                            except Exception:
                                pass
                        return _cb

                    slot_cb = make_slot_cb(track_index, clip_index)
                    slot_cb._passive_target = slot
                    if self._safe_add_listener(slot, "add_has_clip_listener", slot_cb):
                        bindings.append(("add_has_clip_listener", slot_cb))
                    if slot.has_clip:
                        self._bind_clip_listeners(track_index, clip_index, bindings)
            except Exception as e:
                self.log_message("clip slot listeners failed on track %d: %s" % (track_index, str(e)))

            self._passive_track_bindings.append((track, bindings))

    def _bind_clip_listeners(self, track_index, clip_index, bindings=None):
        try:
            track = self._song.tracks[track_index]
            slot = track.clip_slots[clip_index]
            if not slot.has_clip:
                return
            clip = slot.clip

            def on_name():
                name = ""
                try:
                    name = clip.name
                except Exception:
                    pass
                self._enqueue_passive(
                    "clip_name_changed",
                    {"name": name},
                    track_index=track_index,
                    clip_index=clip_index,
                )

            def on_notes():
                self._enqueue_passive(
                    "clip_notes_changed",
                    {},
                    track_index=track_index,
                    clip_index=clip_index,
                )

            def on_playing():
                playing = False
                try:
                    playing = bool(clip.is_playing)
                except Exception:
                    pass
                self._enqueue_passive(
                    "clip_playing_changed",
                    {"is_playing": playing},
                    track_index=track_index,
                    clip_index=clip_index,
                )

            listeners = [("add_name_listener", on_name),
                         ("add_playing_status_listener", on_playing)]
            # Audio clips have no notes; asking logs an error for each one
            if getattr(clip, "is_midi_clip", False):
                listeners.append(("add_notes_listener", on_notes))
            for add_name, cb in listeners:
                cb._passive_target = clip
                if self._safe_add_listener(clip, add_name, cb):
                    if bindings is not None:
                        bindings.append((add_name, cb))
        except Exception as e:
            self.log_message(
                "bind clip listeners %d/%d failed: %s"
                % (track_index, clip_index, str(e))
            )

    # ── Dataset / state snapshot helpers ──────────────────────────────────────

    def _safe_attr(self, obj, attr, cast=None, default=None):
        try:
            val = getattr(obj, attr)
            if callable(val):
                return default
            if cast is not None:
                return cast(val)
            return val
        except Exception:
            return default

    def _notes_from_clip(self, clip):
        """Extract MIDI notes from a clip (incl. MPE/expression when available)."""
        notes = []
        if not clip or not getattr(clip, "is_midi_clip", False):
            return notes

        if hasattr(clip, "get_notes_extended"):
            try:
                raw = clip.get_notes_extended(0, 128, 0.0, float(clip.length) + 1.0)
                for n in raw:
                    entry = {
                        "pitch": int(getattr(n, "pitch", 0)),
                        "start_time": float(getattr(n, "start_time", 0.0)),
                        "duration": float(getattr(n, "duration", 0.0)),
                        "velocity": float(getattr(n, "velocity", 0)),
                        "mute": bool(getattr(n, "mute", False)),
                    }
                    for opt, caster in [
                        ("probability", float),
                        ("velocity_deviation", float),
                        ("release_velocity", float),
                        ("note_id", int),
                    ]:
                        if hasattr(n, opt):
                            try:
                                entry[opt] = caster(getattr(n, opt))
                            except Exception:
                                pass
                    for opt in ("pitch_bend_range", "pressure", "timbre", "slide"):
                        if hasattr(n, opt):
                            try:
                                entry[opt] = float(getattr(n, opt))
                            except Exception:
                                pass
                    notes.append(entry)
                return notes
            except Exception as e:
                self.log_message("get_notes_extended failed, falling back: " + str(e))

        if hasattr(clip, "get_notes"):
            try:
                raw = clip.get_notes(0.0, 0, float(clip.length) + 1.0, 128)
                for n in raw:
                    notes.append({
                        "pitch": int(n[0]),
                        "start_time": float(n[1]),
                        "duration": float(n[2]),
                        "velocity": float(n[3]),
                        "mute": bool(n[4]) if len(n) > 4 else False,
                    })
            except Exception as e:
                self.log_message("get_notes failed: " + str(e))
        return notes

    def _warp_markers_from_clip(self, clip):
        markers = []
        try:
            raw = getattr(clip, "warp_markers", None)
            if not raw:
                return markers
            for m in raw:
                markers.append({
                    "beat_time": float(getattr(m, "beat_time", getattr(m, "time", 0.0))),
                    "sample_time": float(
                        getattr(m, "sample_time", getattr(m, "time", 0.0))
                    ),
                })
        except Exception as e:
            self.log_message("warp_markers read failed: " + str(e))
        return markers

    def _automated_params_for_device(self, device):
        automated = []
        try:
            for param in device.parameters:
                is_auto = False
                try:
                    if hasattr(param, "automation_state"):
                        is_auto = int(param.automation_state) != 0
                    elif hasattr(param, "is_automated"):
                        is_auto = bool(param.is_automated)
                except Exception:
                    continue
                if is_auto:
                    automated.append(param.name)
        except Exception:
            pass
        return automated

    # Racks nest, and a pathological project could nest deeply. Cap the walk so
    # a snapshot can never blow the stack or the payload size.
    _MAX_CHAIN_DEPTH = 4

    def _serialize_device(self, device, device_index, include_params=True, depth=0):
        info = {
            "index": device_index,
            "name": device.name,
            "class_name": device.class_name,
            "type": self._get_device_type(device),
        }
        automated = self._automated_params_for_device(device)
        if automated:
            info["automated_parameters"] = automated
            info["automation_enabled"] = True
        else:
            info["automation_enabled"] = False

        if include_params:
            params = []
            try:
                for p_index, param in enumerate(device.parameters):
                    try:
                        entry = {
                            "index": p_index,
                            "name": param.name,
                            "value": float(param.value),
                            "min": float(param.min),
                            "max": float(param.max),
                            "is_enabled": bool(getattr(param, "is_enabled", True)),
                            "is_quantized": bool(getattr(param, "is_quantized", False)),
                        }
                        if hasattr(param, "value_string"):
                            entry["value_string"] = str(param.value_string)
                        if hasattr(param, "automation_state"):
                            try:
                                entry["automation_state"] = int(param.automation_state)
                            except Exception:
                                pass
                        params.append(entry)
                    except Exception:
                        continue
            except Exception as e:
                self.log_message("Error reading device parameters: " + str(e))
            info["parameters"] = params

        # Devices inside a rack carry the actual sound design — a drum rack's
        # nested Operator, an instrument rack's filter. Without this walk a rack
        # contributes only its 8 macros and the timbral state is invisible.
        if getattr(device, "can_have_chains", False):
            if depth >= self._MAX_CHAIN_DEPTH:
                info["chains_truncated"] = True
            else:
                info["chains"] = self._serialize_chains(
                    device, include_params=include_params, depth=depth
                )
        return info

    def _serialize_chains(self, rack, include_params=True, depth=0):
        chains = []
        try:
            chain_lists = [("chains", getattr(rack, "chains", []))]
            returns = getattr(rack, "return_chains", None)
            if returns:
                chain_lists.append(("return_chains", returns))

            for kind, chain_list in chain_lists:
                for chain_index, chain in enumerate(chain_list):
                    entry = {
                        "index": chain_index,
                        "kind": kind,
                        "chain_name": self._safe_attr(chain, "name", str, ""),
                        "mute": bool(self._safe_attr(chain, "mute", bool, False)),
                        "solo": bool(self._safe_attr(chain, "solo", bool, False)),
                    }
                    try:
                        mixer = chain.mixer_device
                        entry["volume"] = float(mixer.volume.value)
                        entry["panning"] = float(mixer.panning.value)
                    except Exception:
                        pass

                    # Drum racks expose the pad's note, which is what ties a
                    # nested device back to the kick/snare/hat it voices.
                    note = self._safe_attr(chain, "out_note", int, None)
                    if note is not None:
                        entry["out_note"] = note

                    nested = []
                    try:
                        for d_i, dev in enumerate(chain.devices):
                            nested.append(
                                self._serialize_device(
                                    dev,
                                    d_i,
                                    include_params=include_params,
                                    depth=depth + 1,
                                )
                            )
                    except Exception as e:
                        self.log_message("Error reading chain devices: " + str(e))
                    entry["devices"] = nested
                    chains.append(entry)
        except Exception as e:
            self.log_message("Error serializing rack chains: " + str(e))
        return chains

    def _serialize_clip_common(self, clip):
        info = {
            "looping": bool(self._safe_attr(clip, "looping", bool, False)),
            "loop_start": self._safe_attr(clip, "loop_start", float, None),
            "loop_end": self._safe_attr(clip, "loop_end", float, None),
            "warping": bool(self._safe_attr(clip, "warping", bool, False)),
            "warp_mode": self._safe_attr(clip, "warp_mode", int, None),
            "gain": self._safe_attr(clip, "gain", float, None),
            "pitch_coarse": self._safe_attr(clip, "pitch_coarse", int, None),
            "pitch_fine": self._safe_attr(clip, "pitch_fine", int, None),
            "launch_mode": self._safe_attr(clip, "launch_mode", int, None),
        }
        for attr in ("file_path", "file_path_relative"):
            path = self._safe_attr(clip, attr, str, None)
            if path:
                info["file_path"] = path
                break
        markers = self._warp_markers_from_clip(clip)
        if markers:
            info["warp_markers"] = markers
            info["warp_marker_count"] = len(markers)
        return dict((k, v) for k, v in info.items() if v is not None)

    def _serialize_session_clip(self, clip, include_notes=True):
        info = {
            "name": clip.name,
            "length": float(clip.length),
            "is_playing": bool(clip.is_playing),
            "is_recording": bool(getattr(clip, "is_recording", False)),
            "is_midi_clip": bool(getattr(clip, "is_midi_clip", False)),
            "is_audio_clip": bool(getattr(clip, "is_audio_clip", False)),
            "color": int(getattr(clip, "color", 0)),
        }
        info.update(self._serialize_clip_common(clip))
        if include_notes and info["is_midi_clip"]:
            info["notes"] = self._notes_from_clip(clip)
            info["note_count"] = len(info["notes"])
        return info

    def _serialize_arrangement_clip(self, clip, include_notes=True):
        info = {
            "name": clip.name,
            "start_time": float(clip.start_time),
            "end_time": float(clip.end_time),
            "length": float(clip.length),
            "color": int(getattr(clip, "color", 0)),
            "is_midi_clip": bool(getattr(clip, "is_midi_clip", False)),
            "is_audio_clip": bool(getattr(clip, "is_audio_clip", False)),
            "is_playing": bool(getattr(clip, "is_playing", False)),
        }
        info.update(self._serialize_clip_common(clip))
        if include_notes and info["is_midi_clip"]:
            info["notes"] = self._notes_from_clip(clip)
            info["note_count"] = len(info["notes"])
        return info

    def _serialize_sends(self, track):
        sends = []
        try:
            for i, send in enumerate(track.mixer_device.sends):
                sends.append({
                    "index": i,
                    "value": float(send.value),
                    "name": str(getattr(send, "name", "Send %d" % i)),
                })
        except Exception:
            pass
        return sends

    def _serialize_scenes(self):
        scenes = []
        try:
            for i, scene in enumerate(self._song.scenes):
                scenes.append({
                    "index": i,
                    "name": str(scene.name),
                    "tempo": self._safe_attr(scene, "tempo", float, None),
                    "is_triggered": bool(self._safe_attr(scene, "is_triggered", bool, False)),
                })
        except Exception as e:
            self.log_message("scenes serialize failed: " + str(e))
        return scenes

    def _serialize_cue_points(self):
        cues = []
        try:
            for cue in self._song.cue_points:
                cues.append({
                    "name": str(getattr(cue, "name", "")),
                    "time": float(getattr(cue, "time", 0.0)),
                })
        except Exception as e:
            self.log_message("cue_points serialize failed: " + str(e))
        return cues

    def _serialize_return_tracks(self, include_params=True):
        returns = []
        try:
            for i, track in enumerate(self._song.return_tracks):
                devices = []
                for d_i, device in enumerate(track.devices):
                    devices.append(
                        self._serialize_device(device, d_i, include_params=include_params)
                    )
                returns.append({
                    "index": i,
                    "name": track.name,
                    "mute": bool(track.mute),
                    "solo": bool(track.solo),
                    "volume": float(track.mixer_device.volume.value),
                    "panning": float(track.mixer_device.panning.value),
                    "devices": devices,
                })
        except Exception as e:
            self.log_message("return_tracks serialize failed: " + str(e))
        return returns

    def _serialize_master_track(self, include_params=True):
        """Master chain — the bus compressor/limiter that shapes the final sound."""
        try:
            track = self._song.master_track
            devices = []
            for d_i, device in enumerate(track.devices):
                devices.append(
                    self._serialize_device(device, d_i, include_params=include_params)
                )
            return {
                "volume": float(track.mixer_device.volume.value),
                "panning": float(track.mixer_device.panning.value),
                "devices": devices,
            }
        except Exception as e:
            self.log_message("master_track serialize failed: " + str(e))
            return None

    def _get_clip_notes(self, track_index, clip_index):
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            track = self._song.tracks[track_index]
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            slot = track.clip_slots[clip_index]
            if not slot.has_clip:
                raise Exception("No clip in slot")
            clip = slot.clip
            if not getattr(clip, "is_midi_clip", False):
                raise Exception("Clip is not a MIDI clip")
            notes = self._notes_from_clip(clip)
            return {
                "track_index": track_index,
                "clip_index": clip_index,
                "clip_name": clip.name,
                "length": float(clip.length),
                "note_count": len(notes),
                "notes": notes,
            }
        except Exception as e:
            self.log_message("Error getting clip notes: " + str(e))
            raise

    def _get_device_parameters(self, track_index, device_index):
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            track = self._song.tracks[track_index]
            if device_index < 0 or device_index >= len(track.devices):
                raise IndexError("Device index out of range")
            device = track.devices[device_index]
            return {
                "track_index": track_index,
                "device": self._serialize_device(device, device_index, include_params=True),
            }
        except Exception as e:
            self.log_message("Error getting device parameters: " + str(e))
            raise

    def _get_session_snapshot(self, include_notes=True, include_params=True):
        """Full v2 project state dump for trajectory dataset recording."""
        try:
            session = self._get_session_info()
            tracks = []
            for track_index, track in enumerate(self._song.tracks):
                clip_slots = []
                for slot_index, slot in enumerate(track.clip_slots):
                    clip_info = None
                    if slot.has_clip:
                        clip_info = self._serialize_session_clip(
                            slot.clip, include_notes=include_notes
                        )
                    clip_slots.append({
                        "index": slot_index,
                        "has_clip": bool(slot.has_clip),
                        "clip": clip_info,
                    })

                devices = []
                for device_index, device in enumerate(track.devices):
                    devices.append(
                        self._serialize_device(
                            device, device_index, include_params=include_params
                        )
                    )

                arrangement_clips = []
                try:
                    for clip in track.arrangement_clips:
                        arrangement_clips.append(
                            self._serialize_arrangement_clip(
                                clip, include_notes=include_notes
                            )
                        )
                except Exception as e:
                    self.log_message(
                        "arrangement_clips unavailable on track %d: %s"
                        % (track_index, str(e))
                    )

                tracks.append({
                    "index": track_index,
                    "name": track.name,
                    "is_audio_track": bool(track.has_audio_input),
                    "is_midi_track": bool(track.has_midi_input),
                    "mute": bool(track.mute),
                    "solo": bool(track.solo),
                    "arm": self._safe_arm(track),
                    "volume": float(track.mixer_device.volume.value),
                    "panning": float(track.mixer_device.panning.value),
                    "sends": self._serialize_sends(track),
                    "clip_slots": clip_slots,
                    "devices": devices,
                    "arrangement_clips": arrangement_clips,
                })

            return {
                "schema": "ableton_mcp_snapshot_v2",
                "session": session,
                "tracks": tracks,
                "scenes": self._serialize_scenes(),
                "return_tracks": self._serialize_return_tracks(
                    include_params=include_params
                ),
                "master_track": self._serialize_master_track(
                    include_params=include_params
                ),
                "cue_points": self._serialize_cue_points(),
                "include_notes": bool(include_notes),
                "include_params": bool(include_params),
            }
        except Exception as e:
            self.log_message("Error getting session snapshot: " + str(e))
            raise

    def _set_device_parameter(self, track_index, device_index, parameter_index, value):
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            track = self._song.tracks[track_index]
            if device_index < 0 or device_index >= len(track.devices):
                raise IndexError("Device index out of range")
            device = track.devices[device_index]
            if parameter_index < 0 or parameter_index >= len(device.parameters):
                raise IndexError("Parameter index out of range")
            param = device.parameters[parameter_index]
            old = float(param.value)
            param.value = float(value)
            return {
                "track_index": track_index,
                "device_index": device_index,
                "parameter_index": parameter_index,
                "name": param.name,
                "old_value": old,
                "value": float(param.value),
                "min": float(param.min),
                "max": float(param.max),
            }
        except Exception as e:
            self.log_message("Error setting device parameter: " + str(e))
            raise

    def get_browser_tree(self, category_type="all"):
        """
        Get a simplified tree of browser categories.
        
        Args:
            category_type: Type of categories to get ('all', 'instruments', 'sounds', etc.)
            
        Returns:
            Dictionary with the browser tree structure
        """
        try:
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            if not app:
                raise RuntimeError("Could not access Live application")
                
            # Check if browser is available
            if not hasattr(app, 'browser') or app.browser is None:
                raise RuntimeError("Browser is not available in the Live application")
            
            # Log available browser attributes to help diagnose issues
            browser_attrs = [attr for attr in dir(app.browser) if not attr.startswith('_')]
            self.log_message("Available browser attributes: {0}".format(browser_attrs))
            
            result = {
                "type": category_type,
                "categories": [],
                "available_categories": browser_attrs
            }
            
            # The full browser is far too large to serialise, so descend one
            # level and mark anything deeper with has_more. The MCP server
            # renders that as "[...]" and get_browser_items_at_path walks on
            # from there.
            max_depth = 1
            max_children = 64
            folder_count = [0]

            def process_item(item, depth=0):
                if not item:
                    return None

                children = getattr(item, 'children', None) or []
                is_folder = bool(children)
                if is_folder:
                    folder_count[0] += 1

                node = {
                    "name": item.name if hasattr(item, 'name') else "Unknown",
                    "is_folder": is_folder,
                    "is_device": hasattr(item, 'is_device') and item.is_device,
                    "is_loadable": hasattr(item, 'is_loadable') and item.is_loadable,
                    "uri": item.uri if hasattr(item, 'uri') else None,
                    "children": []
                }

                if is_folder and depth >= max_depth:
                    node["has_more"] = True
                    return node

                for child in children[:max_children]:
                    try:
                        processed = process_item(child, depth + 1)
                    except Exception as e:
                        self.log_message("Error processing browser child: {0}".format(str(e)))
                        continue
                    if processed:
                        node["children"].append(processed)
                if len(children) > max_children:
                    node["has_more"] = True

                return node

            # Process based on category type and available attributes
            if (category_type == "all" or category_type == "instruments") and hasattr(app.browser, 'instruments'):
                try:
                    instruments = process_item(app.browser.instruments)
                    if instruments:
                        instruments["name"] = "Instruments"  # Ensure consistent naming
                        result["categories"].append(instruments)
                except Exception as e:
                    self.log_message("Error processing instruments: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "sounds") and hasattr(app.browser, 'sounds'):
                try:
                    sounds = process_item(app.browser.sounds)
                    if sounds:
                        sounds["name"] = "Sounds"  # Ensure consistent naming
                        result["categories"].append(sounds)
                except Exception as e:
                    self.log_message("Error processing sounds: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "drums") and hasattr(app.browser, 'drums'):
                try:
                    drums = process_item(app.browser.drums)
                    if drums:
                        drums["name"] = "Drums"  # Ensure consistent naming
                        result["categories"].append(drums)
                except Exception as e:
                    self.log_message("Error processing drums: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "audio_effects") and hasattr(app.browser, 'audio_effects'):
                try:
                    audio_effects = process_item(app.browser.audio_effects)
                    if audio_effects:
                        audio_effects["name"] = "Audio Effects"  # Ensure consistent naming
                        result["categories"].append(audio_effects)
                except Exception as e:
                    self.log_message("Error processing audio_effects: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "midi_effects") and hasattr(app.browser, 'midi_effects'):
                try:
                    midi_effects = process_item(app.browser.midi_effects)
                    if midi_effects:
                        midi_effects["name"] = "MIDI Effects"
                        result["categories"].append(midi_effects)
                except Exception as e:
                    self.log_message("Error processing midi_effects: {0}".format(str(e)))
            
            # Try to process other potentially available categories
            for attr in browser_attrs:
                if attr not in ['instruments', 'sounds', 'drums', 'audio_effects', 'midi_effects'] and \
                   (category_type == "all" or category_type == attr):
                    try:
                        item = getattr(app.browser, attr)
                        if hasattr(item, 'children') or hasattr(item, 'name'):
                            category = process_item(item)
                            if category:
                                category["name"] = attr.capitalize()
                                result["categories"].append(category)
                    except Exception as e:
                        self.log_message("Error processing {0}: {1}".format(attr, str(e)))
            
            # The MCP server reports this back to the caller; without it the
            # header always claimed zero folders.
            result["total_folders"] = folder_count[0]

            self.log_message("Browser tree generated for {0} with {1} root categories".format(
                category_type, len(result['categories'])))
            return result
            
        except Exception as e:
            self.log_message("Error getting browser tree: {0}".format(str(e)))
            self.log_message(traceback.format_exc())
            raise
    
    def get_browser_items_at_path(self, path):
        """
        Get browser items at a specific path.
        
        Args:
            path: Path in the format "category/folder/subfolder"
                 where category is one of: instruments, sounds, drums, audio_effects, midi_effects
                 or any other available browser category
                 
        Returns:
            Dictionary with items at the specified path
        """
        try:
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            if not app:
                raise RuntimeError("Could not access Live application")
                
            # Check if browser is available
            if not hasattr(app, 'browser') or app.browser is None:
                raise RuntimeError("Browser is not available in the Live application")
            
            # Log available browser attributes to help diagnose issues
            browser_attrs = [attr for attr in dir(app.browser) if not attr.startswith('_')]
            self.log_message("Available browser attributes: {0}".format(browser_attrs))
                
            # Parse the path
            path_parts = path.split("/")
            if not path_parts:
                raise ValueError("Invalid path")
            
            # Determine the root category
            root_category = path_parts[0].lower()
            current_item = None
            
            # Check standard categories first
            if root_category == "instruments" and hasattr(app.browser, 'instruments'):
                current_item = app.browser.instruments
            elif root_category == "sounds" and hasattr(app.browser, 'sounds'):
                current_item = app.browser.sounds
            elif root_category == "drums" and hasattr(app.browser, 'drums'):
                current_item = app.browser.drums
            elif root_category == "audio_effects" and hasattr(app.browser, 'audio_effects'):
                current_item = app.browser.audio_effects
            elif root_category == "midi_effects" and hasattr(app.browser, 'midi_effects'):
                current_item = app.browser.midi_effects
            else:
                # Try to find the category in other browser attributes
                found = False
                for attr in browser_attrs:
                    if attr.lower() == root_category:
                        try:
                            current_item = getattr(app.browser, attr)
                            found = True
                            break
                        except Exception as e:
                            self.log_message("Error accessing browser attribute {0}: {1}".format(attr, str(e)))
                
                if not found:
                    # If we still haven't found the category, return available categories
                    return {
                        "path": path,
                        "error": "Unknown or unavailable category: {0}".format(root_category),
                        "available_categories": browser_attrs,
                        "items": []
                    }
            
            # Navigate through the path
            for i in range(1, len(path_parts)):
                part = path_parts[i]
                if not part:  # Skip empty parts
                    continue
                
                if not hasattr(current_item, 'children'):
                    return {
                        "path": path,
                        "error": "Item at '{0}' has no children".format('/'.join(path_parts[:i])),
                        "items": []
                    }
                
                found = False
                for child in current_item.children:
                    if hasattr(child, 'name') and child.name.lower() == part.lower():
                        current_item = child
                        found = True
                        break
                
                if not found:
                    return {
                        "path": path,
                        "error": "Path part '{0}' not found".format(part),
                        "items": []
                    }
            
            # Get items at the current path
            items = []
            if hasattr(current_item, 'children'):
                for child in current_item.children:
                    item_info = {
                        "name": child.name if hasattr(child, 'name') else "Unknown",
                        "is_folder": hasattr(child, 'children') and bool(child.children),
                        "is_device": hasattr(child, 'is_device') and child.is_device,
                        "is_loadable": hasattr(child, 'is_loadable') and child.is_loadable,
                        "uri": child.uri if hasattr(child, 'uri') else None
                    }
                    items.append(item_info)
            
            result = {
                "path": path,
                "name": current_item.name if hasattr(current_item, 'name') else "Unknown",
                "uri": current_item.uri if hasattr(current_item, 'uri') else None,
                "is_folder": hasattr(current_item, 'children') and bool(current_item.children),
                "is_device": hasattr(current_item, 'is_device') and current_item.is_device,
                "is_loadable": hasattr(current_item, 'is_loadable') and current_item.is_loadable,
                "items": items
            }
            
            self.log_message("Retrieved {0} items at path: {1}".format(len(items), path))
            return result
            
        except Exception as e:
            self.log_message("Error getting browser items at path: {0}".format(str(e)))
            self.log_message(traceback.format_exc())
            raise
