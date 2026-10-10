"""Mix diagnosis from stems and comparison with a reference track.

Pure numpy/scipy over sample arrays, so everything here is testable with
synthetic signals: which stems fight for the same frequencies at the same
time (masking), what each stem contributes, and how a mix's tonal balance,
loudness, dynamics and width differ from a reference — with EQ Eight settings
that move the mix towards it.
"""

from __future__ import annotations

import math

import numpy as np

from .audio_analysis import integrated_loudness, true_peak

# Bands for masking: fine enough to name the problem, coarse enough to be robust
MASKING_BANDS = [
    ("sub", 20, 60), ("low bass", 60, 120), ("upper bass", 120, 250),
    ("low mids", 250, 500), ("mids", 500, 1000), ("upper mids", 1000, 2000),
    ("presence", 2000, 4000), ("brilliance", 4000, 8000), ("air", 8000, 16000),
]
# What to do when two parts collide in a band
MASKING_ADVICE = {
    "sub": "give the sub to one of them: sidechain-duck the other (apply_device_chain "
           "sidechain_duck) or high-pass it",
    "low bass": "sidechain-duck one from the other, or split the range (e.g. kick "
                "fundamental below the bass)",
    "upper bass": "dip one of them 2-4 dB in this range so the other keeps the weight",
    "low mids": "mud: cut 2-4 dB around here on the less important part",
    "mids": "boxy build-up: carve a dip in one, or pan them apart",
    "upper mids": "they fight for definition: dip one, or pan them apart",
    "presence": "presence clash: dip the less important part here, or duck it from the "
                "lead with a sidechain",
    "brilliance": "pan them apart or shelve one down a little",
    "air": "pan them apart or shelve one down a little",
}
FRAME_SECONDS = 0.1
# A part takes part in a clash when it carries this share of the band's energy
SHARE = 0.25
# ...and the two are within this many dB of each other
CLOSE_DB = 6.0

# ISO 1/3-octave centres used for tonal comparison
THIRD_OCTAVES = [25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630,
                 800, 1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000,
                 12500, 16000]


def _mono(data: np.ndarray) -> np.ndarray:
    return data.mean(axis=1) if data.ndim == 2 else data


def _db(power: float, floor: float = -120.0) -> float:
    return max(floor, 10 * math.log10(power)) if power > 0 else floor


def band_energies(data: np.ndarray, rate: int, bands=MASKING_BANDS,
                  frame_seconds: float = FRAME_SECONDS) -> np.ndarray:
    """Energy per (frame, band), from a Hann-windowed FFT of each frame."""
    mono = _mono(data)
    size = max(256, int(rate * frame_seconds))
    count = len(mono) // size
    if count == 0:
        return np.zeros((0, len(bands)))
    frames = mono[:count * size].reshape(count, size) * np.hanning(size)
    power = np.abs(np.fft.rfft(frames, axis=1)) ** 2 / size
    freqs = np.fft.rfftfreq(size, 1 / rate)
    out = np.zeros((count, len(bands)))
    for b, (_name, lo, hi) in enumerate(bands):
        mask = (freqs >= lo) & (freqs < hi)
        if mask.any():
            out[:, b] = power[:, mask].sum(axis=1)
    return out


def masking_report(stems: dict[str, tuple[np.ndarray, int]], top: int = 8) -> dict:
    """Pairs of stems that carry similar energy in the same band at the same
    time, the bands where it happens, and what to do about it.

    overlap = share of the time either part is audible in that band during
    which both carry ≥25% of the band's energy within 6 dB of each other,
    counting only frames where the band matters in the mix (within 30 dB of
    the frame's loudest band).
    """
    labels = list(stems)
    energies = {label: band_energies(*stems[label]) for label in labels}
    frames = min((e.shape[0] for e in energies.values()), default=0)
    if frames == 0 or len(labels) < 2:
        return {"pairs": [], "bands": [b[0] for b in MASKING_BANDS]}
    stack = np.stack([energies[label][:frames] for label in labels])     # (stems, frames, bands)
    total = stack.sum(axis=0) + 1e-20                                     # (frames, bands)
    share = stack / total
    loudest = total.max(axis=1, keepdims=True)
    relevant = total >= loudest * 1e-3                                    # within 30 dB
    audible = (stack > 1e-9) & (share >= 0.1) & relevant                  # roughly -90 dBFS
    pairs = []
    for i in range(len(labels)):
        for j in range(i + 1, len(labels)):
            ratio = np.abs(10 * np.log10((stack[i] + 1e-20) / (stack[j] + 1e-20)))
            clash = (share[i] >= SHARE) & (share[j] >= SHARE) & (ratio <= CLOSE_DB) & relevant
            either = (audible[i] | audible[j]).sum(axis=0)
            overlap = np.where(either > 0, clash.sum(axis=0) / np.maximum(either, 1), 0.0)
            bands = [(MASKING_BANDS[b][0], float(overlap[b])) for b in range(len(MASKING_BANDS))
                     if overlap[b] >= 0.3 and clash[:, b].sum() * FRAME_SECONDS >= 1.0]
            if not bands:
                continue
            bands.sort(key=lambda item: -item[1])
            worst, amount = bands[0]
            lo, hi = next((lo, hi) for name, lo, hi in MASKING_BANDS if name == worst)
            pairs.append({
                "pair": [labels[i], labels[j]],
                "bands": {name: round(value, 2) for name, value in bands},
                "severity": round(amount, 2),
                "advice": f"{labels[i]} and {labels[j]} share the {worst} ({lo}-{hi} Hz) "
                          f"{amount:.0%} of the time: {MASKING_ADVICE[worst]}.",
            })
    pairs.sort(key=lambda p: -p["severity"])
    return {"pairs": pairs[:top], "bands": [f"{n} {lo}-{hi} Hz" for n, lo, hi in MASKING_BANDS]}


def stem_summary(data: np.ndarray, rate: int) -> dict:
    """Loudness, peak, low-end content and stereo picture of one stem."""
    lufs, _short, _lra = integrated_loudness(data, rate)
    peak = float(np.max(np.abs(data))) if data.size else 0.0
    energies = band_energies(data, rate).sum(axis=0)
    total = float(energies.sum()) or 1e-20
    shares = {name: round(100 * float(e) / total, 1)
              for (name, _lo, _hi), e in zip(MASKING_BANDS, energies)}
    summary = {"lufs": lufs, "peak_dbfs": round(20 * math.log10(peak), 1) if peak > 0 else None,
               "band_energy_percent": shares,
               "main_band": max(shares, key=shares.get) if total > 1e-19 else None}
    if data.ndim == 2 and data.shape[1] >= 2:
        left, right = data[:, 0], data[:, 1]
        denom = float(np.sqrt((left ** 2).sum() * (right ** 2).sum()))
        summary["correlation"] = round(float((left * right).sum() / denom), 3) if denom else 1.0
    return summary


_LOW_END_PARTS = ("kick", "bass", "808", "sub", "bd", "low", "tom", "drum", "beat", "perc")


def stem_findings(summaries: dict[str, dict]) -> list[str]:
    notes = []
    for label, s in summaries.items():
        if s["lufs"] <= -60:
            notes.append(f"{label}: silent in this section.")
            continue
        low = s["band_energy_percent"]["sub"] + s["band_energy_percent"]["low bass"]
        if low > 15 and not any(word in label.lower() for word in _LOW_END_PARTS):
            notes.append(f"{label}: {low:.0f}% of its energy is below 120 Hz — high-pass it "
                         "(EQ Eight band 1, ~100-150 Hz) unless it is meant to carry low end.")
        if s.get("correlation") is not None and s["correlation"] < 0:
            notes.append(f"{label}: negative stereo correlation ({s['correlation']}); it will "
                         "thin out or vanish in mono.")
        if s["peak_dbfs"] is not None and s["peak_dbfs"] > -0.1:
            notes.append(f"{label}: peaks at {s['peak_dbfs']} dBFS on its own; lower its gain "
                         "before the master.")
    return notes


# ── Reference comparison ─────────────────────────────────────────────────────

def third_octave_profile(data: np.ndarray, rate: int) -> np.ndarray:
    """Average level (dB) per 1/3-octave band, Welch spectrum over channels."""
    from scipy.signal import welch

    freqs, psd = welch(data, fs=rate, nperseg=min(16384, max(512, len(data))), axis=0)
    psd = psd.mean(axis=1) if psd.ndim == 2 else psd
    levels = []
    for centre in THIRD_OCTAVES:
        lo, hi = centre / 2 ** (1 / 6), centre * 2 ** (1 / 6)
        mask = (freqs >= lo) & (freqs < hi)
        levels.append(_db(float(psd[mask].sum())) if mask.any() else -120.0)
    return np.array(levels)


def loudest_window(data: np.ndarray, rate: int, seconds: float) -> tuple[np.ndarray, float]:
    """The `seconds`-long part with the most energy (1 s steps): compares a
    16-bar drop with the reference's drop, not its whole length."""
    size = int(seconds * rate)
    if size >= len(data):
        return data, 0.0
    blocks = len(data) // rate
    per_second = np.square(_mono(data)[:blocks * rate]).reshape(blocks, rate).sum(axis=1)
    span = max(1, size // rate)
    sums = np.convolve(per_second, np.ones(span), mode="valid")
    start = int(np.argmax(sums)) * rate
    return data[start:start + size], start / rate


def _side_to_mid(data: np.ndarray) -> float | None:
    if data.ndim != 2 or data.shape[1] < 2:
        return None
    mid = (data[:, 0] + data[:, 1]) / 2
    side = (data[:, 0] - data[:, 1]) / 2
    m, s = float(np.mean(mid ** 2)), float(np.mean(side ** 2))
    return round(_db(s) - _db(m), 1) if m > 0 else None


def _crest(data: np.ndarray) -> float:
    peak = float(np.max(np.abs(data)))
    rms = float(np.sqrt(np.mean(data ** 2)))
    return round(20 * math.log10(peak / rms), 1) if peak > 0 and rms > 0 else 0.0


def tonal_difference(mix: np.ndarray, reference: np.ndarray) -> np.ndarray:
    """mix − reference per 1/3 octave after matching their level between
    100 Hz and 8 kHz, smoothed over neighbouring bands."""
    core = [i for i, c in enumerate(THIRD_OCTAVES) if 100 <= c <= 8000]
    diff = (mix - mix[core].mean()) - (reference - reference[core].mean())
    return np.convolve(np.pad(diff, 1, mode="edge"), np.ones(3) / 3, mode="valid")


def eq_moves(diff: np.ndarray, threshold: float = 1.5, amount: float = 0.6,
             max_bands: int = 4) -> list[dict]:
    """EQ moves that take `amount` of the way towards the reference: shelves
    at the ends, bells in between, for runs of bands off by > threshold dB."""
    runs, i = [], 0
    while i < len(diff):
        if abs(diff[i]) <= threshold:
            i += 1
            continue
        j = i
        while j + 1 < len(diff) and abs(diff[j + 1]) > threshold \
                and np.sign(diff[j + 1]) == np.sign(diff[i]):
            j += 1
        runs.append((i, j))
        i = j + 1
    moves = []
    for lo, hi in runs:
        segment = diff[lo:hi + 1]
        peak = lo + int(np.argmax(np.abs(segment)))
        gain = float(np.clip(-amount * diff[peak], -6.0, 6.0))
        # A run that reaches either end of the spectrum is a shelf
        if lo == 0 or THIRD_OCTAVES[hi] <= 100:
            kind, freq = "Low Shelf", THIRD_OCTAVES[hi]
        elif hi == len(diff) - 1 or THIRD_OCTAVES[lo] >= 6300:
            kind, freq = "High Shelf", THIRD_OCTAVES[lo]
        else:
            kind, freq = "Bell", THIRD_OCTAVES[peak]
        width = hi - lo + 1
        moves.append({"type": kind, "frequency_hz": freq, "gain_db": round(gain, 1),
                      "q": round(max(0.5, min(2.0, 4.3 / max(width, 1))), 2),
                      "from_hz": THIRD_OCTAVES[lo], "to_hz": THIRD_OCTAVES[hi],
                      "off_by_db": round(float(diff[peak]), 1)})
    moves.sort(key=lambda m: -abs(m["off_by_db"]))
    return sorted(moves[:max_bands], key=lambda m: m["frequency_hz"])


def eq_eight_settings(moves: list[dict]) -> dict[str, str]:
    """add_device settings for an EQ Eight doing `moves` (bands 2-5)."""
    settings: dict[str, str] = {}
    for band, move in enumerate(moves, start=2):
        settings[f"{band} Filter On A"] = "On"
        settings[f"{band} Filter Type A"] = move["type"]
        settings[f"{band} Frequency A"] = (f"{move['frequency_hz'] / 1000:g} kHz"
                                           if move["frequency_hz"] >= 1000
                                           else f"{move['frequency_hz']:g} Hz")
        settings[f"{band} Gain A"] = f"{move['gain_db']:.1f} dB"
        if move["type"] == "Bell":
            settings[f"{band} Resonance A"] = f"{move['q']:.2f}"
    return settings


def reference_report(mix: tuple[np.ndarray, int], reference: tuple[np.ndarray, int],
                     match_section: bool = True) -> dict:
    """How the mix differs from a reference: loudness, peaks, dynamics, width
    and tonal balance, with EQ Eight settings that move the mix towards it."""
    mix_data, mix_rate = mix
    ref_data, ref_rate = reference
    note = "whole reference"
    if match_section:
        ref_data, offset = loudest_window(ref_data, ref_rate, len(mix_data) / mix_rate)
        if offset or len(ref_data) < len(reference[0]):
            note = (f"the loudest {len(ref_data) / ref_rate:.0f} s of the reference "
                    f"(from {offset:.0f} s)")
    m_lufs, m_short, m_lra = integrated_loudness(mix_data, mix_rate)
    r_lufs, r_short, r_lra = integrated_loudness(ref_data, ref_rate)
    numbers = {
        "integrated_lufs": [m_lufs, r_lufs], "max_short_term_lufs": [m_short, r_short],
        "loudness_range_lu": [m_lra, r_lra],
        "true_peak_dbtp": [true_peak(mix_data), true_peak(ref_data)],
        "crest_factor_db": [_crest(mix_data), _crest(ref_data)],
        "side_to_mid_db": [_side_to_mid(mix_data), _side_to_mid(ref_data)],
    }
    diff = tonal_difference(third_octave_profile(mix_data, mix_rate),
                            third_octave_profile(ref_data, ref_rate))
    moves = eq_moves(diff)
    notes = []
    loud = m_lufs - r_lufs
    if abs(loud) >= 1:
        notes.append(f"The mix is {abs(loud):.1f} LU {'louder' if loud > 0 else 'quieter'} than "
                     f"the reference ({m_lufs} vs {r_lufs} LUFS)."
                     + (" Match loudness before judging tone; push the limiter input, not "
                        "the ceiling." if loud < 0 else ""))
    crest = numbers["crest_factor_db"][0] - numbers["crest_factor_db"][1]
    if abs(crest) >= 2:
        notes.append(f"Crest factor {numbers['crest_factor_db'][0]} vs "
                     f"{numbers['crest_factor_db'][1]} dB: the mix is "
                     f"{'more dynamic (less limited)' if crest > 0 else 'more squashed'} "
                     "than the reference.")
    width = numbers["side_to_mid_db"]
    # Below -30 dB of side signal both are effectively mono
    if None not in width and max(width) > -30 and abs(width[0] - width[1]) >= 3:
        notes.append(f"The mix is {'wider' if width[0] > width[1] else 'narrower'} than the "
                     f"reference (side/mid {width[0]} vs {width[1]} dB).")
    for move in moves:
        direction = "less" if move["off_by_db"] < 0 else "more"
        notes.append(f"{abs(move['off_by_db']):.1f} dB {direction} energy than the reference "
                     f"at {move['from_hz']}-{move['to_hz']} Hz → {move['type']} "
                     f"{move['gain_db']:+.1f} dB at {move['frequency_hz']} Hz.")
    if not notes:
        notes.append("The mix is close to the reference in level, dynamics, width and tone.")
    return {
        "compared_with": note,
        "mix_vs_reference": numbers,
        "tonal_difference_db": {f"{c:g} Hz": round(float(d), 1)
                                for c, d in zip(THIRD_OCTAVES, diff)},
        "eq_moves": moves,
        "eq_eight_settings": eq_eight_settings(moves),
        "observations": notes,
    }
