"""Offline analysis of rendered or recorded audio.

Gives the model something concrete to "hear": loudness (ITU-R BS.1770
integrated LUFS), peaks, dynamics, tonal balance, stereo image, tempo and key
estimates, plus plain-language observations a mix engineer would flag.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

BANDS = [
    ("sub", 20, 60),
    ("bass", 60, 250),
    ("low_mid", 250, 500),
    ("mid", 500, 2000),
    ("high_mid", 2000, 6000),
    ("air", 6000, 20000),
]

_PITCH_CLASSES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
# Krumhansl–Kessler key profiles
_MAJOR_PROFILE = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR_PROFILE = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


def _db(x: float, floor: float = -120.0) -> float:
    return round(max(floor, 20 * math.log10(x)) if x > 0 else floor, 2)


def load_audio(path: str | Path, max_seconds: float | None = 600.0) -> tuple[np.ndarray, int]:
    """Return (samples[frames, channels] float64 in -1..1, sample_rate)."""
    import soundfile as sf

    path = Path(path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"Audio file not found: {path}")
    info = sf.info(str(path))
    frames = -1
    if max_seconds is not None:
        frames = min(info.frames, int(max_seconds * info.samplerate))
    data, rate = sf.read(str(path), frames=frames, dtype="float64", always_2d=True)
    return data, int(rate)


def _k_weight(x: np.ndarray, rate: int) -> np.ndarray:
    """BS.1770 K-weighting (high-shelf + high-pass), coefficients for any rate."""
    from scipy.signal import lfilter

    # Stage 1: high shelf
    f0, gain_db, q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    k = math.tan(math.pi * f0 / rate)
    vh = 10 ** (gain_db / 20)
    vb = vh ** 0.4996667741545416
    a0 = 1 + k / q + k * k
    b = [(vh + vb * k / q + k * k) / a0, 2 * (k * k - vh) / a0, (vh - vb * k / q + k * k) / a0]
    a = [1, 2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0]
    y = lfilter(b, a, x, axis=0)
    # Stage 2: high pass
    f0, q = 38.13547087602444, 0.5003270373238773
    k = math.tan(math.pi * f0 / rate)
    a0 = 1 + k / q + k * k
    a = [1, 2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0]
    return lfilter([1, -2, 1], a, y, axis=0)


def integrated_loudness(data: np.ndarray, rate: int) -> tuple[float, float, float]:
    """(integrated LUFS, max short-term LUFS, loudness range LU)."""
    weighted = _k_weight(data, rate)
    channel_weights = np.ones(data.shape[1])  # L, R (surround weights unused)

    def block_loudness(block_s: float, overlap: float) -> np.ndarray:
        size = int(block_s * rate)
        step = max(1, int(size * (1 - overlap)))
        if len(weighted) < size:
            size = step = len(weighted)
        if size == 0:
            return np.array([])
        starts = range(0, len(weighted) - size + 1, step)
        power = np.array([(np.mean(weighted[s:s + size] ** 2, axis=0) * channel_weights).sum()
                          for s in starts])
        with np.errstate(divide="ignore"):
            return -0.691 + 10 * np.log10(power)

    momentary = block_loudness(0.4, 0.75)
    gated = momentary[momentary > -70]
    if gated.size == 0:
        integrated = -70.0
    else:
        rel = -0.691 + 10 * np.log10(np.mean(10 ** ((gated + 0.691) / 10))) - 10
        final = gated[gated > rel]
        integrated = float(-0.691 + 10 * np.log10(np.mean(10 ** ((final + 0.691) / 10)))) \
            if final.size else -70.0

    short = block_loudness(3.0, 2 / 3)
    short_valid = short[np.isfinite(short) & (short > -70)]
    max_short = float(short_valid.max()) if short_valid.size else -70.0
    lra = 0.0
    if short_valid.size > 2:
        rel = -0.691 + 10 * np.log10(np.mean(10 ** ((short_valid + 0.691) / 10))) - 20
        s = np.sort(short_valid[short_valid > rel])
        if s.size > 2:
            lra = float(np.percentile(s, 95) - np.percentile(s, 10))
    return round(integrated, 2), round(max_short, 2), round(lra, 2)


def true_peak(data: np.ndarray) -> float:
    """4x-oversampled peak estimate (dBTP)."""
    from scipy.signal import resample_poly

    chunk = data[: min(len(data), 48000 * 120)]
    if len(chunk) == 0:
        return -120.0
    over = resample_poly(chunk, 4, 1, axis=0)
    return _db(float(np.max(np.abs(over))))


def band_balance(data: np.ndarray, rate: int) -> tuple[dict[str, float], float]:
    """Energy share per band, averaged over channels (so side-only content
    that cancels in a mono sum is still counted)."""
    from scipy.signal import welch

    freqs, psd = welch(data, fs=rate, nperseg=min(8192, max(256, len(data))), axis=0)
    psd = psd.mean(axis=1)
    total = float(psd[(freqs >= 20) & (freqs <= 20000)].sum()) or 1e-20
    bands = {}
    for name, lo, hi in BANDS:
        share = float(psd[(freqs >= lo) & (freqs < hi)].sum()) / total
        bands[name] = round(100 * share, 1)
    centroid = float((freqs * psd).sum() / (psd.sum() or 1e-20))
    return bands, round(centroid, 1)


def estimate_tempo(mono: np.ndarray, rate: int) -> float | None:
    """Spectral-flux onset envelope + autocorrelation, 70–180 BPM.

    Lags come in frames (11.6 ms at 44.1 kHz, 3.9 BPM apart at 140), so the
    beat lag is refined below a frame: a parabola through the peak at one
    beat, then through the peaks at 2, 4, ... 32 beats while they stand out,
    each dividing the error by its beat count. A 137 BPM click track read
    136.0 on whole frames, 136.73 with the one-beat parabola, 137.005 with
    the multiples.
    """
    hop = 512
    win = 2048
    if len(mono) < win * 8:
        return None
    frames = 1 + (len(mono) - win) // hop
    idx = np.arange(win)[None, :] + hop * np.arange(frames)[:, None]
    spec = np.abs(np.fft.rfft(mono[idx] * np.hanning(win), axis=1))
    flux = np.maximum(0, np.diff(np.log1p(spec), axis=0)).sum(axis=1)
    # Steady material (drones, pads, test tones) has no onsets to lock onto
    if flux.max() < 4 * (np.median(flux) + 1e-6):
        return None
    flux -= flux.mean()
    ac = np.correlate(flux, flux, mode="full")[len(flux) - 1:]
    fps = rate / hop
    lags = np.arange(len(ac))
    with np.errstate(divide="ignore"):
        bpm = 60 * fps / lags
    mask = (bpm >= 70) & (bpm <= 180)
    if not mask.any():
        return None
    best = lags[mask][np.argmax(ac[mask])]
    # A flat onset envelope (drones, pads, test tones) has no beat to find
    if ac[0] <= 0 or ac[best] / ac[0] < 0.2:
        return None
    period = peak_lag(ac, int(best))
    beats = 2
    while beats <= 32:
        guess = int(round(beats * period))
        if guess + 2 >= len(ac) // 2:  # too little overlap left at this lag
            break
        k = guess - 1 + int(np.argmax(ac[guess - 1:guess + 2]))
        if ac[k] / ac[0] < 0.2:
            break
        period = peak_lag(ac, k) / beats
        beats *= 2
    return round(float(60 * fps / period), 2)


def peak_lag(ac: np.ndarray, k: int) -> float:
    """The autocorrelation peak at or next to lag k, to a fraction of a lag:
    up to three steps uphill, then the vertex of the parabola through the
    peak and its two neighbours."""
    for _ in range(3):
        if not 0 < k < len(ac) - 1 or ac[k] >= max(ac[k - 1], ac[k + 1]):
            break
        k = k - 1 if ac[k - 1] > ac[k + 1] else k + 1
    if not 0 < k < len(ac) - 1:
        return float(k)
    before, top, after = ac[k - 1], ac[k], ac[k + 1]
    curve = before - 2 * top + after
    if curve >= 0:
        return float(k)
    return k + float(np.clip(0.5 * (before - after) / curve, -0.5, 0.5))


def estimate_key(mono: np.ndarray, rate: int) -> dict | None:
    win = 8192
    if len(mono) < win:
        return None
    hop = win
    chroma = np.zeros(12)
    freqs = np.fft.rfftfreq(win, 1 / rate)
    valid = (freqs > 50) & (freqs < 5000)
    pcs = (np.round(12 * np.log2(freqs[valid] / 440.0)) + 9).astype(int) % 12
    window = np.hanning(win)
    for start in range(0, len(mono) - win, hop):
        mag = np.abs(np.fft.rfft(mono[start:start + win] * window))[valid]
        np.add.at(chroma, pcs, mag)
    if chroma.sum() == 0:
        return None
    best = None
    for tonic in range(12):
        for mode, profile in (("major", _MAJOR_PROFILE), ("minor", _MINOR_PROFILE)):
            r = float(np.corrcoef(chroma, np.roll(profile, tonic))[0, 1])
            if best is None or r > best[0]:
                best = (r, tonic, mode)
    return {"key": f"{_PITCH_CLASSES[best[1]]} {best[2]}", "confidence": round(best[0], 2)}


# A band one semitone wide: a 12-per-octave constant-Q filter spans
# 1 / (2^(1/12) - 1) = 16.8 cycles of its frequency under a Hann window
SEMITONE_Q = 1 / (2 ** (1 / 12) - 1)
# Below MIDI 53 (168 Hz) those 16.8 cycles last longer than 100 ms: 0.51 s at
# MIDI 24. A window that long, centred on the note's start, read the silence
# or the note before it: on equal-level 808 lines (decaying sines, MIDI 24-45,
# cut by the next note) the levels spread 3.6 dB in quarters at 140 BPM, 4.1
# in 8ths and 10.0 in 16ths, and a sine after silence read 2.8 dB low at MIDI
# 24. Capped at 100 ms and kept inside the note: 0.08, 0.12 and 0.18 dB.
LEVEL_WINDOW_S = 0.1
# Basic Pitch's onsets are off by a frame or two (11.6 ms each)
LEVEL_EARLY_S = 0.02


def note_levels(mono: np.ndarray, rate: int, notes: list[dict],
                span_s: float = 0.06, step_s: float = 0.005) -> np.ndarray:
    """Each note's level at its own pitch, in dB (a sine of amplitude A reads
    20·log10(A)): a Hann-windowed band at the note's fundamental, one
    semitone wide but at most 100 ms long, the loudest of its readings every
    5 ms from a window starting 20 ms before the note's start to one starting
    60 ms after it. With an "end", no window runs past it unless the note is
    shorter than the window. notes carry "start" (seconds) and "pitch" (MIDI).

    A level, unlike Basic Pitch's amplitude (a confidence): on synthetic
    plucks it ranked the notes by their true level far better (see
    audio_to_midi). Other notes within a semitone, or with a harmonic there,
    add to it, so sustained chords measure poorly. Below MIDI 53 the band
    is wider than a semitone: an equal-level note starting with it 2
    semitones away reads 1.4 dB under it at MIDI 28 (13.5 dB with the
    16.8-cycle window), 5 semitones away 10.8 dB; at MIDI 57, 2 semitones
    away, 21.9 dB. So a kick on an 808 note at the same level adds 0.9 dB
    on average, 3.6 dB at most, and an 808 left ringing under the next
    note leaks into it. Decaying notes are read over their first 100 ms
    at most, high notes over less: equal-level plucks decaying with an
    80 ms time constant read 3.2 dB lower at MIDI 36-51 than at MIDI 96
    (6.9 dB before the cap), 0.6 dB with 0.5 s. Onsets 50 ms late on
    16th-note 808s at 140 BPM spread the levels by 1.5 dB."""
    levels = np.full(len(notes), np.nan)
    if not notes or not len(mono):
        return levels
    mono = np.asarray(mono, dtype=np.float64)
    longest = int(LEVEL_WINDOW_S * rate) + 1
    early, span = int(round(LEVEL_EARLY_S * rate)), int(round(span_s * rate))
    step = max(1, int(round(step_s * rate)))
    margin = early + span + longest
    padded = np.concatenate([np.zeros(margin), mono, np.zeros(margin)])
    kernels: dict[int, np.ndarray] = {}
    for index, note in enumerate(notes):
        pitch = int(note["pitch"])
        if pitch not in kernels:
            freq = 440.0 * 2 ** ((pitch - 69) / 12)
            window = np.hanning(min(int(SEMITONE_Q * rate / freq) + 1, longest))
            phase = np.exp(-2j * np.pi * freq * np.arange(len(window)) / rate)
            kernels[pitch] = window * phase * (2 / window.sum())
        kernel = kernels[pitch]
        start = int(round(float(note["start"]) * rate))
        last = start + span
        if note.get("end") is not None:
            last = min(last, int(round(float(note["end"]) * rate)) - len(kernel))
        firsts = np.arange(start - early, max(start, last) + 1, step) + margin
        firsts = np.clip(firsts, 0, len(padded) - len(kernel))
        frames = padded[firsts[:, None] + np.arange(len(kernel))[None, :]]
        levels[index] = 20 * np.log10(max(float(np.abs(frames @ kernel).max()), 1e-9))
    return levels


def analyze(path: str | Path, max_seconds: float | None = 600.0) -> dict:
    data, rate = load_audio(path, max_seconds)
    if data.size == 0:
        raise ValueError("Audio file is empty")
    mono = data.mean(axis=1)
    # Anti-phase material cancels in the sum; estimate tempo/key from one side
    if np.mean(mono ** 2) < 0.1 * np.mean(data ** 2):
        mono = data[:, 0]
    peak = float(np.max(np.abs(data)))
    rms = float(np.sqrt(np.mean(data ** 2)))
    lufs, max_short, lra = integrated_loudness(data, rate)
    tp = true_peak(data)
    bands, centroid = band_balance(data, rate)

    stereo = None
    if data.shape[1] >= 2:
        left, right = data[:, 0], data[:, 1]
        denom = float(np.sqrt((left ** 2).sum() * (right ** 2).sum()))
        corr = float((left * right).sum() / denom) if denom else 1.0
        mid = (left + right) / 2
        side = (left - right) / 2
        side_ratio = _db(float(np.sqrt(np.mean(side ** 2))) /
                         (float(np.sqrt(np.mean(mid ** 2))) or 1e-12))
        low = _lowpass(mid, side, rate)
        stereo = {"correlation": round(corr, 3), "side_to_mid_db": side_ratio,
                  "low_end_side_to_mid_db": low}

    result = {
        "file": str(Path(path).expanduser()),
        "duration_seconds": round(len(data) / rate, 2),
        "sample_rate": rate,
        "channels": int(data.shape[1]),
        "integrated_lufs": lufs,
        "max_short_term_lufs": max_short,
        "loudness_range_lu": lra,
        "sample_peak_dbfs": _db(peak),
        "true_peak_dbtp": tp,
        "rms_dbfs": _db(rms),
        "crest_factor_db": round(_db(peak) - _db(rms), 2),
        "clipped_samples": int(np.sum(np.abs(data) >= 0.9999)),
        "dc_offset": round(float(np.mean(mono)), 5),
        "band_energy_percent": bands,
        "spectral_centroid_hz": centroid,
        "stereo": stereo,
        "tempo_estimate_bpm": estimate_tempo(mono, rate),
        "key_estimate": estimate_key(mono, rate),
    }
    result["observations"] = observations(result)
    return result


def _lowpass(mid: np.ndarray, side: np.ndarray, rate: int) -> float:
    from scipy.signal import butter, sosfilt

    sos = butter(4, 150, btype="low", fs=rate, output="sos")
    m = float(np.sqrt(np.mean(sosfilt(sos, mid) ** 2)))
    s = float(np.sqrt(np.mean(sosfilt(sos, side) ** 2)))
    return _db(s / (m or 1e-12))


def observations(r: dict) -> list[str]:
    notes = []
    if r["integrated_lufs"] <= -60:
        return ["The file is silent or nearly silent."]
    if r["true_peak_dbtp"] > -1.0:
        notes.append(f"True peak {r['true_peak_dbtp']} dBTP is above -1 dBTP; streaming "
                     "encoders may clip. Lower the limiter ceiling.")
    if r["clipped_samples"] > 0:
        notes.append(f"{r['clipped_samples']} samples at full scale — likely clipping.")
    lufs = r["integrated_lufs"]
    if lufs > -7:
        notes.append(f"Very loud master ({lufs} LUFS); dynamics are probably squashed.")
    elif lufs < -20:
        notes.append(f"Quiet ({lufs} LUFS); streaming targets are around -14 LUFS.")
    if r["crest_factor_db"] < 6:
        notes.append(f"Low crest factor ({r['crest_factor_db']} dB): heavily compressed/limited.")
    b = r["band_energy_percent"]
    if b["sub"] + b["bass"] > 70:
        notes.append("Low end dominates the spectrum (>70% of energy below 250 Hz); "
                     "may sound boomy on full-range systems.")
    if b["sub"] < 2 and b["bass"] < 15:
        notes.append("Very little low end; the track may sound thin.")
    if b["low_mid"] > 25:
        notes.append("Heavy low-mids (250–500 Hz); a source of muddiness.")
    if b["high_mid"] + b["air"] > 35:
        notes.append("Bright/harsh balance: a lot of energy above 2 kHz.")
    if b["high_mid"] + b["air"] < 4:
        notes.append("Dull top end: little energy above 2 kHz.")
    st = r.get("stereo")
    if st:
        if st["correlation"] < 0:
            notes.append("Negative stereo correlation — phase problems, will collapse in mono.")
        elif st["correlation"] > 0.97:
            notes.append("Nearly mono image.")
        if st["low_end_side_to_mid_db"] > -12:
            notes.append("Significant stereo content below 150 Hz; consider mono-ing the low end.")
    if abs(r["dc_offset"]) > 0.01:
        notes.append("DC offset present.")
    return notes


# ── Live's track meters ──────────────────────────────────────────────────────

# Live's output meters read 0..1 and are linear in dB: a sine at 0 dBFS peak
# reads 0.921 and every 12 dB lower reads 12/76 less (measured on Live 12.3.8),
# so 0 is -70 dB and 1 is +6 dB, the ends of Live's meter scale. The meters
# show smoothed peak level.
METER_FLOOR_DB = -70.0
METER_SPAN_DB = 76.0


def meter_to_db(value: float) -> float | None:
    """dBFS of a Live meter reading; None for silence (below -70 dB)."""
    if value is None or value <= 0:
        return None
    return round(METER_FLOOR_DB + METER_SPAN_DB * float(value), 1)


def _channel_stats(readings: list[tuple[float | None, float]], end: float | None) -> dict:
    """Peak and time-weighted average (in dB) of one meter's change events.

    readings: (Live timestamp or None for the value when watching began, value).
    A meter only reports when it changes, so each value holds until the next.
    """
    values = [v for _t, v in readings]
    timed = [(t, v) for t, v in readings if t is not None]
    if readings and readings[0][0] is None and timed:
        timed.insert(0, (timed[0][0], readings[0][1]))
    area = duration = 0.0
    for (t0, v0), (t1, _v1) in zip(timed, timed[1:] + [(end, None)]):
        if t1 is None or t1 <= t0:
            continue
        area += (meter_to_db(v0) or METER_FLOOR_DB) * (t1 - t0)
        duration += t1 - t0
    peak = meter_to_db(max(values)) if values else None
    if duration > 0:
        average = round(area / duration, 1)
    else:
        average = meter_to_db(values[-1]) if values else None
    if peak is None:
        average = None
    return {"peak": peak, "average": average}


def level_report(meters: dict) -> dict:
    """Per-track peak/average dBFS and gain-staging notes from meter readings.

    meters: {observer id: {"track": name, "readings": [(ts, value), ...]}},
    two entries (left and right) per track.
    """
    end = max((t for m in meters.values() for t, _v in m["readings"] if t is not None),
              default=None)
    per_track: dict[str, list[dict]] = {}
    for meter in meters.values():
        per_track.setdefault(meter["track"], []).append(_channel_stats(meter["readings"], end))
    levels = []
    for track, channels in per_track.items():
        peaks = [c["peak"] for c in channels if c["peak"] is not None]
        averages = [c["average"] for c in channels if c["average"] is not None]
        levels.append({"track": track,
                       "peak_dbfs": max(peaks) if peaks else None,
                       "average_dbfs": round(sum(averages) / len(averages), 1)
                       if averages else None})
    # Loudest first, silent tracks last
    levels.sort(key=lambda r: (r["peak_dbfs"] is None, -(r["peak_dbfs"] or 0.0)))

    notes = []
    sounding = [r for r in levels if r["peak_dbfs"] is not None]
    silent = [r["track"] for r in levels if r["peak_dbfs"] is None]
    if not sounding:
        notes.append("Nothing sounded while metering: start playback (play=True) or check "
                     "that clips are playing.")
    master = next((r for r in levels if r["track"] == "Master"), None)
    if master and master["peak_dbfs"] is not None:
        if master["peak_dbfs"] > -0.5:
            notes.append(f"Master peaks at {master['peak_dbfs']} dBFS: no headroom. Without "
                         "a limiter on the master this clips on export; for mixing, aim for "
                         "peaks around -6 dBFS and leave loudness to mastering.")
        elif master["peak_dbfs"] < -18:
            notes.append(f"Master peaks at only {master['peak_dbfs']} dBFS: the mix is very "
                         "quiet; raise track levels evenly.")
    hot = [r["track"] for r in sounding if r["track"] != "Master" and r["peak_dbfs"] > 0]
    if hot:
        notes.append("Above 0 dBFS: " + ", ".join(hot) + ". Live's mixer does not clip "
                     "internally, but devices after the fader and the master can; pull "
                     "these down so tracks peak around -10 to -6 dBFS.")
    if silent and sounding:
        notes.append("No signal on: " + ", ".join(silent) + " (muted, empty or not playing).")
    return {"levels": levels, "observations": notes,
            "meter_scale": "Live meters, smoothed peak: dBFS = 76 × meter − 70 "
                           "(−70 to +6 dB)"}
