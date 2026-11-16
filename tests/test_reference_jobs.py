"""Reference jobs (reference_jobs.py, reference_runner.py) and their tools,
with a stand-in for refdissect: real processes, no uv, no models."""

import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from MCP_Server import doctor, extended_tools
from MCP_Server import reference_jobs as jobs
from MCP_Server import reference_runner

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "blueprints"
# Leftover steps and reused pids are found through Unix process groups and ps;
# on Windows (not claimed yet) a job is only what this server's handle says
POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="Unix process groups and ps")

# refdissect's commands as far as a job uses them: each writes what the real
# one writes for the next step. FAKE_MODE: "slow" (analyze waits a minute),
# "no-match" (no match command: a step that is not optional),
# "fail" (analyze fails), "old" (no blueprint command, as before refdissect had one)
FAKE_REFDISSECT = r'''
import json, os, sys, time
from pathlib import Path

mode = os.environ.get("FAKE_MODE", "")
fixtures = Path(os.environ["FAKE_FIXTURES"])
command, *rest = sys.argv[1:]


def option(flag):
    return rest[rest.index(flag) + 1]


print("refdissect", command, *rest, flush=True)
if command == "analyze" and mode == "slow":
    Path(os.environ["FAKE_PIDFILE"]).write_text(str(os.getpid()))
    time.sleep(60)
if command == "match" and mode == "no-match":  # a step that is not optional, missing
    print("refdissect: error: argument command: invalid choice: 'match' (choose from 'plan')", file=sys.stderr)
    sys.exit(2)
if command == "analyze" and mode == "fail":
    print("Traceback (most recent call last):\nValueError: no beat found", flush=True)
    sys.exit(1)
if command == "separate":
    out = Path(option("--out"))
    out.mkdir(parents=True, exist_ok=True)
    for stem in ("drums", "bass", "other", "vocals"):
        (out / f"{stem}.wav").write_bytes(b"RIFF")
elif command == "analyze":
    (Path(option("--work")) / "analysis.json").write_text("{}")
elif command in ("melodic", "match", "fit-808"):
    name = {"melodic": "melodic.json", "match": "matches.json", "fit-808": "synth808.json"}[command]
    (Path(rest[0]) / name).write_text(json.dumps({"argv": rest}))
elif command == "plan":
    work = Path(rest[0])
    (work / "plan.json").write_text((fixtures / "short-beat.plan.json").read_text())
    (work / "midi").mkdir(exist_ok=True)
    (work / "midi" / "drums.mid").write_bytes(b"MThd")
elif command == "blueprint":
    if mode == "old":
        print("usage: refdissect [-h] {inventory,...} ...", file=sys.stderr)
        print("refdissect: error: argument command: invalid choice: 'blueprint' (choose from "
              "'inventory', 'plan')", file=sys.stderr)
        sys.exit(2)
    data = json.loads((fixtures / "short-beat.json").read_text())
    for entry in data["files"].values():
        entry["source"] = str(fixtures / entry["source"])
    (Path(rest[0]) / "blueprint.json").write_text(json.dumps(data))
elif command == "inventory":
    out = Path(option("--out"))
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"path": f"/library/{n}.wav", "role": r, "duration": 0.3, "source": "Samples"}
            for n, r in (("kick", "kick"), ("kick2", "kick"), ("hat", "hat"), ("sub", "808"))]
    out.write_text(json.dumps(rows))
elif command == "index-library":
    Path(option("--out")).write_bytes(b"x" * 2_000_000)
else:
    sys.exit(f"unknown command {command}")
'''


@pytest.fixture
def fake_refdissect(tmp_path, monkeypatch):
    script = tmp_path / "fake_refdissect.py"
    script.write_text(FAKE_REFDISSECT)
    monkeypatch.setattr(jobs, "refdissect_command", lambda: [sys.executable, str(script)])
    monkeypatch.setenv("FAKE_FIXTURES", str(FIXTURES))
    monkeypatch.setenv("ABLETON_MCP_JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("ABLETON_MCP_REFERENCE_DIR", str(tmp_path / "Reference Dissector"))
    monkeypatch.setenv("ABLETON_MCP_PROFILE", "full")
    mcp = MCPServer("t")
    extended_tools.register(mcp, extended_tools._get_connection)
    song = tmp_path / "My Song.wav"
    sf.write(song, np.zeros(44100 * 2), 44100)

    def call(tool, **arguments):
        result = asyncio.run(mcp.call_tool(tool, arguments))
        if result.is_error:
            raise ToolError(result.content[0].text)
        return json.loads(result.content[0].text)

    call.song, call.tmp = song, tmp_path
    yield call
    for job_id, process in list(jobs._PROCESSES.items()):  # nothing outlives a test
        if process.poll() is None:
            jobs._signal_group(process.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            process.kill()
            process.wait()
        jobs._PROCESSES.pop(job_id, None)


def library(tmp_path):
    folder = tmp_path / "Reference Dissector" / "library"
    folder.mkdir(parents=True)
    (folder / "library.npz").write_bytes(b"npz")
    return folder


def finish(call, job_id, limit=30.0):
    deadline = time.monotonic() + limit
    while True:
        status = call("reference_job", job_id=job_id, wait=5)
        if status["state"] not in jobs.ACTIVE or time.monotonic() > deadline:
            return status


def wait_for(predicate, limit=10.0):
    deadline = time.monotonic() + limit
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def test_a_dissection_runs_every_step_and_says_what_it_found(fake_refdissect):
    call = fake_refdissect
    lib = library(call.tmp)
    (lib / "library_808.npz").write_bytes(b"npz")
    started = call("dissect_reference", path=str(call.song))
    assert started["steps"] == list(jobs.DISSECT_STEPS)
    assert started["next"] == f'reference_job(job_id="{started["job"]}") until it is done'
    work = Path(started["work"])
    assert work == call.tmp / "Reference Dissector" / "My Song"
    status = finish(call, started["job"])
    assert status["state"] == "done", status
    assert status["progress"] == 1.0 and set(status["seconds"]) == set(jobs.DISSECT_STEPS)
    summary = status["summary"]
    assert summary["tempo"] == 120.0 and summary["meter"] == "4/4" and summary["bars"] == 2
    assert summary["sections"] == [{"name": "A (1)", "beat": 0.0}, {"name": "B (2)", "beat": 4.0}]
    assert summary["pads"]["42"] == {"role": "hat", "file": "hat-closed.wav", "verdict": "same sample",
                                     "ncc": 0.953, "transpose": 2}
    assert summary["drum_notes"] == 24 and summary["808_notes"] == 3
    assert summary["808"]["transpose"] == 36 and summary["808"]["glide"] is False
    assert summary["files"]["blueprint"] == str(work / "blueprint.json")
    assert summary["files"]["midi"] == [str(work / "midi" / "drums.mid")]
    assert summary["files"]["stems"] == str(work / "stems")
    assert summary["next"] == f'build_from_blueprint(blueprint="{started["job"]}") in an empty Live set'
    # The commands each step ran, from the work folder's job.log, and the record's copy
    record = json.loads((call.tmp / "jobs" / f"{started['job']}.json").read_text())
    argv = {s["name"]: s["argv"][2:] for s in record["steps"]}
    assert argv["separate"] == ["separate", str(call.song), "--out", str(work / "stems"), "--model", "htdemucs"]
    assert argv["analyze"] == ["analyze", str(work / "stems"), "--work", str(work)]
    assert argv["match"] == ["match", str(work), "--library", str(lib / "library.npz")]
    assert argv["fit-808"] == ["fit-808", str(work), "--library-808", str(lib / "library_808.npz")]
    assert json.loads((work / "job.json").read_text())["state"] == "done"
    assert "refdissect plan" in (work / "job.log").read_text()
    # The list of recent jobs
    listed = call("reference_job")
    assert listed["jobs"][0]["job"] == started["job"] and listed["jobs"][0]["state"] == "done"


def test_steps_run_part_of_the_chain_again(fake_refdissect):
    call = fake_refdissect
    library(call.tmp)
    first = call("dissect_reference", path=str(call.song), melodic=False, slides=True, model="htdemucs_ft")
    assert "melodic" not in first["steps"]
    assert finish(call, first["job"])["state"] == "done"
    work = Path(first["work"])
    record = json.loads((call.tmp / "jobs" / f"{first['job']}.json").read_text())
    analyze = next(s for s in record["steps"] if s["name"] == "analyze")["argv"]
    assert analyze[-4:] == [str(work / "stems-htdemucs_ft"), "--work", str(work), "--slides"]
    again = call("dissect_reference", path=str(call.song), steps=["plan", "match"], model="htdemucs_ft")
    assert again["steps"] == ["match", "plan", "blueprint"]  # a new plan makes the old blueprint stale
    status = finish(call, again["job"])
    assert status["state"] == "done" and set(status["seconds"]) == {"match", "plan", "blueprint"}


def test_what_a_dissection_needs_is_checked_before_it_starts(fake_refdissect):
    call = fake_refdissect
    with pytest.raises(ToolError, match="run index_sound_library first"):
        call("dissect_reference", path=str(call.song))
    library(call.tmp)
    with pytest.raises(ToolError, match=r"plan needs analysis.json in .*: run the steps before it too"):
        call("dissect_reference", path=str(call.song), steps=["plan"])
    with pytest.raises(ToolError, match="Unknown step tempo"):
        call("dissect_reference", path=str(call.song), steps=["tempo"])
    with pytest.raises(ToolError, match="No such audio file"):
        call("dissect_reference", path=str(call.tmp / "nothing.wav"))
    with pytest.raises(ToolError, match="Demucs model name"):
        call("dissect_reference", path=str(call.song), model="../x")
    assert not (call.tmp / "jobs").exists() or not list((call.tmp / "jobs").glob("*.json"))


def test_one_job_at_a_time_and_cancel_stops_everything(fake_refdissect, monkeypatch):
    call = fake_refdissect
    library(call.tmp)
    pidfile = call.tmp / "analyze.pid"
    monkeypatch.setenv("FAKE_MODE", "slow")
    monkeypatch.setenv("FAKE_PIDFILE", str(pidfile))
    started = call("dissect_reference", path=str(call.song))
    wait_for(pidfile.exists)
    step_pid = int(pidfile.read_text())
    running = call("reference_job", job_id=started["job"], wait=0)
    assert running["state"] == "running" and running["step"] == "analyze"
    with pytest.raises(ToolError, match=f"Job {started['job']} .* is still running, and one job runs at a time"):
        call("index_sound_library", folders=[str(call.tmp)])
    stopped = call("reference_job", job_id=started["job"], cancel=True)
    assert stopped["state"] == "cancelled"
    wait_for(lambda: not jobs.pid_alive(step_pid))  # the step's own process went with the job
    assert call("reference_job", job_id=started["job"], wait=0)["state"] == "cancelled"
    monkeypatch.setenv("FAKE_MODE", "")
    assert finish(call, call("dissect_reference", path=str(call.song))["job"])["state"] == "done"


def test_a_failed_step_says_why(fake_refdissect, monkeypatch):
    call = fake_refdissect
    library(call.tmp)
    monkeypatch.setenv("FAKE_MODE", "fail")
    status = finish(call, call("dissect_reference", path=str(call.song))["job"])
    assert status["state"] == "failed" and status["step"] == "analyze"
    assert status["error"].startswith("analyze stopped with exit code 1: ")
    assert "ValueError: no beat found" in status["error"]
    assert "ValueError: no beat found" in status["log"]


def test_a_refdissect_without_blueprint_skips_it(fake_refdissect, monkeypatch):
    call = fake_refdissect
    library(call.tmp)
    monkeypatch.setenv("FAKE_MODE", "old")
    started = call("dissect_reference", path=str(call.song))
    status = finish(call, started["job"])
    assert status["state"] == "done"
    assert status["skipped"] == {"blueprint": "this refdissect has no blueprint command"}
    assert status["summary"]["files"]["blueprint"] is None
    assert "cannot write one yet" in status["summary"]["next"]
    with pytest.raises(ToolError, match="wrote no blueprint.json"):
        call("build_from_blueprint", blueprint=started["job"])


def test_a_job_whose_process_is_gone_is_lost(fake_refdissect):
    call = fake_refdissect
    folder = call.tmp / "jobs"
    folder.mkdir()
    process = __import__("subprocess").Popen([sys.executable, "-c", "pass"])
    process.wait()
    now = time.time()
    record = {"id": "dissect-20260101-000000", "kind": "dissect", "name": "Gone", "work": str(call.tmp),
              "steps": [{"name": "analyze", "argv": [], "weight": 0.5}], "state": "running",
              "step": "analyze", "pid": process.pid, "progress": 0.3, "created": now, "updated": now,
              "log": ["refdissect analyze", "reading the 808"]}
    (folder / f"{record['id']}.json").write_text(json.dumps(record))
    status = call("reference_job", job_id=record["id"])
    assert status["state"] == "lost" and "start it again" in status["error"]
    assert status["log"] == ["refdissect analyze", "reading the 808"]
    # a lost job does not block the next one
    assert jobs.running() is None
    stale = dict(record, id="dissect-20260101-000001", pid=os.getpid(), updated=now - jobs.STALE_S - 1)
    (folder / f"{stale['id']}.json").write_text(json.dumps(stale))
    assert call("reference_job", job_id=stale["id"], wait=0)["state"] == "lost"
    with pytest.raises(ToolError, match="No job 'nope'"):
        call("reference_job", job_id="nope")
    with pytest.raises(ToolError, match="Not a job id"):
        call("reference_job", job_id="../etc")


def test_the_sound_library_is_indexed_as_a_job(fake_refdissect):
    call = fake_refdissect
    samples = call.tmp / "Samples"
    samples.mkdir()
    started = call("index_sound_library", folders=[str(samples)])
    status = finish(call, started["job"])
    assert status["state"] == "done", status
    assert status["summary"]["sounds"] == 4 and status["summary"]["roles"] == {"kick": 2, "hat": 1, "808": 1}
    assert status["summary"]["index"] == {"library.npz": "2 MB", "library_808.npz": "2 MB"}
    record = json.loads((call.tmp / "jobs" / f"{started['job']}.json").read_text())
    lib = call.tmp / "Reference Dissector" / "library"
    assert record["steps"][0]["argv"][2:] == ["inventory", str(samples), "--out", str(lib / "inventory.json")]
    assert record["steps"][2]["argv"][2:] == ["index-library", str(lib / "inventory.json"), "--808",
                                              "--out", str(lib / "library_808.npz")]
    with pytest.raises(ToolError, match="No such folder"):
        call("index_sound_library", folders=[str(call.tmp / "none")])


def test_a_finished_job_builds_by_its_id(fake_refdissect, monkeypatch, tmp_path):
    from test_blueprint import EMPTY_SET

    from MCP_Server import als, journal
    from MCP_Server import server as live_server
    from MCP_Server.simulator import Simulator, free_port

    call = fake_refdissect
    library(call.tmp)
    job = call("dissect_reference", path=str(call.song))["job"]
    assert finish(call, job)["state"] == "done"
    session = tmp_path / "live-session.json"
    monkeypatch.setenv("ABLETON_MCP_SESSION_FILE", str(session))
    monkeypatch.delenv("ABLETON_PORT", raising=False)
    monkeypatch.setattr(als, "_user_libraries", lambda: [tmp_path / "User Library"])
    monkeypatch.setattr(extended_tools, "_get_connection", live_server.get_ableton_connection)
    simulator = Simulator(free_port(), session)
    simulator.run_setup(EMPTY_SET)
    simulator.start()
    journal.clear()
    try:
        report = call("build_from_blueprint", blueprint=job)
    finally:
        if live_server._ableton_connection is not None:
            live_server._ableton_connection.disconnect()
            live_server._ableton_connection = None
        simulator.stop()
    assert report["ok"] is True and report["blueprint"] == "Short beat"


# ── The parts ────────────────────────────────────────────────────────────────

def test_refdissect_runs_pinned_or_from_a_checkout(tmp_path, monkeypatch):
    uv = tmp_path / "uv"
    uv.write_text("")
    monkeypatch.setenv("UV", str(uv))
    monkeypatch.delenv("ABLETON_MCP_REFDISSECT", raising=False)
    assert len(jobs.REFDISSECT_COMMIT) == 40 and int(jobs.REFDISSECT_COMMIT, 16) >= 0
    assert jobs.refdissect_command() == [
        str(uv), "run", "--isolated", "--no-project", "--python", "3.12", "--with",
        "reference-dissector @ https://github.com/CanArslanDev/reference-dissector/archive/"
        f"{jobs.REFDISSECT_COMMIT}.zip", "refdissect"]
    monkeypatch.setenv("ABLETON_MCP_REFDISSECT", str(tmp_path))
    with pytest.raises(ValueError, match="holds no pyproject.toml"):
        jobs.refdissect_command()
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'reference-dissector'\n")
    assert jobs.refdissect_command() == [str(uv), "run", "--project", str(tmp_path), "refdissect"]
    assert jobs.refdissect_source()["describe"] == f"local checkout {tmp_path}"


def test_steps_and_progress():
    assert jobs.resolve_steps(None) == list(jobs.DISSECT_STEPS)
    assert jobs.resolve_steps("plan, fit_808,match", melodic=False) == ["match", "fit-808", "plan", "blueprint"]
    assert jobs.resolve_steps(["match"]) == ["match"]
    assert jobs.resolve_steps(["melodic", "plan"], melodic=False) == ["plan", "blueprint"]
    assert abs(sum(jobs.WEIGHTS.values()) - 1.0) < 1e-9
    record = {"steps": [{"name": n, "weight": jobs.WEIGHTS[n], "expected_s": jobs.TYPICAL_S[n]}
                        for n in jobs.DISSECT_STEPS]}
    estimates = [reference_runner.estimate(record, 1, t) for t in (0, 30, 158, 10_000)]
    assert estimates == sorted(estimates) and estimates[0] == 0.05
    assert estimates[-1] == round(0.05 + 0.5 * 0.95, 4)  # never a step's whole share before it ends
    assert reference_runner.estimate(record, 2, 0) == 0.55
    assert reference_runner.estimate({"steps": [{"name": "blueprint", "weight": 0.0}]}, 1, 0) == 1.0
    assert jobs.folder_name('a/b: "c"?') == "a_b_ _c_" and jobs.folder_name("Library") == "Library (song)"


def test_doctor_names_the_refdissect_it_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("ABLETON_MCP_REFERENCE_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ABLETON_MCP_JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.delenv("ABLETON_MCP_REFDISSECT", raising=False)
    check = doctor.check_reference_dissector()
    assert check["check"] == "reference-dissector" and check["status"] == "info"
    assert f"reference-dissector {jobs.REFDISSECT_COMMIT[:7]} (pinned)" in check["detail"]
    assert "no sound library index yet" in check["detail"]
    folder = tmp_path / "data" / "library"
    folder.mkdir(parents=True)
    (folder / "library.npz").write_bytes(b"x" * 3_000_000)
    assert "library index 3 MB" in doctor.check_reference_dissector()["detail"]
    monkeypatch.setenv("ABLETON_MCP_PROFILE", "core")
    assert "its tools are off: this server runs the core profile" in doctor.check_reference_dissector()["detail"]
    monkeypatch.setenv("ABLETON_MCP_PROFILE", "full")
    assert "its tools are off" not in doctor.check_reference_dissector()["detail"]
    monkeypatch.setenv("ABLETON_MCP_REFDISSECT", str(tmp_path / "nowhere"))
    check = doctor.check_reference_dissector()
    assert check["status"] == "warn" and "holds no pyproject.toml" in check["detail"]
    for name in ("check_installed_scripts", "check_bundle_copies", "check_session", "check_live_log",
                 "check_companion"):
        monkeypatch.setattr(doctor, name, lambda: doctor._check("x", "info", ""))
    monkeypatch.setattr(doctor, "check_connection", lambda host, port: ([], None))
    monkeypatch.setattr(doctor, "live_versions", lambda: [])
    assert doctor.run(port=1)["checks"][-1]["check"] == "reference-dissector"


def test_the_tools_are_in_the_full_profile_only(monkeypatch):
    from MCP_Server import tooling

    names = {"dissect_reference", "reference_job", "index_sound_library", "build_from_blueprint"}
    for profile, expected in (("core", set()), ("full", names)):
        monkeypatch.setenv("ABLETON_MCP_PROFILE", profile)
        mcp = MCPServer("t")
        extended_tools.register(mcp, extended_tools._get_connection)
        listed = {t.name for t in asyncio.run(mcp.list_tools())}
        assert listed & names == expected
    assert tooling.active_profile() == "full"


def test_the_prompt_chains_the_tools_in_the_full_profile(monkeypatch):
    import re

    from MCP_Server.prompts import register_prompts

    def prompts(profile):
        monkeypatch.setenv("ABLETON_MCP_PROFILE", profile)
        mcp = MCPServer("t")
        register_prompts(mcp)
        extended_tools.register(mcp, extended_tools._get_connection)
        return mcp, {p.name for p in asyncio.run(mcp.list_prompts())}

    assert "beat_like_reference" not in prompts("core")[1]
    mcp, names = prompts("full")
    assert "beat_like_reference" in names
    text = asyncio.run(mcp.get_prompt("beat_like_reference", {"reference_path": "/songs/a.mp3",
                                                               "bars": "9-16"})).messages[0].content.text
    tools = {t.name for t in asyncio.run(mcp.list_tools())}
    named = set(re.findall(r"\b([a-z]+(?:_[a-z]+)+)\(", text))
    assert {"dissect_reference", "reference_job", "build_from_blueprint", "index_sound_library"} <= named
    assert named <= tools
    assert 'build_from_blueprint(blueprint=<job id>, bars="9-16")' in text


def test_the_runner_keeps_the_record(tmp_path, monkeypatch):
    """reference_runner.py on its own: each step's seconds, progress at each
    step's start, the log's tail, and job.json beside it."""
    monkeypatch.setattr(reference_runner.signal, "signal", lambda *args: None)  # pytest keeps its SIGTERM
    work = tmp_path / "work"
    work.mkdir()
    record_file = tmp_path / "job.json"
    steps = [{"name": n, "weight": w, "expected_s": 1,
              "argv": [sys.executable, "-c", f"print('step {n} done')"]}
             for n, w in (("one", 0.25), ("two", 0.75))]
    record_file.write_text(json.dumps({"id": "t-1", "work": str(work), "steps": steps, "state": "starting"}))
    seen = []
    real_save = reference_runner.Runner.save

    def save(self):
        seen.append((self.record.get("step"), self.record.get("progress")))
        real_save(self)

    runner = reference_runner.Runner(record_file)
    runner.save = save.__get__(runner)
    assert runner.run() == 0
    record = json.loads(record_file.read_text())
    assert record["state"] == "done" and record["progress"] == 1.0 and set(record["seconds"]) == {"one", "two"}
    assert ("one", 0.0) in seen and ("two", 0.25) in seen
    assert record["log"][-1] == "step two done"
    assert json.loads((work / "job.json").read_text())["state"] == "done"


def test_songs_of_one_name_get_folders_of_their_own(fake_refdissect):
    call = fake_refdissect
    library(call.tmp)
    other = call.tmp / "elsewhere" / "My Song.wav"
    other.parent.mkdir()
    sf.write(other, np.zeros(44100), 44100)
    first = call("dissect_reference", path=str(call.song))
    assert finish(call, first["job"])["state"] == "done"
    second = call("dissect_reference", path=str(other))
    assert Path(second["work"]).name == "My Song (2)"
    assert finish(call, second["job"])["state"] == "done"
    again = call("dissect_reference", path=str(call.song), steps=["plan"])
    assert again["work"] == first["work"]
    assert finish(call, again["job"])["state"] == "done"
    assert json.loads((Path(second["work"]) / "source.json").read_text())["file"] == str(other.resolve())


@POSIX_ONLY
def test_a_runner_killed_alone_leaves_steps_that_cancel_stops(fake_refdissect, monkeypatch):
    call = fake_refdissect
    library(call.tmp)
    pidfile = call.tmp / "analyze.pid"
    monkeypatch.setenv("FAKE_MODE", "slow")
    monkeypatch.setenv("FAKE_PIDFILE", str(pidfile))
    started = call("dissect_reference", path=str(call.song))
    wait_for(pidfile.exists)
    step_pid = int(pidfile.read_text())
    runner = jobs._PROCESSES[started["job"]]
    os.kill(runner.pid, signal.SIGKILL)  # the runner alone (a crash, the OOM killer)
    runner.wait()
    lost = call("reference_job", job_id=started["job"], wait=0)
    assert lost["state"] == "lost" and "Processes it started still run" in lost["error"]
    assert jobs.pid_alive(step_pid)
    with pytest.raises(ToolError, match="was lost, but processes it started still run"):
        call("dissect_reference", path=str(call.song))
    assert call("reference_job", job_id=started["job"], cancel=True)["state"] == "cancelled"
    wait_for(lambda: not jobs.pid_alive(step_pid))
    assert jobs.running() is None


@POSIX_ONLY
def test_a_pid_another_process_took_is_not_the_job(fake_refdissect):
    import subprocess

    call = fake_refdissect
    stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                start_new_session=True)
    try:
        now = time.time()
        record = {"id": "dissect-20260101-000002", "kind": "dissect", "name": "Old", "work": str(call.tmp),
                  "steps": [], "state": "running", "step": "analyze", "pid": stranger.pid,
                  "created": now, "updated": now}
        (call.tmp / "jobs").mkdir(exist_ok=True)
        (call.tmp / "jobs" / f"{record['id']}.json").write_text(json.dumps(record))
        status = call("reference_job", job_id=record["id"], wait=0)
        assert status["state"] == "lost" and "still run" not in status["error"]
        assert call("reference_job", job_id=record["id"], cancel=True)["state"] == "cancelled"
        assert stranger.poll() is None  # the process that took the pid lives on
    finally:
        stranger.kill()
        stranger.wait()



@POSIX_ONLY
def test_a_zombie_leader_alone_is_no_leftover(tmp_path):
    import subprocess

    # An unreaped runner: its group answers kill (Linux) or refuses it (macOS)
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    try:
        wait_for(lambda: (jobs.process_line(process.pid) or "Z").startswith("Z"))
        assert not jobs.group_alive(process.pid)
        record = {"id": "dissect-20260101-000003", "pid": process.pid}
        assert not jobs.is_runner(record) and not jobs.leftovers(record)
    finally:
        process.wait()
    assert not jobs.group_alive(process.pid)


@POSIX_ONLY
def test_another_server_sees_a_running_job_and_cancels_it(fake_refdissect, monkeypatch):
    call = fake_refdissect
    library(call.tmp)
    pidfile = call.tmp / "analyze.pid"
    monkeypatch.setenv("FAKE_MODE", "slow")
    monkeypatch.setenv("FAKE_PIDFILE", str(pidfile))
    started = call("dissect_reference", path=str(call.song))
    wait_for(pidfile.exists)
    step_pid = int(pidfile.read_text())
    runner = jobs._PROCESSES.pop(started["job"])  # as a server that did not start it sees it: ps only
    try:
        assert call("reference_job", job_id=started["job"], wait=0)["state"] == "running"
        with pytest.raises(ToolError, match="is still running, and one job runs at a time"):
            call("dissect_reference", path=str(call.song))
        assert call("reference_job", job_id=started["job"], cancel=True)["state"] == "cancelled"
        wait_for(lambda: not jobs.pid_alive(step_pid))
        runner.wait(timeout=10)
    finally:
        if runner.poll() is None:
            jobs._PROCESSES[started["job"]] = runner  # the fixture stops it


@POSIX_ONLY
def test_a_runner_deaf_to_sigterm_is_killed(fake_refdissect, monkeypatch):
    import subprocess

    call = fake_refdissect
    job_id = "dissect-20260101-000004"
    ready = call.tmp / "deaf.ready"
    deaf = subprocess.Popen([sys.executable, "-c",
                             "import pathlib, signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                             "pathlib.Path(sys.argv[3]).touch(); time.sleep(60)",
                             jobs.RUNNER.name, f"{job_id}.json", str(ready)], start_new_session=True)
    try:
        now = time.time()
        record = {"id": job_id, "kind": "dissect", "name": "Deaf", "work": str(call.tmp), "steps": [],
                  "state": "running", "step": "analyze", "pid": deaf.pid, "created": now, "updated": now}
        (call.tmp / "jobs").mkdir(exist_ok=True)
        (call.tmp / "jobs" / f"{job_id}.json").write_text(json.dumps(record))
        wait_for(ready.exists)  # SIGTERM ignored from here on
        assert jobs.is_runner(record)
        monkeypatch.setattr(jobs, "CANCEL_WAIT_S", 0.5)
        assert call("reference_job", job_id=job_id, cancel=True)["state"] == "cancelled"
        assert deaf.wait(timeout=5) == -signal.SIGKILL
    finally:
        if deaf.poll() is None:
            deaf.kill()
            deaf.wait()


def test_a_job_without_a_pid_is_starting_for_a_minute(tmp_path, monkeypatch):
    monkeypatch.setenv("ABLETON_MCP_JOBS_DIR", str(tmp_path / "jobs"))
    (tmp_path / "jobs").mkdir()
    for job_id, age in (("dissect-20260101-000005", 5), ("dissect-20260101-000006", jobs.STARTING_GRACE_S + 5)):
        created = time.time() - age
        (tmp_path / "jobs" / f"{job_id}.json").write_text(json.dumps(
            {"id": job_id, "kind": "dissect", "work": str(tmp_path), "steps": [], "state": "starting",
             "created": created, "updated": created}))
    assert jobs.load("dissect-20260101-000005")["state"] == "starting"
    assert jobs.load("dissect-20260101-000006")["state"] == "lost"


def test_relative_paths_reach_refdissect_absolute(fake_refdissect, monkeypatch):
    call = fake_refdissect
    library(call.tmp)
    monkeypatch.chdir(call.tmp)
    started = call("dissect_reference", path="My Song.wav")
    record = json.loads((call.tmp / "jobs" / f"{started['job']}.json").read_text())
    assert record["steps"][0]["argv"][3] == str(call.song.resolve())
    assert finish(call, started["job"])["state"] == "done"
    (call.tmp / "Samples").mkdir()
    indexed = call("index_sound_library", folders=["Samples"])
    record = json.loads((call.tmp / "jobs" / f"{indexed['job']}.json").read_text())
    assert str((call.tmp / "Samples").resolve()) in record["steps"][0]["argv"]
    assert finish(call, indexed["job"])["state"] == "done"
    monkeypatch.setenv("ABLETON_MCP_REFERENCE_DIR", "relative data")
    assert jobs.data_dir() == (call.tmp / "relative data").resolve()


def test_the_default_sound_folders(tmp_path, monkeypatch):
    from MCP_Server import als

    user, live = tmp_path / "User Library", tmp_path / "Live" / "Resources"
    (live / "Core Library" / "Samples").mkdir(parents=True)
    user.mkdir()
    monkeypatch.setattr(als, "_user_libraries", lambda: [user, tmp_path / "gone"])
    monkeypatch.setattr(als, "_live_resource_dirs", lambda: [live])
    assert jobs.default_sound_folders() == [user, live / "Core Library" / "Samples"]


def test_only_an_optional_step_is_skipped_when_refdissect_lacks_it(fake_refdissect, monkeypatch):
    """"invalid choice" skips blueprint (optional); match, missing too, fails the job."""
    call = fake_refdissect
    library(call.tmp)
    monkeypatch.setenv("FAKE_MODE", "no-match")
    status = finish(call, call("dissect_reference", path=str(call.song))["job"])
    assert status["state"] == "failed" and status["step"] == "match"
    assert status["error"].startswith("match stopped with exit code 2") and "skipped" not in status


def test_the_runner_writes_a_heartbeat_while_a_step_runs(fake_refdissect, monkeypatch):
    """The record is written every couple of seconds while a step runs: a
    record silent for STALE_S reads as lost."""
    call = fake_refdissect
    library(call.tmp)
    pidfile = call.tmp / "analyze.pid"
    monkeypatch.setenv("FAKE_MODE", "slow")
    monkeypatch.setenv("FAKE_PIDFILE", str(pidfile))
    started = call("dissect_reference", path=str(call.song))
    wait_for(pidfile.exists)
    record = jobs.record_path(started["job"])
    first = json.loads(record.read_text())["updated"]
    wait_for(lambda: json.loads(record.read_text())["updated"] > first, limit=reference_runner.WRITE_S * 3)
    assert json.loads(record.read_text())["state"] == "running"
    call("reference_job", job_id=started["job"], cancel=True)


@POSIX_ONLY
def test_a_runner_alive_but_silent_is_lost_and_holds_the_machine(tmp_path, monkeypatch):
    """A record whose runner still runs but has not written for STALE_S (the
    computer slept through it, a disk that refuses the heartbeat) reads as
    lost, with leftovers: a new job waits until cancel stops it."""
    import subprocess

    monkeypatch.setenv("ABLETON_MCP_JOBS_DIR", str(tmp_path / "jobs"))
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        now = time.time()
        record = {"id": "dissect-silent", "kind": "dissect", "name": "Song", "work": str(tmp_path),
                  "steps": [], "state": "running", "step": "analyze", "pid": sleeper.pid,
                  "created": now - jobs.STALE_S - 60, "updated": now - jobs.STALE_S - 1}
        reference_runner.write_json(jobs.record_path(record["id"]), record)
        monkeypatch.setattr(jobs, "process_line", lambda pid: (
            f"S python -I {jobs.RUNNER} {jobs.record_path(record['id'])}" if pid == sleeper.pid else ""))
        loaded = jobs.load(record["id"])
        assert loaded["state"] == "lost" and loaded["leftover"] is True
        assert jobs.running()["id"] == record["id"]
        record["updated"] = time.time()  # it writes again: running, as before
        reference_runner.write_json(jobs.record_path(record["id"]), record)
        assert jobs.load(record["id"])["state"] == "running"
    finally:
        sleeper.kill()
        sleeper.wait()
