"""Masking, stem findings and reference matching on synthetic signals."""

import numpy as np

from MCP_Server import mix_analysis as ma

RATE = 22050


def tone(freq, seconds=4.0, amp=0.3, stereo=True):
    t = np.arange(int(seconds * RATE)) / RATE
    x = amp * np.sin(2 * np.pi * freq * t)
    return np.stack([x, x], axis=1) if stereo else x[:, None]


def noise(seconds=4.0, amp=0.1, seed=0, lowpass=None, highpass=None):
    from scipy.signal import butter, sosfilt

    rng = np.random.default_rng(seed)
    x = rng.normal(0, amp, int(seconds * RATE))
    if lowpass:
        x = sosfilt(butter(4, lowpass, "low", fs=RATE, output="sos"), x)
    if highpass:
        x = sosfilt(butter(4, highpass, "high", fs=RATE, output="sos"), x)
    return np.stack([x, x], axis=1)


def test_masking_flags_parts_that_share_a_band_and_not_the_others():
    kick_and_bass = {"Kick": (tone(50), RATE), "Bass": (tone(55, amp=0.25), RATE),
                     "Hats": (noise(highpass=6000, amp=0.05), RATE)}
    report = ma.masking_report(kick_and_bass)
    assert len(report["pairs"]) == 1
    pair = report["pairs"][0]
    assert pair["pair"] == ["Kick", "Bass"]
    assert "sub" in pair["bands"] and pair["severity"] > 0.9
    assert "sidechain" in pair["advice"]


def test_parts_far_apart_in_level_do_not_mask_each_other():
    report = ma.masking_report({"Lead": (tone(1500), RATE),
                                "Pad": (tone(1500, amp=0.01), RATE)})   # 30 dB lower
    assert report["pairs"] == []


def test_stem_findings_name_low_end_phase_and_overs():
    pad = noise(lowpass=200, amp=0.2) + tone(1000, amp=0.05)
    wide = tone(440)
    wide[:, 1] *= -1
    hot = tone(1000, amp=1.0)
    summaries = {"Pad": ma.stem_summary(pad, RATE), "Wide": ma.stem_summary(wide, RATE),
                 "Lead": ma.stem_summary(hot, RATE), "Bass": ma.stem_summary(tone(50), RATE)}
    notes = " | ".join(ma.stem_findings(summaries))
    assert "Pad:" in notes and "high-pass" in notes
    assert "Bass:" not in notes                 # low end is the bass's job
    assert "Wide: negative stereo correlation" in notes
    assert "Lead: peaks at" in notes


def test_reference_report_suggests_eq_towards_the_reference():
    mix = noise(seconds=6, amp=0.1, seed=1)
    reference = mix * 1.6 + noise(seconds=6, amp=0.3, seed=2, lowpass=90)   # louder, more lows
    report = ma.reference_report((mix, RATE), (reference, RATE))
    lows = [m for m in report["eq_moves"] if m["frequency_hz"] <= 100]
    assert lows and lows[0]["type"] == "Low Shelf" and lows[0]["gain_db"] > 1
    assert any("quieter than the reference" in n for n in report["observations"])
    settings = report["eq_eight_settings"]
    assert settings["2 Filter Type A"] == "Low Shelf"
    assert settings["2 Gain A"].endswith(" dB") and settings["2 Frequency A"].endswith("Hz")


def test_identical_audio_needs_no_changes():
    mix = noise(seconds=5, seed=3)
    report = ma.reference_report((mix, RATE), (mix.copy(), RATE))
    assert report["eq_moves"] == []
    assert report["observations"] == [
        "The mix is close to the reference in level, dynamics, width and tone."]


def test_loudest_window_finds_the_drop():
    quiet = noise(seconds=10, amp=0.01, seed=4)
    loud = noise(seconds=4, amp=0.3, seed=5)
    track = np.concatenate([quiet, loud, quiet])
    section, start = ma.loudest_window(track, RATE, 4)
    assert start == 10.0 and len(section) == 4 * RATE


def test_eq_moves_merge_runs_and_pick_shelves_at_the_ends():
    diff = np.zeros(len(ma.THIRD_OCTAVES))
    diff[:4] = -3.0          # 25-50 Hz: mix lacks lows
    diff[14:17] = 2.5        # 630-1000 Hz: mix has too much
    diff[-2:] = -2.0         # 12.5-16 kHz: mix lacks air
    moves = ma.eq_moves(diff)
    assert [m["type"] for m in moves] == ["Low Shelf", "Bell", "High Shelf"]
    assert moves[0]["gain_db"] == 1.8 and moves[1]["gain_db"] == -1.5
