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
| `MCP_Server/live_updates.py` | Pushes `ableton://events` / `ableton://session/overview` updates: `resources/subscribe` (2025-11-25) and `subscriptions/listen` (2026-07-28). Its long polls use a connection of their own (on the tools' one every command would queue behind them). |
| `MCP_Server/ui/` | MCP Apps panels (`ui://` HTML resources) registered through the SDK's `Apps` extension. |
| `MCP_Server/prompts.py` | MCP prompts (workflows) and the streaming loudness targets. |
| `MCP_Server/doctor.py` | Setup diagnostics behind the `doctor` tool and `ableton-mcp-doctor`. |
| `MCP_Server/midi_files.py` | Standard MIDI File read/write (mido) for clip import/export. |
| `MCP_Server/companion.py` | Client for the Extensions SDK add-on: finds its session file, sends one command. |
| `extension/` | AbletonMCP Companion, the Extensions SDK add-on (TypeScript): share selection, confirm dialog, pre-FX render. |
| `MCP_Server/m4l_device.py` | Builds the AbletonMCP Analyzer (.amxd) as bytes: the Max patch as JSON. |
| `MCP_Server/midi_tools.py`, `MCP_Server/midi_tools/*.js` | Max for Live MIDI Tools (Usul, Makam Melody, Makam Snap) for Live 12's Generate/Transform tabs: v8 code in a `v8.codebox`, tables written from `turkish.py`/`music.py`; `install_midi_tools`. |
| `MCP_Server/analyzer.py` | UDP/OSC listener for the analyzer's messages; levels, bands, correlation, approximate LUFS. |
| `MCP_Server/ml.py` | Runs machine-learning jobs (Demucs, Basic Pitch, CLAP, MusicGen) in uv-built environments; Basic Pitch's presets, octave-ghost removal, bends in cents, velocities from levels, notes placed in beats; the CLAP sound index; generation prompts and folders. |
| `MCP_Server/ml_worker.py` | The job side: runs in those environments, imports nothing from `MCP_Server`, talks JSON lines. |
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
| `MCP_Server/fake_live.py` | In-memory fake of the LOM (objects carry `_live_ptr`, listeners, Live 11 note API, enums, an undo history by Live's rules, the recipes' devices, Drum Rack chains, `replace_sample`, Live's version: 12.4.6). Tests and the simulator use it. |
| `MCP_Server/transitions.py` | Pure Arrangement logic: unrolling split/looped clips, fills, risers, gaps, automation curves. Tools build on it with `rewrite_range` (extended_tools). |
| `MCP_Server/simulator.py` | `ableton-mcp-simulator`: the bundled Remote Script over `fake_live`, served like Live (port + session file); writes the set as JSON and an eval case's verdicts. |
| `evals/` | `claude plugin eval` suite: 32 cases against the simulator, graded on `checks.json` (see `evals/README.md`). |
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
  device (`PARAMETER_RENAMES`); recipes keep the 12.3 names.
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

Not verified in Live (check each in a scratch set before claiming it):

- The rack rebuilt by name when `insert_chain`, `in_note` or
  `Chain.insert_device` fails (the rack is deleted with
  `Track.delete_device` and a new one gets every pad from the browser: no
  call deletes a single rack chain). Live 12.4.6 refused none of them, so
  only `fake_live` has seen it.
- Samples on Live before 12.4 (the browser, by file name).
- `playback_mode` slicing (2), and one_shot on overlapping hits of one pad.
- `audio_to_midi` on a warped clip: `Clip.sample_to_beat_time(frames)`
  minus `start_marker` (beats) as the note's beat in the new clip; on an
  unwarped clip `start_marker` in seconds; `Clip.sample_rate`, else the
  file's rate.
- A 44.1 kHz audio device: every recording ran at 48 kHz.

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
- **Context cost.** Every tool's description and schema is sent to the model
  each session (69 core tools ≈ 15k tokens, down from 22k; with Phase 4's
  tools 76 ≈ 16.7k, measured 2026-10-11), and every result on every call.
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
