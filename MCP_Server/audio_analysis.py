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
    """Spectral-flux onset envelope + autocorrelation, 70–180 BPM."""
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
    return round(float(60 * fps / best), 1)


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
