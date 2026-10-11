# Eval suite

30 tasks a producer would ask for, run by `claude plugin eval` against the
plugin's real MCP server and the **simulator** (`ableton-mcp-simulator`: the
real Remote Script over an in-memory Live set, no Ableton needed). Each run
gets a fresh simulated set; when the agent is done, deterministic checks read
what is in the set, not what the agent says about it.

| Area | Cases |
|---|---|
| Reading | set-overview |
| Session | tempo-and-meter, key-and-scale, scenes |
| Tracks | create-tracks, rename-and-color, delete-track |
| Mixing and devices | mixer-balance, mute-and-solo, compressor-settings, bass-chain |
| Composing | chord-progression, drum-beat, euclidean-hats, melody-in-key, bass-follows-chords |
| Turkish music | makam-hicaz, usul-aksak |
| Editing | transpose-clip, quantize-clip, filter-sweep |
| Arrangement and transitions | locators, arrangement-layout, drum-fill, build-up, fade-out |
| Browser | drum-kit |
| Safety | preview-no-write, undo-tempo, missing-track |

## Running

```bash
uv sync                                   # the scaffold uses .venv/bin/python
claude plugin eval . --scaffold --allow-real-servers \
  --allow-tools "mcp__plugin_ableton-live_ableton__*" \
  --ablation none --no-publish -j 4
```

- `--scaffold` lets each case start its simulator; `--allow-real-servers` and
  `--allow-tools` let the plugin's server run and its tools be called.
- `--ablation none`: without the plugin there are no Live tools, so a
  baseline arm only scores zero.
- Runs are independent (each has its own temporary home, session file and
  port), so `-j` is safe. One pass (`runs: 1` in every case) cost $3.30
  and two minutes with `-j 4` and the default model. `--runs 3` gives
  numbers worth comparing, `--case 'mixer-*'` or `--tag composing` a
  subset (one `--case` per run), `--model` another model.
- First pass, 2026-10-11 (Opus 5.5): 27/27 after two case fixes (a check
  that counted a refused command as a change; the delete case now accepts
  the client declining the confirmation, as a headless run does).
- The report and `aggregate-result.json` land in `evals/results/<timestamp>/`
  (ignored by git).

## How a case works

```
evals/<case>/
  case.yaml     prompt, limits, graders
  scaffold.sh   calls ../start-simulator.sh with this folder
  setup.json    optional: protocol commands that shape the set first
  check.py      check(state, initial) -> {name: bool}
```

`start-simulator.sh` starts the simulator detached, with the session file in
the run's temporary `HOME` (where the server looks for it, taking the port
from it). The simulator keeps two files in the run's workspace, updated
after every change: `live-state.json` (the set: tracks, mixer displays,
devices and parameter displays, clips with notes and envelopes, scenes,
locators, undo history) and `checks.json` (the case's verdicts,
`"check:<name>": true|false`). Each graded name is a `regex` grader on
`checks.json`; a few cases also grade the reply (`regex` on the last
message, or an `llm` judge where wording matters). The agent could read
those files, as it could ask the tools; prompts do not mention them.

`evals/checklib.py` holds the helpers checks share. `tests/test_evals.py`
runs a scripted correct solution of every case through the server's tools
against the simulator and requires every check to pass, and requires that
the untouched set fails it (except where leaving the set alone is the task),
so a check that is impossible or vacuous fails in CI, without a model.

## Adding a case

1. Make the folder with `case.yaml`, `scaffold.sh` (copy one) and
   `check.py`; `setup.json` if the set needs content first.
2. One `regex` grader per check name, on `checks.json`, pattern
   `'"check:<name>":\s*true'`.
3. Add the reference solution to `SOLUTIONS` in `tests/test_evals.py`.
4. If the task needs Live behaviour the simulator lacks, extend
   `MCP_Server/fake_live.py`.
