---
name: ableton-live
description: How to work in Ableton Live through the ableton MCP tools, covering looking before changing, addressing tracks/devices/clips, batching edits, undo, snapshots and listening to the result. Use whenever a request involves Ableton Live, a Live set, its tracks, clips, devices, scenes or mixer.
---

# Working in Ableton Live

The `ableton` MCP server controls the Live set that is open right now, through
the AbletonMCP Remote Script running inside Live. Every change is real and
audible at once, and each tool call is one step in Live's undo history.

## Look first

- `get_session_overview()` returns tempo, meter, Key & Scale, every track
  (type, devices, mixer, Session and Arrangement clips) and the scenes. Call it
  before changing anything and again after structural edits; do not guess
  indices.
- Tracks: index, exact name, `master` or `return A`. Devices: index or name.
  Session clips: `clip_index` (the scene row). Arrangement clips:
  `arrangement_clip_index`.
- Anything without a dedicated tool: read the Live Object Model with
  `lom_get_object(path="live_set tracks 0 devices 0")` and change it with
  `lom_set_property(...)` or `lom_call_method(...)`. Several edits in one
  `run_batch(commands=[...])` take one round trip.

## Change carefully

- Before a series of mix or sound changes: `snapshot(action="save", name="before …")`.
  `snapshot(action="diff", name=...)` shows what changed since, and
  `snapshot(action="restore", name=...)` puts it back. Two snapshots give the
  user an A/B.
- Generators write into a clip slot (`clip_index`) or the Arrangement
  (`arrangement_time`). Pass `replace=true` only when the old notes should go.
- Ask before deleting tracks, clips, scenes or devices. The user can always
  undo in Live (Cmd/Ctrl+Z), and `transport(action="undo")` does the same:
  one step per tool call.
- `select_in_live(...)` shows the user what you are talking about (selects the
  track, clip or device in Live's window).

## Listen to the result

- `measure_levels()` meters every track while the set plays (gain staging).
- `render_and_analyze()` records a section in real time and reports loudness,
  true peak, dynamics, tonal balance and stereo image.
- `mix_doctor()` records every track at once and finds masking and level
  problems (see the mix-and-master skill).
- These play the set in real time, so tell the user before starting a long
  section, and keep sections short (8–16 bars) while iterating.

## Sets on disk

`read_live_set(path=...)` reads an .als without opening it: settings, tracks,
devices, plug-ins, clips and missing samples; `compare_to=` a file from the
project's Backup folder shows what changed. `find_live_sets(query="Serum")`
finds sets by file, track or plug-in name. These never touch the open set.

## Watching what the user does

`watch_live(preset="transport")` (or `selection`, `structure`, or any
`path`/`property`) starts buffering Live's changes; read them with
`get_live_events(since=..., wait_seconds=...)` and stop with `unwatch_live()`.

## When a tool fails

Connection refused, timeouts or "older than this server" errors: run
`doctor()` and follow its fixes; the ableton-setup skill has the details.
