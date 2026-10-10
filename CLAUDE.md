# CLAUDE.md

Guidance for AI coding agents working in this repository.

## What this is

An MCP server (`MCP_Server/`, Python 3.10+, FastMCP) that controls Ableton Live
through a MIDI Remote Script (`AbletonMCP_Remote_Script/__init__.py`) running
inside Live's embedded Python. They talk JSON over TCP on `127.0.0.1:9877`: one
JSON object per request, one per response, no framing (the reader parses until
the buffer is valid JSON). Forked from `ahujasid/ableton-mcp`, whose remote is
`upstream`.

## Layout

| Path | Role |
|---|---|
| `AbletonMCP_Remote_Script/__init__.py` | The Live-side script. Single file: the installer copies only this file. |
| `MCP_Server/bundled_ableton_remote_script/AbletonMCP_init.py` | **Byte-identical copy** of the script, shipped in the wheel. |
| `MCP_Server/server.py` | Connection handling and the original tools; registers `extended_tools` at the bottom. |
| `MCP_Server/extended_tools.py` | All newer tools, written in terms of the generic commands (`lom_get/set/call`, `batch`, …). |
| `MCP_Server/tooling.py` | `register_tool`: annotations, profiles (`ABLETON_MCP_PROFILE`), sync tools off the event loop, errors as MCP tool errors. |
| `MCP_Server/prompts.py` | MCP prompts (workflows) and the streaming loudness targets. |
| `MCP_Server/doctor.py` | Setup diagnostics behind the `doctor` tool and `ableton-mcp-doctor`. |
| `MCP_Server/midi_files.py` | Standard MIDI File read/write (mido) for clip import/export. |
| `MCP_Server/device_recipes.py` | Ready-made device chains (parameter names as Live 12.3 shows them). |
| `MCP_Server/music.py` | Pure music theory + generators + note transforms over note dicts. No Live access. |
| `MCP_Server/audio_analysis.py` | numpy/scipy/soundfile analysis (BS.1770 LUFS, spectrum, stereo, tempo, key). |
| `MCP_Server/remote_script_install.py` | `ableton-mcp-install-script`; holds `EXPECTED_REMOTE_SCRIPT_VERSION`. |
| `MCP_Server/script_handshake.py` | Version/capability handshake with the script. |
| `MCP_Server/telemetry*.py`, `MCP_Server/dataset/` | Inherited from upstream. Inactive without the unshipped `MCP_Server/config.py`. |
| `tests/fake_live.py` | In-memory fake of the LOM (objects carry `_live_ptr`, listeners, Live 11 note API, enums). |

## Commands

```bash
uv sync
uv run --with pytest pytest -q                 # whole suite, no Live needed
uv run ableton-mcp-install-script              # copy script into Live's User Library
uv run ableton-mcp-install-script --sync-bundle  # copy repo script → bundled copy
uv run ableton-mcp                             # run the MCP server (stdio)
uv run ableton-mcp-doctor                      # diagnose versions, copies, connection, Live log
```

## Rules when changing the Remote Script

1. **Keep the two copies identical.** After editing
   `AbletonMCP_Remote_Script/__init__.py`, run
   `cp AbletonMCP_Remote_Script/__init__.py MCP_Server/bundled_ableton_remote_script/AbletonMCP_init.py`
   (or `--sync-bundle`). Tests load the bundled copy.
2. **Bump versions together** when the command surface changes:
   `SCRIPT_VERSION` in the script and `EXPECTED_REMOTE_SCRIPT_VERSION` in
   `remote_script_install.py`; add new commands to `SCRIPT_CAPABILITIES` (a test
   checks every handler is advertised).
3. **Register commands in the handler table** (`_command_handlers` /
   `_lom_command_handlers`). Never add `if/elif` dispatch.
4. **Every LOM access runs on Live's main thread.** `_process_command` marshals
   everything except `OFF_THREAD_COMMANDS` (which must not touch the LOM). Long
   operations go in `LONG_RUNNING_COMMANDS` *and* in the server's
   `long_running_commands` with a slightly larger value.
5. **Read-only commands go in `READ_ONLY_COMMANDS`**, read-only LOM methods in
   `READ_ONLY_METHODS` (methods named `get_*`/`is_*` count too), and a batch
   of read-only steps is read-only. Everything else gets
   `Song.end_undo_step()` called after it (see Undo below).
6. **Stay compatible with Live's Python**: no f-strings or Python-3.8+-only
   syntax in the script; it must import with `_Framework` stubbed (tests do this).
7. **Private names are blocked** in paths (`lom_check_name`); keep it that way —
   the socket is unauthenticated.
8. Live loads the script once per launch: changes need a Live restart to take
   effect.

## Live behaviour verified on Live 12.3.5 (don't "fix" these back)

- **Undo:** Live appends every script change to one open undo step until the
  user acts, so one Cmd+Z would revert all agent edits. Calling
  `end_undo_step()` after each modifying command gives one step per command.
  `begin_undo_step()`/`end_undo_step()` *around* a batch split it into two steps
  instead, so batches don't use it. Parameter changes (volume, tempo, mute) are
  always a separate step from structural ones (name, color, create) — that is
  Live, not a bug.
- **Value objects outside `Live.*`:** `RoutingType` reports module `Track` and has
  no `_live_ptr`; `_lom_value` turns any non-builtin object into a `$ref`.
- **Vector types:** some lists are Live vector classes, not tuples;
  `lom_is_sequence` only excludes objects that have `_live_ptr`.
- **Display rounding:** parameter displays are rounded, so `param_set_display`
  picks the bisection candidate whose display is closest to the target.
- **Enums:** assigning a plain int to an enum property needs conversion through
  the enum type (`_enum_member`); methods taking enums get `{"$enum": ...}`.
- **Live updates** have restored an old `AbletonMCP` folder inside the app bundle
  (`…/Contents/App-Resources/MIDI Remote Scripts/`), which shadows the User
  Library copy.
- Quitting Live with unsaved changes shows "Changes will be lost. Proceed?";
  its buttons ignore Accessibility clicks, Return confirms.

Measured on Live 12.3.8:

- **Meters:** `output_meter_left/right/level` are linear in dB:
  dBFS = 76 × meter − 70 (a 0 dBFS sine reads 0.921; 0 → −70 dB, 1 → +6 dB).
  They show smoothed peaks, and their listeners fire only when the value
  changes.
- **Latency:** a main-thread command takes ~410 ms round trip, an off-thread
  one ~200 ms: Live's Python only lets the socket thread run on its ~100 ms
  tick. Batch steps instead of making several calls.
- `Track.insert_device(name[, index])` inserts a native device by its browser
  name and returns the Device; `create_audio_track`/`create_midi_track`
  return the new Track.
- Live 12 calls the master track "Main".
- `DeviceParameter.display_value` is a bare number in display units; ask
  `lom_get` for `$display` to get the text ("-6.0 dB").
- Glue Compressor's stepped controls display without units (".6", "A" for
  Auto); `parse_display_number` accepts the leading-dot form.

## Server-side conventions

- New tools go in `extended_tools.register()` with the local `@tool` decorator;
  `@tool(read_only=True)`, `@tool(destructive=True)` (deletes or replaces
  content) or `@tool(idempotent=True)` set the MCP annotations. Registration
  goes through `tooling.register_tool`: sync tools run on a worker thread and
  exceptions become MCP tool errors, so raise on failure instead of returning
  error text. The original tools in `server.py` use `@legacy_tool("core" |
  "full")`; "full" ones only appear with `ABLETON_MCP_PROFILE=full`.
- Generators read the set's meter and Key & Scale through `song_context()` /
  `resolve_key()`; explicit arguments win.
- Prefer composing `op_set` / `op_call` / `op_display` into one `batch(...)`
  call over several round-trips; return `batch_summary(...)`.
- Address tracks with `track_path()` (index, name, `master`, `return A`), devices
  with `device_path()`, clips with `clip_path()`; quote names via `selector()`.
- Tools that wait (recording, long-polling) are `async` and use `asend()` so
  they don't block the event loop.
- Put pure logic in `music.py` / `audio_analysis.py` with unit tests; keep tools
  thin.

## Testing

- `tests/test_lom_bridge.py` drives the script's handlers directly and, in the
  `live_server` fixture, runs the real TCP server + real MCP tools against
  `fake_live.FakeSong`. Extend `fake_live.py` when a tool touches new LOM
  surface.
- Anything that depends on real Live behaviour (undo, routing objects, recording)
  must also be checked against a running Live before claiming it works.
