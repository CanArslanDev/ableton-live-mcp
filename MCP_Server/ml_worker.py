"""Runs one machine-learning job in its own Python environment.

The server (ml.py) starts this file with `uv run`, in an environment holding
only the job's libraries (ml.ENVIRONMENTS), so torch and friends never load
into the MCP server and Basic Pitch can have the older Python it needs. The
job arrives as JSON on stdin; progress and the outcome go to stdout as JSON
lines: {"progress": 0..1, "message": ...}, then {"result": ...} or
{"error": ...}. Logs go to stderr.

This file imports nothing from MCP_Server: it does not run in the server's
environment.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path


def emit(**payload) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def progress(fraction: float, message: str) -> None:
    emit(progress=round(max(0.0, min(1.0, fraction)), 3), message=message)


def torch_device(torch, wanted: str | None = None) -> str:
    if wanted:
        return wanted
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def read_audio(path: str, rate: int | None = None, mono: bool = False, seconds: float | None = None):
    """(channels × samples float32 array, sample rate), resampled if `rate`."""
    import numpy as np
    import soundfile as sf

    with sf.SoundFile(path) as f:
        frames = f.frames if seconds is None else min(f.frames, int(seconds * f.samplerate))
        audio = f.read(frames, dtype="float32", always_2d=True).T
        source_rate = f.samplerate
    if mono:
        audio = audio.mean(axis=0, keepdims=True)
    if rate and rate != source_rate:
        from math import gcd

        from scipy.signal import resample_poly
        g = gcd(int(rate), int(source_rate))
        audio = resample_poly(audio, int(rate) // g, int(source_rate) // g, axis=1).astype(np.float32)
        source_rate = rate
    return audio, source_rate


# ── Source separation (Demucs) ───────────────────────────────────────────────

def separate(job: dict) -> dict:
    import numpy as np
    import soundfile as sf
    import torch
    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    progress(0.02, "loading the model")
    model = get_model(job.get("model") or "htdemucs")
    model.eval()
    audio, rate = read_audio(job["path"], model.samplerate)
    if audio.shape[0] == 1:
        audio = np.repeat(audio, 2, axis=0)
    audio = audio[: model.audio_channels]
    wav = torch.from_numpy(audio)
    reference = wav.mean(0)
    mean, std = reference.mean(), reference.std() + 1e-8
    wav = (wav - mean) / std
    shifts = max(1, int(job.get("shifts", 1)))
    length = wav.shape[1]
    segment = int(float(getattr(model, "segment", 7.8)) * model.samplerate)

    def chunk_done(info: dict) -> None:
        # Called before and after each chunk, for each model of a bag and each shift
        if info.get("state") != "end":
            return
        within = min(1.0, (info.get("segment_offset", 0) + segment) / max(1, length))
        done = (info.get("model_idx_in_bag", 0)
                + (info.get("shift_idx", 0) + within) / shifts) / max(1, info.get("models", 1))
        progress(0.1 + 0.8 * done, "separating")

    def run(device: str):
        with torch.no_grad():
            return apply_model(model, wav[None], device=device, split=True, overlap=0.25,
                               shifts=shifts, callback=chunk_done)[0]

    device = torch_device(torch, job.get("device"))
    progress(0.1, f"separating on {device}")
    try:
        sources = run(device)
    except Exception as e:  # an operation missing on this accelerator: the CPU has them all
        if device == "cpu":
            raise
        print(f"{device} failed ({e}); using the CPU", file=sys.stderr)
        device = "cpu"
        sources = run(device)
    sources = (sources * std + mean).cpu().numpy()
    names = list(model.sources)
    stems = {name: sources[i] for i, name in enumerate(names)}
    keep = job.get("two_stems")
    if keep:
        if keep not in stems:
            raise ValueError(f"No stem {keep!r} in this model; it has {', '.join(names)}")
        stems = {keep: stems[keep], f"no_{keep}": sum(s for n, s in stems.items() if n != keep)}
    out = Path(job["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    progress(0.92, "writing the stems")
    files = {}
    for name, data in stems.items():
        target = out / f"{name}.wav"
        sf.write(str(target), data.T, rate, subtype="PCM_24")
        files[name] = str(target)
    return {"stems": files, "sample_rate": rate, "model": job.get("model") or "htdemucs",
            "device": device, "seconds": round(audio.shape[1] / rate, 2)}


# ── Audio to MIDI (Basic Pitch) ──────────────────────────────────────────────

# Basic Pitch's lowest note is A0 (MIDI 21, 27.5 Hz). A minimum frequency under
# it turns into a negative bin index and every note is dropped: 25 Hz gave 0
# notes on a bass stem where 30 Hz gave 232 (measured with basic-pitch 0.4.0).
LOWEST_HZ = 27.5


def frequency_floor(hz: float | None) -> float | None:
    return None if hz is None else max(float(hz), LOWEST_HZ)


def transcribe(job: dict) -> dict:
    """Basic Pitch's notes with their pitch bends as the model gives them:
    thirds of a semitone, one per model frame (FFT_HOP / AUDIO_SAMPLE_RATE s)
    from the note's start; ml.bend_cents corrects them. predict() always
    estimates bends; multiple_pitch_bends only shapes its MIDI object."""
    from basic_pitch import ICASSP_2022_MODEL_PATH
    from basic_pitch.constants import AUDIO_SAMPLE_RATE, FFT_HOP
    from basic_pitch.inference import Model, predict

    progress(0.05, "loading the model")
    model = Model(ICASSP_2022_MODEL_PATH)
    progress(0.2, "listening")
    _output, _midi, events = predict(
        job["path"], model,
        onset_threshold=float(job.get("onset_threshold", 0.5)),
        frame_threshold=float(job.get("frame_threshold", 0.3)),
        minimum_note_length=float(job.get("minimum_note_ms", 127.7)),
        minimum_frequency=frequency_floor(job.get("minimum_frequency")),
        maximum_frequency=job.get("maximum_frequency"),
        multiple_pitch_bends=False,
        melodia_trick=bool(job.get("melodia_trick", True)))
    notes = [{"start": round(float(start), 4), "end": round(float(end), 4), "pitch": int(pitch),
              "amplitude": round(float(amplitude), 4),
              "bends": [int(b) for b in bends] if bends is not None else []}
             for start, end, pitch, amplitude, bends in events]
    notes.sort(key=lambda n: (n["start"], n["pitch"]))
    return {"notes": notes, "frame_s": FFT_HOP / AUDIO_SAMPLE_RATE}


# ── Sound embeddings (CLAP) ──────────────────────────────────────────────────

def embed(job: dict) -> dict:
    import numpy as np
    import torch
    from transformers import ClapModel, ClapProcessor

    name = job.get("model") or "laion/clap-htsat-unfused"
    progress(0.02, "loading the model")
    model = ClapModel.from_pretrained(name).eval()
    processor = ClapProcessor.from_pretrained(name)
    device = torch_device(torch, job.get("device"))
    try:
        model = model.to(device)
    except Exception:
        device = "cpu"
    texts = job.get("texts") or []
    files = job.get("files") or []
    out: dict[str, np.ndarray] = {}

    def normalized(features) -> np.ndarray:
        if hasattr(features, "pooler_output"):  # some versions return a model output
            features = features.pooler_output
        features = torch.nn.functional.normalize(features, dim=-1)
        return features.detach().cpu().numpy().astype(np.float32)

    with torch.no_grad():
        if texts:
            inputs = processor(text=texts, return_tensors="pt", padding=True).to(device)
            out["texts"] = normalized(model.get_text_features(**inputs))
        vectors, failed = [], []
        batch = int(job.get("batch", 16))
        dim = None
        for start in range(0, len(files), batch):
            chunk, ok = [], []
            for index in range(start, min(start + batch, len(files))):
                try:
                    audio, _rate = read_audio(files[index], 48000, mono=True,
                                              seconds=float(job.get("seconds", 10)))
                    chunk.append(audio[0])
                    ok.append(index)
                except Exception as e:  # unreadable file: skip it, report it
                    failed.append({"index": index, "error": str(e)[:200]})
            if chunk:
                inputs = processor(audio=chunk, sampling_rate=48000, return_tensors="pt").to(device)
                features = normalized(model.get_audio_features(**inputs))
                dim = features.shape[1]
                for index, vector in zip(ok, features):
                    vectors.append((index, vector))
            progress(0.05 + 0.9 * min(1.0, (start + batch) / max(1, len(files))),
                     f"listening to {min(start + batch, len(files))} of {len(files)} sounds")
        if files:
            matrix = np.full((len(files), dim or 512), np.nan, dtype=np.float32)
            for index, vector in vectors:
                matrix[index] = vector
            out["files"] = matrix
    np.savez(job["out"], **out)
    return {"texts": len(texts), "files": len(files), "failed": failed if files else [],
            "model": name, "device": device}


# ── Audio generation (MusicGen) ──────────────────────────────────────────────

def generate(job: dict) -> dict:
    """Text to audio with MusicGen: one WAV per variation, peak at -1 dBFS,
    a 10 ms fade at the end."""
    import numpy as np
    import soundfile as sf
    import torch
    from transformers import (AutoProcessor, LogitsProcessor, LogitsProcessorList,
                              MusicgenForConditionalGeneration)

    name = job.get("model") or "facebook/musicgen-small"
    seconds = float(job["seconds"])
    variations = max(1, int(job.get("variations", 1)))
    progress(0.02, "loading MusicGen")
    processor = AutoProcessor.from_pretrained(name)
    model = MusicgenForConditionalGeneration.from_pretrained(name).eval()
    device = torch_device(torch, job.get("device"))
    rate = int(model.config.audio_encoder.sampling_rate)
    tokens = int(seconds * float(model.config.audio_encoder.frame_rate)) + 4

    # Called once per generated step (transformers 5.x no longer feeds
    # MusicGen's streamer), so it can tell how far a take has got
    class Progress(LogitsProcessor):
        def __init__(self, index: int) -> None:
            self.index, self.done = index, 0

        def __call__(self, input_ids, scores):
            self.done += 1
            if self.done % 25 == 0:
                share = (self.index + min(1.0, self.done / tokens)) / variations
                progress(0.08 + 0.88 * share, f"generating {self.index + 1} of {variations}")
            return scores

    def make(index: int, on: str) -> "np.ndarray":
        nonlocal model
        model = model.to(on)
        inputs = processor(text=[job["prompt"]], padding=True, return_tensors="pt").to(on)
        torch.manual_seed(int(job.get("seed", 0)) + index)
        with torch.no_grad():
            audio = model.generate(**inputs, do_sample=True, guidance_scale=3.0,
                                   max_new_tokens=tokens,
                                   logits_processor=LogitsProcessorList([Progress(index)]))
        return audio[0, 0].float().cpu().numpy()

    out_dir = Path(job["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for index in range(variations):
        try:
            audio = make(index, device)
        except RuntimeError as e:  # Apple's GPU can fail where the CPU does not
            if device == "cpu":
                raise
            progress(0.08, f"{device} failed ({str(e)[:80]}); on the CPU")
            device = "cpu"
            audio = make(index, device)
        audio = audio[: int(seconds * rate)]
        peak = float(np.max(np.abs(audio))) or 1.0
        audio = audio * (10 ** (-1 / 20) / peak)
        fade = min(len(audio), int(0.01 * rate))
        if fade:
            audio[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=audio.dtype)
        path = out_dir / f"{job['stem']}{'' if variations == 1 else f'-{index + 1}'}.wav"
        sf.write(str(path), audio, rate, subtype="PCM_24")
        files.append(str(path))
    return {"files": files, "rate": rate, "seconds": round(len(audio) / rate, 3),
            "model": name, "device": device}


def echo(job: dict) -> dict:
    """For tests: the protocol without any library."""
    import time

    progress(0.5, "echo")
    time.sleep(float(job.get("sleep", 0)))
    if job.get("fail"):
        raise ValueError(job["fail"])
    return job


TASKS = {"separate": separate, "transcribe": transcribe, "embed": embed, "generate": generate,
         "echo": echo}


def main() -> int:
    task = sys.argv[1] if len(sys.argv) > 1 else ""
    if task not in TASKS:
        emit(error=f"Unknown task {task!r}; known: {', '.join(TASKS)}")
        return 2
    try:
        job = json.loads(sys.stdin.read() or "{}")
        emit(result=TASKS[task](job))
        return 0
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        emit(error=f"{type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
