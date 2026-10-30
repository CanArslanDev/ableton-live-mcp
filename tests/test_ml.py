"""The machine-learning plumbing (ml.py, ml_worker.py) without any model:
the worker protocol over a real subprocess, the uv command, seconds to beats,
and the sound index."""

import asyncio
import os
from pathlib import Path

import numpy as np
import pytest

from MCP_Server import ml


def test_worker_protocol_round_trip():
    seen = []

    async def report(fraction, message):
        seen.append((fraction, message))

    result = asyncio.run(ml.run("echo", {"x": [1, "ü"]}, report))
    assert result == {"x": [1, "ü"]}
    assert seen == [(0.5, "echo")]


def test_a_result_far_over_64_kb_comes_back():
    """A dense stem's notes are one JSON line of several MB (asyncio's default
    line limit, 64 KB, failed with "Separator is found, but chunk is longer")."""
    notes = [{"start": i * 0.01, "end": i * 0.01 + 0.2, "pitch": 60, "bend": [0] * 40}
             for i in range(20000)]
    result = asyncio.run(ml.run("echo", {"notes": notes}))
    assert len(result["notes"]) == 20000


def test_worker_errors_reach_the_caller():
    with pytest.raises(RuntimeError, match="ValueError: no model here"):
        asyncio.run(ml.run("echo", {"fail": "no model here"}))
    with pytest.raises(RuntimeError, match="Unknown task 'nope'"):
        asyncio.run(ml.run("nope", {}))


def test_a_job_past_its_time_is_stopped():
    with pytest.raises(RuntimeError, match="longer than 1 s"):
        asyncio.run(ml.run("echo", {"sleep": 30}, timeout=1.0))


def test_jobs_run_in_their_own_uv_environment(monkeypatch, tmp_path):
    uv = tmp_path / "uv"
    uv.write_text("")
    monkeypatch.setenv("UV", str(uv))
    assert ml.command("transcribe") == [
        str(uv), "run", "--isolated", "--no-project", "--python", "3.10",
        "--with", "basic-pitch==0.4.0", "--with", "setuptools<81",
        "python", str(ml.WORKER), "transcribe"]
    assert ml.command("separate")[5:8] == ["3.12", "--with", "demucs==4.1.0"]
    monkeypatch.setenv("VIRTUAL_ENV", "/server/.venv")
    monkeypatch.setenv("PYTHONPATH", "/server")
    env = ml.child_environment()
    assert "VIRTUAL_ENV" not in env and "PYTHONPATH" not in env
    assert env["PYTHONUNBUFFERED"] == "1"


def test_missing_uv_is_explained(monkeypatch, tmp_path):
    monkeypatch.delenv("UV", raising=False)
    monkeypatch.setattr(ml.shutil, "which", lambda name: None)
    monkeypatch.setattr(ml.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ml.Path, "is_file", lambda self: False)
    with pytest.raises(RuntimeError, match="uv, which was not found"):
        ml.uv_executable()


def test_events_to_notes_in_beats():
    events = [{"start": 0.5, "end": 1.0, "pitch": 60, "amplitude": 0.5},
              {"start": 2.0, "end": 2.001, "pitch": 64, "amplitude": 1.2}]
    notes = ml.events_to_notes(events, tempo=120)
    assert notes[0] == {"pitch": 60, "start_time": 1.0, "duration": 1.0, "velocity": 64}
    # Velocity is clamped, very short notes keep a 1/64-beat minimum
    assert notes[1]["start_time"] == 4.0 and notes[1]["velocity"] == 127
    assert notes[1]["duration"] == round(1 / 64, 4)
    assert ml.events_to_notes(events, 60, offset_beats=8)[0]["start_time"] == 8.5
    assert [n["velocity"] for n in ml.events_to_notes(events, 120, velocities=[90, 20])] == [90, 20]


def test_place_notes_leaves_out_what_the_clip_never_plays():
    events = [{"start": 0.0, "end": 0.5, "pitch": 60, "amplitude": 0.5},
              {"start": 0.9, "end": 1.5, "pitch": 62, "amplitude": 0.5},
              {"start": 2.0, "end": 2.5, "pitch": 64, "amplitude": 0.5}]

    def from_marker(t):  # a clip that starts one second into its file, at 120 BPM
        return (t - 1.0) * 2

    notes, kept = ml.place_notes(events, from_marker)
    assert kept == [1, 2]
    assert [(n["start_time"], n["duration"]) for n in notes] == [(0.0, 1.0), (2.0, 1.0)]
    # The offset moves the notes it keeps; it brings none back
    notes, kept = ml.place_notes(events, from_marker, offset_beats=4)
    assert kept == [1, 2] and notes[0]["start_time"] == 4.0
    notes, kept = ml.place_notes(events, from_marker, offset_beats=-2.5)
    assert kept == [2] and notes[0]["start_time"] == 0.0 and notes[0]["duration"] == 0.5


def test_transcription_presets_and_overrides():
    # melodic keeps melodia below MIDI 52, where 808s and sub basses show no onsets
    assert ml.transcription_settings("melodic") == {
        "onset_threshold": 0.6, "frame_threshold": 0.3, "minimum_note_ms": 35.0,
        "melodia_trick": False, "melodia_below": 52, "velocity": "level"}
    chords = ml.transcription_settings("chords", onset_threshold=0.5, frame_threshold=None)
    assert chords["onset_threshold"] == 0.5 and chords["frame_threshold"] == 0.3
    assert chords["melodia_trick"] is True and "melodia_below" not in chords
    assert ml.transcription_settings("legacy")["velocity"] == "amplitude"
    # A given melodia_trick holds at every pitch
    for given in (True, False):
        settings = ml.transcription_settings("melodic", melodia_trick=given)
        assert settings["melodia_trick"] is given and "melodia_below" not in settings
    with pytest.raises(ValueError, match="Unknown preset 'drums'"):
        ml.transcription_settings("drums")


def test_melodia_only_below_its_pitch():
    from MCP_Server import ml_worker
    split = 440 * 2 ** ((52 - 69) / 12)  # 164.8 Hz
    assert ml_worker.melodia_ranges({}) == [(None, None, True)]
    assert ml_worker.melodia_ranges({"melodia_trick": False}) == [(None, None, False)]
    above, below = ml_worker.melodia_ranges({"melodia_trick": False, "melodia_below": 52,
                                             "minimum_frequency": 25.0})
    assert above == (pytest.approx(split), None, False) and below == (27.5, pytest.approx(split), True)
    # A range wholly on one side decodes once
    assert ml_worker.melodia_ranges({"melodia_trick": False, "melodia_below": 52,
                                     "maximum_frequency": 98.0}) == [(None, 98.0, True)]
    assert ml_worker.melodia_ranges({"melodia_trick": False, "melodia_below": 52,
                                     "minimum_frequency": 220.0}) == [(220.0, None, False)]
    assert ml_worker.melodia_ranges({"melodia_trick": True, "melodia_below": 52}) == [(None, None, True)]


def test_transcribe_runs_the_model_once_and_decodes_each_range(monkeypatch, capsys):
    """The worker against a stand-in for basic_pitch: one inference, one
    decoding per range, each on its own copy of the model's output."""
    import sys
    import types

    from MCP_Server import ml_worker

    calls = []

    def model_output_to_notes(output, onset_thresh, frame_thresh, min_note_len, min_freq, max_freq,
                              multiple_pitch_bends, melodia_trick):
        calls.append((float(output["note"].sum()), onset_thresh, frame_thresh, min_note_len, min_freq,
                      max_freq, melodia_trick))
        output["note"][:] = 0  # Basic Pitch zeroes outside the range in place
        pitch = 40 if melodia_trick else 64
        return None, [(0.5, 1.0, pitch, 0.7, [1, 1]), (0.1, 0.4, pitch, 0.6, None)]

    package = types.ModuleType("basic_pitch")
    package.ICASSP_2022_MODEL_PATH = "model"
    package.note_creation = types.SimpleNamespace(model_output_to_notes=model_output_to_notes)
    inference = types.ModuleType("basic_pitch.inference")
    inference.Model = lambda path: path
    inference.run_inference = lambda path, model: calls.append(("inference", path)) or {
        "note": np.ones((4, 88)), "onset": np.ones((4, 88)), "contour": np.ones((4, 264))}
    constants = types.ModuleType("basic_pitch.constants")
    constants.AUDIO_SAMPLE_RATE, constants.FFT_HOP = 22050, 256
    for name, module in (("basic_pitch", package), ("basic_pitch.inference", inference),
                         ("basic_pitch.constants", constants)):
        monkeypatch.setitem(sys.modules, name, module)

    result = ml_worker.transcribe({"path": "bass.wav", "onset_threshold": 0.6, "frame_threshold": 0.3,
                                   "minimum_note_ms": 35.0, "melodia_trick": False, "melodia_below": 52})
    assert calls[0] == ("inference", "bass.wav") and len(calls) == 3
    split = pytest.approx(440 * 2 ** ((52 - 69) / 12))
    # 35 ms is 3 model frames; both decodings saw the whole output
    assert calls[1] == (352.0, 0.6, 0.3, 3, split, None, False)
    assert calls[2] == (352.0, 0.6, 0.3, 3, None, split, True)
    assert [(n["start"], n["pitch"]) for n in result["notes"]] == [(0.1, 40), (0.1, 64), (0.5, 40), (0.5, 64)]
    assert result["notes"][0]["bends"] == [] and result["notes"][2]["bends"] == [1, 1]
    assert '"progress"' in capsys.readouterr().out


def test_octave_ghosts_of_low_notes_are_dropped():
    bass = {"start": 0.0, "end": 1.0, "pitch": 33}
    ghost = {"start": 0.05, "end": 0.9, "pitch": 45}      # its second harmonic, read as a note
    jump = {"start": 1.0, "end": 1.5, "pitch": 45}        # an octave up after it: a note
    lead = {"start": 0.0, "end": 1.0, "pitch": 57}        # over the split: kept
    lead_ghost = {"start": 0.0, "end": 1.0, "pitch": 69}
    events = [bass, ghost, jump, lead, lead_ghost]
    assert ml.drop_octave_ghosts(events, 52) == [bass, jump, lead, lead_ghost]
    assert ml.drop_octave_ghosts(events, None) == events


def test_bends_are_corrected_by_one_bin():
    # Basic Pitch 0.4.0 reads an in-tune note as +1 (a third of a semitone)
    assert ml.bend_cents([1, 1, 2, 0, -2]) == [0, 0, 33, -33, -100]
    assert ml.bend_cents([]) == []


def test_velocity_follows_the_level_below_the_loudest_note():
    # 40·log10(velocity / 127) dB: 12 dB down is 64, 24 dB down 32
    assert ml.level_velocities([-10.0, -22.0, -34.0, float("nan"), -200.0]) == [127, 64, 32, 64, 1]
    assert ml.level_velocities([]) == []


def _sounds(tmp_path, names):
    folder = tmp_path / "Samples"
    folder.mkdir(exist_ok=True)
    paths = []
    for name in names:
        path = folder / name
        path.write_bytes(name.encode())
        paths.append(path)
    return paths


def test_audio_files_lists_audio_only(tmp_path):
    _sounds(tmp_path, ["Kick.wav", "Snare.aif", "notes.txt", ".hidden.wav"])
    (tmp_path / "Samples" / "Backup").mkdir()
    (tmp_path / "Samples" / "Backup" / "Old.wav").write_bytes(b"x")
    assert [p.name for p in ml.audio_files([tmp_path])] == ["Kick.wav", "Snare.aif"]


def test_sound_index_finds_the_nearest_and_persists(tmp_path):
    files = _sounds(tmp_path, ["Kick 1.wav", "Kick 2.wav", "Hat.wav"])
    index = ml.SoundIndex("laion/test", folder=tmp_path / "index")
    assert index.stale(files) == files
    vectors = np.array([[1, 0, 0], [0.9, 0.1, 0], [0, 1, 0]], dtype=np.float32)
    assert index.add(files, vectors) == 3
    index.save()

    again = ml.SoundIndex("laion/test", folder=tmp_path / "index")
    assert again.stale(files) == []
    found = again.search([1, 0, 0], files, top=2)
    assert [r["file"] for r in found] == ["Kick 1.wav", "Kick 2.wav"]
    assert found[0]["score"] == 1.0
    assert [r["file"] for r in again.search([1, 0, 0], files, top=1,
                                            exclude=str(files[0]))] == ["Kick 2.wav"]
    # A changed file is indexed again; a vector that could not be computed is skipped
    files[2].write_bytes(b"a longer hat")
    assert again.stale(files) == [files[2]]
    assert again.add([files[2]], [np.full(3, np.nan)]) == 0


def test_damaged_index_starts_over(tmp_path):
    folder = tmp_path / "index"
    folder.mkdir()
    (folder / "laion-test.npz").write_bytes(b"not numpy")
    assert ml.SoundIndex("laion/test", folder=folder).entries == {}


@pytest.mark.skipif(os.environ.get("ABLETON_MCP_ML_TESTS") != "1",
                    reason="downloads models; set ABLETON_MCP_ML_TESTS=1")
def test_real_transcription(tmp_path):
    import soundfile as sf
    rate = 22050
    t = np.arange(rate) / rate
    sf.write(tmp_path / "a4.wav", 0.5 * np.sin(2 * np.pi * 440 * t), rate)
    result = asyncio.run(ml.run("transcribe", {"path": str(tmp_path / "a4.wav")}))
    assert 69 in {n["pitch"] for n in result["notes"]}
    # Bends come back, one per model frame, and an in-tune A4 corrects to 0 cents
    assert result["frame_s"] == pytest.approx(256 / 22050)
    a4 = max((n for n in result["notes"] if n["pitch"] == 69), key=lambda n: n["end"] - n["start"])
    assert len(a4["bends"]) >= 20
    assert np.median(ml.bend_cents(a4["bends"])) == 0


def _808_line(path, rate=22050):
    """Eight 808 notes in quarters at 140 BPM, MIDI 28-40: a 2 ms attack, a
    1.5-semitone drop over 40 ms, a 0.6 s decay, tanh drive, each cut by the
    next."""
    import soundfile as sf
    pitches = [28, 31, 33, 28, 36, 35, 40, 31]
    step = 60 / 140
    y = np.zeros(int((0.5 + len(pitches) * step + 1) * rate))
    for k, pitch in enumerate(pitches):
        t = np.arange(int(step * rate)) / rate
        f = 440 * 2 ** ((pitch - 69 + 1.5 * np.exp(-t / 0.04)) / 12)
        start = int((0.5 + k * step) * rate)
        tone = np.tanh(2 * np.exp(-t / 0.6) * np.sin(2 * np.pi * np.cumsum(f) / rate)) / np.tanh(2)
        y[start:start + len(t)] += 0.7 * np.minimum(1, t / 0.002) * tone
    sf.write(path, y, rate)
    return [(0.5 + k * step, p) for k, p in enumerate(pitches)]


@pytest.mark.skipif(os.environ.get("ABLETON_MCP_ML_TESTS") != "1",
                    reason="downloads models; set ABLETON_MCP_ML_TESTS=1")
def test_real_transcription_finds_a_low_808_line_by_default(tmp_path):
    truth = _808_line(tmp_path / "808.wav")

    def found(settings):
        job = {"path": str(tmp_path / "808.wav"), **{k: v for k, v in settings.items() if k != "velocity"}}
        events = ml.drop_octave_ghosts(asyncio.run(ml.run("transcribe", job))["notes"], job.get("melodia_below"))
        return sum(any(e["pitch"] == p and abs(e["start"] - t) <= 0.05 for e in events) for t, p in truth)

    # Melodia off at every pitch found none of them; the default 6 of 8 within
    # 50 ms. Undriven (pure sines) it found 7, but only 3 within 50 ms: Basic
    # Pitch starts its lowest notes late (up to 170 ms here)
    assert found(ml.transcription_settings("melodic", melodia_trick=False)) <= 1
    assert found(ml.transcription_settings("melodic")) >= 6


def test_worker_has_no_server_imports():
    source = Path(ml.WORKER).read_text(encoding="utf-8")
    assert "MCP_Server" not in source.split('"""', 2)[2]


def test_generation_prompts_take_the_sets_tempo_and_key_unless_given():
    from MCP_Server import ml

    assert ml.generation_prompt("lo-fi drums.", 92, "A", "Minor") == "lo-fi drums, 92 bpm, in A minor"
    assert ml.generation_prompt("techno at 130 BPM in F minor", 120, "C", "Major") == \
        "techno at 130 BPM in F minor"
    assert ml.generation_prompt("ambient pad", None, None) == "ambient pad"
    assert ml.file_stem("Dark, gritty 808 kick!", "20261011-1200") == "dark-gritty-808-kick-20261011-1200"
    assert ml.generated_folder([]).name == "generated"


def test_minimum_frequency_never_goes_under_basic_pitchs_lowest_note():
    from MCP_Server import ml_worker
    assert ml_worker.frequency_floor(25.0) == 27.5
    assert ml_worker.frequency_floor(41.2) == 41.2
    assert ml_worker.frequency_floor(None) is None
