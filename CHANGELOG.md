# Changelog

## Unreleased

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
