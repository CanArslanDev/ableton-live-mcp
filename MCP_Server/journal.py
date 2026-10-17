"""What the agent changed in Live during this server's life, to list and undo.

The Remote Script wraps every modifying command (a batch is one command) in
begin/end_undo_step. Measured on Live 12.4, what that leaves in Live's undo
history:

- one step for the command's parameter changes (device parameters, mixer,
  tempo, mute) and one for its other changes (names, colors, clips, notes,
  devices, tracks): a batch mixing both takes two undos;
- no step for a value set to what it already was, nor for transport, the
  playhead, selection, loop, metronome, arm, solo or the record, punch,
  overdub and automation-arm buttons;
- while the Arrangement records, a step for every value set, plus one for
  the pass; the first undo takes the recorded automation, the rest are empty
  (record_automation counts them and reports the total).

`register_tool` opens an entry for each tool call, the connection reports each
command with its result (`note_command`), `undo_steps` counts the steps by
those rules, and agent_changes(undo=N) undoes exactly that many. Live also
merges two changes of one parameter made within a few hundred ms (a knob
turn); tool calls are further apart than that.

Live's undo history is shared with the user, so an undo after the user edited
something takes their edit back first; tools that change parameters also keep
an automatic snapshot, which survives that (see extended_tools.auto_snapshot).
"""

from __future__ import annotations

import contextvars
import json
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

# The Remote Script's READ_ONLY_COMMANDS and READ_ONLY_METHODS (a test keeps
# them in step): these never open an undo step.
READ_ONLY_COMMANDS = frozenset([
    "get_session_info", "get_track_info", "get_browser_item", "get_browser_tree",
    "get_browser_items_at_path", "get_arrangement_clips", "get_clip_notes",
    "get_device_parameters", "get_session_snapshot", "inspect_rack", "lom_get",
    "clip_get_notes", "get_session_overview", "list_observers", "observe", "unobserve",
    "hello", "get_script_info", "poll_events", "browser_search",
])
READ_ONLY_METHODS = frozenset([
    "str_for_value", "available_main_views", "value_at_time", "events_in_range",
    "note_number_to_name", "find_device_position", "beat_to_sample_time",
    "sample_to_beat_time", "seconds_to_sample_time",
])


# Properties and methods whose changes Live keeps out of the undo history
# (measured on 12.4: transport, playhead, selection, loop, metronome, arm,
# solo, the record, punch, overdub and automation-arm buttons, back to
# arrangement; the other view state by the same rule)
NO_UNDO_PROPERTIES = frozenset([
    "is_playing", "current_song_time", "start_time", "loop", "metronome", "arm", "solo", "record_mode",
    "punch_in", "punch_out", "session_automation_record", "arrangement_overdub",
    "session_record", "back_to_arranger",
    "selected_track", "selected_scene", "selected_parameter", "selected_chain",
    "selected_drum_pad", "detail_clip", "highlighted_clip_slot", "follow_song", "draw_mode",
])
NO_UNDO_METHODS = frozenset([
    "start_playing", "stop_playing", "continue_playing", "stop_all_clips", "play_selection",
    "fire", "stop", "set_fire_button_state", "jump_by", "scrub_by", "jump_to_next_cue",
    "jump_to_prev_cue", "preview_item", "stop_preview", "select_device", "undo", "redo",
    "set_data",
    "show_view", "hide_view", "focus_view", "scroll_view", "zoom_view",
])
NO_UNDO_COMMANDS = frozenset([
    "fire_clip", "stop_clip", "start_playback", "stop_playback",
    "switch_to_arrangement_view", "set_current_song_time",
])
# Changes Live files as parameter changes (a step apart from the others)
PARAMETER_PROPERTIES = frozenset(["value", "tempo", "mute"])
PARAMETER_COMMANDS = frozenset(["param_set_display", "set_tempo", "set_device_parameter"])


def is_read_only(command: str, params: dict | None) -> bool:
    """Whether the Remote Script runs this command without an undo step."""
    params = params or {}
    if command in READ_ONLY_COMMANDS:
        return True
    if command == "lom_call":
        method = str(params.get("method") or "")
        return method in READ_ONLY_METHODS or method.startswith(("get_", "is_"))
    if command == "batch":
        commands = params.get("commands") or []
        return bool(commands) and all(
            isinstance(c, dict) and c.get("type") != "batch"
            and is_read_only(c.get("type", ""), c.get("params") or {}) for c in commands)
    return False


def undo_steps(command: str, params: dict | None, result: Any = None) -> int:
    """How many steps one command left in Live's undo history (0, 1 or 2)."""
    kinds: set[str] = set()
    _change_kinds(command, params or {}, result, kinds)
    return len(kinds)


def _change_kinds(command: str, params: dict, result: Any, kinds: set[str]) -> None:
    if command in NO_UNDO_COMMANDS or is_read_only(command, params):
        return
    outcome = result if isinstance(result, dict) else {}
    if command == "batch":
        steps = [s for s in params.get("commands") or [] if isinstance(s, dict)]
        ran = outcome.get("results")
        for index, step in enumerate(steps):
            done = ran[index] if isinstance(ran, list) and index < len(ran) else None
            if isinstance(ran, list) and (not isinstance(done, dict)
                                          or done.get("status") != "success"):
                continue  # refused, or never ran after an earlier failure
            _change_kinds(str(step.get("type", "")), step.get("params") or {},
                          (done or {}).get("result"), kinds)
        return
    if command == "lom_set":
        prop = str(params.get("property") or "")
        if prop in NO_UNDO_PROPERTIES:
            return
        if "old_value" in outcome and outcome.get("old_value") == outcome.get("value"):
            return  # set to what it already was
        kinds.add("parameter" if prop in PARAMETER_PROPERTIES else "other")
        return
    if command == "param_set_display" and "old" in outcome and outcome.get("old") == outcome.get("new"):
        return
    if command == "lom_call" and str(params.get("method") or "") in NO_UNDO_METHODS:
        return
    kinds.add("parameter" if command in PARAMETER_COMMANDS else "other")


@dataclass
class Entry:
    tool: str
    arguments: str
    at: float
    steps: int = 0
    undone: bool = False
    snapshot: str | None = None

    def describe(self) -> dict:
        item: dict[str, Any] = {"tool": self.tool, "arguments": self.arguments,
                                "at": time.strftime("%H:%M:%S", time.localtime(self.at)),
                                "undo_steps": self.steps}
        if self.snapshot:
            item["snapshot_before"] = self.snapshot
        if self.undone:
            item["undone"] = True
        return item


_current: contextvars.ContextVar[Entry | None] = contextvars.ContextVar("abletonmcp_journal",
                                                                         default=None)
_entries: deque[Entry] = deque(maxlen=200)
_lock = threading.Lock()


def _summary(arguments: dict) -> str:
    shown = {k: v for k, v in arguments.items() if k != "ctx" and v is not None}
    text = json.dumps(shown, ensure_ascii=False, default=str, separators=(",", ":"))
    return text if len(text) <= 160 else text[:157] + "…"


@contextmanager
def recording(tool: str, arguments: dict) -> Iterator[Entry]:
    """One tool call: commands sent while it runs are counted on its entry."""
    entry = Entry(tool, _summary(arguments), time.time())
    token = _current.set(entry)
    try:
        yield entry
    finally:
        _current.reset(token)
        if entry.steps or entry.snapshot:
            with _lock:
                _entries.append(entry)


@contextmanager
def not_recorded() -> Iterator[None]:
    """For commands that must not count as a change (undoing one)."""
    token = _current.set(None)
    try:
        yield
    finally:
        _current.reset(token)


def note_command(command: str, params: dict | None, result: Any = None) -> None:
    """Called by the connection after Live ran a command."""
    entry = _current.get()
    if entry is not None:
        entry.steps += undo_steps(command, params, result)


def add_steps(count: int) -> None:
    """For a tool that knows its undo steps better than its commands tell
    (a recorded automation pass is one step, whatever it sent)."""
    entry = _current.get()
    if entry is not None:
        entry.steps += count


def attach_snapshot(name: str) -> None:
    entry = _current.get()
    if entry is not None:
        entry.snapshot = name


def recent(limit: int = 20) -> list[Entry]:
    with _lock:
        return list(_entries)[-limit:][::-1]


def undoable(count: int) -> list[Entry]:
    """The newest `count` changes not yet undone, newest first. Changes undone
    here are skipped: their steps left Live's undo history (to the redo
    side, and are gone once anything new is done)."""
    picked: list[Entry] = []
    with _lock:
        for entry in reversed(_entries):
            if len(picked) >= count:
                break
            if entry.steps and not entry.undone:
                picked.append(entry)
    return picked


def clear() -> None:
    with _lock:
        _entries.clear()
