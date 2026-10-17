"""render_and_analyze keeps track of its temporary track by id, not index.

The section plays in real time, so the user can add or remove tracks while it
records; an index taken before the recording would then point at their track.
"""

import asyncio
import json

import numpy as np
import pytest
import soundfile as sf
from mcp.server.mcpserver import MCPServer

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
                        "record_mode": False, "is_playing": False, "current_song_time": 4.0,
                        "start_time": 4.0}
            if path == "live_set":
                # where the playhead was moved, once; then past the section's end
                moved, self.playhead = getattr(self, "playhead", None), None
                return {"current_song_time": 10_000.0 if moved is None else moved}
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
                return {"results": [
                    {"index": 0, "type": "lom_call", "status": "success", "result": {}},
                    {"index": 1, "type": "lom_get", "status": "success",
                     "result": {"name": "Audio", "$id": RENDER_ID}}], "failed": 0}
            if commands[0]["params"].get("property") == "input_routing_type":
                if self.setup_fails:
                    results = [{"index": 0, "type": "lom_set", "status": "error",
                                "message": "No item named 'Nope'"}]
                    results += [{"index": i, "type": c["type"], "status": "success",
                                 "result": {}} for i, c in enumerate(commands[1:], start=1)]
                    return {"results": results, "failed": 1}
                # While the section records, the user inserts a track at the top
                self.tracks.insert(0, {"name": "User's new track", "$id": 555,
                                       "can_be_armed": True, "arm": False})
            results = [{"index": i, "type": c["type"], "status": "success", "result": {}}
                       for i, c in enumerate(commands)]
            return {"results": results, "completed": len(results), "failed": 0,
                    "total": len(commands)}
        if command == "lom_set" and params["property"] == "current_song_time":
            self.playhead = params["value"]
            return {}
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
    mcp = MCPServer("t")
    extended_tools.register(mcp, lambda: live)
    content = asyncio.run(mcp.call_tool("render_and_analyze", arguments))
    blocks = content.content
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
    _create, _configure, start, restore = live.batches()
    assert {"type": "lom_set", "params": {"path": "id 101", "property": "arm",
                                          "value": False}} in start
    assert {"type": "lom_set", "params": {"path": f"id {RENDER_ID}", "property": "arm",
                                          "value": True}} in start
    assert {"type": "lom_set", "params": {"path": "id 101", "property": "arm",
                                          "value": True}} in restore
    assert {"type": "lom_set", "params": {"path": f"id {RENDER_ID}", "property": "arm",
                                          "value": False}} in restore


def test_failed_setup_still_removes_the_temporary_track(wav):
    from mcp.server.mcpserver.exceptions import ToolError

    live = ScriptedLive(wav, setup_fails=True)
    with pytest.raises(ToolError, match="Render setup failed"):
        _render(live, length_beats=4, source="Nope")
    assert live.deleted == [RENDER_ID]


def test_unreadable_recording_still_removes_the_temporary_track(tmp_path):
    from mcp.server.mcpserver.exceptions import ToolError

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


# ── Several parts in one pass: capture_stems, mix_doctor, compare_to_reference ─

def _write(path, signal, rate=22050):
    sf.write(path, signal, rate)
    return path


def _stereo(x):
    return np.stack([x, x], axis=1)


@pytest.fixture
def stem_files(tmp_path):
    rate = 22050
    t = np.arange(rate * 4) / rate
    rng = np.random.default_rng(0)
    kick = _stereo(0.3 * np.sin(2 * np.pi * 50 * t))
    bass = _stereo(0.25 * np.sin(2 * np.pi * 55 * t))
    hats = _stereo(0.05 * np.diff(rng.normal(0, 1, len(t) + 1)))
    master = kick + bass + hats
    return {name: _write(tmp_path / f"{name}.wav", signal)
            for name, signal in (("Kick", kick), ("Bass", bass), ("Hats", hats),
                                 ("Master", master))}


class MultiLive:
    """Answers record_section for several temporary tracks at once."""

    def __init__(self, files):
        self.files = files
        self.tracks = [{"name": n, "$id": 100 + i, "can_be_armed": True, "arm": False,
                        "has_audio_output": True, "is_foldable": False, "mute": False}
                       for i, n in enumerate(["Kick", "Bass", "Hats"])]
        self.labels = {}
        self.next_id = 900
        self.deleted = []
        self.setups = []
        self.configures = []

    def send_command(self, command, params=None):
        params = params or {}
        if command == "lom_get":
            path, props = params["path"], params.get("properties") or []
            if path == "live_set" and "tempo" in props:
                return {"tempo": 120.0, "loop": False, "loop_start": 0.0, "metronome": False,
                        "record_mode": False, "is_playing": False, "current_song_time": 0.0,
                        "start_time": 0.0}
            if path == "live_set":
                # where the playhead was moved, once; then past the section's end
                moved, self.playhead = getattr(self, "playhead", None), None
                return {"current_song_time": 10_000.0 if moved is None else moved}
            if path == "live_set tracks":
                return {"items": [dict(t) for t in self.tracks]}
            if path.endswith("arrangement_clips"):
                label = self.labels[int(path.split()[1])]
                return {"items": [{"file_path": str(self.files[label])}]}
            raise AssertionError(path)
        if command == "batch":
            commands = params["commands"]
            if commands[0]["type"] == "lom_get" and commands[0]["params"]["path"] == \
                    "live_set tracks":
                return {"results": [
                    {"status": "success", "result": {"items": self.tracks}},
                    {"status": "success", "result": {"items": []}}]}
            results = []
            if commands[0]["params"].get("method") == "create_audio_track":
                self.setups.append(commands)
                current = None
                for c in commands:
                    if c["type"] == "lom_call":
                        self.next_id += 1
                        current = self.next_id
                        self.tracks.append({"name": "Audio", "$id": current})
                        results.append({"status": "success", "result": {}})
                    else:
                        results.append({"status": "success", "result": {"$id": current}})
                return {"results": results, "failed": 0}
            if commands[0]["params"].get("property") == "input_routing_type":
                self.configures.append(commands)
                for c in commands:
                    if c["params"].get("property") == "name":
                        track_id = int(c["params"]["path"].split()[1])
                        self.labels[track_id] = c["params"]["value"][len("MCP Rec "):]
            return {"results": [{"status": "success", "result": {}} for _ in commands],
                    "failed": 0}
        if command == "lom_set" and params["property"] == "current_song_time":
            self.playhead = params["value"]
            return {}
        if command == "lom_call" and params["method"] == "delete_track":
            self.deleted.append(self.tracks.pop(params["args"][0])["$id"])
            return {}
        return {}


def _call(live, tool, **arguments):
    mcp = MCPServer("t")
    extended_tools.register(mcp, lambda: live)
    content = asyncio.run(mcp.call_tool(tool, arguments))
    blocks = content.content
    return json.loads(blocks[0].text)


def test_capture_stems_records_every_part_in_one_pass(stem_files):
    live = MultiLive(stem_files)
    result = _call(live, "capture_stems", length_beats=8)
    assert len(live.setups) == 1                      # one pass for all parts
    assert set(result["stems"]) == {"Kick", "Bass", "Hats", "Master"}
    assert result["stems"]["Kick"]["main_band"] == "sub"
    assert sorted(live.deleted) == [901, 902, 903, 904]
    inputs = [c["params"]["value"]["$ref"] for c in live.configures[0]
              if c["params"].get("property") == "input_routing_type"]
    assert inputs[-1].endswith("Resampling") and "Kick" in inputs[0]


def test_mix_doctor_names_the_clash_and_reads_the_master(stem_files):
    live = MultiLive(stem_files)
    report = _call(live, "mix_doctor", length_beats=8)
    assert report["masking"][0]["pair"] == ["Kick", "Bass"]
    assert any("Kick and Bass share the sub" in f for f in report["findings"])
    assert report["master"]["integrated_lufs"] > -30
    assert set(report["stems"]) == {"Kick", "Bass", "Hats"}


def test_compare_to_reference_with_files(tmp_path, stem_files):
    report = _call(None, "compare_to_reference", reference_path=str(stem_files["Master"]),
                   mix_path=str(stem_files["Hats"]))
    assert report["mix"]["file"].endswith("Hats.wav")
    assert any(m["type"] == "Low Shelf" for m in report["eq_moves"])
    assert "2 Filter Type A" in report["eq_eight_settings"]


def test_recording_starts_playback_at_start_beat(wav):
    """Live 12.4.2 (measured): continue_playing resumes where Live last stopped,
    whatever current_song_time was set to while stopped; start_playing starts
    at start_time. So the section is started with start_time + start_playing,
    and start_time is put back afterwards."""
    live = ScriptedLive(wav)
    _render(live, start_beat=32, length_beats=4)
    _create, _configure, start, restore = live.batches()
    sets = [(c["params"].get("property"), c["params"].get("value")) for c in start if c["type"] == "lom_set"]
    calls = [c["params"]["method"] for c in start if c["type"] == "lom_call"]
    assert ("start_time", 32.0) in sets and calls[-1] == "start_playing"
    assert "continue_playing" not in calls
    assert sets.index(("start_time", 32.0)) < len(sets)
    assert {"type": "lom_set", "params": {"path": "live_set", "property": "start_time", "value": 4.0}} in restore


def test_capture_stems_reports_clipped_stems(tmp_path):
    rate = 22050
    t = np.arange(rate) / rate
    loud = _stereo(np.clip(1.5 * np.sin(2 * np.pi * 110 * t), -1, 1))
    files = {name: _write(tmp_path / f"{name}.wav", sig) for name, sig in
             (("Kick", loud), ("Bass", 0.1 * loud), ("Hats", 0.1 * loud), ("Master", loud))}
    result = _call(MultiLive(files), "capture_stems", length_beats=8)
    assert set(result["clipped"]) == {"Kick", "Master"}
    assert result["stems"]["Kick"]["clipped_samples"] > 0
    assert "clipped_samples" not in result["stems"]["Bass"]
