"""Blueprints (blueprint.py, docs/blueprint-v1.md) and build_from_blueprint
against the simulator: the real Remote Script over fake Live, an empty set."""

import asyncio
import copy
import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from MCP_Server import als, extended_tools, journal
from MCP_Server import blueprint as bp
from MCP_Server import server as live_server
from MCP_Server.simulator import Simulator, free_port

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "blueprints"
NAMES = ["short-beat", "choke-and-scaled", "glide-and-stem"]
# A new Live set: two empty MIDI tracks, no clips, no locators
EMPTY_SET = ([{"type": "lom_call", "params": {"path": "live_set", "method": "delete_track", "args": [i]}}
              for i in (2, 1, 0)]
             + [{"type": "lom_call", "params": {"path": "live_set", "method": "create_midi_track",
                                                "args": [-1]}}] * 2)


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def absolute(data):
    """The blueprint with its files' and audio clips' paths absolute, as one
    passed as an object needs them."""
    for entry in (data.get("files") or {}).values():
        entry["source"] = str(FIXTURES / entry["source"])
    for track in data["tracks"]:
        for clip in track.get("audio_clips") or []:
            clip["file"] = str(FIXTURES / clip["file"])
    return data


# ── The format ───────────────────────────────────────────────────────────────

def test_the_schema_is_valid_json_schema():
    import jsonschema
    jsonschema.Draft202012Validator.check_schema(bp.schema())
    assert bp.schema()["properties"]["format"]["const"] == bp.FORMAT


@pytest.mark.parametrize("name", NAMES)
def test_fixture_blueprints_are_valid(name):
    data = fixture(name)
    assert bp.validate(data) == []
    assert bp.check_sources(data, FIXTURES) == []


def _broken(change):
    data = fixture("short-beat")
    change(data)
    return " | ".join(bp.validate(data))


@pytest.mark.parametrize("change,expected", [
    (lambda d: d.update(version=2), "version 1"),
    (lambda d: d.pop("tempo"), "'tempo' is a required property"),
    (lambda d: d.update(time_signature="5/3"), "blueprint.time_signature"),
    (lambda d: d["tracks"][0]["instrument"].update(file="nope"), "'nope' is not a key of files"),
    (lambda d: d["tracks"][1].update(name="808 (synth)"), "used twice"),
    (lambda d: d["tracks"][1]["instrument"]["drum_rack"]["36"].update(choke_group=17),
     "choke_group: 17 is greater than the maximum of 16"),
    (lambda d: d["tracks"][1]["instrument"]["drum_rack"].update({"128": {"file": "kick"}}),
     "drum_rack: '128' does not match"),
    (lambda d: d["files"]["kick"].update(gain_db=2.0), "gain_db: 2.0 is greater than the maximum"),
    (lambda d: d["files"]["kick"].update(gain_db=-1.0, name="kick.aif"), "name it .wav"),
    (lambda d: d["files"]["kick"].update(name="sub/kick.wav"), "blueprint.files.kick.name"),
    (lambda d: d["tracks"][1].update(voices=1), "voices is a Simpler's"),
    (lambda d: d["tracks"][0]["instrument"].update(sample="808.wav"), "leave out sample_path"),
    (lambda d: d["tracks"][0]["instrument"].update(choke_group=1), "choke_group is a Drum Rack pad's"),
    (lambda d: d["tracks"][0]["clips"][0]["notes"].append(
        {"pitch": 24, "start_time": 8, "duration": 1}), "at or after the clip's length"),
    (lambda d: d["tracks"].append({"name": "Audio", "kind": "audio", "instrument": "Drift"}),
     "an audio track takes audio_clips"),
    (lambda d: d["locators"].append({"name": "Late", "beat": 8}), "past the end"),
    (lambda d: d.update(colour="red"), "Additional properties are not allowed ('colour'"),
    # oneOf: the shape that was meant says what is wrong
    (lambda d: d["tracks"][0]["instrument"].update(warp=False),
     "blueprint.tracks[0].instrument: Additional properties are not allowed ('warp' was unexpected)"),
    (lambda d: d["tracks"][0]["clips"][0]["notes"][0].update(pitch="C"), "'C' is too short"),
    (lambda d: d["files"]["snare"].update(name=d["files"]["kick"]["name"].upper().replace(".WAV", ".wav")),
     "names two different files"),
])
def test_validate_says_what_is_wrong_and_where(change, expected):
    assert expected in _broken(change)


def test_excerpt_stems_of_one_name_and_stems_that_end_before_it(tmp_path):
    for folder, seconds in (("a", 4.0), ("b", 4.0), ("c", 1.0)):
        (tmp_path / folder).mkdir()
        sf.write(tmp_path / folder / "drums.wav", np.full(int(8000 * seconds), ord(folder) / 1000), 8000)
    data = {"format": "ableton-mcp-blueprint", "version": 1, "tempo": 120, "time_signature": "4/4",
            "length_beats": 8, "tracks": [
                {"name": f"REF {f}", "kind": "audio", "audio_clips": [{"file": f"{f}/drums.wav", "start": 0}]}
                for f in ("a", "b", "c")]}
    assert bp.validate(data) == []
    part = bp.excerpt(data, (2, 2), tmp_path, tmp_path / "excerpt")  # 2 s to 4 s
    a, b = (Path(t["audio_clips"][0]["file"]) for t in part["tracks"][:2])
    assert a != b and sf.read(a)[0][0] != sf.read(b)[0][0]
    assert part["tracks"][2]["audio_clips"] == []  # its 1 s file ends before bar 2


def test_check_raises_with_the_problems():
    with pytest.raises(ValueError, match="cannot be built: blueprint.tracks"):
        bp.check({**fixture("short-beat"), "tracks": []})


def test_check_sources_refuses_a_changed_or_missing_file(tmp_path):
    data = fixture("short-beat")
    data["files"]["808"]["source"] = str(FIXTURES / "samples" / "kick.wav")  # sha1 names the 808
    data["files"]["snare"]["source"] = "samples/gone.wav"
    problems = " | ".join(bp.check_sources(data, FIXTURES))
    assert "files.808" in problems and "not the file the blueprint was made with" in problems
    assert "files.snare: no such file" in problems
    with pytest.raises(ValueError, match="relative"):
        bp.resolve("samples/kick.wav", None)


# ── Files, excerpts, the spec ────────────────────────────────────────────────

def test_files_are_copied_scaled_and_kept(tmp_path):
    data = fixture("choke-and-scaled")
    folder = tmp_path / "Generated"
    placed, rows = bp.put_files(data, FIXTURES, folder)
    actions = {r["file"]: r["action"] for r in rows}
    assert actions == {"kick": "copied", "closed": "copied", "open": "copied", "clap": "scaled"}
    assert placed["kick"].read_bytes() == (FIXTURES / "samples" / "kick.wav").read_bytes()
    clap, rate = sf.read(placed["clap"], always_2d=True)
    source, _ = sf.read(FIXTURES / "samples" / "hot-clap.wav", always_2d=True)
    assert sf.info(placed["clap"]).subtype == "FLOAT" and rate == 22050
    assert np.max(np.abs(source)) > 1.4 and np.max(np.abs(clap)) <= 1.0
    assert np.allclose(clap, source * 10 ** (-3.1 / 20), atol=1e-6)
    _, again = bp.put_files(data, FIXTURES, folder)
    assert {r["action"] for r in again} == {"kept"}
    placed["kick"].write_bytes(b"other")
    _, third = bp.put_files(data, FIXTURES, folder)
    assert {r["file"]: r["action"] for r in third}["kick"] == "replaced"
    assert not list(folder.glob(".*tmp"))


def test_the_spec_points_samplers_at_their_copies(tmp_path):
    data = fixture("glide-and-stem")
    placed, _ = bp.put_files(data, FIXTURES, tmp_path)
    spec = bp.build_spec(data, placed, FIXTURES)
    simpler = spec[0]["instrument"]
    assert simpler["sample_path"] == str(tmp_path / data["files"]["808"]["name"])
    assert simpler["sample"] == data["files"]["808"]["name"] and "file" not in simpler
    assert "voices" not in spec[0] and data["tracks"][0]["voices"] == 1  # the blueprint is untouched
    assert spec[1]["instrument"]["drum_rack"]["38"] == {"sample": "Snare Tight.wav",
                                                        "settings": {"Volume": -6.0}}
    assert spec[2]["audio_clips"][0]["file"] == str(FIXTURES / "samples" / "ref-drums.wav")


@pytest.mark.parametrize("text,bars", [("9-16", (9, 16)), ("3", (3, 3)), ([2, 4], (2, 4)),
                                       ("2 to 5", (2, 5)), (7, (7, 7))])
def test_bars(text, bars):
    assert bp.parse_bars(text) == bars


def test_an_excerpt_starts_at_zero_with_its_stems_cut(tmp_path):
    data = fixture("glide-and-stem")
    part = bp.excerpt(data, (2, 2), FIXTURES, tmp_path / "excerpt")
    notes = part["tracks"][0]["clips"][0]["notes"]
    assert [(n["pitch"], n["start_time"]) for n in notes] == [(26, 0), (31, 2)]
    assert part["tracks"][0]["clips"][0]["length"] == 4 and part["length_beats"] == 4
    assert part["locators"] == [{"name": "B (2)", "beat": 0}]
    stem = Path(part["tracks"][2]["audio_clips"][0]["file"])
    assert stem.parent == tmp_path / "excerpt" and part["tracks"][2]["audio_clips"][0]["start"] == 0
    assert stem.name == f"ref-drums {bp.audio_digest(stem)[:8]}.wav"
    info = sf.info(stem)
    assert info.samplerate == 8000 and info.subtype == "PCM_16"
    assert abs(info.frames - (8 - 4) * 60 / 140 * 8000) <= 1
    assert part["meta"]["excerpt"] == {"bars": [2, 2], "beats": [4, 8], "seconds": [1.714286, 3.428571]}
    assert bp.validate(part) == []
    with pytest.raises(ValueError, match="not in the blueprint's 1-2"):
        bp.excerpt(data, (2, 3), FIXTURES, tmp_path)
    # a note that starts before the excerpt is left out, one that sounds past it is cut
    short = bp.excerpt(fixture("short-beat"), (1, 1), FIXTURES, tmp_path)
    bass = short["tracks"][0]["clips"][0]["notes"]
    assert [(n["start_time"], n["duration"]) for n in bass] == [(0, 1.5)]


# ── build_from_blueprint against the simulator ───────────────────────────────

@pytest.fixture
def empty_live(tmp_path, monkeypatch):
    session = tmp_path / "live-session.json"
    monkeypatch.setenv("ABLETON_MCP_SESSION_FILE", str(session))
    monkeypatch.delenv("ABLETON_PORT", raising=False)
    monkeypatch.setenv("ABLETON_MCP_PROFILE", "full")
    library = tmp_path / "User Library"
    library.mkdir()
    monkeypatch.setattr(als, "_user_libraries", lambda: [library])
    monkeypatch.setattr(extended_tools, "_get_connection", live_server.get_ableton_connection)
    mcp = MCPServer("t")
    extended_tools.register(mcp, live_server.get_ableton_connection)
    simulator = Simulator(free_port(), session)
    simulator.run_setup(EMPTY_SET)
    simulator.start()
    journal.clear()

    def call(tool, **arguments):
        result = asyncio.run(mcp.call_tool(tool, arguments))
        if result.is_error:
            raise ToolError(result.content[0].text)
        return json.loads(result.content[0].text)

    call.simulator, call.library = simulator, library
    yield call
    if live_server._ableton_connection is not None:
        live_server._ableton_connection.disconnect()
        live_server._ableton_connection = None
    simulator.stop()


def generated(call):
    return call.library / "Samples" / "AbletonMCP Generated"


def test_a_blueprint_is_built_and_read_back(empty_live):
    call = empty_live
    report = call("build_from_blueprint", blueprint=str(FIXTURES / "short-beat.json"))
    assert report["ok"] is True, report
    assert [t["track"] for t in report["tracks"]] == ["808 (synth)", "Drums (library)"]
    assert report["files"] == {"folder": str(generated(call)), "copied": 4}
    assert report["locators"] == 2 and "locators_moved" not in report
    checks = report["checks"]
    assert checks["time_signature"] == {"want": "4/4", "live": "4/4", "same": True}
    assert checks["tempo"] == {"want": 120.0, "live": 120.0, "same": True}
    assert checks["samples"] == {"checked": 4, "wrong": {}}
    assert checks["volumes"] == {"checked": 3, "held": 3}
    state = call.simulator.state()
    assert state["tempo"] == 120 and state["signature"] == "4/4"
    assert [c["name"] for c in state["locators"]] == ["A (1)", "B (2)"]
    tracks = {t["name"]: t for t in state["tracks"]}
    assert list(tracks) == ["1-MIDI", "2-MIDI", "808 (synth)", "Drums (library)"]
    simpler = tracks["808 (synth)"]["devices"][0]
    assert simpler["sample"] == fixture("short-beat")["files"]["808"]["name"]
    shown = simpler["parameters"]
    assert (shown["Transpose"], shown["Detune"], shown["Snap"], shown["F On"]) == ("36 st", "-12 ct", "Off", "Off")
    assert shown["Vol < Vel"] == "35 %" and shown["Ve Release"] == "50.0 ms"
    assert tracks["808 (synth)"]["volume"]["display"] == "-2.0 dB"
    chains = tracks["Drums (library)"]["devices"][0]["chains"]
    hat = chains["42"][0]["devices"][0]
    assert hat["sample"].startswith("hat-closed ") and hat["parameters"]["Volume"] == "-9.5 dB"
    assert hat["parameters"]["Transpose"] == "2 st"
    # reference-dissector's pad settings (its PAD_OFF): filter off, Fade In 0 where a
    # Simpler made by path starts at 0.1 ms (Live 12.4.16); the 808 keeps its own
    assert (hat["parameters"]["F On"], hat["parameters"]["Fade In"]) == ("Off", "0.0 ms")
    assert shown["Fade In"] == "0.1 ms" and hat["parameters"]["Fade Out"] == "0.1 ms"
    clip = tracks["Drums (library)"]["arrangement_clips"][0]
    assert (clip["name"], clip["length"], len(clip["notes"])) == ("Drums", 8, 24)
    # The Simplers hold the copies in the User Library, not the blueprint's files
    for name in ("808", "kick", "snare", "hat"):
        assert (generated(call) / fixture("short-beat")["files"][name]["name"]).is_file()


def test_choke_groups_and_a_scaled_copy(empty_live):
    call = empty_live
    report = call("build_from_blueprint", blueprint=str(FIXTURES / "choke-and-scaled.json"))
    assert report["ok"] is True, report
    assert report["files"]["scaled"] == 1 and report["files"]["copied"] == 3
    assert report["checks"]["choke_groups"] == {"checked": 2, "wrong": {}}
    assert report["checks"]["time_signature"]["live"] == "3/4"
    state = call.simulator.state()
    rack = next(t for t in state["tracks"] if t["name"] == "Drums (library)")["devices"][0]
    assert [rack["chains"][n][0]["choke_group"] for n in ("36", "39", "42", "46")] == [0, 0, 1, 1]
    clap = rack["chains"]["39"][0]["devices"][0]["sample"]
    assert clap.endswith(" -3.10dB.wav") and sf.info(generated(call) / clap).subtype == "FLOAT"
    other = next(t for t in state["tracks"] if t["name"] == "other (notes)")
    assert other["mute"] and other["devices"][0]["name"] == "Drift"
    assert [(c["name"], c["time"]) for c in state["locators"]] == [("Intro (1)", 0), ("Hook (3)", 6)]


def test_an_excerpt_with_glide_voices_a_stem_and_a_pad_by_name(empty_live, tmp_path):
    call = empty_live
    folder = tmp_path / "work"  # the excerpt's stems go beside the blueprint
    folder.mkdir()
    data = absolute(fixture("glide-and-stem"))
    (folder / "blueprint.json").write_text(json.dumps(data))
    report = call("build_from_blueprint", blueprint=str(folder), bars="2-2")
    assert report["ok"] is True, report
    assert report["excerpt"]["bars"] == [2, 2]
    assert report["checks"]["voices"] == {"808 (synth)": {"want": 1, "live": 1, "same": True}}
    state = call.simulator.state()
    tracks = {t["name"]: t for t in state["tracks"]}
    simpler = tracks["808 (synth)"]["devices"][0]
    assert simpler["voices"] == 1 and simpler["parameters"]["Glide Mode"] == "Glide"
    assert simpler["parameters"]["Glide Time"] == "45.0 ms"
    stem = tracks["REF drums"]["arrangement_clips"][0]
    assert Path(stem["file_path"]).parent == folder / "excerpt-2-2"
    assert Path(stem["file_path"]).name.startswith("ref-drums ")  # named by its content
    assert tracks["REF drums"]["mute"]
    pad = tracks["Drums (by name)"]["devices"][0]["chains"]["38"][0]["devices"][0]
    assert pad["sample"] == "Snare Tight.wav"
    assert pad["parameters"]["Fade In"] == "0.0 ms"  # the browser's pads start at 0 (Live 12.4.16)
    assert [(c["name"], c["time"]) for c in state["locators"]] == [("B (2)", 0)]
    assert state["browser"]["loaded"]


def test_volumes_that_do_not_hold_go_again_as_text(empty_live, monkeypatch):
    call = empty_live
    from MCP_Server import fake_live

    original = fake_live.FakeParam.__setattr__

    def coarse(self, name, value):  # a Volume that lands on 0.05 dB steps, whatever number it is sent
        if name == "value" and self.name == "Volume" and isinstance(value, float):
            value = round(value / 0.05) * 0.05
        original(self, name, value)

    monkeypatch.setattr(fake_live.FakeParam, "__setattr__", coarse)
    data = absolute(fixture("short-beat"))
    data["tracks"][1]["instrument"]["drum_rack"]["38"]["settings"]["Volume"] = -6.63
    report = call("build_from_blueprint", blueprint=data)
    volumes = report["checks"]["volumes"]
    assert volumes["held"] == 2 and volumes["held_by_text"] == ["Drums (library) pad 38"]
    assert report["ok"] is True


def test_a_volume_held_is_not_sent_again_for_its_display():
    # Live shows one decimal below -10 dB: -12.34 held exactly reads "-12.3 dB"
    assert bp.volume_held(-12.34, -12.34, "-12.3 dB")
    assert bp.volume_held(-6.63, -6.63, "-6.63 dB") and not bp.volume_held(-6.63, -6.63, "-6.7 dB")
    assert not bp.volume_held(-6.63, -6.65, "-6.65 dB")
    assert bp.volume_held(-3.0, -3.0, None)


def test_locators_live_moves_are_deleted_and_reported(empty_live):
    call = empty_live
    data = fixture("short-beat")
    # a third of a beat is off even the finest grid (the fake's: 1/64 beat), so Live moves it
    data["locators"].append({"name": "C (2.3)", "beat": 6 + 1 / 3})
    data["length_beats"] = 12
    data = absolute(data)
    report = call("build_from_blueprint", blueprint=data)
    assert report["locators_moved"] == [{"name": "C (2.3)", "asked": pytest.approx(6 + 1 / 3),
                                         "live": 405 / 64, "deleted": True}]
    assert report["ok"] is False
    assert [c["time"] for c in call.simulator.state()["locators"]] == [0, 4]


def test_the_fake_snaps_cues_to_the_grid_of_the_zoom():
    from MCP_Server import fake_live

    song = fake_live.FakeSong()
    song.cue_points = []
    app = fake_live.FakeApplication(fake_live.FakeBrowser(song))

    def cue(at):
        song.current_song_time = at
        song.set_or_delete_cue()
        return song.cue_points[-1].time

    assert [cue(b) for b in (9.0, 21.0, 33.0)] == [8.0, 20.0, 32.0]   # 4/4, default zoom (Live 12.4.6)
    song.cue_points = []
    song.signature_numerator, song.signature_denominator = 3, 4
    assert [cue(b) for b in (9.0, 21.0, 33.0)] == [12.0, 24.0, 36.0]
    for _ in range(4):
        app.view.zoom_view(3, "Arranger", False)
    song.cue_points = []
    assert cue(9.0) == 9.0                                             # 4 steps in
    for _ in range(26):
        app.view.zoom_view(3, "Arranger", False)
    assert cue(10.125) == 10.125                                       # ~30: an eighth of a beat
    app.view.zoom_view(0, "Arranger", False)                           # track heights only
    for _ in range(30):
        app.view.zoom_view(2, "Arranger", False)
    assert song.arranger_grid() == 6.0


def test_locators_land_exactly_zoomed_in_and_the_zoom_goes_back(empty_live):
    call = empty_live
    song = call.simulator.song
    data = absolute(fixture("short-beat"))
    data["time_signature"], data["length_beats"] = "3/4", 48
    data["locators"] = [{"name": n, "beat": b} for n, b in
                        (("A", 0), ("B", 9), ("C", 21), ("D", 33), ("E", 45))]
    report = call("build_from_blueprint", blueprint=data)
    assert report["locators"] == 5 and "locators_moved" not in report, report
    assert [(c["name"], c["time"]) for c in call.simulator.state()["locators"]] == [
        ("A", 0), ("B", 9), ("C", 21), ("D", 33), ("E", 45)]
    assert song.arranger_grid() == 6.0  # zoomed back out: 3/4's default grid
    assert "Arranger" in call.simulator.surface.application().view.shown


def test_without_the_zoom_live_would_move_them(empty_live):
    call = empty_live
    song = call.simulator.song
    song.signature_numerator, song.signature_denominator = 3, 4
    placed = extended_tools.place_locators([("B", 9.0)], zoom=False)
    assert placed == [{"name": "B", "time": 9.0, "error": "Live did not create it"}]
    assert [c.time for c in song.cue_points] == [12.0]


def test_a_playhead_live_refuses_places_no_cue(empty_live, monkeypatch):
    call = empty_live
    song = call.simulator.song
    prop = type(song).current_song_time

    def refuse_past_the_end(self, value):  # Live keeps the playhead where it was
        if value <= 40.0:
            prop.fset(self, value)

    monkeypatch.setattr(type(song), "current_song_time", property(prop.fget, refuse_past_the_end))
    song.current_song_time = 2.0
    placed = extended_tools.place_locators([("A", 8.0), ("End", 50.0)])
    assert placed == [{"name": "A", "time": 8.0}, {
        "name": "End", "time": 50.0,
        "error": "Live kept the playhead at beat 8 (a time past the song's end is refused)"}]
    assert [(c.name, c.time) for c in song.cue_points] == [("A", 8.0)]
    assert song.current_song_time == 2.0


def test_create_locator_off_the_bar_lands(empty_live):
    call = empty_live
    song = call.simulator.song
    song.signature_numerator, song.signature_denominator = 3, 4
    # create_locator is one of server.py's own tools; beat 9 is a bar line the default grid misses
    result = asyncio.run(live_server.mcp.call_tool("create_locator", {"name": "Drop", "time": 9.0}))
    assert "beat 9.0" in result.content[0].text, result.content[0].text
    assert [(c.name, c.time) for c in song.cue_points] == [("Drop", 9.0)]
    assert song.arranger_grid() == 6.0  # zoomed back out: 3/4's default grid


def test_a_relative_blueprint_path_gives_live_absolute_files(empty_live, monkeypatch):
    monkeypatch.chdir(FIXTURES.parent)
    report = empty_live("build_from_blueprint", blueprint="blueprints/glide-and-stem.json")
    assert report["ok"] is True, report
    stem = next(t for t in empty_live.simulator.state()["tracks"] if t["name"] == "REF drums")
    assert stem["arrangement_clips"][0]["file_path"] == str(FIXTURES / "samples" / "ref-drums.wav")


def test_a_set_that_is_not_empty_is_refused(tmp_path, monkeypatch, empty_live):
    call = empty_live
    call("build_from_blueprint", blueprint=str(FIXTURES / "short-beat.json"))
    with pytest.raises(ToolError, match="The set is not empty"):
        call("build_from_blueprint", blueprint=str(FIXTURES / "short-beat.json"))


def test_a_changed_file_is_refused_before_live_is_touched(empty_live, tmp_path):
    call = empty_live
    data = fixture("short-beat")
    for entry in data["files"].values():
        entry["source"] = str(FIXTURES / entry["source"])
    data["files"]["kick"]["sha1"] = "00000000"
    before = call.simulator.state()
    with pytest.raises(ToolError, match="not the file the blueprint was made with"):
        call("build_from_blueprint", blueprint=data)
    with pytest.raises(ToolError, match="cannot be built"):
        call("build_from_blueprint", blueprint={**data, "tempo": 5})
    with pytest.raises(ToolError, match="No blueprint at"):
        call("build_from_blueprint", blueprint=str(tmp_path))
    assert call.simulator.state()["tracks"] == before["tracks"]
    assert not generated(call).exists()


def test_a_failed_build_says_what_it_left(empty_live, monkeypatch):
    call = empty_live
    data = copy.deepcopy(fixture("short-beat"))
    data = absolute(data)
    data["tracks"][1]["instrument"] = "No Such Synth"
    # the track that failed is not named yet: build_tracks names it with its settings
    with pytest.raises(ToolError, match=r"build_tracks stopped: Could not insert 'No Such Synth'.* It left "
                                        r"'808 \(synth\)' \(Simpler\), '4-MIDI' \(no device\) in the set") as stopped:
        call("build_from_blueprint", blueprint=data)
    assert ".." not in str(stopped.value)  # Live's message ends in a period of its own (Live 12.4.16, 2026-11-18)


def test_a_scaled_copy_and_a_cut_stem_are_kept_across_a_second(tmp_path):
    # libsndfile stamps the time into a float WAV's PEAK chunk: the bytes of
    # two writes a second apart differ, their audio does not
    import time

    data = fixture("choke-and-scaled")
    folder = tmp_path / "generated"
    bp.put_files(data, FIXTURES, folder)
    stem = tmp_path / "float stem.wav"
    sf.write(str(stem), np.linspace(-0.5, 0.5, 8000, dtype=np.float32), 8000, subtype="FLOAT")
    first = bp.trim(stem, tmp_path / "cut", (0.1, 0.5))
    time.sleep(1.1)
    _placed, rows = bp.put_files(data, FIXTURES, folder)
    assert {r["action"] for r in rows} == {"kept"}
    assert bp.trim(stem, tmp_path / "cut", (0.1, 0.5)) == first
    assert len(list((tmp_path / "cut").iterdir())) == 1


def test_the_validator_refuses_what_the_build_would_drop_or_fail_on():
    def problems(change):
        data = fixture("short-beat")
        change(data)
        return " | ".join(bp.validate(data))

    def pitch(value):
        return lambda d: d["tracks"][0]["clips"][0]["notes"][0].update(pitch=value)

    assert "pitch 'zz' is not a note" in problems(pitch("zz"))
    assert "pitch 'C9' is not a note" in problems(pitch("C9"))  # MIDI 132
    assert problems(pitch("C1")) == ""
    assert "takes the track's settings" in problems(
        lambda d: d["tracks"][0]["instrument"].update(settings={"Transpose": 12}))
    assert "settings go on its pads" in problems(lambda d: d["tracks"][1].update(settings={"Volume": -3}))
    assert "a locator is at beat 4 already" in problems(
        lambda d: d["locators"].append({"name": "Again", "beat": 4}))
    assert "sidechain_from 'Kick' names no other track" in problems(
        lambda d: d["tracks"][0].update(effects=[{"device": "Compressor", "sidechain_from": "Kick"}]))
    assert problems(lambda d: d["tracks"][0].update(
        effects=[{"device": "Compressor", "sidechain_from": "Drums (library)"}])) == ""
    assert "keeps its file's format: name it .wav, not .aif" in problems(
        lambda d: d["files"]["kick"].update(name="kick 820c5e7b.aif"))
    assert problems(lambda d: d["files"]["kick"].update(name="kick 820c5e7b.WAV")) == ""


def test_one_file_under_two_paths_is_one_copy():
    data = fixture("short-beat")
    data["files"]["kick2"] = {**data["files"]["kick"], "source": "elsewhere/kick.wav"}
    assert bp.validate(data) == []
    data["files"]["kick2"]["sha1"] = "00000000"
    assert "names two different files" in " | ".join(bp.validate(data))
    del data["files"]["kick2"]["sha1"]
    assert "names two different files" in " | ".join(bp.validate(data))


def test_a_blueprint_older_than_its_plan_is_refused(empty_live, tmp_path):
    import hashlib
    import shutil

    folder = tmp_path / "song"
    shutil.copytree(FIXTURES, folder)
    plan = folder / "plan.json"
    plan.write_text('{"tempo": 120}', encoding="utf-8")
    data = fixture("short-beat")
    data["meta"] = {"plan": "plan.json", "plan_sha1": hashlib.sha1(plan.read_bytes()).hexdigest()[:12]}
    (folder / "blueprint.json").write_text(json.dumps(data), encoding="utf-8")
    assert bp.plan_changed(data, folder) is None
    plan.write_text('{"tempo": 121}', encoding="utf-8")
    assert "plan.json changed after this blueprint" in bp.plan_changed(data, folder)
    with pytest.raises(ToolError, match="plan.json changed after this blueprint"):
        empty_live("build_from_blueprint", blueprint=str(folder))
    assert bp.plan_changed(data, None) is None  # an object has no folder to compare with


def test_tempo_and_audio_clips_are_read_back(empty_live, monkeypatch):
    call = empty_live
    song = call.simulator.song
    original = type(song).__setattr__

    def slow(self, name, value):  # a tempo Live does not take as sent
        original(self, name, value - 1.0 if name == "tempo" else value)

    monkeypatch.setattr(type(song), "__setattr__", slow)
    report = call("build_from_blueprint", blueprint=str(FIXTURES / "short-beat.json"))
    assert report["checks"]["tempo"] == {"want": 120.0, "live": 119.0, "same": False}
    assert report["ok"] is False


def test_an_audio_clip_live_leaves_warped_is_a_problem(empty_live, monkeypatch):
    from MCP_Server import fake_live

    call = empty_live
    original = fake_live.FakeClip.__setattr__

    def stays_warped(self, name, value):
        if name == "warping" and getattr(self, "is_audio_clip", False):
            value = True
        original(self, name, value)

    monkeypatch.setattr(fake_live.FakeClip, "__setattr__", stays_warped)
    report = call("build_from_blueprint", blueprint=str(FIXTURES / "glide-and-stem.json"))
    stem = next(t for t in report["tracks"] if t["track"] == "REF drums")
    assert any("Live left it warped" in p for p in stem["problems"])
    assert report["ok"] is False


def test_a_missing_sample_path_is_found_before_the_build(tmp_path):
    data = fixture("short-beat")
    data["tracks"][0]["instrument"] = {"sample_path": str(tmp_path / "gone.wav"), "warping": False}
    assert bp.validate(data) == []
    assert any("no such file" in p and "gone.wav" in p for p in bp.check_sources(data, FIXTURES))


def test_clips_that_start_inside_an_excerpt(tmp_path):
    data = fixture("glide-and-stem")
    rate = 8000
    stem = tmp_path / "late.wav"
    sf.write(str(stem), np.linspace(-0.5, 0.5, rate * 4, dtype=np.float32), rate, subtype="FLOAT")
    bar, tempo = bp.bar_beats(data), float(data["tempo"])
    data["length_beats"] = 4 * bar
    data["tracks"].append({"name": "Late notes", "instrument": "Drift",
                           "clips": [{"start": bar + 1, "length": 2, "notes": [
                               {"pitch": 60, "start_time": 0.5, "duration": 4}]}]})
    data["tracks"].append({"name": "REF late", "kind": "audio",
                           "audio_clips": [{"file": str(stem), "start": bar + 1, "warping": False}]})
    part = bp.excerpt(data, (2, 2), FIXTURES, tmp_path / "cut")
    late = next(t for t in part["tracks"] if t["name"] == "Late notes")["clips"][0]
    assert (late["start"], late["length"]) == (1, 2)  # the clip moves with the bar, notes stay in it
    assert late["notes"] == [{"pitch": 60, "start_time": 0.5, "duration": 1.5}]
    audio = next(t for t in part["tracks"] if t["name"] == "REF late")["audio_clips"][0]
    assert audio["start"] == 1  # from the file's start, at the clip's place in the bar
    frames = sf.info(audio["file"]).frames
    assert frames == round((bar - 1) * 60 / tempo * rate)


def _no_zoom(call, monkeypatch, refuse_past=None):
    """Live's zoom_view failing, and (refuse_past) the playhead refused past
    that beat, as Live refuses a time past the song's end."""
    song = call.simulator.song
    view = call.simulator.surface.application().view

    def broken(self, *args):
        raise RuntimeError("no zoom here")

    monkeypatch.setattr(type(view), "zoom_view", broken)
    if refuse_past is not None:
        prop = type(song).current_song_time

        def refuse(self, value):
            if value <= refuse_past:
                prop.fset(self, value)

        monkeypatch.setattr(type(song), "current_song_time", property(prop.fget, refuse))
    return song


def test_a_stray_live_keeps_the_playhead_from_is_left_and_a_good_locator_stays(empty_live, monkeypatch):
    """No zoom: 45 of a 3/4 song snaps to 48, past where Live takes the
    playhead. Deleting that stray would toggle at the playhead left behind
    (0, where locator A is): it is left in place and reported, A stays."""
    call = empty_live
    _no_zoom(call, monkeypatch, refuse_past=46.0)
    data = absolute(fixture("short-beat"))
    data["time_signature"], data["length_beats"] = "3/4", 48
    data["locators"] = [{"name": "A", "beat": 0}, {"name": "E", "beat": 45}]
    report = call("build_from_blueprint", blueprint=data)
    cues = [(c["name"], c["time"]) for c in call.simulator.state()["locators"]]
    assert ("A", 0) in cues and report["locators"] == 1
    assert report["locators_moved"] == [{"name": "E", "asked": 45.0, "live": 48.0, "playhead": 0.0,
                                         "deleted": False}]
    assert report["zoom_errors"] and "no zoom here" in report["zoom_errors"][0]
    assert report["ok"] is False


def test_a_set_with_a_locator_is_not_empty(empty_live):
    call = empty_live
    song = call.simulator.song
    song.current_song_time = 0.0
    song.set_or_delete_cue()
    song.cue_points[-1].name = "my marker"
    with pytest.raises(ToolError, match=r"The set is not empty \(locator 'my marker' at beat 0\)"):
        call("build_from_blueprint", blueprint=absolute(fixture("short-beat")))
    assert [c.name for c in song.cue_points] == ["my marker"] and len(song.tracks) == 2


def test_create_locator_deletes_a_cue_live_moved_and_says_so(empty_live, monkeypatch):
    """create_locator with the zoom failing: Live snaps beat 9 of 3/4 to 12;
    that cue is deleted and the error says where Live put it and why."""
    call = empty_live
    song = _no_zoom(call, monkeypatch)
    song.signature_numerator, song.signature_denominator = 3, 4
    placed = extended_tools.place_locators([("Drop", 9.0)])
    assert placed == [{"name": "Drop", "time": 9.0, "error": "Live did not create it; Live put it at beat 12, "
                       "deleted (zooming the Arrangement in failed: no zoom here)"}]
    assert song.cue_points == []


def _legacy(tool, **arguments):
    result = asyncio.run(live_server.mcp.call_tool(tool, arguments))
    return result.content[0].text


def test_create_audio_clip_checks_and_fits_what_live_warped(empty_live, tmp_path, monkeypatch):
    """Live warps a new clip at the tempo it guesses for the file (a 120 BPM
    click track came in at 96 BPM, Live 12.4.16): the tool says so, fits it
    to `bars`, or with warping=false plays it as recorded, its Session
    length (what copies get) the file's at the set's tempo."""
    from MCP_Server import fake_live

    call = empty_live
    song = call.simulator.song
    song.create_audio_track(-1)
    index = len(song.tracks) - 1
    wav = tmp_path / "loop.wav"
    sf.write(wav, np.zeros(88200), 44100)  # 2 s: 4 beats at 120 BPM
    monkeypatch.setitem(fake_live.FILE_TEMPOS, str(wav), 96.0)

    def made(slot, **options):
        text = _legacy("create_audio_clip", track_index=index, clip_index=slot, path=str(wav),
                       **options)
        assert text.startswith("Created audio clip"), text
        return json.loads(text.split(": ", 1)[1]), song.tracks[index].clip_slots[slot].clip

    guessed, clip = made(0)
    assert guessed["length_beats"] == 3.2 and guessed["live_guess_bpm"] == 96.0
    assert guessed["warp_bpm"] == 96.0  # as Live warped it
    assert "96.0 BPM" in guessed["warning"] and "4 beats" in guessed["warning"]
    fitted, clip = made(1, bars=1)
    assert fitted["length_beats"] == 4.0 and "warning" not in fitted and clip.warping is True
    # Live's first reading stays labelled as such; fitted, the file plays at the set's tempo
    assert (fitted["live_guess_bpm"], fitted["warp_bpm"]) == (96.0, 120.0)
    assert [m.beat_time for m in clip.warp_markers[:2]] == [0.0, 4.0]
    as_recorded, clip = made(2, warping=False)
    assert (as_recorded["length_beats"], as_recorded["warping"]) == (4.0, False)
    assert "warning" not in as_recorded and (clip.loop_start, clip.start_marker) == (0.0, 0.0)
    copy = json.loads(_legacy("duplicate_to_arrangement", track_index=index, clip_index=2,
                              destination_time=8.0))
    assert (copy["start_time"], copy["end_time"]) == (8.0, 12.0) and "warnings" not in copy
    # a loop made at another tempo (not whole bars at the set's): Live's reading stands
    loop90 = tmp_path / "loop90.wav"
    sf.write(loop90, np.zeros(int(44100 * 8 / 3)), 44100)  # 4 beats at 90 BPM
    monkeypatch.setitem(fake_live.FILE_TEMPOS, str(loop90), 90.0)
    text = _legacy("create_audio_clip", track_index=index, clip_index=3, path=str(loop90))
    info = json.loads(text.split(": ", 1)[1])
    assert info["live_guess_bpm"] == 90.0 and info["length_beats"] == 4.0 and "warning" not in info


def test_create_audio_clip_refuses_bars_unwarped_before_making_it(empty_live, tmp_path):
    call = empty_live
    song = call.simulator.song
    song.create_audio_track(-1)
    index = len(song.tracks) - 1
    with pytest.raises(ToolError, match="warping=false"):
        _legacy("create_audio_clip", track_index=index, clip_index=0,
                path=str(tmp_path / "x.wav"), bars=2, warping=False)
    assert not song.tracks[index].clip_slots[0].has_clip


def test_create_audio_clip_says_the_clip_is_there_when_its_set_up_fails(empty_live,
                                                                         monkeypatch):
    call = empty_live
    song = call.simulator.song
    song.create_audio_track(-1)
    index = len(song.tracks) - 1

    def broken(*_args, **_kwargs):
        raise RuntimeError("Live went away")

    monkeypatch.setattr(extended_tools, "fit_audio_clip", broken)
    text = _legacy("create_audio_clip", track_index=index, clip_index=0,
                   path=str(FIXTURES / "samples" / "kick.wav"))
    assert text.startswith(f"Created audio clip 'kick' at track {index}, slot 0, but setting it")
    assert "Live went away" in text and song.tracks[index].clip_slots[0].has_clip


def test_duplicate_to_arrangement_says_where_the_copy_landed(empty_live):
    """Live makes the copy the clip's length (loop_end - loop_start) even when
    an unlooped clip's end_marker is earlier: said, and `length` sizes it."""
    from MCP_Server import fake_live

    call = empty_live
    song = call.simulator.song
    clip = fake_live.FakeClip(name="Pad", length=16.0)
    clip.looping, clip.end_marker = False, 4.0
    slot = song.tracks[0].clip_slots[0]
    slot.clip, slot.has_clip = clip, True
    whole = json.loads(_legacy("duplicate_to_arrangement", track_index=0, clip_index=0,
                               destination_time=16.0))
    assert (whole["start_time"], whole["end_time"]) == (16.0, 32.0)
    assert "end_marker 4" in whole["warnings"][0]
    short = json.loads(_legacy("duplicate_to_arrangement", track_index=0, clip_index=0,
                               destination_time=32.0, length=4.0))
    assert (short["start_time"], short["end_time"]) == (32.0, 36.0) and "warnings" not in short
    assert (clip.loop_end, clip.end_marker, clip.looping) == (16.0, 4.0, False)
    assert song.current_song_time == 0.0  # Live moved it to each copy; put back


def test_the_original_create_audio_clip_plays_the_file_from_its_start(empty_live):
    """server.py's create_audio_clip: Live's warp analysis moves a new clip's
    start to the file's first transient; the tool puts it back."""
    call = empty_live
    song = call.simulator.song
    song.create_audio_track(-1)
    index = len(song.tracks) - 1
    result = asyncio.run(live_server.mcp.call_tool("create_audio_clip", {
        "track_index": index, "clip_index": 0, "path": str(FIXTURES / "samples" / "kick.wav")}))
    assert "Created audio clip" in result.content[0].text, result.content[0].text
    clip = song.tracks[index].clip_slots[0].clip
    assert (clip.loop_start, clip.start_marker) == (0.0, 0.0)
