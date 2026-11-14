"""This case's home: a reference job that finished, as dissect_reference
leaves one (its record, and a work folder with plan.json, blueprint.json and
the samples the blueprint names), and an empty User Library to copy into.
The blueprint is tests/fixtures/blueprints/short-beat.json.

    python prepare.py <home>       (scaffold.sh; tests/test_evals.py calls prepare())
"""

import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "blueprints"
JOB_ID = "dissect-20261101-120000"


def prepare(home: Path) -> str:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from MCP_Server import reference_jobs as jobs
    from MCP_Server.reference_runner import write_json

    (home / "Music" / "Ableton" / "User Library").mkdir(parents=True, exist_ok=True)
    work = jobs.data_dir() / "Short beat"
    samples = work / "samples"
    samples.mkdir(parents=True, exist_ok=True)
    blueprint = json.loads((FIXTURES / "short-beat.json").read_text(encoding="utf-8"))
    for entry in blueprint["files"].values():
        source = FIXTURES / entry["source"]
        shutil.copyfile(source, samples / source.name)
        entry["source"] = str(samples / source.name)
    write_json(work / "blueprint.json", blueprint)
    shutil.copyfile(FIXTURES / "short-beat.plan.json", work / "plan.json")
    now = time.time()
    seconds = {"separate": 4.1, "analyze": 41.0, "melodic": 7.9, "match": 15.2, "fit-808": 11.6,
               "plan": 0.8, "blueprint": 0.3}
    write_json(jobs.record_path(JOB_ID), {
        "id": JOB_ID, "kind": "dissect", "name": "Short beat", "work": str(work),
        "refdissect": jobs.refdissect_source()["describe"],
        "options": {"source": str(work / "Short beat.wav"), "model": "htdemucs"},
        "steps": [{"name": n, "argv": [], "weight": jobs.WEIGHTS[n]} for n in jobs.DISSECT_STEPS],
        "state": "done", "step": "blueprint", "step_index": len(jobs.DISSECT_STEPS) - 1,
        "progress": 1.0, "seconds": seconds, "skipped": {}, "error": None, "pid": None,
        "created": now - 90, "started": now - 90, "updated": now - 5, "finished": now - 5,
        "log": ["plan: 24 drum notes on 3 pads, 3 808 notes", "blueprint.json written"]})
    return JOB_ID


if __name__ == "__main__":
    prepare(Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home())
