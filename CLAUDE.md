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
5. **Read-only commands go in `READ_ONLY_COMMANDS`.** Everything else gets
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

## Server-side conventions

- New tools go in `extended_tools.register()` with the local `@tool` decorator
  (telemetry wrapper + exceptions returned as `"Error in <tool>: …"` text).
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
