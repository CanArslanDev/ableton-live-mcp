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
