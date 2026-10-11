"""Notes that outlast a conversation.

Two places: what this song is and what was decided about it ("reference:
Burial – Archangel", "kick at -6 dB") is kept inside the Live set, so it
travels with the set and comes back when it is opened; the user's own taste
and habits are kept in ~/.ableton-mcp/memory.json, for every set.

Measured on Live 12.4: Song.set_data/get_data keep strings, numbers, lists
and dicts in the set (saved with it) and leave no step in the undo history.
A note is {"text", "key"?, "at"}; a keyed note replaces the earlier one with
that key, so decisions can be updated rather than piled up.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

SET_KEY = "ableton_mcp_memory"
LIMIT = 60  # notes per place; the oldest go first


def user_file() -> Path:
    override = os.environ.get("ABLETON_MCP_MEMORY_FILE")
    return Path(override) if override else Path.home() / ".ableton-mcp" / "memory.json"


def parse(raw: Any) -> list[dict]:
    """Notes from what get_data or the file held (a JSON string or a list)."""
    if isinstance(raw, str):
        if not raw.strip():
            return []
        try:
            raw = json.loads(raw)
        except ValueError:
            return [{"text": raw, "at": ""}]  # something else wrote plain text there
    if not isinstance(raw, list):
        return []
    return [n for n in raw if isinstance(n, dict) and isinstance(n.get("text"), str)]


def dumps(notes: list[dict]) -> str:
    return json.dumps(notes, ensure_ascii=False, separators=(",", ":"))


def load_user() -> list[dict]:
    try:
        return parse(user_file().read_text(encoding="utf-8"))
    except OSError:
        return []


def save_user(notes: list[dict]) -> None:
    path = user_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(dumps(notes), encoding="utf-8")
    os.replace(temp, path)


def add(notes: list[dict], text: str, key: str | None = None) -> tuple[list[dict], str]:
    """(notes, what happened): a keyed note replaces that key's; the same text
    is not stored twice; past LIMIT the oldest note goes."""
    text = text.strip()
    if not text:
        raise ValueError("Nothing to remember: give text")
    note: dict[str, Any] = {"text": text, "at": time.strftime("%Y-%m-%d %H:%M")}
    if key:
        note["key"] = key.strip()
    kept = [n for n in notes if not (key and n.get("key") == note.get("key"))]
    outcome = "replaced" if len(kept) < len(notes) else "added"
    if any(n["text"] == text and n.get("key") == note.get("key") for n in kept):
        return notes, "already known"
    kept.append(note)
    return kept[-LIMIT:], outcome


def forget(notes: list[dict], key: str | None = None, text: str | None = None
           ) -> tuple[list[dict], list[dict]]:
    """(the notes left, the ones removed): by key, or every note containing
    all the words of `text`."""
    if not key and not (text and text.strip()):
        raise ValueError("Say what to forget: a key, or words of the note")
    words = (text or "").lower().split()

    def matches(note: dict) -> bool:
        if key:
            return note.get("key") == key.strip()
        return all(word in note["text"].lower() for word in words)
    removed = [n for n in notes if matches(n)]
    return [n for n in notes if not matches(n)], removed


def brief(notes: list[dict], limit: int = 12) -> list[str]:
    """The newest notes as lines, for the overview."""
    return [f"{n['key']}: {n['text']}" if n.get("key") else n["text"] for n in notes[-limit:]]
