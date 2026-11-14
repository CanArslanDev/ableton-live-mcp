"""Runs one reference job's steps, detached from the MCP server.

reference_jobs.py writes a job record (~/.ableton-mcp/jobs/<id>.json) with
the command of every step and starts this file with the server's Python, in
a process group of its own, so the job outlives the server and a cancel can
stop it with every process it started (uv, refdissect, Demucs, beat_this,
Basic Pitch). Steps run in order; their output goes to the work folder's
job.log. After each step, and every couple of seconds while one runs, the
record is replaced (atomically) with the job's state, and a copy goes to the
work folder's job.json.

Only the standard library: this file is run by path (python -I), without
importing the server's package.
"""

from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

LOG_LINES = 20  # the log's last lines kept in the record
TICK_S = 0.5  # how often the runner looks at the step
WRITE_S = 2.0  # how often the record is written while a step runs (its heartbeat)
NO_SUCH_COMMAND = "invalid choice"  # argparse's words for a subcommand it does not have
# A step opens no console window on Windows (the runner itself has none)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def write_json(path: Path, data: dict) -> None:
    """Replace path with data at once: a reader never sees half a file.
    (Windows refuses the replace while a reader has the file open: tried
    again for a second.)"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    for attempt in range(20):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)


def log_tail(log: Path, lines: int = LOG_LINES) -> list[str]:
    """The log's last lines, without the progress bars' carriage-return frames."""
    try:
        with open(log, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 16384))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    rows = [row.rsplit("\r", 1)[-1].rstrip() for row in text.splitlines()]
    return [row[:300] for row in rows if row.strip()][-lines:]


def estimate(record: dict, index: int, elapsed: float) -> float:
    """The job's progress: the weights of the steps done, and of the step
    running as much as its typical time suggests (1 - e^(-t/T), at most 0.95
    of its weight): the number always rises, and only a finished step
    completes its share."""
    steps = record["steps"]
    weights = [float(s.get("weight") or 0.0) for s in steps]
    if not sum(weights):
        weights = [1.0] * len(steps)
    done = sum(weights[:index])
    if index < len(steps):
        expected = max(1.0, float(steps[index].get("expected_s") or 60.0))
        done += weights[index] * min(0.95, 1.0 - math.exp(-elapsed / expected))
    return round(min(1.0, done / sum(weights)), 4)


class Runner:
    def __init__(self, record_path: Path):
        self.path = record_path
        self.record = json.loads(record_path.read_text(encoding="utf-8"))
        self.work = Path(self.record["work"])
        self.log = self.work / "job.log"
        self.cancelled = False
        self.child: subprocess.Popen | None = None

    def save(self) -> None:
        self.record["updated"] = time.time()
        self.record["log"] = log_tail(self.log)
        write_json(self.path, self.record)
        try:
            write_json(self.work / "job.json", self.record)
        except OSError:  # the work folder is gone: the record still says what happened
            pass

    def stop_child(self) -> None:
        if self.child is None or self.child.poll() is not None:
            return
        self.child.terminate()
        try:
            self.child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.child.kill()
            self.child.wait()

    def stop(self, *_args) -> None:
        """SIGTERM: the server cancels (it signals the whole group)."""
        self.cancelled = True
        if self.child is not None and self.child.poll() is None:
            try:
                self.child.terminate()
            except OSError:
                pass

    def run_step(self, index: int, step: dict) -> int:
        started = time.monotonic()
        # the steps before it complete their shares now, not on the next heartbeat
        self.record.update(step=step["name"], step_index=index, progress=estimate(self.record, index, 0.0))
        self.save()
        with open(self.log, "ab") as log:
            log.write(f"\n== {step['name']}: {' '.join(step['argv'])}\n".encode())
            log.flush()
            self.child = subprocess.Popen(step["argv"], stdin=subprocess.DEVNULL, stdout=log,
                                          stderr=subprocess.STDOUT, cwd=str(self.work),
                                          creationflags=NO_WINDOW)
        written = time.monotonic()
        while True:
            try:
                self.child.wait(timeout=TICK_S)
                break
            except subprocess.TimeoutExpired:
                pass
            if time.monotonic() - written >= WRITE_S:
                self.record["progress"] = estimate(self.record, index, time.monotonic() - started)
                try:
                    self.save()
                except OSError:  # a heartbeat missed; the next one tries again
                    pass
                written = time.monotonic()
        self.record.setdefault("seconds", {})[step["name"]] = round(time.monotonic() - started, 1)
        return self.child.returncode

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self.stop)
        self.work.mkdir(parents=True, exist_ok=True)
        self.record.update(state="running", pid=os.getpid(), started=time.time(), progress=0.0,
                           seconds={}, skipped={}, error=None)
        self.save()
        state, error = "done", None
        try:
            for index, step in enumerate(self.record["steps"]):
                if self.cancelled:
                    break
                code = self.run_step(index, step)
                if self.cancelled:
                    break
                if code == 0:
                    continue
                tail = log_tail(self.log, 40)
                if step.get("optional") and any(NO_SUCH_COMMAND in row for row in tail[-5:]):
                    self.record["skipped"][step["name"]] = f"this refdissect has no {step['name']} command"
                    continue
                state = "failed"
                error = f"{step['name']} stopped with exit code {code}: " + " | ".join(tail[-3:])
                break
        except Exception as e:  # a step that could not start (no uv, a missing file)
            state, error = "failed", f"{self.record.get('step')}: {e}"
        finally:  # nothing the job started outlives it
            self.stop_child()
        if self.cancelled:
            state, error = "cancelled", None
        self.record.update(state=state, error=error, finished=time.time())
        if state == "done":
            self.record["progress"] = 1.0
        self.save()
        return 0 if state == "done" else 1


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: reference_runner.py <job record>", file=sys.stderr)
        return 2
    return Runner(Path(argv[1])).run()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
