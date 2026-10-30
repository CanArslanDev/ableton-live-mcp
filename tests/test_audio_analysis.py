import numpy as np
import pytest
import soundfile as sf

from MCP_Server import audio_analysis

RATE = 48000


def write(tmp_path, name, data, subtype="FLOAT"):
    path = tmp_path / name
    sf.write(str(path), data, RATE, subtype=subtype)
    return str(path)


def sine(freq, seconds, amplitude):
    t = np.arange(int(RATE * seconds)) / RATE
    return amplitude * np.sin(2 * np.pi * freq * t)


def test_bs1770_reference_tone(tmp_path):
    # A 1 kHz tone at -20 dBFS in both channels reads -20 LUFS (BS.1770)
    tone = sine(1000, 5, 0.1)
    result = audio_analysis.analyze(write(tmp_path, "tone.wav", np.stack([tone, tone], 1)))
    assert result["integrated_lufs"] == pytest.approx(-20.0, abs=0.1)
    assert result["sample_peak_dbfs"] == pytest.approx(-20.0, abs=0.05)
    assert result["band_energy_percent"]["mid"] > 99
    assert result["stereo"]["correlation"] == pytest.approx(1.0)
    assert result["tempo_estimate_bpm"] is None


def test_flags_clipping_phase_and_bass_heavy_mixes(tmp_path):
    bass = np.clip(sine(50, 4, 1.4), -1, 1)
    result = audio_analysis.analyze(write(tmp_path, "bad.wav", np.stack([bass, -bass], 1)))
    text = " ".join(result["observations"])
    assert result["clipped_samples"] > 0
    assert "clipping" in text
    assert "Negative stereo correlation" in text
    assert "Low end dominates" in text


def test_tempo_and_key_estimates(tmp_path):
    seconds = 12
    y = np.zeros(RATE * seconds)
    n = np.arange(3000)
    hit = np.exp(-n / 300) * np.sin(2 * np.pi * 60 * n / RATE)
    for k in range(int(seconds * 2)):  # 120 BPM kicks
        start = int(k * 0.5 * RATE)
        y[start:start + len(hit)] += hit[: len(y) - start]
    # A minor triad drone on top
    for freq in (220.0, 261.63, 329.63, 440.0):
        y += sine(freq, seconds, 0.05)
    result = audio_analysis.analyze(write(tmp_path, "groove.wav", y))
    assert result["tempo_estimate_bpm"] == pytest.approx(120, abs=2)
    assert result["key_estimate"]["key"] in ("A minor", "C major")
    assert result["stereo"] is None


def click_track(bpm, rate, seconds=30.0):
    """A 1 kHz click (4 ms decay) on every beat."""
    y = np.zeros(int(rate * seconds))
    n = np.arange(int(0.03 * rate))
    click = 0.5 * np.exp(-n / (0.004 * rate)) * np.sin(2 * np.pi * 1000 * n / rate)
    for beat in range(int(seconds * bpm / 60) + 1):
        start = int(round(beat * 60 / bpm * rate))
        part = click[: max(0, len(y) - start)]
        y[start:start + len(part)] += part
    return y


@pytest.mark.parametrize("rate", [44100, 48000])
def test_tempo_is_found_between_frames(rate):
    # 137 BPM falls between two whole-frame lags (136.0 and 139.7 at 44.1 kHz)
    assert audio_analysis.estimate_tempo(click_track(137.0, rate), rate) == pytest.approx(137.0, abs=0.05)
    for bpm in (90.0, 128.0, 140.0):
        assert audio_analysis.estimate_tempo(click_track(bpm, rate), rate) == pytest.approx(bpm, abs=0.05)


def test_peak_lag_finds_the_vertex_between_lags():
    lags = np.arange(40.0)
    ac = -((lags - 21.3) ** 2)
    assert audio_analysis.peak_lag(ac, 21) == pytest.approx(21.3)
    assert audio_analysis.peak_lag(ac, 19) == pytest.approx(21.3)  # walks uphill first
    assert audio_analysis.peak_lag(ac, 0) == 0.0


def test_note_levels_read_each_notes_level_at_its_pitch():
    rate = 44100
    t = np.arange(rate // 2) / rate
    y = np.zeros(4 * rate)
    notes, truth = [], []
    for k, (pitch, db) in enumerate([(45, -6.0), (57, -20.0), (69, -12.0), (81, -30.0)]):
        freq = 440 * 2 ** ((pitch - 69) / 12)
        y[k * rate:k * rate + len(t)] += 10 ** (db / 20) * np.sin(2 * np.pi * freq * t)
        notes.append({"start": float(k), "pitch": pitch})
        truth.append(db)
    assert audio_analysis.note_levels(y, rate, notes) == pytest.approx(truth, abs=0.05)
    # A note three semitones above, 20 dB louder and at the same time, adds
    # under a decibel
    loud = np.sin(2 * np.pi * 440 * 2 ** (3 / 12) * t)
    y[2 * rate:2 * rate + len(t)] += loud
    assert audio_analysis.note_levels(y, rate, notes[2:3])[0] == pytest.approx(-12.0, abs=1.0)
    assert np.isnan(audio_analysis.note_levels(np.zeros(0), rate, notes)).all()


def line_808(rate, pitches, dbs, step_s, tau=0.5):
    """A mono line of decaying sines (3 ms attack), each cut by the next note."""
    y = np.zeros(int((0.5 + len(pitches) * step_s + 1.0) * rate))
    n = int(step_s * rate)
    t = np.arange(n) / rate
    notes = []
    for k, (pitch, db) in enumerate(zip(pitches, dbs, strict=True)):
        freq = 440 * 2 ** ((pitch - 69) / 12)
        start = int((0.5 + k * step_s) * rate)
        y[start:start + n] += 10 ** (db / 20) * np.exp(-t / tau) * np.minimum(1, t / 0.003) * np.sin(
            2 * np.pi * freq * t)
        notes.append({"start": start / rate, "end": (start + n) / rate, "pitch": pitch})
    return y, notes


@pytest.mark.parametrize("step_s", [30 / 140, 15 / 140])  # 8ths and 16ths at 140 BPM
def test_note_levels_read_low_notes_alike(step_s):
    rate = 48000
    pitches = list(np.random.default_rng(5).permutation(np.arange(24, 44)))
    y, notes = line_808(rate, pitches, [-6.0] * len(pitches), step_s)
    levels = audio_analysis.note_levels(y, rate, notes)
    # A 16.8-cycle window centred on the start read MIDI 24 2.8 dB under MIDI
    # 43 (8ths: 3.7 dB spread, 16ths 7.8 dB); 100 ms from the start, alike
    assert levels.max() - levels.min() < 0.3
    assert levels.mean() == pytest.approx(-6.9, abs=0.2)  # the first 100 ms of a 0.5 s decay
    # A sine after silence reads its level at the lowest pitches too
    y = np.zeros(2 * rate)
    y[rate:] = 0.5 * np.sin(2 * np.pi * 32.703 * np.arange(rate) / rate)
    assert audio_analysis.note_levels(y, rate, [{"start": 1.0, "pitch": 24}])[0] == pytest.approx(-6.02, abs=0.05)


def test_note_levels_stop_at_the_notes_end():
    # An 808 restating its note in 16ths, 24 dB louder each other time. The
    # centred window read -14.7 and -11.7 dB; now the quiet notes read their
    # own level, plus 0.7 dB from the loud one's last 20 ms
    rate = 48000
    y, notes = line_808(rate, [31] * 8, [-30.0, -6.0] * 4, 15 / 140)
    levels = audio_analysis.note_levels(y, rate, notes)
    assert levels[0::2] == pytest.approx([-30.4] * 4, abs=0.4)
    assert levels[1::2] == pytest.approx([-6.75] * 4, abs=0.1)


def test_note_levels_rank_plucks_by_their_level():
    from scipy.stats import spearmanr

    rate = 44100
    rng = np.random.default_rng(3)
    step = int(0.107 * rate)  # 16ths at 140 BPM
    n = np.arange(step * 2)
    y = np.zeros(step * 66)
    notes, truth = [], []
    for k in range(64):
        pitch, db = int(rng.integers(52, 84)), float(rng.uniform(-30, 0))
        freq = 440 * 2 ** ((pitch - 69) / 12)
        tone = sum(np.sin(2 * np.pi * h * freq * n / rate) / h for h in range(1, 6))
        y[k * step:k * step + len(n)] += 10 ** (db / 20) * np.exp(-n / (0.08 * rate)) * tone
        notes.append({"start": k * step / rate, "pitch": pitch})
        truth.append(db)
    assert spearmanr(audio_analysis.note_levels(y, rate, notes), truth)[0] > 0.95


def test_reads_aiff_and_integer_pcm(tmp_path):
    tone = sine(440, 1, 0.5)
    path = tmp_path / "tone.aiff"
    sf.write(str(path), tone, RATE, subtype="PCM_24", format="AIFF")
    result = audio_analysis.analyze(str(path))
    assert result["sample_peak_dbfs"] == pytest.approx(-6.02, abs=0.05)


def test_silence_and_missing_files(tmp_path):
    silent = audio_analysis.analyze(write(tmp_path, "silence.wav", np.zeros((RATE, 2))))
    assert silent["observations"] == ["The file is silent or nearly silent."]
    with pytest.raises(FileNotFoundError):
        audio_analysis.analyze(str(tmp_path / "nope.wav"))


def test_meter_scale_matches_live():
    # Measured on Live 12.3.8 with a 1 kHz sine at known peak levels
    assert audio_analysis.meter_to_db(0.9210) == 0.0
    assert audio_analysis.meter_to_db(0.7631) == -12.0
    assert audio_analysis.meter_to_db(0.2894) == -48.0
    assert audio_analysis.meter_to_db(1.0) == 6.0
    assert audio_analysis.meter_to_db(0.0) is None


def test_level_report_peaks_time_weighted_averages_and_notes():
    def track(name, readings):
        return {"track": name, "readings": readings}

    meters = {
        # Bass: -12 dB for 3 s, then -6 dB for 1 s → peak -6, average -10.5
        1: track("Bass", [(None, 0.0), (10.0, 0.7631), (13.0, 0.8421)]),
        2: track("Bass", [(None, 0.0), (10.0, 0.7631), (13.0, 0.8421)]),
        3: track("Keys", [(None, 0.0)]),
        4: track("Keys", [(None, 0.0)]),
        5: track("Master", [(None, 0.95), (12.0, 0.99), (14.0, 0.97)]),
        6: track("Master", [(None, 0.95)]),
    }
    report = audio_analysis.level_report(meters)
    levels = {r["track"]: r for r in report["levels"]}
    assert levels["Bass"]["peak_dbfs"] == -6.0
    assert levels["Bass"]["average_dbfs"] == -10.5
    assert levels["Keys"]["peak_dbfs"] is None
    assert levels["Master"]["peak_dbfs"] == 5.2
    text = " ".join(report["observations"])
    assert "no headroom" in text and "No signal on: Keys" in text
    assert report["levels"][0]["track"] == "Master"
