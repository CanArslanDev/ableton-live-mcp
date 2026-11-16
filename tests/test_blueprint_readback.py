"""build_from_blueprint's read-backs where Live does not take what was
asked: the simulator made to refuse a file, a choke group, a Volume (as a
number and as text) and voices. Each difference is reported and makes the
report's ok false; a check turned off in the blueprint is not run."""

import json

from MCP_Server import fake_live
from tests import test_blueprint
from tests.test_blueprint import FIXTURES, absolute, fixture

empty_live = test_blueprint.empty_live  # the simulator's empty set, as test_blueprint builds into


def _names(data):
    return [entry["name"] for entry in data["files"].values()]


def test_a_simpler_holding_another_file_is_wrong(empty_live, monkeypatch):
    """Live refuses every file by path, and the browser's item of the copy's
    name holds another file (as Live's did in the live check of 2026-10-29):
    each sampler is reported wrong, with why it went by name."""
    call = empty_live
    data = absolute(fixture("short-beat"))

    def refuse(self, file_path):
        raise RuntimeError("Could not load it")

    monkeypatch.setattr(fake_live.FakeSimpler, "replace_sample", refuse)
    call.simulator.browser.samples.children.extend(
        fake_live.FakeBrowserItem(n, source="User Library") for n in _names(data))
    report = call("build_from_blueprint", blueprint=data)
    wrong = report["checks"]["samples"]["wrong"]
    assert report["checks"]["samples"]["checked"] == 4 and len(wrong) == 4
    assert all(v["same"] is False and v["live"].startswith("/samples/") for v in wrong.values())
    assert all(t.get("loaded_by_name") for t in report["tracks"] if t["track"] in ("808 (synth)", "Drums (library)"))
    assert report["ok"] is False


def test_a_choke_group_live_does_not_take_is_wrong(empty_live, monkeypatch):
    call = empty_live
    original = fake_live.FakeDrumChain.__setattr__

    def deaf(self, name, value):
        if name != "choke_group":
            original(self, name, value)

    monkeypatch.setattr(fake_live.FakeDrumChain, "__setattr__", deaf)
    report = call("build_from_blueprint", blueprint=str(FIXTURES / "choke-and-scaled.json"))
    wrong = report["checks"]["choke_groups"]["wrong"]
    assert wrong and all(v["live"] == 0 and v["same"] is False for v in wrong.values())
    assert report["ok"] is False


def _volumes_off_by(monkeypatch, db):
    """Every Simpler Volume Live holds lands db off what was set, number or text."""
    original = fake_live.FakeParam.__setattr__

    def off(self, name, value):
        if name == "value" and self.name == "Volume" and self.min == -36.0:
            value = value + db
        original(self, name, value)

    monkeypatch.setattr(fake_live.FakeParam, "__setattr__", off)


def test_a_volume_held_neither_as_a_number_nor_as_text_is_not_held(empty_live, monkeypatch):
    call = empty_live
    _volumes_off_by(monkeypatch, 0.5)
    report = call("build_from_blueprint", blueprint=absolute(fixture("short-beat")))
    volumes = report["checks"]["volumes"]
    assert volumes["held"] == 0 and len(volumes["not_held"]) == volumes["checked"] == 3
    row = next(iter(volumes["not_held"].values()))
    assert row["held"] is False and row["by_text"] is False and "then" in row
    assert "held_by_text" not in volumes and report["ok"] is False


def test_voices_live_does_not_take_are_reported(empty_live, monkeypatch):
    call = empty_live
    original = fake_live.FakeSimpler.__setattr__

    def deaf(self, name, value):
        if name != "voices":
            original(self, name, value)

    monkeypatch.setattr(fake_live.FakeSimpler, "__setattr__", deaf)
    report = call("build_from_blueprint", blueprint=absolute(fixture("glide-and-stem")))
    voices = report["checks"]["voices"]
    assert voices and all(v["same"] is False and v["live"] != v["want"] for v in voices.values())
    assert report["ok"] is False


def test_checks_turned_off_are_not_run(empty_live, monkeypatch):
    """checks: {"volumes": false, ...}: a Volume Live does not take is not
    read back, and the build is ok."""
    call = empty_live
    _volumes_off_by(monkeypatch, 0.5)
    data = absolute(fixture("short-beat"))
    data["checks"] = {"volumes": False, "samples": False, "time_signature": False, "tempo": False}
    report = call("build_from_blueprint", blueprint=data)
    assert set(report["checks"]) <= {"choke_groups", "voices"}
    assert report["ok"] is True


def test_a_blueprint_object_with_bars_cuts_its_stems_into_the_data_folder(empty_live, monkeypatch, tmp_path):
    """A blueprint given as an object has no folder: an excerpt's stems go
    to the reference data folder's excerpt-A-B/."""
    from MCP_Server import reference_jobs as jobs

    call = empty_live
    monkeypatch.setenv("ABLETON_MCP_REFERENCE_DIR", str(tmp_path / "data"))
    report = call("build_from_blueprint", blueprint=json.dumps(absolute(fixture("glide-and-stem"))), bars="1-1")
    assert report["excerpt"]["bars"] == [1, 1]
    cut = list((jobs.data_dir() / "excerpt-1-1").iterdir())
    assert cut and all(p.suffix == ".wav" for p in cut)
