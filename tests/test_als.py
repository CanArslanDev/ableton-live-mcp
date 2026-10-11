"""Reading Live Set files (.als): MCP_Server/als.py on small sets in Live 12's
layout (tests/als_sets.py), plus Live's own demo set when Live is installed."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import als_sets  # noqa: E402

from MCP_Server import als  # noqa: E402

DEMO = Path("/Applications/Ableton Live 12 Trial.app/Contents/App-Resources/Core Library/"
            "Lessons/Demo Songs/Chuck Sutton - Patience (Live 12 Standard Demo).als")


@pytest.fixture
def song_set(tmp_path):
    return als_sets.project(tmp_path)


def _track(info, name):
    return next(t for t in info["tracks"] + info["return_tracks"] if t["name"] == name)


def test_song_settings(song_set):
    info = als.read_set(song_set)
    assert info["saved_with"] == "Ableton Live 12.3.2"
    assert info["tempo"] == 124.0 and info["time_signature"] == "7/8"
    assert info["key"] == {"root": "D", "scale": "Dorian", "in_key": True}
    # 7/8 bars are 3.5 beats long
    assert info["loop"] == {"start_bar": 3.0, "bars": 4.0}
    assert info["locators"] == [{"name": "Drop", "bar": 3.0}]
    assert info["arrangement_length"] == {"beats": 24.0, "bars": 6.86}
    assert info["scenes"] == [{"index": 0, "name": "Verse", "tempo": 128.0}]


def test_tracks_mixer_and_devices(song_set):
    info = als.read_set(song_set)
    assert [t["name"] for t in info["tracks"]] == ["Band", "Keys", "Drums", "Vox"]
    keys = _track(info, "Keys")
    assert keys["type"] == "midi" and keys["group"] == "Band"
    assert keys["volume"] == "-6.0 dB" and keys["pan"] == "20L"
    assert keys["sends"] == {"A-Reverb": "-12.0 dB"}
    assert keys["devices"] == ['EQ Eight [off]', 'Instrument Rack "Pad Stack" [Wavetable]',
                               "Serum (VST3)", "LFO (Max for Live)"]
    assert keys["session_clips"] == 1 and keys["arrangement_clips"] == 1
    assert _track(info, "Drums")["devices"] == ["Drum Rack (1 pad)"]
    vox = _track(info, "Vox")
    assert vox["muted"] and vox["frozen"] and vox["type"] == "audio"
    assert _track(info, "A-Reverb")["devices"] == ["Reverb"]
    assert info["main_track"] == {"volume": "0.0 dB", "devices": ["Limiter"]}
    assert info["plugins"] == [{"name": "Serum", "format": "VST3", "tracks": ["Keys"]}]
    assert info["max_for_live"] == [{"name": "LFO", "tracks": ["Keys"]}]


def test_samples_found_in_the_project_and_missing(song_set):
    info = als.read_set(song_set)
    assert info["samples"] == {"count": 2, "missing": 1,
                               "by_location": {"project": 1, "elsewhere": 1},
                               "missing_files": ["/nonexistent/missing.wav"]}
    assert any("1 of 2 samples are missing" in n for n in info["notes"])
    assert any("Serum (VST3)" in n for n in info["notes"])
    assert any("Frozen tracks: Vox" in n for n in info["notes"])


def test_full_detail_lists_clips_chains_and_pads(song_set):
    info = als.read_set(song_set, "full")
    keys = _track(info, "Keys")
    assert keys["session_clips"] == [{"slot": 1, "name": "Chords", "kind": "midi",
                                      "length": 8.0, "loop": [0.0, 8.0], "notes": 3}]
    assert keys["arrangement_clips"] == [{"bar": 5.57, "name": "Verse", "kind": "midi",
                                          "start": 16.0, "end": 24.0, "notes": 3}]
    assert keys["output"] == "Main"
    rack = keys["devices"][1]
    assert rack["name"] == "Pad Stack" and rack["chains"][0]["devices"] == [{"device": "Wavetable"}]
    drums = _track(info, "Drums")["devices"][0]
    assert drums["chains"] == [{"name": "Kick", "devices": [{"device": "Simpler"}], "note": 36}]
    assert {s["file"] for s in info["samples"]["files"]} == {"take.wav", "missing.wav"}


def test_clip_notes_are_the_played_region_from_zero(song_set):
    session = als.clip(song_set, "Keys", clip_index=1)
    assert session["length"] == 8.0 and session["looping"] and session["notes_outside"] == 1
    assert [(n["pitch"], n["start_time"], n["duration"]) for n in session["notes"]] == [
        (60, 0.0, 1.0), (60, 4.0, 2.0)]
    assert session["notes"][1]["probability"] == 0.5
    assert session["set_tempo"] == 124.0 and session["set_time_signature"] == [7, 8]
    # Arrangement clip with the loop off: its markers 4..12 are what plays
    arranged = als.clip(song_set, 1, arrangement_clip_index=0)
    assert [(n["pitch"], n["start_time"], n["duration"]) for n in arranged["notes"]] == [
        (62, 0.0, 1.0), (65, 7.5, 0.5)]
    assert arranged["notes_outside"] == 1 and not arranged["looping"]


def test_clip_errors_say_what_exists(song_set):
    with pytest.raises(ValueError, match=r"slots with clips: \[1\]"):
        als.clip(song_set, "Keys", clip_index=0)
    with pytest.raises(ValueError, match="audio clip"):
        als.clip(song_set, "Vox", clip_index=0)
    with pytest.raises(ValueError, match="Tracks: Band, Keys, Drums, Vox"):
        als.clip(song_set, "Nope")
    with pytest.raises(ValueError, match="out of range"):
        als.clip(song_set, 9)
    with pytest.raises(ValueError, match="1 Arrangement clips"):
        als.clip(song_set, "Keys", arrangement_clip_index=3)


def test_compare_reports_what_changed(tmp_path, song_set):
    xml = als_sets.song()
    changed = (xml.replace('<Eq8 Id="1"><UserName Value="" /><On><Manual Value="false" /></On></Eq8>', "")
               .replace('<Manual Value="124" />', '<Manual Value="126" />')
               .replace("</Tracks>", als_sets.track("MidiTrack", 15, "Bass") + "</Tracks>"))
    newer = als_sets.write_set(song_set.parent / "Song 2.als", changed)
    changes = als.compare(newer, song_set)["changes"]
    assert "tempo: 124.0 → 126.0" in changes
    assert "tracks added: Bass" in changes
    assert "Keys: devices removed: EQ Eight [off]" in changes
    assert als.compare(song_set, song_set)["changes"] == ["no differences"]


def test_find_sets_skips_backups_and_matches_tracks_and_plugins(tmp_path):
    song = als_sets.project(tmp_path)
    other = als_sets.write_set(tmp_path / "Other Project" / "Other.als",
                               als_sets.song(tempo=90, tracks=als_sets.track("AudioTrack", 1, "Guitar")))
    als_sets.write_set(song.parent / "Backup" / "Song [2026-10-10 120000].als", als_sets.song())
    (tmp_path / "Broken Project").mkdir()
    (tmp_path / "Broken Project" / "Broken.als").write_bytes(b"not gzip, not xml")
    os.utime(song, (1_000_000, 1_000_000))
    os.utime(other, (2_000_000, 2_000_000))

    found = als.find_sets(tmp_path)
    assert found["sets_found"] == 3 and found["read"] == 2
    assert [Path(s["file"]).name for s in found["sets"]] == ["Other.als", "Song.als"]
    assert found["unreadable"][0]["file"].endswith("Broken.als")
    newest = found["sets"][1]
    assert newest["key"] == "D Dorian" and newest["time_signature"] == "7/8"
    assert newest["tracks"] == {"group": 1, "midi": 2, "audio": 1}
    assert newest["plugins"] == ["Serum"] and newest["missing_samples"] == 1
    assert als.find_sets(tmp_path, include_backups=True)["sets_found"] == 4
    assert [Path(s["file"]).name for s in als.find_sets(tmp_path, "guitar")["matches"]] == ["Other.als"]
    assert [Path(s["file"]).name for s in als.find_sets(tmp_path, "SERUM")["matches"]] == ["Song.als"]
    with pytest.raises(FileNotFoundError):
        als.find_sets(tmp_path / "nowhere")


def test_older_sets_and_odd_values(tmp_path):
    legacy = als_sets.write_set(tmp_path / "Old.als", als_sets.song(
        main_tag="MasterTrack", creator="Ableton Live 11.3.2", scale=99, signature=201, loop=None))
    info = als.read_set(legacy)
    assert info["tempo"] == 124.0 and info["time_signature"] == "4/4"
    assert info["key"]["scale"] == "scale #99" and "loop" not in info
    assert info["main_track"]["devices"] == ["Limiter"]
    # An uncompressed set still reads
    plain = tmp_path / "Plain.als"
    plain.write_text(als_sets.song(), encoding="utf-8")
    assert als.read_set(plain)["tempo"] == 124.0


def test_refuses_what_is_not_a_live_set(tmp_path):
    with pytest.raises(ValueError, match="Not a Live Set"):
        als.read_set(tmp_path / "song.txt")
    with pytest.raises(FileNotFoundError):
        als.read_set(tmp_path / "missing.als")
    doctype = tmp_path / "Evil.als"
    doctype.write_text('<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "y">]><Ableton />')
    with pytest.raises(ValueError, match="not a Live Set"):
        als.read_set(doctype)
    other = als_sets.write_set(tmp_path / "Other.als", "<Project><LiveSet /></Project>")
    with pytest.raises(ValueError, match="not a Live Set"):
        als.read_set(other)
    with pytest.raises(ValueError, match="detail"):
        als.read_set(tmp_path / "x.als", "everything")


@pytest.mark.skipif(not DEMO.is_file(), reason="Live 12 Trial's demo set is not installed")
def test_lives_own_demo_set():
    info = als.read_set(DEMO)
    assert info["tempo"] == 167.0 and info["key"]["root"] == "C#" and info["key"]["scale"] == "Minor"
    assert len(info["locators"]) == 9 and len(info["tracks"]) == 15 and len(info["return_tracks"]) == 2
    assert {"Envelope Follower", "Expression Control"} <= {m["name"] for m in info["max_for_live"]}
    assert info["samples"]["count"] > 0 and info["samples"]["missing"] == 0
    bass = als.clip(DEMO, "Rose Bass", clip_index=5)
    assert len(bass["notes"]) == 23 and bass["length"] == 32.0
