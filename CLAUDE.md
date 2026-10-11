# CLAUDE.md

Guidance for AI coding agents working in this repository.

## What this is

An MCP server (`MCP_Server/`, Python 3.10+, MCP Python SDK 2.x `MCPServer`) that controls Ableton Live
through a MIDI Remote Script (`AbletonMCP_Remote_Script/__init__.py`) running
inside Live's embedded Python. They talk JSON over TCP on `127.0.0.1:9877`,
protocol 2: one JSON object per line each way, requests carry an `id` the reply
echoes, and the first request is `hello` with the per-launch token Live writes
to `~/.ableton-mcp/live-session.json` (mode 600). Bare objects without a
newline (protocol 1, older clients) are still parsed, but need the token too.
Forked from `ahujasid/ableton-mcp`, whose remote is `upstream`.

## Layout

| Path | Role |
|---|---|
| `AbletonMCP_Remote_Script/__init__.py` | The Live-side script. Single file: the installer copies only this file. |
| `MCP_Server/bundled_ableton_remote_script/AbletonMCP_init.py` | **Byte-identical copy** of the script, shipped in the wheel. |
| `MCP_Server/server.py` | The shared connection and the original tools; registers `extended_tools` at the bottom. |
| `MCP_Server/connection.py` | `AbletonConnection`: hello + token, line framing, reply ids, fallback to protocol 1 for scripts before 2.0. |
| `MCP_Server/extended_tools.py` | All newer tools, written in terms of the generic commands (`lom_get/set/call`, `batch`, …). |
| `MCP_Server/tooling.py` | `register_tool`: annotations, profiles (`ABLETON_MCP_PROFILE`), errors as MCP tool errors with their message, the MCP Apps panel a tool shows. |
| `MCP_Server/live_updates.py` | Pushes `ableton://events` / `ableton://session/overview` updates: `resources/subscribe` (2025-11-25) and `subscriptions/listen` (2026-07-28). |
| `MCP_Server/ui/` | MCP Apps panels (`ui://` HTML resources) registered through the SDK's `Apps` extension. |
| `MCP_Server/prompts.py` | MCP prompts (workflows) and the streaming loudness targets. |
| `MCP_Server/doctor.py` | Setup diagnostics behind the `doctor` tool and `ableton-mcp-doctor`. |
| `MCP_Server/midi_files.py` | Standard MIDI File read/write (mido) for clip import/export. |
| `MCP_Server/m4l_device.py` | Builds the AbletonMCP Analyzer (.amxd) as bytes: the Max patch as JSON. |
| `MCP_Server/analyzer.py` | UDP/OSC listener for the analyzer's messages; levels, bands, correlation, approximate LUFS. |
| `MCP_Server/ml.py` | Runs machine-learning jobs (Demucs, Basic Pitch, CLAP) in uv-built environments; seconds→beats; the CLAP sound index. |
| `MCP_Server/ml_worker.py` | The job side: runs in those environments, imports nothing from `MCP_Server`, talks JSON lines. |
| `MCP_Server/als.py` | Reads Live Set files (.als) without Live: settings, tracks, devices, clips and notes, samples, comparing and finding sets. Never writes them. |
| `MCP_Server/device_recipes.py` | Ready-made device chains (parameter names as Live 12.3 shows them). |
| `MCP_Server/snapshots.py` | Capture / compare / restore mixer and device parameters (A/B), stored in `~/.ableton-mcp/snapshots`. |
| `MCP_Server/music.py` | Pure music theory + generators + note transforms over note dicts. No Live access. |
| `MCP_Server/turkish.py` | Makams (AEU commas, 12-tone approximation, `.ascl` tunings) and usuls (düm/tek cycles). Pure. |
| `MCP_Server/audio_analysis.py` | numpy/scipy/soundfile analysis (BS.1770 LUFS, spectrum, stereo, tempo, key). |
| `MCP_Server/mix_analysis.py` | Masking between stems, stem findings, reference comparison with EQ Eight settings. |
| `MCP_Server/remote_script_install.py` | `ableton-mcp-install-script`; holds `EXPECTED_REMOTE_SCRIPT_VERSION`. |
| `MCP_Server/script_handshake.py` | Version/capability handshake with the script. |
| `MCP_Server/telemetry*.py`, `MCP_Server/dataset/` | Inherited from upstream. Inactive without the unshipped `MCP_Server/config.py`. |
| `tests/fake_live.py` | In-memory fake of the LOM (objects carry `_live_ptr`, listeners, Live 11 note API, enums). |
| `packaging/build_mcpb.py`, `packaging/mcpb/` | Claude Desktop extension: manifest template, icon, build script (fills in version and tools). |
| `.claude-plugin/`, `skills/` | Claude Code plugin (MCP server + skills) and its marketplace entry. |
| `.github/workflows/` | CI (tests, lint, wheel/sdist/.mcpb) and tag-triggered GitHub releases. |

## Commands

```bash
uv sync
uv run --with pytest pytest -q                 # whole suite, no Live needed
uv run ableton-mcp-install-script              # copy script into Live's User Library
uv run ableton-mcp-install-script --sync-bundle  # copy repo script → bundled copy
uv run ableton-mcp                             # run the MCP server (stdio)
uv run ableton-mcp-doctor                      # diagnose versions, copies, connection, Live log
uvx ruff check .                               # lint (rules in pyproject.toml)
uv run python packaging/build_mcpb.py          # dist/ableton-live-mcp-<version>.mcpb (needs Node.js)
claude plugin validate .                       # plugin manifest + skills
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
4. **There are no threads.** The sockets are non-blocking and polled on Live's
   main thread (a `Live.Base.Timer` every 10 ms, `update_display` as fallback);
   a command runs inside that poll, so a handler must never block or wait for
   Live to change. Waiting belongs to the server (poll again) or to a deferred
   reply like `poll_events` with `wait`. Commands that can take long get a
   larger timeout in `connection.LONG_RUNNING_COMMANDS` on the server.
   `hello` and waiting `poll_events` are handled by the transport
   (`_handle_request`) because they act on the connection.
5. **Read-only commands go in `READ_ONLY_COMMANDS`**, read-only LOM methods in
   `READ_ONLY_METHODS` (methods named `get_*`/`is_*` count too), and a batch
   of read-only steps is read-only. Everything else runs between
   `Song.begin_undo_step()` and `Song.end_undo_step()` (see Undo below).
6. **Stay compatible with Live's Python**: no f-strings or Python-3.8+-only
   syntax in the script; it must import with `_Framework` stubbed (tests do this).
7. **Private names are blocked** in paths (`lom_check_name`); keep it that way.
   The token keeps other users and web pages out, not other programs running
   as this user. A connection whose first byte is not `{` is dropped unanswered
   (a browser's POST to localhost would otherwise run its JSON body line).
8. Live loads the script once per launch: changes need a Live restart to take
   effect.

## Live behaviour verified on Live 12.3.5 (don't "fix" these back)

- **Undo:** Live appends every script change to one open undo step until the
  user acts, so one Cmd+Z would revert all agent edits. From a
  `schedule_message` callback, `end_undo_step()` after each command was enough;
  from a `Live.Base.Timer` callback (12.3.8) it is not, and only
  `begin_undo_step()` before plus `end_undo_step()` after each modifying
  command keeps one step per command. Parameter changes (volume, tempo, mute)
  are always a separate step from structural ones (name, color, create), and
  Live may still merge two changes of the *same* parameter made within a few
  hundred ms (like a knob turn) — that is Live, not a bug.
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
- **Latency:** with the socket polled by a 10 ms `Live.Base.Timer` on the main
  thread (script 2.0) a command takes ~10 ms round trip (`get_session_overview`
  ~20 ms). The old socket-thread design took ~410 ms: Live's Python only lets a
  background thread run on its ~100 ms tick. Changing the set from the timer
  callback works. Batching still saves round trips.
- `Live.Base.Timer(callback=..., interval=<ms>, repeat=True).start()` exists in
  Live 12 (Ableton's Move and MxDCore scripts use it).
- `Track.insert_device(name[, index])` inserts a native device by its browser
  name and returns the Device; `create_audio_track`/`create_midi_track`
  return the new Track.
- Live 12 calls the master track "Main".
- `DeviceParameter.display_value` is a bare number in display units; ask
  `lom_get` for `$display` to get the text ("-6.0 dB").
- Glue Compressor's stepped controls display without units (".6", "A" for
  Auto); `parse_display_number` accepts the leading-dot form.
- **Browser:** the roots are Live 12's sidebar labels. `samples` is one flat
  list (~12,000 items on this machine, walked in ~0.1 s), `drums` holds kits
  and `Drum Hits/<Kick, Snare, Hihat, …>` (user samples included).
  `BrowserItem` has no `_live_ptr`; `browser_search` registers results by
  `id(obj)` so `{"$ref": "id N"}` reaches them. Selecting a pad
  (`rack.view.selected_drum_pad`) and then `browser.load_item(sample)` puts
  the sample on that pad as a Simpler (~100 ms each).
- **Recording several tracks at once:** in one call, only the first audio
  track created gets its `available_input_routing_types` filled; create the
  tracks in one call and set their inputs in the next (`record_section`
  retries). A track's input channel "Post Mixer" takes its sound after
  devices, fader and pan. `ClipSlot.fire(length)` (positional) on an armed
  empty slot records a fixed-length Session clip from the next launch
  boundary; firing a scene and those slots in one batch starts them together.
- **Deferred state:** a playhead moved by `current_song_time` is not in effect
  until the callback returns — `set_or_delete_cue()` in the same call toggles
  a cue at the *old* position (and would delete a cue there). Move the
  playhead in one call, toggle in the next (`place_locators`); the script's
  `create_locator` toggles on the next update via `schedule_message`.
- **Tunings:** `Song.tuning_system` is None until a tuning is loaded, and
  `browser.load_item` on an `.ascl` in User Library › Tunings does not load it
  (12.4) — the user double-clicks it. Live picks up new files there after a
  few seconds.
- **Live 12.4 renamed device parameters:** "Output Gain"/"Gain"/"Makeup"/
  "Master Output" → "Output", EQ Eight "N Resonance A" → "N Q A", and names in
  Reverb, Delay, Auto Filter, Echo, Chorus-Ensemble and Roar. Settings go
  through `resolve_parameter_names`, which maps either version's name onto the
  device (`PARAMETER_RENAMES`); recipes keep the 12.3 names.
- Ableton's updater installed Live 12.4 when Live was quit on 2026-10-10;
  script 2.0 and the timer transport work the same there.

## Max for Live analyzer (built and checked in Live 12.4 Trial, Max 9)

- An unfrozen .amxd is chunks: `ampf` + uint32 4 + type (`aaaa` audio
  effect), `meta` + uint32 4 + 4 zero bytes, `ptch` + uint32 length + the
  patcher JSON ending in NUL (as Live's own `Misc/Max Devices/Max Audio
  Effect.amxd`; Ableton's devices are encrypted, `ciph`).
  `project.amxdtype` is the type as a big-endian int.
- Max's `live.path` ids are not the Remote Script's LOM ids (the first
  device reported id 1), so the device sends its track's *name*, read with
  `live.object get name` on every message.
- `fffb~`'s resonant bands let the bass leak upwards (5–15 dB too much above
  1 kHz); a chain of `cross~` crossovers at the octave edges matches octave
  filters within ~1 dB and sums to the full-band power.
- Live keeps a loaded device's patch when the file changes: the OSC address
  carries a version (`/ableton-mcp/analyzer/2`), and `install_analyzer`
  replaces a copy that still sends an older one.
- The device file is generated, never committed: change `m4l_device.py`,
  bump `VERSION`, and check in Live (it must load and send).

## Machine learning (measured on an M-series Mac, 2026-10-11)

- Each job runs `ml_worker.py` through `uv run --isolated --no-project
  --python X --with …` (`ml.ENVIRONMENTS`): torch never loads into the
  server and the memory is gone when the job ends. uv sets `$UV` for what it
  runs, so the server finds it. Progress comes back as JSON lines and goes
  out as MCP progress notifications.
- Basic Pitch 0.4.0 pins TensorFlow < 2.15 on Python 3.11+ (macOS), which no
  longer installs: its environment is Python 3.10, where it uses Core ML
  (macOS), ONNX (Windows) or TFLite. Its resampy needs `pkg_resources`:
  `setuptools<81`.
- Demucs 4.1 (Hugging Face weights, `apply_model(callback=…)` for progress)
  runs on MPS: a 20 s mix in ~9 s once cached; known stems came back at
  14–19 dB SDR (drums, bass, vocals), 10 dB for "other".
- CLAP: `laion/larger_clap_music` gives every text the same vector under
  transformers 5.x (its text pooler loads untrained), so the model is
  `laion/clap-htsat-unfused`: P@10 0.72 for text queries over 10 drum
  classes (chance 0.10), 0.62 sound-to-sound; ~0.06–0.1 s per sound, so
  indexing stops at 1,500 files per call (`INDEX_PER_CALL`).
- `lowest`/`highest` use Live's note names: A1 = MIDI 45 = 110 Hz.
- Tests mock `ml.run` (and test the protocol with the worker's `echo` task);
  `ABLETON_MCP_ML_TESTS=1` adds a run with the real Basic Pitch model.

## Live Set files (.als), as read from Live 12.4's own sets

- gzip-compressed XML; parsing drops automation and modulation subtrees as
  it goes (a 27 MB set takes ~80 MB instead of ~215 MB, ~0.4 s).
- Device elements are named by `Device.class_name` (Eq8, StereoGain …);
  `als.DEVICE_NAMES` was read from Live 12.4 by inserting each native device.
  Max for Live devices are `MxDevice*` named by their `.amxd` file;
  plug-ins are `PluginDevice` with `PluginDesc/Vst3PluginInfo` (or VST2, AU).
- Time signature: `(numerator - 1) + 99 * log2(denominator)` (201 = 4/4).
  ScaleInformation: root 0 = C and the scale as an index (0 Major, 1 Minor
  checked against the demo set's notes; the rest in Live/Push order).
- A clip plays `Loop/LoopStart..LoopEnd` whether the loop is on or off (with
  it off they are the start and end markers); arrangement clips sit at
  `CurrentStart..CurrentEnd`. Drum pad note = 128 − `ReceivingNote`.
- Sample paths: `RelativePathType` 1 = project folder, 5 = a Live Pack
  (`LivePackName`, e.g. the Core Library in the app bundle), 6 = Library,
  7 = Live's Builtin folder; `Path` is absolute on the machine that saved it.
- No writer on purpose: it would be the way around the Trial's disabled
  saving, and a damaged set is worse than none.

## Server-side conventions

- New tools go in `extended_tools.register()` with the local `@tool` decorator;
  `@tool(read_only=True)`, `@tool(destructive=True)` (deletes or replaces
  content) or `@tool(idempotent=True)` set the MCP annotations, `ui="ui://…"`
  the panel. Registration goes through `tooling.register_tool`: sync tools run
  on the SDK's worker threads, and any exception becomes a `ToolError` carrying
  its message (mcp 2.x would otherwise show only "Error executing tool …"), so
  raise on failure instead of returning error text. Long `async` tools take
  `ctx: Context | None = None` and call `report_progress(ctx, …)`.
- mcp 2.x: `from mcp.server.mcpserver import MCPServer, Context`; types use
  snake_case attributes (`input_schema`, `read_only_hint`); `call_tool()`
  returns a `CallToolResult`. Clients negotiate 2025-11-25 (`mode="legacy"`) or
  2026-07-28; push updates and tests cover both. The original tools in `server.py` use `@legacy_tool("core" |
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

## Panels (MCP Apps)

- A panel is one self-contained HTML document (`ui/panels.py`, shared bridge
  and theme in `ui/base.py`): no external URLs, no `innerHTML` (names come
  from the user's set), host theme variables with light/dark fallbacks. It
  speaks the ext-apps protocol by hand: `ui/initialize` (protocol
  `2026-01-26`) → `ui/notifications/initialized`, then `tool-input` /
  `tool-result`; buttons call tools with `tools/call` only when the host
  offers `serverTools`, otherwise they are disabled.
- Wire a tool with `@tool(..., ui=ui.<PANEL>)`; its text result must stay
  complete for clients without Apps. `tests/test_ui.py` checks serving,
  wiring and (with node) that every panel's script parses; check the look in
  a real or mock host (headless Chrome) after changing a panel.

## Packaging and releases

- One version, three places: `pyproject.toml`, `.claude-plugin/plugin.json` and
  the plugin entry in `.claude-plugin/marketplace.json` (a test checks they
  agree). The `.mcpb` manifest takes it from pyproject at build time.
- The `.mcpb` is a "uv" bundle: source, `pyproject.toml` and `uv.lock`, which
  Claude Desktop installs with uv. The wheel's packages come from
  `packages.find` (`MCP_Server*`), so new subpackages ship by themselves.
- Skills name tools as `` `tool_name(...)` ``; a test fails when a skill names
  a tool that no longer exists. `claude plugin validate .` warns that
  CLAUDE.md at the plugin root is not plugin context: expected, it is for this
  repo.
- A `v<version>` tag publishes a GitHub release (wheel, sdist, .mcpb). Nothing
  goes to PyPI: `ableton-live-mcp` is taken there.

## Testing

- `tests/test_lom_bridge.py` drives the script's handlers directly and, in the
  `live_server` fixture, runs the real TCP server + real MCP tools against
  `fake_live.FakeSong`. Extend `fake_live.py` when a tool touches new LOM
  surface.
- Anything that depends on real Live behaviour (undo, routing objects, recording)
  must also be checked against a running Live before claiming it works.
