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
