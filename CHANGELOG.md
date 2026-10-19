# Changelog

## Unreleased

### Added

- `audio_to_midi` takes a `preset`: `melodic` (the default: onset 0.6,
  frame 0.3, 35 ms, melodia off), `chords` (the same with melodia on) or
  `legacy` (what it did before: 0.5, 0.3, 127.7 ms, melodia on), and
  `melodia_trick`, which it never passed (it was always on). On 63 synthetic
  renders (plucks, 32nd runs, leads, pads; with reverb, an 808 and a bleed
  loop), onset F1 went from 0.583 (legacy) to 0.687 (melodic), 0.706 with
  `lowest` just above the 808. legacy keeps only notes of 139 ms or more:
  on 32nd-note runs at 140 BPM its F1 was 0.36, melodic's 0.90 (4 clean
  renders through this server's worker). melodic needs an attack: it found
  3 of 16 pure sines with a 10 ms fade-in, chords 15.
- `audio_to_midi(bends=true)` lists the pitch bend of each bent note in
  cents, one value per model frame (11.6 ms). Basic Pitch 0.4.0 reads
  in-tune notes a third of a semitone sharp (+1 in 99.4–100 % of the frames
  of sines at 110, 220, 440 and 880 Hz); the values are corrected by that
  bin, after which in-tune and ±33-cent sines read 0 and ±33 cents. Live's
  API has no per-note pitch bend, so bends are returned, not written.
- `audio_to_midi` velocities follow each note's level: a one-semitone
  constant-Q band at the note's pitch, the loudest reading over its first
  60 ms (`audio_analysis.note_levels`); the loudest note gets 127 and the
  others 127 × 10^(dB below it / 40). Rank correlation with the true level
  (Spearman, Basic Pitch on 4 clean synthetic renders each), against 127 ×
  Basic Pitch's amplitude before: plucks 0.62 → 0.98, a legato lead
  0.71 → 0.99, triad pads 0.27 → 0.94. With reverb and detuned pad voices a
  similar measure ranked plucks at 0.81–0.83, a lead at 0.57 and pads at
  0.20. `legacy` keeps the old velocity.
- `audio_to_midi` places notes where the source clip plays them: a warped
  clip through Live's own warp (`Clip.sample_to_beat_time`, in batches), an
  unwarped one from its start marker; notes the clip never plays are left
  out. `offset_beats` moves every note, and `timing` in the result says
  what applied (or that Live could not map a warped clip). Checked against
  the fake Live, not yet in Live. `lowest` and `highest` also take Hz
  ("82.4 Hz").

### Changed

- `audio_to_midi` defaults to the `melodic` preset and to velocities from
  each note's level; `preset="legacy"` gives what it gave before.
- The tempo estimate of the audio analysis tools is found between frames: a
  parabola through the autocorrelation peak at one beat, then at 2 to 32
  beats. Whole-frame lags are 3.9 BPM apart at 140 BPM (44.1 kHz), and a
  137 BPM click track read 136.0; it reads 137.0 now. Click tracks from 70
  to 175 BPM at 44.1 and 48 kHz came within 0.01 BPM wherever the tempo
  octave was right; the octave choice is unchanged (150 BPM still reads 75).

### Documentation

- `capture_stems` says its recordings are at the audio device's sample rate
  (often 48 kHz). Read as 44.1 kHz, a capture drifts by 9 %.
- CLAUDE.md: recordings' sample rate; Live keeps playing a sample it loaded
  after the file is overwritten on disk, until it restarts (found in
  beat-lab).

## 2.2.0 — 2026-10-17

Remote Script 2.1.0: reinstall it (`ableton-mcp-install-script`) and restart
Live. Checked in Live 12.4.3: the script loads without errors, recordings
start at `start_beat`, `build_tracks`, browser ids, clipping reports and
`watch_live` events work as before.

### Removed

- The telemetry and dataset recorder inherited from upstream
  (`MCP_Server/telemetry*.py`, `MCP_Server/dataset/`, ~3,200 lines, the
  `dataset` extra with `supabase`). It was inactive in this fork (it needed an
  unshipped `MCP_Server/config.py`), but it read a Supabase service key from
  the environment and its six tools (`set_dataset_consent`, `submit_intent`,
  `rate_last_action`, `prefer_candidate`, `reject_last_action`,
  `record_audition`) were in the source. The server now reads no credentials.
- The Remote Script's passive listeners (on tempo, signature, playback and
  every track, clip slot and mixer) and `drain_passive_events`: only the
  dataset recorder read them, and they ran in Live all the time.

### Fixed

- `rack_control` (info) crashed on a device that does not report
  `visible_macro_count` (an error dict was added to a number).

### Tests

- Every tool is now called by at least one test (18 were not): the original
  full-profile tools run through the real Remote Script on the fake Live.

## 2.1.2 — 2026-10-17

### Added

- `build_tracks` builds Drum Racks (a sample per pad, with settings), audio
  tracks with clips that start at the file's start (Live's warp analysis
  otherwise moves them to the first transient), mute, and Simplers with
  warping off. A sample named with its extension is found by exact name,
  waiting up to `sample_wait_s` for Live to list a file just written. Checked
  in Live 12.4.2: reference-dissector's PIRAMIT plan (an 808, six drum pads,
  675 notes, five stems) builds in 4.9 s, every setting as planned.

### Fixed

- Recordings now really start at `start_beat` in Live 12.4.2. 2.1.1 moved the
  playhead before `continue_playing`, but `continue_playing` resumes where
  Live last stopped whatever the playhead says; recording now sets
  `Song.start_time` and calls `start_playing` (checked in Live: a section at
  beat 32 gives a 4.07 s file with its first hit at 0.9 ms).

## 2.1.1 — 2026-10-17

Remote Script 2.0.1: reinstall it (`ableton-mcp-install-script`) and restart
Live for the browser fix; everything else works with 2.0.0.

### Added

- `build_tracks` builds MIDI tracks from a spec in one call: instrument (a
  device by name or a sample from the browser), settings, effects with
  Compressor sidechains (routed once every track exists), fader and pan, and
  Arrangement clips with their notes. Problems (a sidechain source Live does
  not offer, a setting that failed) are reported per track.
- `capture_stems` lists stems that reached full scale (`clipped`, and
  `clipped_samples` per stem); `mix_doctor` says so in its findings.

### Fixed

- `render_and_analyze`, `capture_stems` and `mix_doctor` recorded from
  wherever the playhead was, not from `start_beat`: Live applies a playhead
  move only after the command that made it, and the move and
  `continue_playing` went in one batch. The playhead is now moved first and
  read back.
- `audio_to_midi` and the other ML jobs failed on results over 64 KB
  ("Separator is found, but chunk is longer than limit"), such as a dense
  3-minute stem's notes.
- `audio_to_midi` returned no notes when `lowest` was under A0 (27.5 Hz,
  Basic Pitch's lowest note); the floor is raised to it.
- Browser ids went stale during long searches ("Unknown id"): results shared
  the 4096-entry object registry and a walk of ~12,000 samples evicted them.
  They now have a registry of their own (Remote Script 2.0.1).
- `edit_clip_notes` says note times count from the clip's start (also for an
  Arrangement clip placed later); `apply_device_chain` says Gate's sidechain
  cannot be set from a script.

### Changed

- `manage_track` lists its actions (`create_midi`, `create_audio`,
  `create_return`, `delete`, …) as an enum in its input schema, so a client
  or a registry sees what it can do without reading the description. An
  unknown action is refused with the list of valid ones; actions are now
  matched exactly (lowercase).

### Fixed

- The .mcpb manifest summed up `lom_call_method` as "Call any LOM method,
  e.g.": a tool's summary no longer ends at "e.g." or "i.e.".

## 2.1.0 — 2026-10-11

The Remote Script is unchanged (2.0.0): no reinstall or Live restart needed.

### Added

- **Safety net.** `agent_changes` lists what the assistant changed in this
  session and, with `undo=N`, takes back exactly the undo steps those changes
  made, counted the way Live 12.4 records them (parameter and other changes
  are separate steps; unchanged values, transport, selection, arm and solo
  make none). Parameter changes leave an automatic snapshot (the last 10),
  which still works after the user edited the set. The seven generators,
  `transform_clip_notes` and `apply_midi_tool` take `preview=true`. Deleting a
  track, scene, clip or device asks the user first in clients with MCP
  elicitation, in both protocol versions.
- **Simulator.** `ableton-mcp-simulator` runs the real Remote Script over an
  in-memory Live set (with an undo history that follows Live's rules), so the
  server and its tools work without Ableton; `--state-file` keeps the set as
  JSON.
- **Eval suite** (`evals/`): 32 `claude plugin eval` cases, each against a
  fresh simulated set, graded by deterministic checks of the resulting set
  (plus the reply where wording matters). A scripted correct solution of
  every case runs in the tests.

- **Arrangement automation and transitions.** `write_arrangement_automation`
  puts automation over a stretch of the Arrangement on MIDI tracks at once
  (the notes there go into a Session clip with the envelope, laid back over
  the stretch: Live 12.4 draws envelopes only on Session clips, and a copy
  keeps them). `record_automation` records track automation on any track,
  the main one included, by moving the parameter while the Arrangement
  records. `design_transition` builds risers, reversed crashes, drum fills,
  gaps before a drop and high-pass sweeps. All verified on Live 12.4,
  including undoing each with `agent_changes`.

- **Audio generation on this computer.** `generate_audio` makes a sound or
  a loop from a text prompt with MusicGen small (in its own environment,
  like the other models) and puts it in the set: a clip on an audio track,
  a Drum Rack pad, or a Simpler. The set's tempo and key go into the prompt;
  the files land in the User Library. MusicGen's weights are CC-BY-NC 4.0,
  which the tool says (non-commercial use).
- **Project memory.** `memory` keeps notes about a song inside the Live set
  (`Song.set_data`, saved with the set, no undo step) and the user's
  preferences in `~/.ableton-mcp/memory.json`; a keyed note replaces the
  earlier one. `get_session_overview` shows them, so a later session (or
  another client) starts from what was decided.
- **MIDI Tools in Live's clip view.** `install_midi_tools` adds three Max for
  Live MIDI Tools that run inside Live, without the server: Usul and Makam
  Melody (Generate) and Makam Snap (Transform). Their JavaScript is a port
  of the server's generators (the usuls match note for note) and lives in
  the device. Verified in Live 12.4 with Max 9.1.4.

### Changed

- **Less context per session and per call.** The core profile's tool
  descriptions and schemas take about 15k tokens instead of 22k: schemas lose
  pydantic's titles and null defaults, long descriptions are tighter, and six
  original tools that newer ones replace (`load_instrument_or_effect`,
  `get_browser_tree`, `get_browser_items_at_path`, `load_drum_kit`,
  `get_remote_script_info`, `get_arrangement_clips`) moved to the `full`
  profile. Results are compact JSON (a quarter to a half smaller), and notes
  come as rows under `note_columns` (an 8-bar melody: 885 characters instead
  of about 4,100).
- The server takes Live's port from the session file the Remote Script
  writes (`ABLETON_PORT` still wins).
- Makam melodies arrive on the karar through the makam's first tetrachord
  (G F# Eb D in Hicaz on D), so its characteristic intervals are always
  heard; before, one 4-bar Hicaz melody in five never played its F#.

### Fixed

- While a client watched a resource (Claude Code does), the background
  watcher's 2-second long polls held the connection the tools use, so a tool
  call could wait up to 2 s per command. The watcher now has a connection of
  its own. Found by the eval suite (a recorded fade lost whole seconds).
- A server started before Live refused `create_locator` and the other tools
  that check the Remote Script's capabilities until it was restarted: the
  capabilities are now taken from every connection's hello.

## 2.0.0 — 2026-10-11

### Breaking

- **Remote Script 2.0.** Commands run on Live's main thread (about 10 ms
  each instead of ~410 ms), one JSON object per line with request ids, and a
  per-launch session token. Reinstall the script (ask the assistant to run
  `install_remote_script`, or `ableton-mcp-install-script`) and restart Live;
  servers older than 2.0 cannot talk to it.
- **MCP Python SDK 2.x** (`mcp>=2.3`), Python 3.10 or newer.
- **Tool profiles.** The default `core` profile has one tool per job; the
  original tools that newer ones replace need `ABLETON_MCP_PROFILE=full`.

### Added

- **Working with Live:** one undo step per tool call; `doctor` and
  `install_remote_script`; Live's changes pushed to subscribed clients
  (`ableton://events`, `ableton://session/overview`, both MCP protocol
  versions); progress notifications for long tools.
- **Writing music:** generators that follow the set's meter and Key & Scale
  (chords, drums, bass, arpeggios, melodies, Euclidean rhythms); harmony
  analysis; Live 12 MIDI Tools equivalents; Turkish makams with `.ascl`
  tunings and usuls; `build_arrangement`; MIDI file import and export; drum
  kits from one-shots; browser search, loading and preview.
- **Mixing and listening:** `measure_levels`, `render_and_analyze`,
  `capture_stems`, `mix_doctor` (masking between parts), `compare_to_reference`
  with EQ Eight settings, snapshots with A/B, ready-made device chains.
- **Panels** (MCP Apps) for 22 tools: mixer and Session grid, meters, masking
  heat map, reference curve, snapshots, piano roll, drum pads, makam keyboard.
- **Live Set files:** `read_live_set` (and comparing with a backup),
  `find_live_sets`, `import_from_live_set`, without opening Live.
- **Machine learning,** each in its own environment set up by uv on first
  use: `separate_stems` (Demucs), `audio_to_midi` (Basic Pitch),
  `find_similar_sounds` (CLAP).
- **Max for Live analyzer:** `install_analyzer` puts a generated device on a
  track; `read_analyzer` reports octave bands, loudness, correlation and width
  while the set plays.
- **Extensions SDK add-on** (`extension/`, Live 12.4.5+ Suite beta; not yet
  run inside Live): `get_live_selection`, `confirm_in_live`,
  `render_track_audio`.
- **Distribution:** a Claude Desktop extension (`.mcpb`), a Claude Code plugin
  with five skills, CI, and GitHub releases from version tags.
