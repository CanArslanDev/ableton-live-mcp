"""Recording a section: render_and_analyze, capture_stems, mix_doctor.

The section plays in real time, so the user can add or remove tracks while it
records; an index taken before the recording would then point at their track.
Only the temporary tracks may record (Crank: a track re-armed before the stop
recorded and split a clip), and a recording must hold the section
(Nerdeysen: with "No Device" as Live's output, Live wrote 92-byte WAVs).
"""

import asyncio
import json
import time

import numpy as np
import pytest
import soundfile as sf
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from MCP_Server import doctor, extended_tools

RENDER_ID = 999
READ_AUDIO_DEVICES = doctor.audio_devices


@pytest.fixture(autouse=True)
def quick_and_known_device(monkeypatch):
    monkeypatch.setattr(extended_tools, "SETTLE_S", 0.05)
    monkeypatch.setattr(doctor, "audio_devices", lambda log=None: {
        "output": "Speakers (0 In, 2 Out)", "input": "Mic",
        "line": "2026-11-23T08:36:03: info: Audio In Out: Output Device: Speakers (0 In, 2 Out)"})


class RecLive:
    """Answers what record_section sends, like the Remote Script over Live
    12.4.16 would: arm and implicit_arm per track, the selection, the
    transport. Every armed or implicitly armed track records while Live plays
    with record mode on (`recorded_on`), checked after each step of a batch.

    files: {label: path} (label = the part's name after "MCP Rec ")."""

    def __init__(self, files, tracks=None, song=None, setup_fails=False,
                 insert_during=False, sticky=(), stops_at=None, stuck=False, clip_beats=None,
                 scene_tempo=None, unreadable_arms=False, recording_for=0.0):
        self.files = {k: str(v) for k, v in files.items()}
        self.tracks = tracks if tracks is not None else [
            {"name": "Drums", "$id": 101, "can_be_armed": True, "arm": True,
             "implicit_arm": False},
            {"name": "Bass", "$id": 102, "can_be_armed": True, "arm": False,
             "implicit_arm": False},
        ]
        for t in self.tracks:
            t.setdefault("has_audio_output", True)
            t.setdefault("is_foldable", False)
            t.setdefault("mute", False)
        self.song = dict({"tempo": 120.0, "loop": True, "loop_start": 0.0, "metronome": True,
                          "record_mode": False, "is_playing": False, "current_song_time": 4.0,
                          "start_time": 4.0, "back_to_arranger": False,
                          "signature_numerator": 4, "signature_denominator": 4}, **(song or {}))
        self.selected = self.tracks[-1]["$id"]
        self.setup_fails, self.insert_during = setup_fails, insert_during
        self.sticky, self.stops_at, self.stuck = set(sticky), stops_at, stuck
        self.clip_beats = clip_beats
        self.scene_tempo, self.unreadable_arms = scene_tempo, unreadable_arms
        self.channels = {}  # track id -> input channel
        self.recording_for, self.fired_at = recording_for, None
        self.next_id = RENDER_ID - 1
        self.sent, self.deleted, self.recorded_on = [], [], set()
        self.started_at = None

    # ── helpers ──
    def track(self, path):
        track_id = int(path.split()[1])
        return next((t for t in self.tracks if t["$id"] == track_id), None)

    def label_of(self, track_id):
        name = next(t["name"] for t in self.tracks if t["$id"] == track_id)
        return name[len("MCP Rec "):]

    def clip(self, track_id, session=False):
        start = self.started_at if self.started_at is not None else 0.0
        beats = (self.clip_beats if self.clip_beats is not None
                 else self.length if session else self.length + 0.3)
        recording = self.fired_at is not None and time.monotonic() < self.fired_at + self.recording_for
        return {"file_path": self.files[self.label_of(track_id)], "start_time": start,
                "end_time": start + beats, "length": beats, "is_recording": recording}

    def recording_check(self):
        if self.song["is_playing"] and self.song["record_mode"]:
            for t in self.tracks:
                if t.get("arm") is True or t.get("implicit_arm") is True:
                    self.recorded_on.add(t["$id"])

    def step(self, command):
        kind, params = command["type"], command["params"]
        path = params.get("path", "")
        if kind == "lom_call":
            method = params["method"]
            if method == "create_audio_track":
                self.next_id += 1
                self.tracks.append({"name": "Audio", "$id": self.next_id, "arm": False,
                                    "implicit_arm": False, "can_be_armed": True})
                self.selected = self.next_id
            elif method == "start_playing":
                self.song["is_playing"] = True
                self.started_at = self.song["start_time"]
            elif method == "stop_playing":
                self.song["is_playing"] = False
            elif method == "fire" and "clip_slots" in path:
                self.song["is_playing"] = True
                self.fired_at = time.monotonic()
            elif method == "fire":                    # a scene: its clips override
                self.song["back_to_arranger"] = True  # the Arrangement
                self.launched = True
                if self.scene_tempo:                  # and its tempo is the set's
                    self.song["tempo"] = self.scene_tempo
            elif method == "stop_all_clips":
                self.launched = False
            elif method == "delete_track":
                index = params["args"][0]
                self.deleted.append(self.tracks.pop(index)["$id"])
                self.selected = self.tracks[min(index, len(self.tracks) - 1)]["$id"]
            return {"method": method, "result": None}
        if kind == "lom_set":
            prop, value = params["property"], params["value"]
            if path == "live_set":
                self.song[prop] = value
                if prop == "current_song_time":
                    self.playhead = value
            elif path == "live_set view":
                self.selected = int(value["$ref"].split()[1])
            else:
                track = self.track(path)
                if prop == "input_routing_type" and self.setup_fails:
                    raise RuntimeError("No item named 'Nope'")
                if prop in ("arm", "implicit_arm") and track["$id"] in self.sticky:
                    return {}
                if prop == "input_routing_type":
                    self.channels[track["$id"]] = "Post Mixer"  # as Live 12.4.16 does
                if prop == "input_routing_channel":
                    self.channels[track["$id"]] = value["$ref"].rsplit('"', 2)[1]
                track[prop] = value
            return {"property": prop, "value": value}
        if kind == "lom_get":
            if path == "live_set":
                return {k: self.song[k] for k in params["properties"]}
            if path == "live_set tracks":
                if self.unreadable_arms and "implicit_arm" in params["properties"] \
                        and len(self.tracks) > 2:
                    raise RuntimeError("Live is busy")
                return {"items": [dict(t) for t in self.tracks], "length": len(self.tracks)}
            if path == "live_set return_tracks":
                return {"items": []}
            if path == "live_set scenes":
                return {"items": [{"name": "Intro", "$id": 50}]}
            if path == "live_set scenes 0":
                return {"tempo": self.scene_tempo or -1.0,
                        "tempo_enabled": self.scene_tempo is not None}
            if path.startswith("live_set tracks ") and path.split()[-1].isdigit():
                track = self.tracks[int(path.split()[-1])]
                return {"name": track["name"], "$id": track["$id"]}
            if path == "live_set tracks -1":
                return {"name": self.tracks[-1]["name"], "$id": self.tracks[-1]["$id"]}
            if path.endswith("arrangement_clips"):
                return {"items": [self.clip(int(path.split()[1]))]}
            if "clip_slots" in path:
                return self.clip(int(path.split()[1]), session=True)
            if path.startswith("id ") and params["properties"] == ["input_routing_channel"]:
                track_id = int(path.split()[1])
                return {"input_routing_channel": {"display_name": self.channels.get(
                    track_id, "1/2")}}
            raise AssertionError(f"unexpected lom_get {path}")
        raise AssertionError(f"unexpected step {kind}")

    def send_command(self, command, params=None):
        params = params or {}
        self.sent.append((command, params))
        if command == "batch":
            results = []
            for i, c in enumerate(params["commands"]):
                try:
                    result = self.step(c)
                    results.append({"index": i, "type": c["type"], "status": "success",
                                    "result": result})
                except RuntimeError as e:
                    results.append({"index": i, "type": c["type"], "status": "error",
                                    "message": str(e)})
                self.recording_check()
            if self.insert_during and params["commands"][0]["params"].get("property") == \
                    "input_routing_type":
                # While the section records, the user inserts a track at the top
                self.tracks.insert(0, {"name": "User's new track", "$id": 555,
                                       "can_be_armed": True, "arm": False,
                                       "implicit_arm": False})
                self.insert_during = False
            failed = sum(r["status"] == "error" for r in results)
            return {"results": results, "completed": len(results), "failed": failed,
                    "total": len(results)}
        if command == "lom_get":
            path, props = params["path"], params.get("properties") or []
            if path == "live_set" and "tempo" in props:
                return {k: self.song[k] for k in props}
            if path == "live_set":
                if self.stops_at is not None and self.song["is_playing"]:
                    # playing a little first, then stopped there (by the user)
                    self.polls = getattr(self, "polls", 0) + 1
                    self.song["is_playing"] = self.polls < 2
                    return {"current_song_time": self.stops_at - 2 + self.polls,
                            "is_playing": self.song["is_playing"]}
                if self.stuck or not self.song["is_playing"]:
                    return {"current_song_time": self.started_at or 0.0,
                            "is_playing": self.song["is_playing"]}
                return {"current_song_time": 10_000.0, "is_playing": True}
            if path == "live_set view":
                return {"selected_track": {"$type": "Track", "$id": self.selected}}
            return self.step({"type": "lom_get", "params": params})
        result = self.step({"type": command, "params": params})
        self.recording_check()
        return result

    def batches(self):
        return [p["commands"] for c, p in self.sent if c == "batch"]

    @property
    def length(self):
        return getattr(self, "_length", 4.0)


def _call(live, tool, **arguments):
    mcp = MCPServer("t")
    extended_tools.register(mcp, lambda: live)
    if live is not None:
        live._length = float(arguments.get("length_beats", 16.0))
    content = asyncio.run(mcp.call_tool(tool, arguments))
    return json.loads(content.content[0].text)


def _render(live, **arguments):
    return _call(live, "render_and_analyze", **arguments)


def _stereo(x):
    return np.stack([x, x], axis=1)


def _write(path, signal, rate=22050):
    sf.write(path, signal, rate)
    return path


@pytest.fixture
def wav(tmp_path):
    """2.5 s: 4 beats at 120 BPM and the bit Live records past the end."""
    t = np.arange(int(48000 * 2.5)) / 48000
    return _write(tmp_path / "render.wav", _stereo(0.25 * np.sin(2 * np.pi * 220 * t)), 48000)


def test_temporary_track_is_deleted_by_id_after_tracks_shift(wav):
    live = RecLive({"master": wav}, insert_during=True)
    result = _render(live, length_beats=4)
    assert result["render"]["track_kept"] is False
    # The render track sat at index 2 when created; the user's insert moved it to 3
    assert live.deleted == [RENDER_ID]
    assert [t["$id"] for t in live.tracks] == [555, 101, 102]


def test_only_the_temporary_track_records_and_arms_come_back_after_the_stop(wav):
    """Crank: the old restore re-armed tracks in the batch that stopped the
    transport, before stop_playing, so they recorded. Implicitly armed
    tracks (Push's auto-arm) record too (Live 12.4.16)."""
    tracks = [{"name": "Drums", "$id": 101, "arm": True, "implicit_arm": False},
              {"name": "Lead", "$id": 102, "arm": False, "implicit_arm": True},
              {"name": "Pad", "$id": 103, "arm": False, "implicit_arm": False}]
    live = RecLive({"master": wav}, tracks=tracks)
    live.selected = 103
    result = _render(live, length_beats=4)
    assert live.recorded_on == {RENDER_ID}
    assert result["render"]["disarmed_for_take"] == ["Drums", "Lead"]
    by_id = {t["$id"]: t for t in live.tracks}
    assert by_id[101]["arm"] is True and by_id[102]["implicit_arm"] is True
    assert by_id[103]["arm"] is False
    assert live.selected == 103                       # selection put back
    batches = live.batches()
    stop = next(i for i, b in enumerate(batches)
                if any(c["params"].get("method") == "stop_playing" for c in b))
    rearm = next(i for i, b in enumerate(batches)
                 if {"type": "lom_set", "params": {"path": "id 101", "property": "arm",
                                                   "value": True}} in b)
    assert rearm > stop
    assert {"type": "lom_set", "params": {"path": f"id {RENDER_ID}", "property": "arm",
                                          "value": False}} in batches[stop]


def test_a_track_that_stays_armed_stops_the_render_before_it_plays(wav):
    live = RecLive({"master": wav}, sticky={101})
    with pytest.raises(ToolError, match="Drums stayed armed"):
        _render(live, length_beats=4)
    assert not any(c["params"].get("method") == "start_playing"
                   for b in live.batches() for c in b)
    assert live.deleted == [RENDER_ID] and live.recorded_on == set()
    assert live.song["metronome"] is True                 # turned off, then back on


def test_arms_that_cannot_be_read_back_stop_the_render(wav):
    live = RecLive({"master": wav}, unreadable_arms=True)
    with pytest.raises(ToolError, match="arms could not be read back"):
        _render(live, length_beats=4)
    assert live.recorded_on == set() and live.song["metronome"] is True
    assert live.tracks[0]["arm"] is True


def test_the_recording_takes_its_source_after_the_mixer(tmp_path):
    """Live 12.4.16 sets the channel to Post Mixer with the input type, but
    in that batch the later tracks still list Ext. In's channels, so setting
    the channel there failed in Live's log. It is read in a later command
    and set only where it is not."""
    t = np.arange(int(48000 * 2.5)) / 48000
    files = {n: _write(tmp_path / f"{n}.wav", _stereo(0.2 * np.sin(2 * np.pi * 220 * t)), 48000)
             for n in ("Drums", "Bass")}
    live = RecLive(files)
    _call(live, "capture_stems", tracks=[0, 1], length_beats=4, include_master=False)
    configure = next(b for b in live.batches()
                     if b[0]["params"].get("property") == "input_routing_type")
    assert not any(c["params"].get("property") == "input_routing_channel" for c in configure)
    assert set(live.channels.values()) == {"Post Mixer"}


def test_failed_setup_still_removes_the_temporary_track(wav):
    live = RecLive({"Nope": wav}, setup_fails=True)
    with pytest.raises(ToolError, match="Render setup failed"):
        _render(live, length_beats=4, source="Nope")
    assert live.deleted == [RENDER_ID]
    assert live.tracks[0]["arm"] is True              # never disarmed for good


def test_unreadable_recording_still_removes_the_temporary_track(tmp_path):
    broken = tmp_path / "broken.wav"
    broken.write_bytes(b"not audio at all")
    live = RecLive({"master": broken})
    with pytest.raises(ToolError):
        _render(live, length_beats=4)
    assert live.deleted == [RENDER_ID]


def test_a_header_only_recording_names_lives_audio_device(tmp_path, monkeypatch):
    """Nerdeysen: Live's output was "No Device"; Live wrote 92-byte WAVs
    (2 frames) and the old check (size > 0) analysed them."""
    empty = _write(tmp_path / "empty.wav", np.zeros((2, 2)), 44100)
    line = "2026-11-23T00:18:08: info: Audio In Out: Output Device: No Device"
    monkeypatch.setattr(doctor, "audio_devices",
                        lambda log=None: {"output": "No Device", "input": "x", "line": line})
    live = RecLive({"master": empty})
    with pytest.raises(ToolError) as error:
        _render(live, length_beats=4)
    assert "recorded no audio" in str(error.value) and line in str(error.value)
    assert "Settings › Audio" in str(error.value)
    assert live.deleted == [RENDER_ID]


def test_a_scene_with_its_own_tempo_renders_and_the_tempo_comes_back(tmp_path):
    """A scene at 160 BPM plays 4 beats in 1.5 s, not the set's 2 s: the
    clip's beats measure the take (the set's tempo refused it), and the
    set's tempo is put back after the scene changed it."""
    t = np.arange(int(48000 * 1.5)) / 48000
    fast = _write(tmp_path / "fast.wav", _stereo(0.2 * np.sin(2 * np.pi * 220 * t)), 48000)
    live = RecLive({"master": fast}, scene_tempo=160.0,
                   song={"loop": False, "current_song_time": 0.0, "start_time": 0.0})
    result = _render(live, scene=0, length_beats=4)
    assert result["duration_seconds"] == pytest.approx(1.5)   # the whole take
    assert result["render"]["seconds"] == 1.5
    assert live.song["tempo"] == 120.0 and result["render"]["tempo_put_back"] == 120.0


def test_tempo_automation_in_the_arrangement_is_no_short_take(tmp_path):
    """4.3 beats recorded in 1.6 s (faster automation): complete by its beats."""
    t = np.arange(int(48000 * 1.6)) / 48000
    quick = _write(tmp_path / "quick.wav", _stereo(0.2 * np.sin(2 * np.pi * 220 * t)), 48000)
    result = _render(RecLive({"master": quick}), start_beat=4, length_beats=4)
    assert result["duration_seconds"] == pytest.approx(1.6 * 4 / 4.3, abs=0.01)
    assert "tempo_put_back" not in result["render"]


def test_a_take_live_cut_short_is_refused(wav):
    with pytest.raises(ToolError, match="stopped after 3.00 of 4 beats"):
        _render(RecLive({"master": wav}, clip_beats=3.0), length_beats=4)


def test_playback_stopping_before_the_end_raises(wav):
    live = RecLive({"master": wav}, stops_at=6.0)
    with pytest.raises(ToolError, match="Playback stopped at beat 6"):
        _render(live, start_beat=4, length_beats=4)
    assert live.deleted == [RENDER_ID] and live.tracks[0]["arm"] is True


def test_a_take_that_never_reaches_the_end_raises(wav, monkeypatch):
    monkeypatch.setattr(extended_tools, "RECORD_GRACE_S", 0.3)
    live = RecLive({"master": wav}, stuck=True)
    with pytest.raises(ToolError, match="did not finish recording the section in time"):
        _render(live, length_beats=1)
    assert live.song["is_playing"] is False and live.song["record_mode"] is False


def test_the_render_names_its_mode_file_and_analyses_the_section_only(wav):
    result = _render(RecLive({"master": wav}), start_beat=4, length_beats=4)
    render = result["render"]
    assert list(result)[0] == "render"
    assert render["mode"] == "arrangement" and render["played"] == "Arrangement, beats 4 to 8"
    assert result["file"] == str(wav) and "file" not in render
    # 4 of the 4.3 beats Live recorded (the file's 2.5 s): the stop's lag is left out
    assert result["duration_seconds"] == pytest.approx(2.5 * 4 / 4.3, abs=0.01)


def test_a_scene_render_records_session_clips(wav):
    live = RecLive({"master": wav}, song={"loop": False, "current_song_time": 0.0,
                                          "start_time": 0.0})
    result = _render(live, scene=0, length_beats=4)
    assert result["render"]["mode"] == "session"
    assert result["render"]["played"] == "Session scene 1"
    # The scene's clips stopped again and the Arrangement plays (Live 12.4.16
    # left them playing on the stopped transport, Back to Arrangement lit)
    assert live.launched is False and live.song["back_to_arranger"] is False
    fired = [c for b in live.batches() for c in b if c["params"].get("method") == "fire"]
    assert fired[0]["params"]["path"] == "live_set scenes 0"
    assert fired[1]["params"] == {"path": f"id {RENDER_ID} clip_slots 0", "method": "fire",
                                  "args": [4.0]}


def test_keep_track_leaves_it_in_place(wav):
    live = RecLive({"master": wav})
    result = _render(live, length_beats=4, keep_track=True)
    assert result["render"]["track_kept"] is True
    assert live.deleted == []
    assert next(t for t in live.tracks if t["$id"] == RENDER_ID)["arm"] is False


def test_recording_starts_playback_at_start_beat(wav):
    """Live 12.4.2 (measured): continue_playing resumes where Live last stopped,
    whatever current_song_time was set to while stopped; start_playing starts
    at start_time. So the section is started with start_time + start_playing,
    and start_time is put back afterwards."""
    live = RecLive({"master": wav})
    _render(live, start_beat=32, length_beats=4)
    start = next(b for b in live.batches()
                 if any(c["params"].get("method") == "start_playing" for c in b))
    sets = [(c["params"].get("property"), c["params"].get("value"))
            for c in start if c["type"] == "lom_set"]
    calls = [c["params"]["method"] for c in start if c["type"] == "lom_call"]
    assert ("start_time", 32.0) in sets and calls[-1] == "start_playing"
    assert "continue_playing" not in calls
    assert live.song["start_time"] == 4.0


# ── Several parts in one pass: capture_stems, mix_doctor, compare_to_reference ─

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


def multi(files):
    return RecLive(files, tracks=[{"name": n, "$id": 100 + i, "can_be_armed": True,
                                   "arm": False, "implicit_arm": False}
                                  for i, n in enumerate(["Kick", "Bass", "Hats"])],
                   song={"loop": False, "metronome": False, "current_song_time": 0.0,
                         "start_time": 0.0})


def test_capture_stems_records_every_part_in_one_pass(stem_files):
    live = multi(stem_files)
    result = _call(live, "capture_stems", length_beats=8)
    creates = [b for b in live.batches() if b[0]["params"].get("method") == "create_audio_track"]
    assert len(creates) == 1                          # one pass for all parts
    assert set(result["stems"]) == {"Kick", "Bass", "Hats", "Master"}
    assert result["stems"]["Kick"]["main_band"] == "sub"
    assert sorted(live.deleted) == [999, 1000, 1001, 1002]
    assert live.recorded_on == {999, 1000, 1001, 1002}
    configure = next(b for b in live.batches()
                     if b[0]["params"].get("property") == "input_routing_type")
    inputs = [c["params"]["value"]["$ref"] for c in configure
              if c["params"].get("property") == "input_routing_type"]
    assert inputs[-1].endswith("Resampling") and "Kick" in inputs[0]


def test_mix_doctor_names_the_clash_and_reads_the_master(stem_files):
    report = _call(multi(stem_files), "mix_doctor", length_beats=8)
    assert report["masking"][0]["pair"] == ["Kick", "Bass"]
    assert any("Kick and Bass share the sub" in f for f in report["findings"])
    assert report["master"]["integrated_lufs"] > -30
    assert set(report["stems"]) == {"Kick", "Bass", "Hats"}
    assert report["files"]["Master"] == str(stem_files["Master"])


def test_compare_to_reference_with_files(tmp_path, stem_files):
    report = _call(None, "compare_to_reference", reference_path=str(stem_files["Master"]),
                   mix_path=str(stem_files["Hats"]))
    assert report["mix"]["file"].endswith("Hats.wav")
    assert any(m["type"] == "Low Shelf" for m in report["eq_moves"])
    assert "2 Filter Type A" in report["eq_eight_settings"]


def test_capture_stems_reports_clipped_stems(tmp_path):
    rate = 22050
    t = np.arange(rate * 4) / rate
    loud = _stereo(np.clip(1.5 * np.sin(2 * np.pi * 110 * t), -1, 1))
    files = {name: _write(tmp_path / f"{name}.wav", sig) for name, sig in
             (("Kick", loud), ("Bass", 0.1 * loud), ("Hats", 0.1 * loud), ("Master", loud))}
    result = _call(multi(files), "capture_stems", length_beats=8)
    assert set(result["clipped"]) == {"Kick", "Master"}
    assert result["stems"]["Kick"]["clipped_samples"] > 0
    assert "clipped_samples" not in result["stems"]["Bass"]


# ── Kept recording tracks ───────────────────────────────────────────────────

def test_delete_recording_tracks_removes_only_mcp_rec_tracks(wav):
    live = RecLive({"master": wav})
    live.tracks += [{"name": "MCP Rec master", "$id": 7, "arm": False},
                    {"name": "Synth", "$id": 8, "arm": False},
                    {"name": "MCP Rec Drums", "$id": 9, "arm": False}]
    result = _call(live, "manage_track", action="delete_recording_tracks")
    assert result["deleted"] == ["MCP Rec master", "MCP Rec Drums"]
    assert [t["$id"] for t in live.tracks] == [101, 102, 8]
    assert "Samples/Recorded" in result["note"]


# ── Live's audio device, from its Log.txt ───────────────────────────────────

def test_doctor_reads_lives_last_audio_output_device(tmp_path, monkeypatch):
    """Live 12.4.16 writes an "Audio In Out" block at start and on each
    change, sometimes several in a row; the last one holds."""
    monkeypatch.setattr(doctor, "audio_devices", READ_AUDIO_DEVICES)
    log = tmp_path / "Log.txt"
    block = ("2026-11-22T14:54:15.946475: info: Audio In Out: Input Device: {i}\n"
             "2026-11-22T14:54:15.946486: info: Audio In Out: Output Device: {o}\n"
             "2026-11-22T14:54:15.946490: info: Audio In Out: Sample Rate: 44100\n")
    log.write_text(block.format(i="Mic (1 In, 0 Out)", o="Speakers (0 In, 2 Out)")
                   + block.format(i="No Device", o="No Device"))
    check = doctor.check_audio_device(log)
    assert check["status"] == "warn" and "No Device" in check["detail"]
    assert "Settings › Audio" in check["fix"]
    log.write_text(log.read_text() + block.format(i="Mic", o="Speakers (0 In, 2 Out)"))
    assert doctor.audio_devices(log)["output"] == "Speakers (0 In, 2 Out)"
    assert doctor.check_audio_device(log)["status"] == "ok"
    assert doctor.check_audio_device(tmp_path / "none.txt")["status"] == "info"


def test_delete_recording_tracks_refuses_while_one_records(wav):
    """An armed "MCP Rec" track is a take running, in this server or
    another session's (RECORDING knows only this one's): nothing goes."""
    live = RecLive({"master": wav})
    live.tracks += [{"name": "MCP Rec master", "$id": 7, "arm": False},
                    {"name": "MCP Rec Drums", "$id": 9, "arm": True}]
    with pytest.raises(ToolError, match="A recording is running .MCP Rec Drums"):
        _call(live, "manage_track", action="delete_recording_tracks")
    live.tracks[-1]["arm"] = False
    extended_tools.RECORDING.add(9)
    try:
        with pytest.raises(ToolError, match="A recording is running"):
            _call(live, "manage_track", action="delete_recording_tracks")
    finally:
        extended_tools.RECORDING.discard(9)
    assert [t["$id"] for t in live.tracks] == [101, 102, 7, 9]


def test_a_slow_scene_gets_the_time_it_takes(tmp_path, monkeypatch):
    """A scene at 60 BPM plays twice as long as the set's 120: the take's
    deadline follows the slower tempo (here a deadline 0.2 s past the start
    at 120 BPM, 16.7 s at 60, for a take that records for 1 s)."""
    monkeypatch.setattr(extended_tools, "RECORD_GRACE_S", -16.3)
    t = np.arange(48000) / 48000
    slow = _write(tmp_path / "slow.wav", _stereo(0.2 * np.sin(2 * np.pi * 220 * t)), 48000)
    live = RecLive({"master": slow}, scene_tempo=60.0, recording_for=1.0,
                   song={"loop": False, "current_song_time": 0.0, "start_time": 0.0})
    assert _render(live, scene=0, length_beats=1)["render"]["seconds"] == 1.0


def test_a_take_that_runs_out_of_time_does_not_blame_the_audio_device(wav, monkeypatch):
    monkeypatch.setattr(extended_tools, "RECORD_GRACE_S", 0.3)
    with pytest.raises(ToolError) as error:
        _render(RecLive({"master": wav}, stuck=True), length_beats=1)
    assert "in time" in str(error.value) and "audio" not in str(error.value)


def test_analyses_read_at_most_ten_minutes(wav, monkeypatch):
    monkeypatch.setattr(extended_tools, "ANALYSIS_MAX_S", 1.0)
    result = _render(RecLive({"master": wav}), start_beat=4, length_beats=4)
    assert result["duration_seconds"] == pytest.approx(1.0)
    # the section's real length is still reported
    assert result["render"]["seconds"] == pytest.approx(2.5 * 4 / 4.3, abs=0.01)


def test_a_kept_track_is_disarmed_when_the_setup_stops(wav):
    live = RecLive({"master": wav}, sticky={101})
    with pytest.raises(ToolError, match="Drums stayed armed"):
        _render(live, length_beats=4, keep_track=True)
    kept = next(t for t in live.tracks if t["$id"] == RENDER_ID)
    assert kept["arm"] is False and live.deleted == []


def test_the_launch_wait_follows_the_meter(tmp_path, monkeypatch):
    """Session takes start within 8 bars: 56 beats in 7/4, not 32."""
    monkeypatch.setattr(extended_tools, "RECORD_GRACE_S", -16.2)  # 4/4: 0.3 s; 7/4: 12.3 s
    t = np.arange(24000) / 48000
    take = _write(tmp_path / "take.wav", _stereo(0.2 * np.sin(2 * np.pi * 220 * t)), 48000)
    live = RecLive({"master": take}, recording_for=1.0,
                   song={"loop": False, "current_song_time": 0.0, "start_time": 0.0,
                         "signature_numerator": 7})
    assert _render(live, scene=0, length_beats=1)["render"]["mode"] == "session"
