"""Notes that outlast a conversation (MCP_Server/memory.py) and the memory tool."""

import pytest

from MCP_Server import memory


def test_keyed_notes_replace_and_plain_ones_do_not_repeat():
    notes, outcome = memory.add([], "Reference: Burial – Archangel", key="reference")
    assert outcome == "added" and notes[0]["key"] == "reference"
    notes, outcome = memory.add(notes, "Reference: Four Tet – Baby", key="reference")
    assert outcome == "replaced" and [n["text"] for n in notes] == ["Reference: Four Tet – Baby"]
    notes, _ = memory.add(notes, "Kick sits at -6 dB")
    notes, outcome = memory.add(notes, "Kick sits at -6 dB")
    assert outcome == "already known" and len(notes) == 2
    with pytest.raises(ValueError):
        memory.add(notes, "   ")


def test_forget_by_key_or_words_and_keep_the_newest():
    notes = []
    for i in range(memory.LIMIT + 5):
        notes, _ = memory.add(notes, f"note {i}")
    assert len(notes) == memory.LIMIT and notes[0]["text"] == "note 5"
    notes, _ = memory.add(notes, "Vocal is the lead, keep it at -3 dB", key="vocal")
    left, removed = memory.forget(notes, text="VOCAL lead")
    assert [n["key"] for n in removed] == ["vocal"] and len(left) == memory.LIMIT - 1
    left, removed = memory.forget(notes, key="vocal")
    assert len(removed) == 1
    with pytest.raises(ValueError):
        memory.forget(notes)
    assert memory.brief(notes, 2) == ["note 64", "vocal: Vocal is the lead, keep it at -3 dB"]


def test_what_the_set_and_the_file_hold_is_read_back(tmp_path, monkeypatch):
    assert memory.parse("") == [] and memory.parse(None) == []
    assert memory.parse("plain words") == [{"text": "plain words", "at": ""}]
    assert memory.parse('[{"text": "a"}, 3, {"no": "text"}]') == [{"text": "a"}]
    monkeypatch.setenv("ABLETON_MCP_MEMORY_FILE", str(tmp_path / "sub" / "memory.json"))
    assert memory.load_user() == []
    memory.save_user([{"text": "Prefers warm, tape-saturated drums", "at": "x"}])
    assert memory.load_user()[0]["text"].startswith("Prefers warm")
