"""Machine-learning jobs, run outside the MCP server.

Separating stems (Demucs), turning audio into MIDI (Basic Pitch) and finding
sounds by description (CLAP) need torch or Core ML, model weights and, for
Basic Pitch, an older Python than the server's. None of that belongs in the
server: each job runs `ml_worker.py` in an environment `uv run` builds from
ENVIRONMENTS the first time (a download of a few hundred MB, cached after
that) and gone from memory when the job ends. Progress comes back as JSON
lines and is passed on as MCP progress notifications.

The pure parts (Basic Pitch's presets, seconds to beats, velocities and
pitch bends, the sound index) are here and tested without any model.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import shutil
import sys
import tempfile
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger("AbletonMCPServer")

WORKER = Path(__file__).with_name("ml_worker.py")
AUDIO_SUFFIXES = {".wav", ".aif", ".aiff", ".flac", ".mp3", ".ogg"}


@dataclass(frozen=True)
class Environment:
    python: str
    packages: tuple[str, ...]
    download: str  # what the first run fetches, for the progress message


ENVIRONMENTS = {
    # Demucs 4.1 loads its weights from Hugging Face (htdemucs: ~80 MB)
    "separate": Environment("3.12", ("demucs==4.1.0", "soundfile>=0.12", "scipy>=1.11"),
                            "torch and Demucs, ~400 MB"),
    # Basic Pitch 0.4 wants TensorFlow < 2.15 on Python 3.11+; on 3.10 it
    # runs its own model through Core ML (macOS), ONNX (Windows) or TFLite
    # (its resampy still imports pkg_resources, which setuptools 81 dropped)
    "transcribe": Environment("3.10", ("basic-pitch==0.4.0", "setuptools<81"),
                              "Basic Pitch and Core ML/ONNX, ~150 MB"),
    "embed": Environment("3.12", ("torch>=2.4", "transformers>=4.45,<6", "soundfile>=0.12",
                                  "scipy>=1.11"),
                         "torch, transformers and the CLAP model, ~1.2 GB"),
    # MusicGen small: the libraries above (one cached environment for both)
    "generate": Environment("3.12", ("torch>=2.4", "transformers>=4.45,<6", "soundfile>=0.12",
                                     "scipy>=1.11"),
                            "torch, transformers and MusicGen small, ~2.5 GB"),
}

ProgressCallback = Callable[[float, str], Awaitable[None]]

# The worker answers with one JSON line: a dense 3-minute stem's notes (with
# pitch bends) are several MB, far over asyncio's default 64 KB line limit
LINE_LIMIT = 256 * 1024 * 1024


def uv_executable() -> str:
    """uv, which set up the server too (uv sets $UV for what it runs)."""
    home = Path.home()
    candidates = [os.environ.get("UV"), shutil.which("uv"), str(home / ".local" / "bin" / "uv"),
                  str(home / ".cargo" / "bin" / "uv"), "/opt/homebrew/bin/uv", "/usr/local/bin/uv"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError("This tool sets up its libraries with uv, which was not found. Install "
                       "it (https://docs.astral.sh/uv/) and restart the MCP client.")


def command(task: str) -> list[str]:
    if task not in ENVIRONMENTS:  # tests: no libraries, the server's own Python
        return [sys.executable, str(WORKER), task]
    env = ENVIRONMENTS[task]
    cmd = [uv_executable(), "run", "--isolated", "--no-project", "--python", env.python]
    for package in env.packages:
        cmd += ["--with", package]
    return cmd + ["python", str(WORKER), task]


def child_environment() -> dict[str, str]:
    env = dict(os.environ)
    # The server's own environment must not leak into the job's
    for name in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "UV_PROJECT_ENVIRONMENT", "CONDA_PREFIX"):
        env.pop(name, None)
    env.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", HF_HUB_DISABLE_TELEMETRY="1",
               TOKENIZERS_PARALLELISM="false",
               # an operation Apple's GPU lacks runs on the CPU instead of failing
               PYTORCH_ENABLE_MPS_FALLBACK="1")
    return env


async def run(task: str, job: dict, report: ProgressCallback | None = None,
              timeout: float = 3600.0) -> dict:
    """Run one job and return its result; raise with the worker's message on failure."""
    process = await asyncio.create_subprocess_exec(
        *command(task), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=child_environment(), limit=LINE_LIMIT)
    log: deque[str] = deque(maxlen=60)

    async def read_log() -> None:
        async for raw in process.stderr:
            log.append(raw.decode("utf-8", "replace").rstrip())

    async def read_messages() -> dict:
        result: dict | None = None
        error: str | None = None
        async for raw in process.stdout:
            try:
                message = json.loads(raw)
            except ValueError:
                logger.debug("%s: %s", task, raw.decode("utf-8", "replace").rstrip())
                continue
            if "progress" in message and report is not None:
                await report(float(message["progress"]), str(message.get("message", "")))
            if "result" in message:
                result = message["result"]
            if "error" in message:
                error = str(message["error"])
        await process.wait()
        if error is not None:
            raise RuntimeError(error)
        if result is None:
            tail = "\n".join(list(log)[-12:])
            raise RuntimeError(f"The {task} job ended without a result (exit code "
                               f"{process.returncode}). Last output:\n{tail}")
        return result

    if report is not None and task in ENVIRONMENTS:
        await report(0.0, f"Starting; the first run sets up {ENVIRONMENTS[task].download}")
    process.stdin.write(json.dumps(job).encode("utf-8"))
    await process.stdin.drain()
    process.stdin.close()
    logs = asyncio.create_task(read_log())
    try:
        return await asyncio.wait_for(read_messages(), timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(f"The {task} job took longer than {timeout:.0f} s and was stopped") from None
    finally:
        if process.returncode is None:  # timed out or the request was cancelled
            process.kill()
            await process.wait()
        await logs


# ── Audio generation (MusicGen) ──────────────────────────────────────────────

MUSICGEN_MODEL = "facebook/musicgen-small"
MUSICGEN_LICENSE = ("MusicGen's weights are CC-BY-NC 4.0: what it makes is for "
                    "non-commercial use")
MAX_GENERATED_SECONDS = 30.0


def generation_prompt(prompt: str, tempo: float | None = None, key: str | None = None,
                      scale_name: str | None = None) -> str:
    """The prompt with the set's tempo and key, unless it already gives them:
    MusicGen follows "120 bpm" and "in A minor" in the text."""
    text = prompt.strip().rstrip(".")
    lower = text.lower()
    extra = []
    if tempo and "bpm" not in lower:
        extra.append(f"{tempo:g} bpm")
    if key and " in " not in f" {lower} " and not any(
            word in lower for word in ("major", "minor", "key of")):
        extra.append(f"in {key} {(scale_name or 'major').lower()}")
    return ", ".join([text] + extra)


def generated_folder(libraries: list[Path]) -> Path:
    """In the User Library, where Live's browser finds the files (and can put
    them on Drum Rack pads); else ~/.ableton-mcp/generated."""
    if libraries:
        return libraries[0] / "Samples" / "AbletonMCP Generated"
    return Path.home() / ".ableton-mcp" / "generated"


def file_stem(prompt: str, when: str) -> str:
    words = "".join(c if c.isalnum() else " " for c in prompt.lower()).split()
    return ("-".join(words[:6]) or "sound") + "-" + when


# ── Audio to MIDI: settings, seconds to beats, velocity, bends ───────────────

# Basic Pitch's decoding settings by kind of part. Measured on 63 synthetic
# renders (plucks, arpeggios with 32nd runs, leads with glides, pads; clean,
# with an 808, with a bleed loop, wet), onset F1 (50 ms, 50 cents) averaged
# over 7 groups: legacy (Basic Pitch's own defaults) 0.583, melodic 0.687
# (0.706 with lowest just above the 808), chords 0.636. legacy keeps only
# notes of 12 frames (139 ms) or more: on 32nd runs at 140 BPM (54 ms) its
# F1 was 0.36 against melodic's 0.90. Without melodia, onsets without an
# attack are missed: chord tones sounding on reverberant pads 0.82 (chords)
# against 0.43 (melodic); pure sines with a 10 ms fade-in 15 of 16 against 3.
TRANSCRIPTION_PRESETS = {
    "melodic": {"onset_threshold": 0.6, "frame_threshold": 0.3, "minimum_note_ms": 35.0,
                "melodia_trick": False, "velocity": "level"},
    "chords": {"onset_threshold": 0.6, "frame_threshold": 0.3, "minimum_note_ms": 35.0,
               "melodia_trick": True, "velocity": "level"},
    "legacy": {"onset_threshold": 0.5, "frame_threshold": 0.3, "minimum_note_ms": 127.7,
               "melodia_trick": True, "velocity": "amplitude"},
}

# Basic Pitch 0.4.0's per-note bends count thirds of a semitone from one bin
# too low (get_pitch_bends centres a note on bin 3 * (pitch - 21), the
# semitone's centre is the bin above): in-tune sines at 110-880 Hz read +1
# in 99.4-100 % of their frames. One value per model frame, 256 / 22050 s.
BEND_OFFSET_BINS = 1
CENTS_PER_BEND_BIN = 100.0 / 3
BEND_FRAME_S = 256 / 22050


def transcription_settings(preset: str = "melodic", **given: Any) -> dict:
    """The preset's settings with the given ones (not None) on top."""
    if preset not in TRANSCRIPTION_PRESETS:
        raise ValueError(f"Unknown preset {preset!r}; known: {', '.join(TRANSCRIPTION_PRESETS)}")
    settings = dict(TRANSCRIPTION_PRESETS[preset])
    settings.update({k: v for k, v in given.items() if v is not None})
    return settings


def bend_cents(bends: list[int]) -> list[int]:
    """Basic Pitch's bends (thirds of a semitone) as cents off the note, corrected."""
    return [round((int(b) - BEND_OFFSET_BINS) * CENTS_PER_BEND_BIN) for b in bends]


def level_velocities(levels_db: list[float]) -> list[int]:
    """Velocity from each note's measured level: the loudest note gets 127 and
    the rest 127 × 10^(dB below it / 40), so 12 dB down is 64 and 24 dB down
    32 (gain = 40·log10(velocity/127) dB). An unmeasured level gives 64."""
    levels = [float(v) for v in levels_db]
    finite = [v for v in levels if math.isfinite(v)]
    loudest = max(finite) if finite else 0.0
    return [max(1, min(127, round(127 * 10 ** ((v - loudest) / 40)))) if math.isfinite(v) else 64
            for v in levels]


def place_notes(events: list[dict], beat_of: Callable[[float], float],
                velocities: list[int] | None = None,
                offset_beats: float = 0.0) -> tuple[list[dict], list[int]]:
    """Basic Pitch's note events (seconds, amplitude 0..1) as clip notes, and
    the index of the event behind each.

    beat_of maps a time in the file to beats from the source clip's start;
    offset_beats then moves every note. A note that ends before either
    start is left out (the source clip never plays it), one that starts
    before it starts there. velocities: one per event, else 127 × amplitude
    (Basic Pitch's confidence, not a level)."""
    notes, kept = [], []
    for index, event in enumerate(events):
        start, end = beat_of(float(event["start"])), beat_of(float(event["end"]))
        start, end = max(0.0, start) + offset_beats, end + offset_beats
        if end <= offset_beats or end <= 0:
            continue
        start = max(0.0, start)
        velocity = (velocities[index] if velocities is not None
                    else max(1, min(127, round(127 * float(event["amplitude"])))))
        notes.append({"pitch": int(event["pitch"]), "start_time": round(start, 4),
                      "duration": round(max(1 / 64, end - start), 4), "velocity": int(velocity)})
        kept.append(index)
    return notes, kept


def events_to_notes(events: list[dict], tempo: float, offset_beats: float = 0.0,
                    velocities: list[int] | None = None) -> list[dict]:
    """Basic Pitch's note events as clip notes at one tempo, the file's
    second 0 at offset_beats."""
    beats_per_second = tempo / 60.0
    return place_notes(events, lambda t: t * beats_per_second, velocities, offset_beats)[0]


# ── Finding sounds: the CLAP index ───────────────────────────────────────────

INDEX_DIR = Path.home() / ".ableton-mcp" / "sound-index"
# laion/larger_clap_music gives one text vector for every text in transformers
# 5.x (its text pooler loads untrained); this one ranked drum one-shots by
# description at P@10 0.72 (10 classes, chance 0.10).
CLAP_MODEL = "laion/clap-htsat-unfused"
INDEX_PER_CALL = 1500  # ~2.5 minutes of indexing


def audio_files(folders: list[Path], limit: int = 20000) -> list[Path]:
    found: list[Path] = []
    for folder in folders:
        for root, dirs, files in os.walk(folder):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "Backup")
            for name in sorted(files):
                path = Path(root) / name
                if path.suffix.lower() in AUDIO_SUFFIXES and not name.startswith("."):
                    found.append(path)
                    if len(found) >= limit:
                        return found
    return found


class SoundIndex:
    """CLAP vectors of audio files, kept per model in ~/.ableton-mcp/sound-index."""

    def __init__(self, model: str, folder: Path | None = None) -> None:
        self.model = model
        slug = "".join(c if c.isalnum() else "-" for c in model)
        self.path = (folder or INDEX_DIR) / f"{slug}.npz"
        self.entries: dict[str, tuple[int, float, Any]] = {}  # path → (size, mtime, vector)
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        import numpy as np
        try:
            # Closed again right away: save() replaces this file, which Windows
            # refuses while it is open
            with np.load(self.path, allow_pickle=False) as data:
                columns = [np.array(data[k]) for k in ("paths", "sizes", "mtimes", "vectors")]
            for path, size, mtime, vector in zip(*columns):
                self.entries[str(path)] = (int(size), float(mtime), vector)
        except Exception as e:  # damaged index: start again
            logger.warning("Ignoring the sound index %s: %s", self.path, e)
            self.entries = {}

    def stale(self, files: list[Path]) -> list[Path]:
        """Files never indexed, or changed since."""
        todo = []
        for path in files:
            try:
                stat = path.stat()
            except OSError:
                continue
            known = self.entries.get(str(path))
            if known is None or known[0] != stat.st_size or abs(known[1] - stat.st_mtime) > 1e-3:
                todo.append(path)
        return todo

    def add(self, files: list[Path], vectors) -> int:
        import numpy as np
        added = 0
        for path, vector in zip(files, vectors):
            if vector is None or not np.all(np.isfinite(vector)):
                continue
            stat = path.stat()
            self.entries[str(path)] = (stat.st_size, stat.st_mtime, np.asarray(vector, np.float16))
            added += 1
        return added

    def save(self) -> None:
        import numpy as np
        self.path.parent.mkdir(parents=True, exist_ok=True)
        paths = list(self.entries)
        vectors = (np.stack([self.entries[p][2] for p in paths]).astype(np.float16)
                   if paths else np.zeros((0, 512), np.float16))
        fd, temp = tempfile.mkstemp(dir=self.path.parent, suffix=".npz")
        os.close(fd)
        np.savez(temp, paths=np.array(paths, dtype=str),
                 sizes=np.array([self.entries[p][0] for p in paths], dtype=np.int64),
                 mtimes=np.array([self.entries[p][1] for p in paths], dtype=np.float64),
                 vectors=vectors)
        os.replace(temp, self.path)

    def search(self, query, files: list[Path], top: int = 10,
               exclude: str | None = None) -> list[dict]:
        """The indexed `files` closest to the query vector (cosine similarity)."""
        import numpy as np
        candidates = [(str(p), self.entries[str(p)][2]) for p in files
                      if str(p) in self.entries and str(p) != exclude]
        if not candidates:
            return []
        matrix = np.stack([v for _, v in candidates]).astype(np.float32)
        query = np.asarray(query, np.float32)
        scores = matrix @ (query / (np.linalg.norm(query) or 1.0))
        order = np.argsort(-scores)[:top]
        return [{"file": Path(candidates[i][0]).name, "path": candidates[i][0],
                 "score": round(float(scores[i]), 3)} for i in order]
