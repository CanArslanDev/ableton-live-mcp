"""What the agent changed in Live during this server's life, to list and undo.

The Remote Script runs every modifying command as its own step in Live's undo
history (begin/end_undo_step around it; a batch is one command). Measured on
Live 12.4: a tool call that sent N modifying commands is taken back by exactly
N undos, mixer, notes, devices and chains alike. So `register_tool` opens an
entry for each tool call, the connection counts the modifying commands sent
inside it, and agent_changes(undo=N) undoes exactly those steps.

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
    "hello", "get_script_info", "drain_passive_events", "poll_events", "browser_search",
])
READ_ONLY_METHODS = frozenset([
    "str_for_value", "available_main_views", "value_at_time", "events_in_range",
    "note_number_to_name", "find_device_position", "beat_to_sample_time",
    "sample_to_beat_time", "seconds_to_sample_time",
])


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


def note_command(command: str, params: dict | None) -> None:
    """Called by the connection after Live ran a command."""
    entry = _current.get()
    if entry is not None and not is_read_only(command, params):
        entry.steps += 1


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
