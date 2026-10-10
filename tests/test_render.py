"""render_and_analyze keeps track of its temporary track by id, not index.

The section plays in real time, so the user can add or remove tracks while it
records; an index taken before the recording would then point at their track.
"""

import asyncio
import json

import numpy as np
import pytest
import soundfile as sf
from mcp.server.fastmcp import FastMCP

from MCP_Server import extended_tools

RENDER_ID = 999


class ScriptedLive:
    """Answers the commands render_and_analyze sends, like the Remote Script would."""

    def __init__(self, wav_path, setup_fails=False):
        self.tracks = [
            {"name": "Drums", "$id": 101, "can_be_armed": True, "arm": True},
            {"name": "Bass", "$id": 102, "can_be_armed": True, "arm": False},
        ]
        self.wav_path = str(wav_path)
        self.setup_fails = setup_fails
        self.sent = []
        self.deleted = []

    def send_command(self, command, params=None):
        params = params or {}
        self.sent.append((command, params))
        if command == "lom_get":
            path, props = params["path"], params.get("properties") or []
            if path == "live_set" and "tempo" in props:
                return {"tempo": 120.0, "loop": True, "loop_start": 0.0, "metronome": True,
                        "record_mode": False, "is_playing": False, "current_song_time": 4.0}
            if path == "live_set":
                return {"current_song_time": 10_000.0}
            if path == "live_set tracks":
                return {"items": [dict(t) for t in self.tracks], "length": len(self.tracks)}
            if path == f"id {RENDER_ID} arrangement_clips":
                return {"items": [{"file_path": self.wav_path}], "length": 1}
            raise AssertionError(f"unexpected lom_get {path}")
        if command == "batch":
            commands = params["commands"]
            results = []
            if commands[0]["params"].get("method") == "create_audio_track":
                self.tracks.append({"name": "MCP Render", "$id": RENDER_ID,
                                    "can_be_armed": True, "arm": False})
                results = [{"index": 0, "type": "lom_call", "status": "success",
                            "result": {"method": "create_audio_track", "result": None}},
                           {"index": 1, "type": "lom_get", "status": "success",
                            "result": {"name": "Audio", "$id": RENDER_ID}}]
                if self.setup_fails:
                    results.append({"index": 2, "type": "lom_set", "status": "error",
                                    "message": "No item named 'Nope'"})
                    return {"results": results, "completed": 2, "failed": 1,
                            "total": len(commands)}
                # While the section records, the user inserts a track at the top
                self.tracks.insert(0, {"name": "User's new track", "$id": 555,
                                       "can_be_armed": True, "arm": False})
            results += [{"index": i, "type": c["type"], "status": "success", "result": {}}
                        for i, c in enumerate(commands[len(results):], start=len(results))]
            return {"results": results, "completed": len(results), "failed": 0,
                    "total": len(commands)}
        if command == "lom_call" and params["method"] == "delete_track":
            index = params["args"][0]
            self.deleted.append(self.tracks.pop(index)["$id"])
            return {"method": "delete_track", "result": None}
        if command == "lom_call":
            return {"method": params["method"], "result": None}
        raise AssertionError(f"unexpected command {command}")

    def batches(self):
        return [p["commands"] for c, p in self.sent if c == "batch"]


def _render(live, **arguments):
    mcp = FastMCP("t")
    extended_tools.register(mcp, lambda: live)
    content = asyncio.run(mcp.call_tool("render_and_analyze", arguments))
    blocks = content[0] if isinstance(content, tuple) else content
    return json.loads(blocks[0].text)


@pytest.fixture
def wav(tmp_path):
    path = tmp_path / "render.wav"
    t = np.arange(48000) / 48000
    sf.write(path, np.stack([0.25 * np.sin(2 * np.pi * 220 * t)] * 2, axis=1), 48000)
    return path


def test_temporary_track_is_deleted_by_id_after_tracks_shift(wav):
    live = ScriptedLive(wav)
    result = _render(live, length_beats=4)
    assert result["render"]["track_kept"] is False
    # The render track sat at index 2 when created; the user's insert moved it to 3
    assert live.deleted == [RENDER_ID]
    assert [t["$id"] for t in live.tracks] == [555, 101, 102]


def test_arm_states_are_restored_by_id(wav):
    live = ScriptedLive(wav)
    _render(live, length_beats=4)
    setup, restore = live.batches()
    assert {"type": "lom_set", "params": {"path": "id 101", "property": "arm",
                                          "value": False}} in setup
    assert {"type": "lom_set", "params": {"path": "id 101", "property": "arm",
                                          "value": True}} in restore
    assert {"type": "lom_set", "params": {"path": f"id {RENDER_ID}", "property": "arm",
                                          "value": False}} in restore


def test_failed_setup_still_removes_the_temporary_track(wav):
    from mcp.server.fastmcp.exceptions import ToolError

    live = ScriptedLive(wav, setup_fails=True)
    with pytest.raises(ToolError, match="Render setup failed"):
        _render(live, length_beats=4, source="Nope")
    assert live.deleted == [RENDER_ID]


def test_unreadable_recording_still_removes_the_temporary_track(tmp_path):
    from mcp.server.fastmcp.exceptions import ToolError

    broken = tmp_path / "broken.wav"
    broken.write_bytes(b"not audio at all")
    live = ScriptedLive(broken)
    with pytest.raises(ToolError):
        _render(live, length_beats=4)
    assert live.deleted == [RENDER_ID]


def test_keep_track_leaves_it_in_place(wav):
    live = ScriptedLive(wav)
    result = _render(live, length_beats=4, keep_track=True)
    assert result["render"]["track_kept"] is True
    assert live.deleted == []
