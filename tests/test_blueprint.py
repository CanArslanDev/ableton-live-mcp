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
    (lambda d: d["tracks"][1]["instrument"]["drum_rack"].update({"128": {"file": "kick"}}), "128"),
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
    assert stem.name == f"ref-drums {bp.sha1(stem)[:8]}.wav"
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
    data = json.loads(json.dumps(fixture("glide-and-stem")).replace('"samples/', f'"{FIXTURES}/samples/'))
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
    assert [(c["name"], c["time"]) for c in state["locators"]] == [("B (2)", 0)]
    assert state["browser"]["loaded"]


def test_volumes_that_do_not_hold_go_again_as_text(empty_live):
    call = empty_live
    data = fixture("short-beat")
    for entry in data["files"].values():
        entry["source"] = str(FIXTURES / entry["source"])
    data["tracks"][1]["instrument"]["drum_rack"]["38"]["settings"]["Volume"] = -6.63
    report = call("build_from_blueprint", blueprint=data)
    volumes = report["checks"]["volumes"]
    assert volumes["held"] == 2 and volumes["held_by_text"] == ["Drums (library) pad 38"]
    assert report["ok"] is True


def test_locators_live_moves_are_deleted_and_reported(empty_live, monkeypatch):
    call = empty_live
    song = call.simulator.song
    original = type(song).set_or_delete_cue

    def on_the_bar_grid(self):  # Live snaps a new cue to the Arrangement's grid (zoomed out: bars)
        position = self.current_song_time
        self.current_song_time = 4.0 * round(position / 4.0)
        original(self)
        self.current_song_time = position

    monkeypatch.setattr(type(song), "set_or_delete_cue", on_the_bar_grid)
    data = fixture("short-beat")
    data["locators"].append({"name": "C (2.3)", "beat": 6.5})  # 6.5 snaps to 8
    data["length_beats"] = 12
    data = json.loads(json.dumps(data).replace('"samples/', f'"{FIXTURES}/samples/'))
    report = call("build_from_blueprint", blueprint=data)
    assert report["locators_moved"] == [{"name": "C (2.3)", "asked": 6.5, "live": 8.0, "deleted": True}]
    assert report["ok"] is False
    assert [c["time"] for c in call.simulator.state()["locators"]] == [0, 4]


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
    data = json.loads(json.dumps(data).replace('"samples/', f'"{FIXTURES}/samples/'))
    data["tracks"][1]["instrument"] = "No Such Synth"
    # the track that failed is not named yet: build_tracks names it with its settings
    with pytest.raises(ToolError, match=r"build_tracks stopped: Could not insert 'No Such Synth'.* It left "
                                        r"'808 \(synth\)' \(Simpler\), '4-MIDI' \(no device\) in the set"):
        call("build_from_blueprint", blueprint=data)
