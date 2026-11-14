"""Reference jobs: reference-dissector (refdissect) analysing a song, or
indexing a sample library, as jobs that run detached from the MCP server.

refdissect is not a dependency of this package. It runs as a command in a
uv environment of its own (Python 3.12, pinned to one commit of
github.com/CanArslanDev/reference-dissector), the way ml.py runs Demucs or
Basic Pitch: its libraries and its memory (2 GB for an analysis) never
enter the server, and it keeps its own pins (torch for Demucs).
ABLETON_MCP_REFDISSECT=<a local checkout> runs that checkout instead
(`uv run --project <checkout> refdissect`), for development.

A job is a record in ~/.ableton-mcp/jobs/<id>.json (ABLETON_MCP_JOBS_DIR)
that lists the refdissect command of every step. reference_runner.py runs
them in a process group of its own and keeps the record up to date (state,
step, seconds per step, progress, the log's last lines, error), with a copy
in the work folder's job.json; the server only starts, reads and cancels
jobs. A job outlives the server and the MCP session: any session can poll
it by id. A record that says running while its process is gone (or silent
for STALE_S) reads as "lost". One job runs at a time: two analyses at 2 GB
each and Demucs on the GPU would compete, so a second start is refused.

Data (ABLETON_MCP_REFERENCE_DIR, default ~/Music/Ableton MCP/Reference
Dissector/): the library index in library/, one work folder per song (its
file name), holding the stems, analysis.json, matches.json, plan.json,
blueprint.json, MIDI files, job.json and job.log.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .reference_runner import log_tail, write_json

REFDISSECT_COMMIT = "21d82d72ee89e21b82a7bff4d8d8805af283bac9"
REFDISSECT_URL = ("https://github.com/CanArslanDev/reference-dissector/archive/"
                  f"{REFDISSECT_COMMIT}.zip")
REFDISSECT_PYTHON = "3.12"  # refdissect needs 3.11+; the server runs on 3.10 too
RUNNER = Path(__file__).with_name("reference_runner.py")

DISSECT_STEPS = ("separate", "analyze", "melodic", "match", "fit-808", "plan", "blueprint")
# Each step's share of a job's progress, and its seconds on a 126 s song
# (PIRAMIT on an Apple M4, warm): separate ~15 s, analyze 158 s, match 58 s,
# fit-808 44 s (melodic and plan were not timed with them)
WEIGHTS = {"separate": 0.05, "analyze": 0.50, "melodic": 0.10, "match": 0.20, "fit-808": 0.14,
           "plan": 0.01, "blueprint": 0.0}
TYPICAL_S = {"separate": 15, "analyze": 158, "melodic": 30, "match": 58, "fit-808": 44, "plan": 3,
             "blueprint": 2}
TYPICAL_SONG_S = 126.0
# refdissect commands a job may meet missing in an older refdissect: skipped, not failed
OPTIONAL_STEPS = {"blueprint"}
INDEX_STEPS = ("inventory", "index", "index-808")
# index-library took 52 s for 8,853 sounds (reference-dissector docs/12); the others are guesses
INDEX_WEIGHTS = {"inventory": 0.10, "index": 0.75, "index-808": 0.15}
INDEX_TYPICAL_S = {"inventory": 20, "index": 52, "index-808": 15}
# What each step writes in the work folder, and what it needs there first
OUTPUTS = {"analyze": "analysis.json", "melodic": "melodic.json", "match": "matches.json",
           "fit-808": "synth808.json", "plan": "plan.json", "blueprint": "blueprint.json"}
NEEDS = {"melodic": ["analysis.json"], "match": ["analysis.json"], "fit-808": ["analysis.json"],
         "plan": ["analysis.json"], "blueprint": ["plan.json"]}
ACTIVE = ("starting", "running")
FINAL = ("done", "failed", "cancelled")
STARTING_GRACE_S = 60.0  # a record the runner has not taken over yet, still alive
STALE_S = 600.0  # a running record not written for this long: its runner is gone
CANCEL_WAIT_S = 5.0

# The runners this server started: polled, so a finished one is reaped (a
# zombie would still look alive)
_PROCESSES: dict[str, subprocess.Popen] = {}


def data_dir() -> Path:
    custom = os.environ.get("ABLETON_MCP_REFERENCE_DIR")
    if custom:
        return Path(custom).expanduser()
    return Path.home() / "Music" / "Ableton MCP" / "Reference Dissector"


def library_dir() -> Path:
    return data_dir() / "library"


def jobs_dir() -> Path:
    custom = os.environ.get("ABLETON_MCP_JOBS_DIR")
    return Path(custom).expanduser() if custom else Path.home() / ".ableton-mcp" / "jobs"


def refdissect_source() -> dict:
    """Which refdissect jobs run: the pinned commit, or the checkout
    ABLETON_MCP_REFDISSECT names (problem says what is wrong with it)."""
    local = os.environ.get("ABLETON_MCP_REFDISSECT")
    if local:
        path = Path(local).expanduser()
        problem = None if (path / "pyproject.toml").is_file() else (
            f"ABLETON_MCP_REFDISSECT={local} holds no pyproject.toml (a reference-dissector checkout)")
        return {"kind": "local", "path": str(path), "problem": problem,
                "describe": f"local checkout {path}"}
    return {"kind": "pinned", "commit": REFDISSECT_COMMIT, "url": REFDISSECT_URL, "problem": None,
            "describe": f"reference-dissector {REFDISSECT_COMMIT[:7]} (pinned)"}


def refdissect_command() -> list[str]:
    """The command line that runs refdissect, before its subcommand."""
    from .ml import uv_executable

    source = refdissect_source()
    if source["problem"]:
        raise ValueError(source["problem"])
    uv = uv_executable()
    if source["kind"] == "local":
        return [uv, "run", "--project", source["path"], "refdissect"]
    return [uv, "run", "--isolated", "--no-project", "--python", REFDISSECT_PYTHON,
            "--with", f"reference-dissector @ {REFDISSECT_URL}", "refdissect"]


# ── Planning a job ───────────────────────────────────────────────────────────

def folder_name(name: str) -> str:
    """A song's work folder name: its file name, safe on every system."""
    safe = re.sub(r'[\\/:*?"<>|]+', "_", name).strip(" .") or "song"
    return f"{safe} (song)" if safe.lower() == library_dir().name else safe


def work_folder(name: str, source: str | Path) -> Path:
    """A song's work folder: <data>/<its name>, or "<name> (2)", "(3)", …
    when that one holds the analysis of another file of the same name
    (source.json says which file a folder is for)."""
    want = str(Path(source).expanduser().resolve())
    base, n = folder_name(name), 1
    while True:
        work = data_dir() / (base if n == 1 else f"{base} ({n})")
        try:
            theirs = json.loads((work / "source.json").read_text(encoding="utf-8")).get("file")
        except (OSError, ValueError, AttributeError):
            theirs = None  # a new folder, or one made before source.json: this song's
        if theirs in (None, want):
            return work
        n += 1


def claim_folder(work: Path, source: str | Path) -> None:
    work.mkdir(parents=True, exist_ok=True)
    write_json(work / "source.json", {"file": str(Path(source).expanduser().resolve())})


def stems_folder(work: Path, model: str) -> Path:
    return work / ("stems" if model == "htdemucs" else f"stems-{model}")


def resolve_steps(steps: list[str] | str | None, melodic: bool = True) -> list[str]:
    """The steps to run, in the chain's order (melodic=False leaves melodic out)."""
    if steps is None:
        wanted = set(DISSECT_STEPS)
    else:
        names = steps.split(",") if isinstance(steps, str) else list(steps)
        wanted = {str(n).strip().lower().replace("_", "-") for n in names if str(n).strip()}
        wanted = {"fit-808" if n == "fit808" else n for n in wanted}
        unknown = sorted(wanted - set(DISSECT_STEPS))
        if unknown:
            raise ValueError(f"Unknown step {', '.join(unknown)}: the steps are "
                             f"{', '.join(DISSECT_STEPS)}")
    chosen = [s for s in DISSECT_STEPS if s in wanted and (melodic or s != "melodic")]
    if not chosen:
        raise ValueError("No step to run")
    return chosen


def audio_seconds(path: str | Path) -> float | None:
    try:
        import soundfile as sf
        info = sf.info(str(path))
        return info.frames / info.samplerate
    except Exception:
        return None


def dissect_steps(source: str | Path | None, work: Path, chosen: list[str], model: str = "htdemucs",
                  slides: bool = False) -> list[dict]:
    """Each step as the refdissect command that runs it, checked against the
    files it needs (in the work folder, or made by an earlier chosen step)."""
    library = library_dir()
    stems = stems_folder(work, model)
    have = {name for name in OUTPUTS.values() if (work / name).is_file()}
    if (stems / "drums.wav").is_file() and (stems / "bass.wav").is_file():
        have.add("stems")
    problems = []
    for step in chosen:
        if step == "separate" and not (source and Path(source).is_file()):
            problems.append(f"separate needs the song's audio file ({source or 'none given'})")
        if step == "analyze" and "stems" not in have:
            problems.append(f"analyze needs {model}'s stems in {stems}: run separate too")
        if step == "match" and not (library / "library.npz").is_file():
            problems.append(f"match needs a sound library index ({library / 'library.npz'}): "
                            "run index_sound_library first (once; it takes minutes)")
        missing = [n for n in NEEDS.get(step, []) if n not in have]
        if missing:
            problems.append(f"{step} needs {', '.join(missing)} in {work}: run the steps "
                            "before it too")
        have.add("stems" if step == "separate" else OUTPUTS.get(step, ""))
    if problems:
        raise ValueError("; ".join(problems))
    seconds = audio_seconds(source) if source else None
    scale = min(10.0, max(0.1, seconds / TYPICAL_SONG_S)) if seconds else 1.0
    run = refdissect_command()
    args = {"separate": ["separate", str(source), "--out", str(stems), "--model", model],
            "analyze": ["analyze", str(stems), "--work", str(work)] + (["--slides"] if slides else []),
            "melodic": ["melodic", str(work)],
            "match": ["match", str(work), "--library", str(library / "library.npz")],
            "fit-808": ["fit-808", str(work)] + (
                ["--library-808", str(library / "library_808.npz")]
                if (library / "library_808.npz").is_file() else []),
            "plan": ["plan", str(work)],
            "blueprint": ["blueprint", str(work)]}
    return [{"name": step, "argv": run + args[step], "weight": WEIGHTS[step],
             "expected_s": round(TYPICAL_S[step] * scale, 1),
             **({"optional": True} if step in OPTIONAL_STEPS else {})} for step in chosen]


def index_steps(folders: list[Path]) -> list[dict]:
    library = library_dir()
    inventory = str(library / "inventory.json")
    run = refdissect_command()
    args = {"inventory": ["inventory", *map(str, folders), "--out", inventory],
            "index": ["index-library", inventory, "--out", str(library / "library.npz")],
            "index-808": ["index-library", inventory, "--808", "--out",
                          str(library / "library_808.npz")]}
    return [{"name": step, "argv": run + args[step], "weight": INDEX_WEIGHTS[step],
             "expected_s": INDEX_TYPICAL_S[step]} for step in INDEX_STEPS]


def default_sound_folders() -> list[Path]:
    """The User Library and Live's Core Library samples, as far as they exist."""
    from . import als

    folders = list(als._user_libraries())
    folders += [r / "Core Library" / "Samples" for r in als._live_resource_dirs()]
    return [f for f in folders if f.is_dir()]


# ── Records ──────────────────────────────────────────────────────────────────

def record_path(job_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", job_id or ""):
        raise ValueError(f"Not a job id: {job_id!r}")
    return jobs_dir() / f"{job_id}.json"


def pid_alive(pid: int) -> bool:
    """Whether a process has this pid (a zombie too)."""
    if os.name == "nt":  # os.kill(pid, 0) would end the process there
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x00100000 | 0x1000, False, int(pid))
        if not handle:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_line(pid: int) -> str | None:
    """`ps`'s state and command line of a pid ("Z <defunct>" for a zombie),
    "" for no such process, None where ps cannot tell (Windows)."""
    if os.name == "nt":
        return None
    try:
        found = subprocess.run(["ps", "-o", "stat=,command=", "-p", str(int(pid))],
                               capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return found.stdout.strip()


def is_runner(record: dict) -> bool:
    """Whether the record's pid is still its runner, and not a process that
    took the pid after the runner was gone, or the runner's zombie."""
    pid = record.get("pid")
    if not pid:
        return False
    line = process_line(pid)
    if line is None:
        return pid_alive(pid)
    return (bool(line) and not line.startswith("Z") and RUNNER.name in line
            and f"{record['id']}.json" in line)


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(int(pgid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def leftovers(record: dict) -> bool:
    """Whether processes of a job whose runner is gone or silent still run:
    its process group (the runner's pid, as it leads a session of its own)
    outlives the runner while a step runs. A pid another process took
    cannot be the job's group: Unix gives no process the id of a living
    process group."""
    pid = record.get("pid")
    if not pid or os.name == "nt":
        return False
    if is_runner(record):
        return True
    line = process_line(pid)
    if line and not line.startswith("Z"):
        return False
    return group_alive(pid)


def alive(record: dict) -> bool:
    process = _PROCESSES.get(record["id"])
    if process is not None:
        return process.poll() is None
    if not record.get("pid"):
        return time.time() - float(record.get("created") or 0) < STARTING_GRACE_S
    return is_runner(record)


def load(job_id: str) -> dict:
    """A job's record, its state "lost" when it says running but nothing runs
    it (leftover: the processes it started still run)."""
    path = record_path(job_id)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        known = ", ".join(r["id"] for r in recent(5)) or "none"
        raise LookupError(f"No job {job_id!r} (recent: {known})") from None
    if record.get("state") in ACTIVE:
        silent = time.time() - float(record.get("updated") or record.get("created") or 0)
        if not alive(record) or silent > STALE_S:
            record["state"] = "lost"
            record["error"] = ("Its runner is gone (the computer slept through it, restarted, or "
                               "the job was killed); start it again (steps= picks up from the "
                               "files already written)")
            if leftovers(record):
                record["leftover"] = True
                record["error"] += ". Processes it started still run: cancel=True stops them"
    return record


def recent(limit: int = 10) -> list[dict]:
    folder = jobs_dir()
    if not folder.is_dir():
        return []
    records = []
    for path in sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            records.append(load(path.stem))
        except (LookupError, ValueError, OSError):
            continue
    return records


def running() -> dict | None:
    """The job that holds the machine: running, or lost with processes left."""
    return next((r for r in recent(50)
                 if r.get("state") in ACTIVE or (r.get("state") == "lost" and r.get("leftover"))), None)


@contextlib.contextmanager
def start_lock():
    """One server at a time checks for a running job and claims an id."""
    folder = jobs_dir()
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / ".lock", "a+") as lock:
        try:
            import fcntl
        except ImportError:  # Windows: no lock (two servers starting in the same instant)
            yield
            return
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def new_id(kind: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    job_id, n = f"{kind}-{stamp}", 2
    while record_path(job_id).exists():
        job_id, n = f"{kind}-{stamp}-{n}", n + 1
    return job_id


def spawn(record: dict) -> subprocess.Popen:
    """reference_runner.py on the record, detached: its own process group
    (session), no stdin, output to the work folder's job.log."""
    from .ml import child_environment

    work = Path(record["work"])
    options: dict[str, Any] = {}
    if os.name == "nt":
        options["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                    | getattr(subprocess, "DETACHED_PROCESS", 0x8))
    else:
        options["start_new_session"] = True
    with open(work / "job.log", "ab") as log:
        return subprocess.Popen([sys.executable, "-I", str(RUNNER), str(record_path(record["id"]))],
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                cwd=str(work), env=child_environment(), close_fds=True, **options)


def start(kind: str, name: str, work: Path, steps: list[dict], options: dict | None = None) -> dict:
    """Write the job's record and start its runner; refused while another runs."""
    with start_lock():
        busy = running()
        if busy is not None and busy.get("state") == "lost":
            raise RuntimeError(
                f"Job {busy['id']} ({busy['kind']} of {busy.get('name')}) was lost, but processes it "
                f"started still run. Stop them with reference_job(job_id=\"{busy['id']}\", cancel=True).")
        if busy is not None:
            raise RuntimeError(
                f"Job {busy['id']} ({busy['kind']} of {busy.get('name')}, step "
                f"{busy.get('step') or 'starting'}) is still running, and one job runs at a time. "
                f"Wait for it with reference_job(job_id=\"{busy['id']}\"), or stop it with cancel=True.")
        work.mkdir(parents=True, exist_ok=True)
        now = time.time()
        record = {"id": new_id(kind), "kind": kind, "name": name, "work": str(work),
                  "refdissect": refdissect_source()["describe"], "options": options or {},
                  "steps": steps, "state": "starting", "step": None, "progress": 0.0,
                  "created": now, "updated": now}
        write_json(record_path(record["id"]), record)
        with open(work / "job.log", "ab") as log:
            log.write(f"\n=== job {record['id']} ({time.strftime('%Y-%m-%d %H:%M:%S')})\n".encode())
        _PROCESSES[record["id"]] = spawn(record)
    return record


def _signal_group(pid: int, sig: int) -> None:
    """Signal a job's process group: the runner's pid, which leads it (and
    goes on naming it after the runner is gone, while its steps run)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
        return
    try:
        os.killpg(int(pid), sig)
    except (ProcessLookupError, PermissionError):
        pass


def cancel(job_id: str) -> dict:
    """Stop a job with every process it started; its record says cancelled.
    A lost job's leftover processes are stopped too."""
    record = load(job_id)
    if record["state"] in FINAL:
        return record
    process = _PROCESSES.get(job_id)
    pid = record.get("pid") or (process.pid if process is not None else None)
    if record["state"] in ACTIVE and not pid:
        raise RuntimeError(f"Job {job_id} is still starting; cancel it again in a moment")
    if os.name == "nt" and process is None and record["state"] in ACTIVE:
        raise RuntimeError(f"Job {job_id} was started by another server; on Windows only that one "
                           "can stop it (or end its python process in the Task Manager)")
    if record["state"] in ACTIVE or record.get("leftover"):
        _signal_group(int(pid), signal.SIGTERM)
        deadline = time.monotonic() + CANCEL_WAIT_S
        while time.monotonic() < deadline and (alive(record) or leftovers(record)):
            try:  # the runner says cancelled as it ends
                if json.loads(record_path(job_id).read_text(encoding="utf-8")).get("state") in FINAL:
                    break
            except (OSError, ValueError):
                pass
            time.sleep(0.1)
        if alive(record) or leftovers(record):
            _signal_group(int(pid), getattr(signal, "SIGKILL", signal.SIGTERM))
            time.sleep(0.2)
    if process is not None:
        process.poll()
    record = json.loads(record_path(job_id).read_text(encoding="utf-8"))
    if record.get("state") not in FINAL:  # killed before it could say so, or lost
        record.update(state="cancelled", finished=time.time(), updated=time.time(), error=None)
        record["log"] = log_tail(Path(record["work"]) / "job.log")
        write_json(record_path(job_id), record)
    return record


# ── What a job says ──────────────────────────────────────────────────────────

def elapsed(record: dict) -> float | None:
    begun = record.get("started") or record.get("created")
    if not begun:
        return None
    end = record.get("finished") if record.get("state") in FINAL else None
    return round((end or time.time()) - float(begun), 1)


def status(record: dict, log_lines: int = 3) -> dict:
    """A job's state in a few lines: done jobs carry their summary."""
    out: dict[str, Any] = {"job": record["id"], "kind": record.get("kind"), "name": record.get("name"),
                           "state": record.get("state"),
                           "progress": round(float(record.get("progress") or 0.0), 3),
                           "step": record.get("step"),
                           "steps": [s["name"] for s in record.get("steps", [])],
                           "seconds": record.get("seconds") or {}, "elapsed_s": elapsed(record),
                           "work": record.get("work")}
    if record.get("skipped"):
        out["skipped"] = record["skipped"]
    state = record.get("state")
    if state in ("failed", "lost"):
        out["error"] = record.get("error")
        out["log"] = (record.get("log") or [])[-8:]
    elif state in ACTIVE:
        out["log"] = (record.get("log") or [])[-log_lines:]
    elif state == "done":
        out["progress"] = 1.0
        out["summary"] = summary(record)
    return out


def brief(record: dict) -> dict:
    """One row of the job list."""
    return {"job": record["id"], "kind": record.get("kind"), "name": record.get("name"),
            "state": record.get("state"), "step": record.get("step"),
            "progress": round(float(record.get("progress") or 0.0), 3),
            "started": time.strftime("%Y-%m-%d %H:%M",
                                     time.localtime(float(record.get("created") or 0)))}


def summary(record: dict) -> dict:
    work = Path(record["work"])
    try:
        if record.get("kind") == "index":
            return index_summary(work)
        return plan_summary(work, record)
    except Exception as e:  # a refdissect whose files read differently
        return {"work": str(work), "unreadable": f"{type(e).__name__}: {e}"}


def index_summary(library: Path) -> dict:
    rows = json.loads((library / "inventory.json").read_text(encoding="utf-8"))
    roles: dict[str, int] = {}
    for row in rows:
        roles[row.get("role") or "?"] = roles.get(row.get("role") or "?", 0) + 1
    files = {}
    for name in ("library.npz", "library_808.npz", "library_speckle.npz"):
        if (library / name).is_file():
            files[name] = f"{(library / name).stat().st_size / 1e6:.0f} MB"
    return {"sounds": len(rows), "roles": dict(sorted(roles.items(), key=lambda kv: -kv[1])),
            "index": files, "folder": str(library)}


def plan_summary(work: Path, record: dict | None = None) -> dict:
    """What a finished analysis found, from its plan.json (refdissect's plan:
    tempo, meter, locators, pads with their matches, the 808 and the notes)."""
    plan = json.loads((work / "plan.json").read_text(encoding="utf-8"))
    beats_per_bar, unit = int(plan.get("beats_per_bar", 4)), int(plan.get("beat_unit", 4))
    bar = beats_per_bar * 4 / unit
    drums, t808 = plan["tracks"]["drums"], plan["tracks"]["808"]
    pads = {}
    for note, pad in sorted(drums.get("pads", {}).items(), key=lambda kv: int(kv[0])):
        row = {"role": pad.get("role"), "file": Path(str(pad.get("path", ""))).name,
               "verdict": pad.get("verdict")}
        if pad.get("ncc") is not None:
            row["ncc"] = round(float(pad["ncc"]), 3)
        if pad.get("transpose"):
            row["transpose"] = pad["transpose"]
        if pad.get("choke_group"):
            row["choke_group"] = pad["choke_group"]
        pads[str(note)] = row
    out: dict[str, Any] = {
        "tempo": round(float(plan["tempo"]), 2), "meter": f"{beats_per_bar}/{unit}",
        "bars": round(float(plan.get("length_beats", 0)) / bar, 2),
        "sections": [{"name": name, "beat": beat} for name, beat in plan.get("locators", [])],
        "pads": pads, "drum_notes": len(drums.get("notes") or []),
        "808_notes": len(t808.get("notes") or [])}
    if t808.get("sample_path"):
        out["808"] = {"one_shot": Path(str(t808["sample_path"])).name,
                      "transpose": t808.get("transpose"), "detune_cents": t808.get("detune_cents"),
                      "tuning_cents": t808.get("tuning_cents"), "glide": t808.get("voices") == 1}
    melodic = {stem: len(t.get("notes") or []) for stem, t in plan["tracks"].get("melodic", {}).items()}
    if melodic:
        out["melodic_notes"] = melodic
    files: dict[str, Any] = {"plan": str(work / "plan.json")}
    blueprint = work / "blueprint.json"
    files["blueprint"] = str(blueprint) if blueprint.is_file() else None
    midi = sorted(str(p) for p in (work / "midi").glob("*.mid"))
    if midi:
        files["midi"] = midi
    model = (record or {}).get("options", {}).get("model", "htdemucs")
    if stems_folder(work, model).is_dir():
        files["stems"] = str(stems_folder(work, model))
    out["files"] = files
    if blueprint.is_file() and record is not None:
        out["next"] = f'build_from_blueprint(blueprint="{record["id"]}") in an empty Live set'
    elif not blueprint.is_file():
        out["next"] = ("No blueprint.json: this refdissect cannot write one yet (it needs its "
                       "`blueprint` command), so build_from_blueprint has nothing to build")
    return out
