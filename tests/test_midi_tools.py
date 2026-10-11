"""The Max for Live MIDI Tools: their JavaScript, run in Node with Max's
globals stood in for, against the Python generators they port; and the
devices themselves."""

import json
import shutil
import subprocess

import pytest

from MCP_Server import midi_tools, music, turkish
from MCP_Server.m4l_device import read_amxd

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="needs Node.js")

# What Max gives a v8 program, enough to run a MIDI Tool's apply cycle
HARNESS = r"""
const vm = require("vm");
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
const sent = [];
const sandbox = {
  inlet: 0, inlets: 1, outlets: 1, post: () => {},
  outlet: (n, ...args) => sent.push({outlet: n, args}),
  outlet_dictionary: (n, d) => sent.push({outlet: n, dict: JSON.parse(JSON.stringify(d))}),
};
vm.createContext(sandbox);
vm.runInContext(input.code, sandbox);
for (const step of input.steps) {
  if (step.call) { sandbox.inlet = 0; vm.runInContext(step.call, sandbox); continue; }
  sandbox.inlet = 1; sandbox.msg_dictionary(step.context);
  sandbox.inlet = 0; sandbox.msg_dictionary({notes: step.notes || []});
}
process.stdout.write(JSON.stringify({sent, inlets: sandbox.inlets, outlets: sandbox.outlets}));
"""


def run(tool, steps):
    result = subprocess.run([NODE, "-e", HARNESS], input=json.dumps(
        {"code": midi_tools.code(tool), "steps": steps}), capture_output=True, text=True,
        timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def context(start=0.0, end=4.0, seed=7, root=0):
    return {"clip": {"time_selection_start": start, "time_selection_end": end},
            "grid": {"interval": 0.25, "enabled": 1}, "seed": seed,
            "scale": {"root_note": root, "scale_intervals": [0, 2, 4, 5, 7, 9, 11],
                      "scale_mode": 1}}


def written(result):
    return [s["dict"]["notes"] for s in result["sent"] if "dict" in s]


@needs_node
def test_usul_matches_the_python_generator():
    result = run("usul", [{"context": context(0, 9)}])
    assert (result["inlets"], result["outlets"]) == (2, 2)
    notes = written(result)[0]
    pads = {"düm": 36, "tek": 38, "te": 37, "ke": 37, "ka": 37}
    expected = turkish.usul_notes("aksak", 2, pads)  # 9/8: 4.5 beats a cycle
    assert [(n["pitch"], n["start_time"], n["velocity"]) for n in notes] == [
        (n["pitch"], n["start_time"], n["velocity"]) for n in expected]
    # Other usuls, pads and ornaments through the controls
    index = midi_tools.USUL_NAMES.index("sofyan")
    result = run("usul", [{"call": f"usul({index}); dum(35); ornament(100)"},
                          {"context": context(0, 8)}])
    notes = written(result)[0]
    assert [n["start_time"] for n in notes if n["pitch"] == 35] == [0, 4]
    assert any(n["pitch"] == 37 and n["start_time"] == 1 for n in notes)  # filled "Düüm"


@needs_node
def test_makam_snap_moves_notes_to_the_makam():
    karar_d = midi_tools.KARAR_ITEMS.index("D")
    notes = [{"note_id": i + 1, "pitch": p, "start_time": i, "duration": 0.5, "velocity": 90,
              "mute": 0, "probability": 1, "velocity_deviation": 0, "release_velocity": 64}
             for i, p in enumerate([62, 64, 65, 67, 70, 72])]
    result = run("makam_snap", [{"call": f"karar({karar_d})"},
                                {"context": context(), "notes": notes}])
    moved = written(result)[0]
    allowed = {(2 + i) % 12 for i in turkish.intervals_12tet("hicaz")}  # D Eb F# G A B C
    assert all(n["pitch"] % 12 in allowed for n in moved)
    assert [n["pitch"] for n in moved] == [62, 63, 66, 67, 69, 72]  # ties go down: Bb -> A
    assert [n["note_id"] for n in moved] == [1, 2, 3, 4, 5, 6]
    assert all(m["start_time"] == n["start_time"] for m, n in zip(moved, notes))


@needs_node
def test_makam_melody_stays_in_the_makam_and_ends_on_the_karar():
    result = run("makam_melody", [{"context": context(4, 20, seed=11)},
                                  {"context": context(4, 20, seed=11)},
                                  {"context": context(4, 20, seed=12)}])
    first, again, other = written(result)
    karar = music.parse_pitch_class(turkish.makam("hicaz")["karar"])  # A
    allowed = {(karar + i) % 12 for i in turkish.intervals_12tet("hicaz")}
    assert len(first) >= 8 and all(n["pitch"] % 12 in allowed for n in first)
    assert all(4 <= n["start_time"] < 20 for n in first)
    # Down the first tetrachord to the karar: D C# Bb A on A
    assert [n["pitch"] % 12 for n in first[-4:]] == [(karar + i) % 12 for i in (5, 4, 1, 0)]
    assert first == again and first != other  # the seed decides


@needs_node
def test_controls_apply_again_only_after_the_first_apply():
    result = run("makam_snap", [{"call": "makam(2)"}, {"context": context()},
                                {"call": "makam(3)"}])
    bangs = [s for s in result["sent"] if s.get("outlet") == 1]
    assert len(bangs) == 1 and bangs[0]["args"] == ["bang"]


def test_the_devices_are_midi_tools_with_their_code_inside():
    for name, tool in midi_tools.TOOLS.items():
        kind, patcher = read_amxd(midi_tools.device(name))
        assert kind == tool["type"]
        patcher = patcher["patcher"]
        assert patcher["project"]["amxdtype"] == int.from_bytes(kind, "big")
        classes = {b["box"]["maxclass"]: b["box"] for b in patcher["boxes"]}
        texts = [b["box"].get("text", "") for b in patcher["boxes"]]
        assert "live.miditool.in" in texts and "live.miditool.out" in texts
        assert "function transform" in classes["v8.codebox"]["code"]
        assert patcher["dependency_cache"] == []
        names = [b["box"]["saved_attribute_attributes"]["valueof"]["parameter_longname"]
                 for b in patcher["boxes"] if b["box"].get("parameter_enable")]
        assert names == [label for label, _c, _f in tool["controls"]]
        ids = {b["box"]["id"] for b in patcher["boxes"]}
        for line in patcher["lines"]:
            assert {line["patchline"]["source"][0], line["patchline"]["destination"][0]} <= ids


def test_install_writes_each_tool_where_live_lists_it(tmp_path):
    library = tmp_path / "User Library"
    paths = midi_tools.install([library])
    assert sorted(p.relative_to(library).as_posix() for p in paths) == [
        "MIDI Tools/Max Generators/Makam Melody.amxd", "MIDI Tools/Max Generators/Usul.amxd",
        "MIDI Tools/Max Transformations/Makam Snap.amxd"]
    before = {p: p.stat().st_mtime_ns for p in paths}
    midi_tools.install([library])  # unchanged: not rewritten
    assert {p: p.stat().st_mtime_ns for p in paths} == before


def test_every_usul_and_makam_has_its_turkish_name():
    assert set(midi_tools.USUL_LABELS) == set(turkish.USULS)
    assert set(midi_tools.MAKAM_LABELS) == set(turkish.MAKAMS)
    _kind, patcher = read_amxd(midi_tools.device("Usul"))
    menus = [b["box"] for b in patcher["patcher"]["boxes"] if b["box"]["maxclass"] == "live.menu"]
    assert "Türk Aksağı" in menus[0]["saved_attribute_attributes"]["valueof"]["parameter_enum"]
