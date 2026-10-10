import mido
import pytest

from MCP_Server import midi_files


NOTES = [
    {"pitch": 60, "start_time": 0.0, "duration": 1.0, "velocity": 100},
    {"pitch": 64, "start_time": 0.0, "duration": 0.5, "velocity": 90},
    {"pitch": 60, "start_time": 1.0, "duration": 1.0, "velocity": 80},  # re-triggered
    {"pitch": 67, "start_time": 2.25, "duration": 0.125, "velocity": 70},
]


def test_round_trip_keeps_notes_tempo_and_meter(tmp_path):
    path = tmp_path / "clip.mid"
    midi_files.write_midi(path, NOTES, tempo_bpm=93.5, numerator=7, denominator=8,
                          name="Keys")
    info = midi_files.read_midi(path)
    assert info["tempo_bpm"] == pytest.approx(93.5, abs=0.01)
    assert info["time_signature"] == "7/8"
    notes = midi_files.select_notes(info)
    assert [(n["pitch"], n["start_time"], n["duration"], n["velocity"]) for n in notes] == [
        (60, 0.0, 1.0, 100), (64, 0.0, 0.5, 90), (60, 1.0, 1.0, 80), (67, 2.25, 0.125, 70)]
    assert info["tracks"][1]["name"] == "Keys"


def test_reads_velocity_zero_note_offs_channels_and_dangling_notes(tmp_path):
    midi = mido.MidiFile(type=0, ticks_per_beat=96)
    track = mido.MidiTrack()
    track += [mido.Message("note_on", note=36, velocity=110, channel=9, time=0),
              mido.Message("note_on", note=36, velocity=0, channel=9, time=48),
              mido.Message("note_on", note=48, velocity=90, channel=0, time=0),
              mido.Message("note_on", note=50, velocity=90, channel=0, time=96)]
    midi.tracks.append(track)
    path = tmp_path / "drums.mid"
    midi.save(str(path))
    info = midi_files.read_midi(path)
    assert info["tracks"][0]["channels"] == [1, 10]
    drums = midi_files.select_notes(info, channel=10)
    assert [(n["pitch"], n["duration"]) for n in drums] == [(36, 0.5)]
    keys = midi_files.select_notes(info, channel=1)
    # never released: they end where the track ends (tick 144)
    assert [n["pitch"] for n in keys] == [48, 50]
    assert keys[0]["start_time"] == 0.5 and keys[0]["duration"] == pytest.approx(1.0)


def test_select_by_track_name_and_errors(tmp_path):
    path = tmp_path / "x.mid"
    midi_files.write_midi(path, NOTES, name="Lead")
    info = midi_files.read_midi(path)
    assert len(midi_files.select_notes(info, track="lead")) == 4
    assert midi_files.select_notes(info, track=0) == []  # the tempo track
    with pytest.raises(ValueError, match="No MIDI track named"):
        midi_files.select_notes(info, track="Bass")
    with pytest.raises(ValueError, match="out of range"):
        midi_files.select_notes(info, track=7)


def test_only_midi_paths_are_accepted(tmp_path):
    with pytest.raises(ValueError, match=r"\.mid"):
        midi_files.write_midi(tmp_path / "notes.txt", NOTES)
    with pytest.raises(ValueError, match=r"\.mid"):
        midi_files.read_midi(tmp_path / "song.wav")
    with pytest.raises(FileNotFoundError):
        midi_files.read_midi(tmp_path / "missing.mid")


def test_free_path_never_overwrites(tmp_path):
    first = midi_files.free_path(tmp_path, "Bass: take/1")
    assert first.name == "Bass_ take_1.mid"
    first.write_bytes(b"x")
    assert midi_files.free_path(tmp_path, "Bass: take/1").name == "Bass_ take_1 2.mid"
