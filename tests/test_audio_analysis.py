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


def groove(bpm, events, bars=16, rate=44100):
    """events: (beat in the bar, sound) repeated every 4/4 bar."""
    y = np.zeros(int(rate * 60 / bpm * 4 * bars) + rate)
    for bar in range(bars):
        for beat, sound in events:
            start = int(round((bar * 4 + beat) * 60 / bpm * rate))
            y[start:start + len(sound)] += sound[:len(y) - start]
    return y


def _hit(freq, decay, amp, rate=44100):
    n = np.arange(int(0.15 * rate))
    return amp * np.exp(-n / (decay * rate)) * np.sin(2 * np.pi * freq * n / rate)


def _noise(decay, amp, seed, rate=44100):
    n = np.arange(int(0.08 * rate))
    return amp * np.exp(-n / (decay * rate)) * np.random.default_rng(seed).normal(0, 1, len(n))


KICK, SNARE, HAT = _hit(55, 0.08, 0.8), _noise(0.05, 0.4, 0), _noise(0.01, 0.08, 1)


def test_tempo_picks_the_beat_among_its_relations():
    """The strongest autocorrelation peak alone read a house groove at 124
    BPM as 82.7 (2:3) and four on the floor at 145 as 72.5; the pulse over
    the period's multiples and the preference around 120 BPM pick the beat."""
    import tempo_bench as tb

    house = tb.case_audio("house", 124, 4)
    assert audio_analysis.estimate_tempo(house, tb.RATE) == pytest.approx(124, abs=0.1)
    floor = [(i, KICK) for i in range(4)] + [(i + 0.5, HAT) for i in range(4)]
    assert audio_analysis.estimate_tempo(groove(145, floor), 44100) == pytest.approx(145, abs=0.1)


def test_an_uncertain_tempo_names_its_alternatives(tmp_path):
    """A 102 BPM trap beat with 16th hats reads 136 (4:3, like the remake
    session's 136 BPM song read as 102): too close to call, so it says so."""
    trap = [(0, KICK), (2.75, KICK), (2, SNARE)] + [(i / 4, HAT) for i in range(16)]
    sf.write(tmp_path / "trap.wav", groove(102, trap), 44100)
    result = audio_analysis.analyze(tmp_path / "trap.wav")
    assert result["tempo_confidence"] < audio_analysis.TEMPO_UNSURE
    assert 102 in [round(a) for a in result["tempo_alternatives_bpm"] + [result["tempo_estimate_bpm"]]]
    assert any(o.startswith("Tempo uncertain") for o in result["observations"])


def test_a_weak_key_is_flagged(tmp_path):
    noise = np.random.default_rng(0).normal(0, 0.1, RATE * 4)
    result = audio_analysis.analyze(write(tmp_path, "noise.wav", noise))
    assert result["key_estimate"]["weak"] is True and "or" in result["key_estimate"]
    assert any(o.startswith("Key estimate weak") for o in result["observations"])
    # A triad alone is C major or E minor; a scale is C major
    triad = audio_analysis.estimate_key(sum(sine(f, 4, 0.1) for f in (261.63, 329.63, 392.0)), RATE)
    assert triad["weak"] and triad["or"] == "E minor"
    scale = [261.63, 293.66, 329.63, 349.23, 392.0, 440.0, 493.88, 523.25, 392.0, 261.63]
    clear = audio_analysis.estimate_key(np.concatenate([sine(f, 0.5, 0.1) for f in scale]), RATE)
    assert clear["key"] == "C major" and "weak" not in clear


def test_tempo_benchmark_never_loses_what_the_peak_got_right(monkeypatch):
    """tests/tempo_bench.py: click tracks, four on the floor, house, trap,
    drill, boom bap, half-time, drum and bass, reggaeton, waltzes and the
    remake session's 136 BPM song with accents every 4/3 beat. The
    strongest autocorrelation peak alone (the old pick) got 10 of 19; the
    relations must never turn one of those wrong."""
    import tempo_bench as tb

    def run(switch):
        monkeypatch.setattr(audio_analysis, "TEMPO_SWITCH", switch)
        return [audio_analysis.tempo_estimate(tb.case_audio(*case), tb.RATE)
                for case in tb.CASES]

    switch = audio_analysis.TEMPO_SWITCH
    peak, picked = run(1e9), run(switch)
    right = [abs(p["bpm"] - bpm) <= 1 for p, (_s, bpm, _m) in zip(picked, tb.CASES)]
    for was, now, ok, (style, bpm, _m) in zip(peak, picked, right, tb.CASES):
        assert ok or abs(was["bpm"] - bpm) > 1, f"{style} at {bpm}: {now['bpm']}"
    assert sum(right) >= 11
    # Right picks are rarely called uncertain
    assert sum(ok and p["confidence"] < audio_analysis.TEMPO_UNSURE
               for p, ok in zip(picked, right)) <= 2
    # The remake song: the peak says 102, and 136 is offered
    song = picked[[c[0] for c in tb.CASES].index("triplet accents")]
    assert round(song["bpm"]) == 102 and 136 in [round(a) for a in song["alternatives"]]


def test_a_close_four_to_three_tempo_is_named(tmp_path):
    """The remake song reads 102 BPM, its 4:3 (136) scoring 0.78 of that:
    the observations name it. Click tracks and four on the floor stay quiet."""
    import tempo_bench as tb

    def notes(style, bpm):
        sf.write(tmp_path / "x.wav", tb.case_audio(style, bpm, 4), tb.RATE)
        return [o for o in audio_analysis.analyze(tmp_path / "x.wav")["observations"]
                if o.startswith("Tempo")]

    assert notes("triplet accents", 136) == [
        "Tempo 101.96 BPM; could be 135.9 (4:3): check by ear."]
    for style, bpm in (("click", 128), ("click", 90), ("four on the floor", 120),
                       ("house", 124)):
        assert notes(style, bpm) == [], (style, bpm)


def test_a_peak_refined_past_200_bpm_still_gives_a_tempo(monkeypatch):
    """Geriye Kalan: render_and_analyze failed with StopIteration. The
    strongest lag in the 70-180 BPM window (29 frames at 44.1 kHz) sat on
    the slope of a broad peak at 25.6 frames; refined uphill it read 202
    BPM, outside 60-200, so the peak's own tempo was never scored."""
    lags = np.arange(4096, dtype=float)
    shape = np.exp(-0.5 * ((lags - 25.6) / 3) ** 2) + 0.01 * np.exp(-lags / 300)
    shape[0] = 2.0
    monkeypatch.setattr(audio_analysis.np, "correlate", lambda a, b, mode: np.concatenate(
        [np.zeros(len(a) - 1), shape[:len(a)]]))
    signal = np.zeros(44100 * 10)
    signal[::20000] = 1.0
    found = audio_analysis.tempo_estimate(signal, 44100)
    assert found is not None and 195 < found["bpm"] < 210
