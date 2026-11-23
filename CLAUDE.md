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
| `MCP_Server/tooling.py` | `register_tool`: annotations, profiles (`ABLETON_MCP_PROFILE`, else `ABLETON_MCP_PROFILE_SAVED`: the plugin's "Tool profile" option, so the shell's variable wins), errors as MCP tool errors with their message, the MCP Apps panel a tool shows. |
| `MCP_Server/live_updates.py` | Pushes `ableton://events` / `ableton://session/overview` updates: `resources/subscribe` (2025-11-25) and `subscriptions/listen` (2026-07-28). Its long polls use a connection of their own (on the tools' one every command would queue behind them). |
| `MCP_Server/ui/` | MCP Apps panels (`ui://` HTML resources) registered through the SDK's `Apps` extension. |
| `MCP_Server/prompts.py` | MCP prompts (workflows) and the streaming loudness targets. |
| `MCP_Server/doctor.py` | Setup diagnostics behind the `doctor` tool and `ableton-mcp-doctor`; Live's audio device from its Log.txt (`audio_devices`). |
| `MCP_Server/midi_files.py` | Standard MIDI File read/write (mido) for clip import/export. |
| `MCP_Server/companion.py` | Client for the Extensions SDK add-on: finds its session file, sends one command. |
| `extension/` | AbletonMCP Companion, the Extensions SDK add-on (TypeScript): share selection, confirm dialog, pre-FX render. |
| `MCP_Server/m4l_device.py` | Builds the AbletonMCP Analyzer (.amxd) as bytes: the Max patch as JSON. |
| `MCP_Server/midi_tools.py`, `MCP_Server/midi_tools/*.js` | Max for Live MIDI Tools (Usul, Makam Melody, Makam Snap) for Live 12's Generate/Transform tabs: v8 code in a `v8.codebox`, tables written from `turkish.py`/`music.py`; `install_midi_tools`. |
| `MCP_Server/analyzer.py` | UDP/OSC listener for the analyzer's messages; levels, bands, correlation, approximate LUFS. |
| `MCP_Server/ml.py` | Runs machine-learning jobs (Demucs, Basic Pitch, CLAP, MusicGen) in uv-built environments; Basic Pitch's presets, octave-ghost removal, bends in cents, velocities from levels, notes placed in beats; the CLAP sound index; generation prompts and folders. |
| `MCP_Server/ml_worker.py` | The job side: runs in those environments, imports nothing from `MCP_Server`, talks JSON lines. |
| `MCP_Server/reference_jobs.py` | reference-dissector (refdissect) as detached jobs: the pinned commit (`REFDISSECT_COMMIT`), each step's command, job records in `~/.ableton-mcp/jobs`, one job at a time, lost and cancelled jobs, the summaries `reference_job` gives. |
| `MCP_Server/reference_runner.py` | The job side: run by path (`python -I`) in its own process group, stdlib only; runs the steps, keeps the record (and the work folder's `job.json`) up to date. |
| `MCP_Server/blueprint.py`, `MCP_Server/schemas/blueprint-v1.schema.json`, `docs/blueprint-v1.md` | The blueprint format `build_from_blueprint` builds: schema, validator, files to copy (scaled where they peak over full scale), excerpts (`bars`), the `build_tracks` spec. |
| `MCP_Server/als.py` | Reads Live Set files (.als) without Live: settings, tracks, devices, clips and notes, samples, comparing and finding sets. Never writes them. |
| `MCP_Server/device_recipes.py` | Ready-made device chains (parameter names as Live 12.3 shows them). |
| `MCP_Server/memory.py` | Notes that outlast a conversation: in the set (`Song.set_data`, key `ableton_mcp_memory`) and in `~/.ableton-mcp/memory.json`; the `memory` tool and the overview's `memory`. |
| `MCP_Server/snapshots.py` | Capture / compare / restore mixer and device parameters (A/B), stored in `~/.ableton-mcp/snapshots`. |
| `MCP_Server/music.py` | Pure music theory + generators + note transforms over note dicts. No Live access. |
| `MCP_Server/turkish.py` | Makams (AEU commas, 12-tone approximation, `.ascl` tunings) and usuls (düm/tek cycles). Pure. |
| `MCP_Server/audio_analysis.py` | numpy/scipy/soundfile analysis (BS.1770 LUFS, spectrum, stereo, tempo, key, each note's level at its pitch). |
| `MCP_Server/mix_analysis.py` | Masking between stems, stem findings, reference comparison with EQ Eight settings. |
| `MCP_Server/remote_script_install.py` | `ableton-mcp-install-script`; holds `EXPECTED_REMOTE_SCRIPT_VERSION`. |
| `MCP_Server/script_handshake.py` | Version/capability handshake with the script. |
| `MCP_Server/fake_live.py` | In-memory fake of the LOM (objects carry `_live_ptr`, listeners, Live 11 note API, enums, an undo history by Live's rules, the recipes' devices, Drum Rack chains, Instrument Racks and chain mixers, `replace_sample`, Simpler's reverse, clip lengths, warp markers and unwarped clips as Live sizes them, Live's version: 12.4.16). Tests and the simulator use it. |
| `MCP_Server/transitions.py` | Pure Arrangement logic: unrolling split/looped clips, fills, risers, gaps, automation curves. Tools build on it with `rewrite_range` (extended_tools). |
| `MCP_Server/simulator.py` | `ableton-mcp-simulator`: the bundled Remote Script over `fake_live`, served like Live (port + session file); writes the set as JSON and an eval case's verdicts. |
| `evals/` | `claude plugin eval` suite: 33 cases against the simulator, graded on `checks.json` (see `evals/README.md`). |
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
uv run ableton-mcp-simulator                   # a fake Live set to run the server against
claude plugin eval . --scaffold --allow-real-servers \
  --allow-tools "mcp__plugin_ableton-live_ableton__*" --ablation none -j 4   # evals/
uvx ruff check .                               # lint (rules in pyproject.toml)
uv run python packaging/build_mcpb.py          # dist/ableton-live-mcp-<version>.mcpb (needs Node.js)
claude plugin validate .                       # plugin manifest + skills
uv run python -m MCP_Server.blueprint FILE.json  # a blueprint's problems (schema, references, files)
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
- **Undo history** (12.4, probes with a marker change before each case):
  one command (a batch too) leaves one step for its parameter changes
  (device parameters, mixer, tempo, mute) and one for everything else
  (names, colors, clips, notes, devices, tracks), so a batch mixing both
  takes two undos. A value set to what it already was leaves no step, and
  neither do transport, the playhead, selection, loop, metronome, arm or
  solo. `journal.undo_steps` and the fake's undo (`fake_live.FakeSong`)
  follow these rules; verified end to end with `agent_changes(undo=1)`.
- **Arrangement automation** (12.4): `create_automation_envelope` on an
  Arrangement clip is refused ("Not a session clip"), and
  `automation_envelope` there returns None even when the clip has one. A
  Session clip copied with `duplicate_clip_to_arrangement` keeps its
  envelopes, and they play. A copy laid over a clip splits it: both parts
  keep all the notes and move their start/end markers. An envelope step
  needs a duration (zero-length steps do nothing). Parameter changes made
  while the Arrangement records become track automation on any track; the
  pass leaves an undo step per value set plus one (measured with 2 to 48
  values), the first undo removing the automation. `transitions.py` and
  `rewrite_range` build on these.
- **Song.set_data/get_data** (12.4): strings (any Unicode), numbers, lists
  and dicts round-trip; no undo step. The Trial cannot save, so keeping the
  data through save and reopen rests on Ableton's documentation here.
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
  its buttons ignore Accessibility clicks, Return confirms. Live 12.4.16
  (2026-11-23) asks "Save changes to "Untitled" before closing?" instead
  (Don't Save, Cancel, Save): Return picks Save (the Trial's Save As
  panel, whose Cancel takes a click), an Accessibility click on Don't
  Save does nothing, Cmd+D quits without saving.

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
- **Where playback starts** (12.4.2, measured): `continue_playing` resumes
  where Live last stopped, even after `current_song_time` was set (and read
  back) while stopped; `start_playing` starts at `Song.start_time`, which
  takes effect inside the same batch. `record_section` therefore sets
  `start_time` and calls `start_playing`, and puts `start_time` back (it
  leaves no undo step). With `continue_playing` every section was recorded
  from the last stop position, the requested section somewhere inside the file.
- **Tunings:** `Song.tuning_system` is None until a tuning is loaded, and
  `browser.load_item` on an `.ascl` in User Library › Tunings does not load it
  (12.4) — the user double-clicks it. Live picks up new files there after a
  few seconds.
- **Live 12.4 renamed device parameters:** "Output Gain"/"Gain"/"Makeup"/
  "Master Output" → "Output", EQ Eight "N Resonance A" → "N Q A", and names in
  Reverb, Delay, Auto Filter, Echo, Chorus-Ensemble and Roar. Settings go
  through `resolve_parameter_names`, which maps either version's name onto the
  device (`PARAMETER_RENAMES`); recipes keep the 12.3 names. Not for every
  device alike: Limiter's 12.3 "Gain" was its input gain, and 12.4.16
  (2026-11-23) has "Input Gain" and "Output" (Device On, Input Gain, Ceiling,
  Release, Auto, Link, M/S Link, Lookahead, Routing, Mode, Maximize On,
  Threshold, Output), so `DEVICE_PARAMETER_RENAMES` maps Limiter "Gain" →
  "Input Gain" and keeps the general "Gain" → "Output" (Utility) off it.
  Glue Compressor has no "Makeup" ("Output" is right); Compressor has both.
- **Racks and the browser** (12.4.16, 2026-11-23): a Core Library kit like
  "BNYX Boot Kit" is an Instrument Rack (`InstrumentGroupDevice`) whose one
  chain holds the Drum Rack and a Saturator; `Chain.devices` and
  `Chain.mixer_device` (ChainMixerDevice: "Chain Volume", "Chain Panorama",
  sends "Chain Send 0", "Speaker On") are there for rack and Drum Rack pad
  chains alike. Selecting a pad of that nested Drum Rack and
  `browser.load_item(sample)` fills it. A track still named as Live named it
  ("1-MIDI") takes the name of an instrument that lands on it, by the browser
  or `insert_device` ("1-BNYX Boot Kit", "1-Drift", visible in the same
  batch), of an instrument that replaces it ("1-Operator"), and again when its
  Simpler reverses ("2-Crash 707 R"); on a regular track an effect (Reverb)
  leaves it, and deleting the instrument gives "1-MIDI" back. A return track
  is renamed by effects too: `add_device("return A", "Utility")` made
  "A-Reverb" "A-Reverb | Utility" (deleting it restored it); the fake does not
  model returns. Tools that insert or load pin the track by id first
  (`pin_track`, `TrackId`). `SimplerDevice.reverse()` toggles; the reversed
  sample is a copy, `<project>/Samples/Processed/Reverse/<name> R.<ext>`,
  readable at once as `sample.file_path`, and reversing again gives the
  original path back. When a device lands on a MIDI track with no device, by
  `insert_device` (Reverb, Drum Rack, Drift alike) or the browser, Live arms
  it (`arm`, not `implicit_arm`), disarms every other track (exclusive arm)
  and selects it; later devices, or an instrument loaded onto a track that has
  one, change none of these. The arms read back changed in the same batch; the
  selection only from the next command on (deferred, like the playhead), so
  the selection is set back in a later command whatever was read. Live also
  selects (after the command) a track any device is inserted on (Reverb onto
  "1-Drift", onto an empty audio track, Utility onto a return), a new track
  (`create_midi_track`, `create_audio_track`: the next command reads it
  selected), and the track of a deleted device when another device stays on it
  (none when the chain is left empty; deletes inside racks unmeasured). An
  instrument onto a MIDI track that holds only a MIDI effect (Arpeggiator)
  does not arm it. `insert_devices` reads whether the target is an empty MIDI
  track and the selected one and skips the put-back when nothing can change;
  `keeping_arms` wraps `build_tracks` and `design_transition`;
  `manage_device(delete)` always sets the selection back. `arm` set through
  the LOM is not exclusive (2-MIDI and 3-Audio both stayed armed), and
  `insert_device` refuses a second instrument ("Device chains cannot have more
  than one instrument each"). `put_back_arms` restores arms and the selection
  after `inserted_device_ids`, `load_items` and the legacy load tools.
- **Sidechain** (12.4): Compressor has `input_routing_type` (its sidechain
  source, e.g. `available_input_routing_types 'Kick'`); Gate has none, so its
  sidechain cannot be set from a script.
- **Recordings** (`record_section`, `capture_stems`) are written at the audio
  device's sample rate (48 kHz on the test machine). Read as 44.1 kHz, a
  16-beat capture drifted by 9 % (2026-10-18).
- **Loaded samples stay loaded** (12.4): Live keeps playing what it loaded
  under a file name after the file is overwritten on disk, until it restarts.
  Overwritten library samples kept their old sound in a set for a day
  (a capture still held the old sample's second hit). Give changed
  content a new file name.
- **Default levels** (12.4): a new Simpler's Volume is −12 dB, Operator's −18 dB;
  raising Simpler to 0 dB made recordings clip at 0 dBFS (`capture_stems` now
  reports `clipped_samples`).
- **Clip note times** count from the clip's start, also for an Arrangement clip
  placed later in the song.
- Ableton's updater installed Live 12.4 when Live was quit on 2026-10-10;
  script 2.0 and the timer transport work the same there.

## Extensions SDK add-on (`extension/`, not yet run inside Live)

- Needs Live 12.4.5+ Suite (beta); the Trial cannot load extensions, so it is
  checked against the SDK's declarations (every class takes the API version,
  `Track<"1.0.0">`) and, in `test/host.test.ts`, as a bundle in a `vm` context
  set up like the Extension Host: no `global`, no `URL`, `require` for
  `node:` modules, integers as bigint, errors from another realm.
- Keep the SDK out of runtime imports everywhere but `src/extension.ts`
  (tests run the other files on plain Node with type stripping);
  `types/ableton-sdk.d.ts` declares only what the add-on uses, without
  default type arguments, so it fails where the real SDK would.
- The server side (`companion.py`, tools `get_live_selection`,
  `confirm_in_live`, `render_track_audio`, the doctor's `companion` line) is
  tested against a Python stand-in of the protocol.

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

## MIDI Tools (verified in Live 12.4 with Max 9.1.4, 2026-10-11)

- An .amxd of type `nagg` (generator) or `natt` (transformation), same
  chunks as other devices (Live's templates: `Misc/Max MIDI Tools`). Files
  in `<User Library>/MIDI Tools/Max Generators` and `Max Transformations`
  appear under "User:" in the clip view's tool menus; `browser.load_item`
  on one does nothing visible.
- `live.miditool.in` sends the context from its right outlet, then the
  notes; the context has `clip.time_selection_start/end` (the whole clip
  when nothing is selected), `scale` and `seed`. `v8.codebox` keeps its
  code in the patch (`code`, `filename: none`) and takes dicts as objects
  (`msg_dictionary`, `outlet_dictionary`).
- A transformation gets only the selected notes (after Generate, the new
  notes stay selected). Banging `live.miditool.in` when a control changes
  after Apply replaces that apply's result in place.
- Checked by driving Live's UI: Quartz mouse events work (AppleScript
  `click at` reaches the element but does not press it, and the tool tabs'
  accessibility elements all report the first tab). Tests run the JS in
  Node with Max's globals stood in for (`tests/test_midi_tools.py`).

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
- MusicGen small (`facebook/musicgen-small`, transformers 5.19, torch
  2.14): the first run downloaded ~2.4 GB in ~5 minutes; then a 6 s take
  needs ~17–19 s in all (start, load, generate), an 8 s loop ~19 s. MPS
  generates about twice as fast as the CPU (6.3 s against 13.6 s for 5 s).
  One seed looked broken on MPS (all bass), but across seeds both devices
  gave the same spectral spread and MPS kept "C major" prompts in C, so it
  stays the default (the EnCodec decoder gives identical audio on either).
  transformers 5.x no longer feeds MusicGen's `streamer`; progress comes
  from a `LogitsProcessor`, called once per step. Weights CC-BY-NC 4.0.
  Stable Audio Open (gated, needs a Hugging Face token) is not included.
- `lowest`/`highest` use Live's note names (A1 = MIDI 45 = 110 Hz) or Hz
  ("82.4 Hz", `music.frequency`).
- Basic Pitch 0.4.0 (2026-10-19): `predict()`'s note events always carry
  bends (thirds of a semitone per 11.6 ms frame); `multiple_pitch_bends`
  only shapes its MIDI object. They read one bin sharp: in-tune sines at
  110–880 Hz gave +1 in 99.4–100 % of frames, so `ml.bend_cents` subtracts
  one. Without melodia a note needs an attack: onset 0.6 found 3 of 16
  pure sines with a 10 ms fade-in (chords and legacy 15 and 14) and 2 notes
  on six 808 stems of 56 notes each. The melodic preset therefore decodes
  the model's output twice (`ml_worker.melodia_ranges`): melodia off from
  MIDI 52 up, on below, then `ml.drop_octave_ghosts`. `constrain_frequency`
  zeroes the arrays in place, so each decoding gets a copy. Notes at
  29–52 Hz start a median 109 ms late (IQR 48–187 ms). At those notes'
  pitch the onset head read a median 0.29 at the true onset and peaked at
  0.37, 81 ms later; 0.3 % of the 336 notes reached 0.6 and 7 % 0.5, and
  onset thresholds from 0.2 to 0.6 all gave F1 ≤ 0.17 on those stems.
- CLAP scores (cosine) on 1,500 Core Library one-shots (2026-11-23): text
  queries for sounds the folder holds topped at 0.40–0.77 ("808 bass"
  0.77, "closed hi-hat" 0.40), for sounds it lacks ("sustained synth
  lead", "warm pad", "long string ensemble") at 0.23–0.31, all drums; a
  sound's nearest others at 0.75–0.95. `ml.WEAK_SCORE` warns below 0.35
  (text) and 0.6 (example). Embedding 10 texts took 10.9 s, mostly start-up.
- `ml.run` runs one job at a time per server (a lock per event loop; a
  queued call reports what it waits for) and the worker lowers its own
  priority (`os.nice(10)`).
- Tests mock `ml.run` (and test the protocol with the worker's `echo` task);
  `ABLETON_MCP_ML_TESTS=1` adds two runs with the real Basic Pitch model (an
  A4 and its bends; a driven 808 line the default preset must find).

## Replica mode: Simpler and Drum Rack pads, measured in Live 12.4.6

Measured in Live 12.4.6 (Trial) on 2026-10-19 with Remote Script 2.1.0 and
this branch's `build_tracks`: values read back, and `capture_stems`
recordings at the audio device's 48 kHz.

- `SimplerDevice.replace_sample(path)` loads a file into a Simpler just
  inserted with `insert_device` (no sample yet): it returns None within
  ~13 ms and `sample file_path` reads back the exact path (`build_tracks`
  waits up to 2 s for it, then falls back to the browser). Unlike the
  browser, it reads the file again: after the file is overwritten on disk,
  `replace_sample` with the same path, or a new Simpler on it, plays the
  new content, while a pad loaded through the browser by the same name
  still plays the old one (see "Loaded samples stay loaded" above).
- Drum Rack pads without the browser: `RackDevice.insert_chain()` returns
  the new DrumChain (`build_tracks` finds new chains by comparing the
  rack's chain ids instead), which starts on `in_note` 36 with `out_note`
  60, not on −1 (All Notes) as in `fake_live`; `build_tracks` sets
  `in_note` either way. `Chain.insert_device("Simpler")` and
  `replace_sample` then load the pad, and the chain takes the file's name;
  `DrumPad.delete_all_chains()` ran on a pad whose file Live refused
  before the browser loaded it. Two chains on one note both sound (the pad
  reads "Multi").
- Defaults differ by route: a Simpler made with `insert_device` (a
  track's, or a pad's built by path) is Classic, with its filter on and
  "Vol < Vel" 35 %; a pad loaded through the browser is One-Shot, with the
  filter off and 45 %. Both start at Volume −12 dB. `playback_mode` set as
  0 or 1 (classic, one_shot) reads back.
- `Sample.start_marker` counts sample frames of the file: 441 and 4410
  moved a 44.1 kHz file's onset by 10.000 and 100.000 ms ("S Start" still
  showed 0.00 %).
- Transpose (±48) keeps whole semitones: a fraction is truncated toward
  zero (11.99 → 11, −11.99 → −11), so 11.99 plays 1100 cents up. Detune
  keeps whole cents from −50 to +50: a number is truncated (11.5 → 11,
  49.9 → 49; 60 is refused), while display text goes to the nearest value
  (`"11.5 ct"` → +12). Played pitches follow the stored values within
  0.002 cents.
- Velocity sets a Simpler's level: dB = 0.4 × A × (velocity − 70) − 0.096
  over its Volume, A being "Vol < Vel" as a fraction (within 0.002 dB over
  6 amounts × 18 velocities; fractional velocities follow it too). So
  velocity 70 plays at the Volume (less 0.096 dB) whatever the amount, and
  velocity 127 adds 7.9 dB at 35 %, 10.2 dB at 45 %. Volume set by the
  display "0.0 dB" lands on −0.005 dB.
- A choke group cuts the sounding pad with a linear 1 ms fade from the new
  hit's onset. Groups 0 (none) to 16 are accepted; 17, 99 and −1 are
  refused ("Invalid choke group.").
- Simpler's "Snap" (on by default) stops a sample 10–30 ms before its end
  with a short fade, the same on every hit (a kick 28.8 of its 163.5 ms, a
  hat 9.9 of 71.4); with Snap off it plays to the last frame.
- Glide, with one voice and overlapping notes, slides linearly in
  semitones over exactly the Glide Time (29.95, 59.95, 119.5 and 249.5 ms
  for 30, 60, 120 and 250 ms) for intervals of 2, 5, 7 and 12 semitones
  alike, without restarting the sample; notes 25 ms apart jump (in
  Portamento too).
- Timing: a note starts on the first device sample at or after its time
  and sounds 0.469 samples later (the default filter's group delay), the
  same in three passes; a recording adds no offset of its own (an
  Arrangement click lands 0.016 samples from its beat). The drum hits of a
  5-track build landed within 0.04 ms of their times (mean +0.017 ms).
- Every new `build_tracks` option passed and read back: `sample_path` on a
  Simpler and on pads, `playback_mode`, `start_marker`, `choke_group`,
  `warping: false`, `"Detune": "-12 ct"` in `settings`, `signature`
  ("7/8"), the browser fallback for a file Live refuses (a Simpler's
  `loaded_by_name` carries Live's message, a pad's says "the Simpler holds
  no sample"), and the refusals before anything is built (a relative or
  missing path, choke group 17, playback mode "loop", a negative start
  marker, 7/9; no track was created). The 5-track build (an 808 Simpler
  and Drum Rack pads, by path) took 7.7 s with no fallback. The version
  check (`Application.get_major/minor/bugfix_version()`) let path loading
  run.

Measured again on 2026-10-29 (Live 12.4.6 Trial, 48 kHz device) in
reference-dissector's two live checks, which built 8 plans and a song with
2.2.0's `build_tracks` (pads through the browser, so One-Shot; the 808 a
Simpler made with `insert_device`, so Classic) and recorded each pad and
the 808 with `capture_stems`:

- **One-Shot pads hit again** cut the sounding hit with a straight-line
  fade over 1 ms and start the new one 1.000 ms late (median of 391 hits on
  18 pads; 98 % within 0.95–1.05 ms). A hit that comes after the file (or
  Snap) has ended starts on time.
- **The Classic 808 Simpler** releases over its default Ve Release of 50
  ms: −1.683 dB a millisecond, −40 dB 23.8 ms after the note's end, silent
  (−60 dB) at 35.7 ms. A note after one of its own pitch restarts the voice
  0.94–1.05 ms late. Its filter (on, 22 kHz) delays each note by 0.13–0.46
  ms, more for lower notes, and cuts upper harmonics.
- **Simpler loses top end** against the file it plays: about −0.14, −0.32
  and −0.9 to −2.5 dB at 4–8, 8–12 and 12–20 kHz for a 48 kHz file at
  Transpose 0; 44.1 kHz files lose more (−1.6 to −1.8 dB at 12–16 kHz,
  −3.1 to −3.7 dB at 16–18 kHz).
- **Volume by display text** is held a display step low: "-6.6 dB" reads
  back −6.605 dB, "-14.9 dB" −14.95 dB; hits play the held value within
  0.003 dB.
- **By file name**, the browser's first hit is what loads: a name two files
  shared put an 808 on an open-hat pad, and a name the browser never listed
  ("3 .wav") made `build_tracks` raise after its 40 s wait, leaving the
  tracks already made in the set. `sample_path` does not go through the
  browser.
- **Cues land on the Arrangement's grid**, which depends on its zoom:
  `set_or_delete_cue` at beats 9, 21 and 33 made cues at 8, 20 and 32 in
  4/4 and at 12, 24 and 36 in 3/4 at the default zoom, and
  `place_locators` reported them as failed; after four `zoom_view` steps
  in, beat 9 was placed exactly. `place_locators` therefore shows the
  Arrangement and sends 30 `zoom_view(3, "Arranger", False)` (2 zooms out)
  before it places cues, then zooms out as many; a cue Live still puts
  elsewhere is deleted (`delete_strays`, the playhead read back before
  each toggle). Checked in Live 12.4.16
  on 2026-11-16 in an empty set: a 3/4 `build_from_blueprint` (0.9 s)
  placed locators on 0, 9, 21, 33 and 45 exactly (`ok`, no
  `locators_moved`), and `create_locator` with the Session view in front
  placed 15 and 40.5 exactly. A cue placed afterwards without zooming
  (beat 39, a bar line) landed too, so the zoom left behind was not
  measured. Nothing in the LOM reads the zoom.
- **Names made only of digits** (Live 12.4.16, 2026-11-16, script 2.1.0
  with 2.4.2's server): `set_track_mixer(track="808")` muted the track
  named "808" and no other, and `set_track_routing(input_type="808")` set
  an audio track's input to the audio track "808" (a MIDI track without an
  instrument is not among the input types).
- **Eight clients at most.** The script serves `MAX_CLIENTS` (8)
  connections; a ninth is closed at once ("Refusing a connection" in Live's
  log), which the client sees as "Connection reset by peer". Each MCP
  server holds one, so eight sessions with the plugin fill it.

Measured on 2026-11-14 (Live 12.4.16 Trial, 48 kHz device; Live's own log:
12.4.16 first ran that day at 16:02, 12.4.6 last on 2026-11-08) by
reference-dissector's `live-build`, which builds a plan through
`build_from_blueprint` (Simplers by path), against its `live-build
--direct` (pads through the browser), each recorded with `capture_stems`:

- An 8-bar excerpt (8 tracks, 133 notes, 4.4 s) and PIRAMIT's whole plan
  (10 tracks, 1,881 notes in one `build_tracks` call, 7.1 s; `--direct`
  7.6 s) built with clean read-backs: every Simpler held its file (4 and
  11), 13 of 13 pad Volumes held as numbers (none needed the text), every
  locator placed, 4/4 read back.
- **A Simpler made with `insert_device` starts with a 0.1 ms Fade In**; a
  pad the browser loads starts at 0 (Fade Out 0.1 ms on both). It was the
  only audible difference between the two builds: 4 samples at 48 kHz at
  each kick and hat onset (the drum track's residual −68.4 dB). With
  "Fade In" set to 0 (refdissect's blueprints do) every track of both
  plans recorded equal to `--direct`'s sample for sample.
- Every pad's chain plays at C3 (60), built by `insert_chain` or by the
  browser. A pad made by path differs from the browser's in what a
  One-Shot pad with no modulation does not play (voices 6 against 3; LFO
  and pitch envelope on with no amount; envelope times).
- The Remote Script served 4 clients; the build's connection was not
  refused.

Measured on 2026-11-23 (Live 12.4.16 Trial, empty set, click tracks made
at 48 kHz) for the Arrangement and audio clip tools:

- **Copies into the Arrangement** (`duplicate_clip_to_arrangement`) are as
  long as the clip's `length`, which is `loop_end - loop_start` looped or
  not: an unlooped Session clip with end_marker 16 and loop_end 64 was
  copied 64 beats long. With the loop off, start_marker follows
  loop_start (set alone it stayed) and end_marker is independent (turning
  the loop off copies loop_end into it). loop_end set to 6, the copy made
  and loop_end put back, all in one batch, gave a 6-beat copy keeping that
  loop (`sized_copy`). An Arrangement clip works as the source (its span).
  A copy covering a clip replaces it; one laid over part of an audio clip
  splits it like a MIDI one (later parts' start_marker 17, 32).
  `Track.delete_clip(clip)` deletes an Arrangement clip. `cut_ranges`,
  `build_arrangement` and the gap transition build on these.
- **An unlooped Arrangement clip ends at its loop_end** (16 → 10 ended it 6
  beats earlier; an unwarped one's loop_end is in seconds, 40 → 4 made 80
  beats 8); a looped one keeps its span (loop_end changes only the loop),
  and turning its loop off keeps the span (loop_end becomes its end). The
  span changes after the command: a copy of a clip trimmed in the same
  batch took its old length (`trimmed_copies` uses three commands).
- **The playhead follows new Arrangement clips**: while stopped,
  `duplicate_clip_to_arrangement`, `create_midi_clip` and
  `Track.create_audio_clip` move `current_song_time` to the new clip's
  start (deleting a clip leaves it). `playhead_put_back` /
  `keeping_playhead` put it back.
- **New audio clips are warped at a guessed tempo**: looped, with three
  warp markers (`beat_time`, `sample_time` in seconds): the file's start,
  its end, and a shadow marker 1/32 beat later that `move_warp_marker`
  refuses ("The shadow marker can't be moved."). Click tracks of 8 s and
  19.2 s made at 120 and 100 BPM came in right (16, 32 beats), a 40 s one
  at 120 BPM as 96 BPM (64 beats), and a 32 s one (8 s of clicks, then
  silence) right. `move_warp_marker(64, 16)` moved the end marker to beat
  80 (the file at 120 BPM) without touching the loop or markers; setting
  end_marker and loop_end to 80 then gave an 80-beat clip.
  `remove_warp_marker(beat_time)` works; `add_warp_marker` wants a
  WarpMarker object (a dict is refused), so the script cannot add one.
- **Warping off** turns the loop off and puts loop times in seconds
  (loop_end 40). A Session clip keeps the length it had (64 beats, from
  the guess; loop_end 40 → 39 → 40 left it 64), so its copies last 64 beats
  (32 s of the 40 s file); an Arrangement clip ends at the file's length
  at the set's tempo (80 beats), read back from the next command on.
  Warped straight to 80 beats first, then unwarped in the same batch, the
  Session clip was 80 beats and so was its copy (`fit_audio_clip`).
  Warping turned back on left only the start and shadow markers (the
  guessed tempo kept, end_marker 102.4), so nothing can move the file's
  end: such a clip cannot be fitted (`fit_audio_clip` says so).
- **Audio tracks take their first clip's name**: a track still named as
  Live named it ("4-Audio") became "4-click16_120" when an audio clip
  landed on it (Session or Arrangement), kept that while a clip was there
  and got "4-Audio" back when the last went; a name set by the user stays.
  Setting the name to "" gives Live's naming back. Address such tracks by
  index or id.

Measured on 2026-11-23 (Live 12.4.16 Trial, empty unsaved set, 44.1 kHz
output) for the recording tools:

- **Implicit arm records.** `Track.implicit_arm` (Push's auto-arm of the
  selected track) stayed False on every track here, selected or not, with
  or without an instrument (no Push attached); it can be set through the
  LOM, leaves no undo step, and a MIDI track with it set recorded a MIDI
  clip during an Arrangement render like an armed one. Selecting another
  track did not clear it. `record_section` and `record_automation` turn
  arm and implicit arm off for the take (`REC_ARMS`, read back before
  playing) and back on after the transport stopped, in a later command.
- **Recording files** follow the audio device's rate: at 44.1 kHz here
  (94,720 frames for 4.30 beats at 120 BPM), 48 kHz on the earlier
  device. An unsaved set records into `~/Music/Ableton/Live
  Recordings/<date time> Temp Project/Samples/Recorded/` (a saved one into
  its project's `Samples/Recorded`), named "<track> 0001 [<date time>].wav".
  The Arrangement clip's `file_path` reads empty while recording; the file
  was complete (size and frames final) 30 ms after `stop_playing`, its
  frames matching the clip's span, which runs past the asked end by the
  time the stop takes (4 beats asked, 4.30 recorded). A Session take
  (`fire(length)`) also pre-creates the next take's file ("… 0002", deleted
  with the track), and an armed audio track re-armed after a Session take
  got an empty (0-byte) take file that went when it was disarmed; no clip.
- **A scene render left the scene's clips playing** on the stopped
  transport (`playing_slot_index` 0, `back_to_arranger` True);
  `stop_all_clips(False)` stopped them and `back_to_arranger` set to False
  read back from the next command. A render leaves the selection on a
  neighbour of its deleted track; `record_section` sets it back.
- **Input channels**: setting a new track's `input_routing_type` to a
  track or return sets its channel to "Post Mixer" with it. In the batch
  that set three tracks' inputs, the later tracks still listed Ext. In's
  channels (1/2, 1, 2), so setting "Post Mixer" there failed (logged by
  the script) though the channel already was; the next command read Post
  Mixer on all three. `record_section` reads it then (`post_mixer`).
- **A scene with a tempo** (scene 1 at 160 BPM, `tempo_enabled`) changed
  the set's tempo for good when a render fired it; its 4 beats lasted
  1.5 s. `record_section` puts the tempo back and measures takes by the
  clip's beats.
- **Live without an audio device** (the user's sessions of 2026-11-22/23):
  Log.txt has "Audio In Out: Output Device: No Device", and its renders
  were 92-byte WAVs: a JUNK chunk, 24-bit stereo at 44.1 kHz, 2 frames.
  Live writes an "Audio In Out:" block at start and on each change of
  device, several in a row while switching (a transient "No Device" before
  the device): the last "Output Device" line is the current one
  (`doctor.audio_devices`).

Measured on 2026-11-23 (Live 12.4.16 Trial, empty unsaved set) for Remote
Script 2.2.0:

- **Parameter displays** (`str_for_value` at 0, ¼, ½, ¾ and 1 of the range
  of Drum Buss, Compressor, EQ Eight, Utility, Reverb, Glue Compressor,
  Auto Filter, Saturator, Delay and a track's mixer): dB ("-6.00 dB",
  "-inf dB" at the bottom of faders, sends, Trim, Threshold), Hz switching
  to kHz ("500 Hz" … "20.0 kHz"), ms switching to s ("459 ms", "1.36 s"),
  "%" (also "-100 %" … "100 %" and "0.0 %" … "400 %"), pan "50L" / "C" /
  "50R" (Utility Balance too), ratio "4.00 : 1" up to "inf : 1",
  Compressor's Expansion Ratio "1 : 1.25", "st" (Auto Filter Pitch), "°"
  (LFO Phase), note values "8", "5/16", "1/64", "1 / 16", "17  / 16", and
  numbers without a unit: Drum Buss Transients "-1.00" … "1.00" (range
  −1..1), EQ Q "0.10" … "18.0", Reverb's "0.50 " (with a trailing space),
  Glue Compressor's ".01" … "30" and Release ending in "A". Quantized
  ones list `value_items` ("Off"/"On", "Peak"/"RMS"/"Expand", "0 ms"/"1
  ms"/"10 ms"). Most parameters keep one unit family across their range,
  and `display_unit` (dB, Hz, ms, %, pan, ratio, st, °, note, morph,
  none) taken at five points tells which units a target may carry; a bare
  number is always read in display units. Not all: Glue Compressor's
  Release ends in "A", Roar's Blend is a split ("100 / 0", "75 / 25" …
  "0 / 100", read as its first number), Roar's FB Note shows note names
  (12..84, "C-1" … "C5", read as MIDI notes with C3 = 60), and the Filter
  Morph of Simpler, Operator and Wavetable restarts its number in each
  section ("0 lp/bp" … "75 lp/bp", "0 bp/hp" … "75 no/lp", "100 no/lp"),
  so it is read as section × 100 + number (bisecting on the number alone
  set "25 lp/bp" to "25 no/lp", in 2.1.0 too). Script 2.1.0 clamped
  "40 %" on Transients to 1.00, "100 Hz" on Drum Buss Damping Freq to 500
  Hz, "-inf dB" on Compressor Output to −36 dB, and read "1/16" as 1/64;
  2.2.0 refuses the first three (unit or range) and sets 1/16.
- **Values Live keeps and shows** (sweep of 629 continuous parameters of
  16 effects and 4 instruments, 7 displays each, 4,403 sets): Live stores
  a float32, so a value found right at the edge of a display ("12 dB" on
  Limiter's Input Gain) can be kept a hair below it and read back one
  step off ("11 dB"); `param_set_display` then moves it into the display
  it chose. Values a hair below zero show "-0.0 %", and Operator's
  Freq<Vel shows "-50" at −50 exactly but "-50.0" beside it, so a value
  that reads as asked is looked for next to the one found (rounded, then
  nudged). A fader with "-inf dB" at its bottom has a lowest number above
  it (Compressor Threshold −69.7 dB; a track's volume shows −69.565 dB at
  0.001 and −inf at 0.0001, a send −69.695 dB at 0.005 and −inf at
  0.003): a finite target below that sets −inf, as Live's faders do, and
  the result says so (`note`); 2.1.0 landed it on the lowest number.
  Server tools send such levels (`set_track_mixer(sends_db={"A": -80})`,
  `build_tracks` volume_db), so none may be refused. The value set is a
  rounded one or the middle of the chosen display ("C" pan 0.0, "25L"
  −0.5), and every check runs before the one write (a refusal writes
  nothing, so the journal's undo count stays right). A target past an end
  by at most one display step there (`_end_step`: the distance to the
  next display inward as read, or one unit of the end's own last digit
  when it is a plain number and that is larger: "100 ms" beside "99.9
  ms") sets the end with a `note` ("0 ms", "0.00 ms" on Attack, lowest
  0.01 ms), whatever its own precision. Stepped controls take half their
  last digit ("3.4" on an octave whose top is "+3 st"), and an end that is
  not a plain number takes nothing past it (Ti Rate "1/256", "7" past "4
  D"; "1:2.6" and "1:2.01" on Expansion Ratio, top "1 : 2.00"); "6
  kHz" on a 5.50 kHz top, "2 s" on 1.50 s and "0" on a 0.20 bottom are
  refused. Halfway between two displays goes to the larger
  number, with a note ("1" on Glue Release: ".8" and "1.2" → 1.2). Tools
  pass the notes on (`notes`, `setting_notes`, the summaries' steps). The
  crossfader shows "50A" … "0" … "50B" (A as negative, like L). Live
  does not always keep what is written as a float32 would: Shifter's
  LFO Amt St shows "0.38 st" from
  exactly 0.25, and 0.25 is kept as 0.24999998 ("0.37 st"), so a rounded
  value at a display's edge is not taken and a read-back that differs is
  stepped inside. A third sweep (890
  parameters, 17 points, the display and four Python-formatted variants
  each: 43,632 sets), left 101 flags, none a wrong set: 42 bare numbers on
  note-name parameters (refused, out of the MIDI-note range), 56 targets
  rounded past a range end by more than one display step (refused: "0 ms"
  on a 0.10 ms bottom, "2 s" on a 1.50 s top), nearest values on
  note-value displays ("2.400" → "4 T", i.e. 2.67; "0.400" → "6/16") and
  Glue Release's tie ("1" → 1.2).
- **More display forms** (a second sweep: 883 continuous parameters of 26
  effects and 6 instruments, 11 displays each, 9,713 sets): Resonators'
  I Note writes sharps as "♯" ("C♯3", read as "#"; "♭" as "b"); Hybrid
  Reverb's Ti Rate (0..29, stepped) shows note values with triplets and
  dots ("1/128 T" … "1/4 D", "1 T", "1", "1 D", "2 D", "4 D", read as
  the note value × 2/3 or × 1.5); its Vintage (0..4, not quantized) shows
  only names (Off, Subtle, Old, Older, Extreme), set by name. Compressor's
  Ratio ("4.00 : 1") and Expansion Ratio ("1 : 1.25") are told apart, so
  "1:4" on Ratio is refused. Before these, "C3" on I Note set E4 and
  reported success. `param_set_display` now refuses any result whose
  display is not the target (within its last digit) and does not have
  the target between it and the display one step away (`_reads_near`):
  a display it misreads can no longer set something else silently. With
  script 2.2.0 as installed on 2026-11-23, both sweeps (4,403 sets over
  629 parameters, 9,713 over 883) set every display read back exactly as
  asked. Displays outside those sweeps (other devices, plug-ins, Max
  devices) are untested.
- **Glue Compressor's Attack, Release and Ratio are stepped, not
  quantized**: `is_quantized` False, ranges 0..6, 0..6, 0..2, each step
  shown from its value up (Attack ".01" ".1" ".3" "1" "3" "10" "30",
  Release ".1" ".2" ".4" ".6" ".8" "1.2" "A", Ratio "2" "4" "10"); a
  value of 2.5 reads back 2.0 (Live floors it). With Release ending in
  "A" (Auto), script 2.1.0 refused every number ("not numeric");
  `_numeric_span` now bisects over the steps that show numbers and sets
  the others ("A") by name. `fake_live` models these (`_stepped`).
- **Browser listings are not lazy.** `BrowserItem.children` builds the
  whole vector: ~0.3 s for a folder of 100,000 samples (~3 µs an item),
  and `iter_children` (a `BrowserItemIterator`) costs as much to create.
  Reading the items afterwards is cheap: script 2.1.0 walked 120,000
  items in 0.86 s in all. `hasattr` reads the property, so `_lom_resolve`
  lists a folder twice (`… children 0` took 0.6 s there). The 100,000
  files (empty WAVs) showed up after ~2 minutes; files added later to an
  indexed folder did not show within 5 minutes, until it was renamed.
  `browser.samples` lists the User Library's samples as well. Walks
  therefore bound the time between slices: `BROWSER_WALK_SHARE` (0.5)
  gives a slice no more time than Live had since the last one, so a
  client that asks again at once (server 2.4.3: 120 ms slices) gets short
  slices, and `rest_ms` tells a client how long to wait for a whole one
  (this server: 60 ms slices; its time limits leave the waits out). The
  share is per walk: the script keeps up to four, and walks run side by
  side (several sessions searching) can take more of the thread together.
  A walk of 208,705 items then took 48 % of the main thread over 2.2 s,
  with other clients' commands at 8 ms median and 336 ms at most (one
  listing); with 2.1.0 another client waited 396 ms median, 710 ms at
  most, during a 120,000-item walk.
- **Ids alone:** `{"$ref": 4965354872}` (a bare id, as a number or
  digits) is read as `"id 4965354872"`; no root name is made of digits.
- **Server 2.4.3 with script 2.2.0** (the plugin's server, expecting
  2.1.0): handshake, capabilities, `get_session_overview`,
  `browser_search`, `run_batch` with `param_set_display` and the LOM tools
  work; its `doctor` reports the script as outdated (two warnings).

Not verified in Live (check each in a scratch set before claiming it):

- The start put back on stems by `separate_stems(to_tracks=True)` (the
  source's start marker, unwarped sources only) with real stems; the reset
  itself was checked on synthetic files in Live 12.4.16 (above).
- The rack rebuilt by name when `insert_chain`, `in_note` or
  `Chain.insert_device` fails (the rack is deleted with
  `Track.delete_device` and a new one gets every pad from the browser: no
  call deletes a single rack chain). Live 12.4.6 refused none of them, so
  only `fake_live` has seen it.
- Samples on Live before 12.4 (the browser, by file name).
- `playback_mode` slicing (2), and one_shot set by `playback_mode` on a pad
  built by path (overlapping hits were measured on One-Shot pads loaded
  through the browser, above).
- `audio_to_midi` on a warped clip: `Clip.sample_to_beat_time(frames)`
  minus `start_marker` (beats) as the note's beat in the new clip; on an
  unwarped clip `start_marker` in seconds; `Clip.sample_rate`, else the
  file's rate.
- `fit_audio_clip` on a clip Live warped with inner markers (the remake
  sessions saw 17 on a padded file): it removes them with
  `remove_warp_marker` before moving the last; only three-marker clips
  were measured. That clip envelopes play in a shortened copy rests on
  the copy keeping them (Arrangement envelopes cannot be read back).

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
- Sample paths (`FileRef`): `RelativePathType` 3 = relative to the Live
  Project (the folder holding "Ableton Project Info"): every project sample
  in the user's five Live 12.4.16 sets, "Samples/Imported/…"; 5 = a Live
  Pack (`LivePackName`, e.g. "Core Library" in the app bundle); 7 = Live's
  Builtin folder (IRs, MPE Control); 1 = relative to the set's own folder
  with ".." steps (seen on presets' refs, from the machine that made them);
  6 = Library (not seen there); 0 = none. `Path` is absolute on the
  machine that saved it: a moved project ("Dreyko Lead Project" → "Dreyko
  Lead", 2026-11-23) keeps the old one, so type 3 tries the project first.
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
- **Context cost.** Every tool's description and schema is sent to the model
  each session (69 core tools ≈ 15k tokens, down from 22k; with Phase 4's
  tools 76 ≈ 16.7k, measured 2026-10-11; the four reference tools add
  3,123 characters, ≈ 0.8k tokens, to the full profile only), and every
  result on every call.
  So: `register_tool` strips pydantic
  titles, `"default": null` and nullable `anyOf` from the advertised schema
  (`compact_schema`; validation still uses the signature); `to_json` writes
  compact JSON; notes go out as rows under `note_columns` (`note_table`, ~80%
  smaller than objects). Keep docstrings to what the model needs to call the
  tool right; tools a newer one replaces belong in the `full` profile.
- **Safety net.** `journal.py` gives each tool call an entry (a contextvar
  set in `register_tool`); `AbletonConnection.send_command` reports every
  command with its result, and `journal.undo_steps` counts the steps Live
  recorded by the rules under "Undo history" below (`journal.is_read_only`
  mirrors the script's READ_ONLY_COMMANDS/METHODS; a test keeps them equal),
  so `agent_changes(undo=N)` undoes exactly the last N calls. Parameter tools call `auto_snapshot` first (the last
  10 are kept; tests write them to a temp folder via conftest).
  `@tool(..., preview=True)` + a `preview` parameter: `PREVIEW` makes
  `prepare_clip`/`write_notes` no-ops and `send` refuse any change. Deletes
  take `go_ahead: Annotated[ElicitationResult[_Confirm], Resolve(ask_…)]`:
  the SDK runs the module-level resolver first and asks through elicitation
  in either protocol era (2026-07-28 has no back-channel, so `ctx.elicit`
  inside a tool fails there); resolvers must be module-level, because the
  SDK resolves the annotation strings with `get_type_hints` and silently
  drops unresolvable ones. A plain resolver return reaches the tool wrapped
  as `AcceptedElicitation(data=…)`.
- Generators read the set's meter and Key & Scale through `song_context()` /
  `resolve_key()`; explicit arguments win.
- Prefer composing `op_set` / `op_call` / `op_display` into one `batch(...)`
  call over several round-trips; return `batch_summary(...)`.
- Address tracks with `track_path()` (index, name, `master`, `return A`), devices
  with `device_path()`, clips with `clip_path()`; quote names via `selector()`.
  A LOM path takes every number for an index, so text made only of digits
  that an item is named ("808") goes through `named()`, which looks the name
  up first (one round trip); an integer is always an index. Devices by name
  go through `find_device`: the track's own devices, then its racks' chains
  level by level (`nested_devices`, a batched read per level). Digit-only
  text (`digits_device`) is a top-level name, else, if that index exists
  and a nested device has the name, an error; else the one nested device
  of that name; else the index. A path ("live_set …", "id N …") must
  be on the given track (`device_on_track`).
- A new audio clip's start: Live moved it to the file's first transient
  (21 ms to 3.6 s in, 2026-10-11) where warping went off. In Live 12.4.16
  (2026-11-16) a new clip starts at 0 while warped, also once its warp
  analysis is done (the .asd written, ~12 s for a 150 s file); warping
  turned off after that analysis moved the start (0 to 0.53), turned off
  with the start reset in the same batch it stayed at 0 after the analysis.
  Every path that creates one puts it back (`from_its_start`, inside
  `fit_audio_clip`: loop_start,
  then start_marker, after warping); the fake moves it at once
  (`FIRST_TRANSIENT`), a stricter Live, so a path that forgets is caught.
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

## Reference jobs and blueprints (run with the pinned refdissect and built in Live 12.4.16)

- refdissect runs as `uv run --isolated --no-project --python 3.12 --with
  "reference-dissector @ <archive of REFDISSECT_COMMIT>" refdissect …`
  (`reference_jobs.refdissect_command`), never imported: its memory (2 GB
  for an analysis), its Python (3.11+) and its pins (torch for Demucs) stay
  out of the server. Move the pin by changing `REFDISSECT_COMMIT` to a commit
  of reference-dissector's main; `ABLETON_MCP_REFDISSECT=<checkout>` runs a
  local checkout instead and `doctor` names which one is used.
- A job's steps are refdissect's own commands (separate, analyze, melodic,
  match, fit-808, plan, blueprint; inventory and index-library for the
  library). A missing subcommand (argparse's "invalid choice") skips an
  optional step instead of failing the job: `blueprint` exists only in a
  refdissect that writes this format.
- Records are written atomically and by one process at a time: the server
  before the runner starts and after it is gone (cancel), the runner in
  between. "lost" is never written; it is read from a running record whose
  runner is gone or that has not been written for `STALE_S`. The runner
  leads a session of its own, so its pid names the job's process group even
  after it dies (`killpg(pid)`); `is_runner` checks the pid's command line
  (`ps`), so a pid another process took is never signalled (Unix gives no
  process the id of a living group, so leftovers cannot hide behind one).
- `build_from_blueprint` runs inside the server on its connection (no ninth
  client), calls `build_tracks` once, then reads back. Its steps follow
  refdissect's `live-build --direct` as measured in Live 12.4.6 (meter
  before locators, Volume as a number then as text, choke groups on the
  chain, `voices` after the build). Checked in Live 12.4.16 on 2026-11-14
  through refdissect's `live-build` (below, under replica mode).
- refdissect's `live-build` (its `[live]` extra) imports `MCP_Server.server`
  after setting `ABLETON_MCP_PROFILE=full`, calls the tool in-process
  (`mcp.call_tool("build_from_blueprint", {"blueprint": <file>, "bars":
  "A-B"})`), calls `blueprint.excerpt(data, (a, b), base, out_dir)`
  directly for `--stems-out`, and reads the report's keys (`tracks`,
  `problems`, `locators`, `locators_moved` with `name`/`asked`/`live`/
  `deleted`, `checks.samples|volumes|choke_groups|voices|time_signature`
  with `want`/`live`/`same`, `volumes.held_by_text`/`not_held`, `files`,
  `excerpt`, `ok`). These are an interface: change one only with
  refdissect, and say so in the CHANGELOG.
- The jobs ran the pinned refdissect (f907dd2) for real on 2026-11-16, on
  an Apple M4: `index_sound_library` indexed 12,093 sounds in 33.8 s;
  `dissect_reference` took PIRAMIT (a 126 s mp3) apart in 442 s the first
  time (uv set up the environments, Demucs and Basic Pitch were fetched)
  and in 224 s on a copy afterwards, with the same plan (565 drum notes on
  11 pads, 95 808 notes, 553 and 499 melodic notes, 8 sections).
  `TYPICAL_S` and `INDEX_TYPICAL_S` are those runs. Then
  `build_from_blueprint(blueprint=<job id>)` built it in Live 12.4.16 in
  under 5 s with every read-back clean (12 samples, 11 pad Volumes as
  numbers, 8 locators exact, 4/4 and the tempo), and the four unwarped
  reference stems still started at 0 once Live had written their .asd
  files. Paths go to refdissect absolute (its steps run in the work or
  library folder).
- A rerun `plan` brings `blueprint` along; a blueprint file whose
  `meta.plan_sha1` no longer matches its `plan.json` is refused
  (`blueprint.plan_changed`).
- libsndfile writes the time into a float WAV's PEAK chunk: two writes of
  the same samples a second apart differ in bytes. Scaled copies and cut
  stems are therefore compared and named by their audio
  (`blueprint.audio_digest`), never by the file's SHA-1.
- A process group whose only member is its unreaped zombie leader still
  answers `killpg(pgid, 0)` (Linux) or refuses it with EPERM (macOS);
  `group_alive` asks `ps` for a member that is not a zombie.
- Tests stand a script in for refdissect (`tests/test_reference_jobs.py`:
  real processes, no uv, no models); conftest points the jobs and data
  folders at temporary ones. The eval case needs `ABLETON_MCP_PROFILE=full`.

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
  `fake_live.FakeSong`. Extend `MCP_Server/fake_live.py` when a tool touches
  new LOM surface; its undo follows `journal.py`'s rules, so keep both in step
  with what Live does.
- `tests/test_evals.py` runs a scripted correct solution of every eval case
  through the real server's tools against the simulator; a new case needs one
  in `SOLUTIONS`. The server takes Live's port from the session file
  (`connection.live_port`), which is how eval runs find their simulator.
- Give tools in a test a fake connection (`extended_tools.register(mcp,
  lambda: conn)`) or one that raises. The server's own connection reads
  `~/.ableton-mcp/live-session.json`, else port 9877, and so reaches a Live
  open on the machine; `test_evals.py` and `test_arrangement_tools.py` use it
  only after pointing `ABLETON_MCP_SESSION_FILE` at their simulator.
- Anything that depends on real Live behaviour (undo, routing objects, recording)
  must also be checked against a running Live before claiming it works.
