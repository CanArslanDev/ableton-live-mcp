<div align="center">

# Ableton Live MCP

**Give an AI assistant the whole of Ableton Live, not just a handful of commands.**

An MCP server and Live Remote Script that expose the complete Live Object Model,
plus music-theory generators and an audio "ear" that measures what the set
actually sounds like.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Live 11 / 12](https://img.shields.io/badge/Ableton%20Live-11%20%7C%2012-black)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)

</div>

Built on [ahujasid/ableton-mcp](https://github.com/ahujasid/ableton-mcp) (MIT),
whose history is kept in this repository. This fork adds a generic bridge to
the Live Object Model, about 40 new tools, workflow prompts, and fixes found
while testing against Live 12.3.

---

## What you can ask for

- *"Give me an overview of the set, then put a i–VI–III–VII progression in A minor on the pad track with smooth voicings."*
- *"Write a trap beat on the drum rack, swing it a little, and humanize the hats."*
- *"Pull the bass down 3 dB, send 20% of it to reverb, and sidechain-style duck the pad with a volume envelope."*
- *"Set the filter cutoff on the Operator to 800 Hz and draw a 2-bar sweep up to 5 kHz."*
- *"Record bars 9–16 of the master and tell me how loud it is and whether the low end is muddy."*
- *"Watch what I change in Live for the next minute and tell me what I did."*
- *"What chords are in the Keys clip? Write a walking bass that follows them."*
- *"Play the drop and tell me which tracks are too hot."*
- *"Put a vocal chain on the Vox track and duck the bass from the kick."*
- *"Build me an 808 kit from my own samples and write a trap beat on it."*
- *"Aksak usulünde bir darbuka ritmi ve Hicaz makamında bir melodi yaz."*
- *"Save a snapshot, make the mix brighter, then let me A/B the two."*
- *"Which parts are fighting in my drop? Compare it with this reference track."*

## Features

| Area | Tools |
|---|---|
| **Any LOM object** | `lom_get_object`, `lom_set_property`, `lom_call_method` reach anything Max for Live can, by path |
| **Orientation** | `get_session_overview`: tracks, groups, mixer, sends, devices, clips, scenes, cue points, key/scale |
| **Batching** | `run_batch` applies many commands in one round-trip on Live's main thread |
| **Mixer & tracks** | `set_track_mixer` (dB, pan, sends, mute/solo/arm, monitoring), `manage_track`, `set_track_routing` |
| **Scenes & clips** | `manage_scene`, `set_clip_properties` (loop, markers, warp, gain, pitch, launch), `manage_clip` (duplicate, crop, quantize, Arrangement clips) |
| **Notes** | `read_clip_notes`, `edit_clip_notes` (probability, velocity deviation, release velocity, edit by note id), `transform_clip_notes` (transpose, snap to scale, quantize, humanize, reverse, invert, legato, …) |
| **MIDI Tools** | `apply_midi_tool` does what Live 12's MIDI Transformations do — strum, ornament (flam, grace notes), chop, connect, time warp, velocity shaper, recombine, span, arpeggiate — in place, keeping note ids |
| **Song structure** | `build_arrangement` lays Session clips out as sections (scene minus some tracks, or clips per track), repeats loops, cuts MIDI exactly and names a locator at each section |
| **Automation** | `write_clip_automation` draws envelopes for any device or mixer parameter, from points or shapes; `write_arrangement_automation` does it over a stretch of the Arrangement on MIDI tracks, at once; `record_automation` records track automation on any track (audio, returns, the main track) by playing the stretch while Live records |
| **Transitions** | `design_transition` builds what leads into a section: a riser, a reversed crash swelling into the downbeat, a drum fill (snare roll, toms, build), a breath of silence before the drop, a high-pass sweep |
| **Devices & racks** | `set_device_parameter_by_name` accepts what Live displays (`"800 Hz"`, `"-6 dB"`, `"25L"`, `"Lowpass"`), `manage_device`, `rack_control` (macros, variations), `get_drum_pads` |
| **Song & transport** | `set_song_properties` (time signature, swing, loop, Live 12 key/scale, quantization), `transport` (undo/redo, capture MIDI, cues), `select_in_live` |
| **Generators** | `music_theory`, `generate_chord_progression`, `generate_drum_pattern` (maps your Drum Rack pads by name), `generate_bassline`, `generate_arpeggio`, `generate_melody`, `generate_euclidean` (Euclidean rhythms per pad) — they follow the set's time signature and, unless told otherwise, its Key & Scale (Live 12) |
| **Turkish makam & usul** | `turkish_makam` gives 24 makams in the Arel-Ezgi-Uzdilek system (steps in commas, karar, güçlü) and writes an `.ascl` tuning that plays their true comma pitches in Live 12; every generator takes a makam as its scale (`scale="hicaz"`); `generate_usul` writes usuls (sofyan, türk aksağı, yürük semai, devr-i hindi, düyek, aksak, aksak semai, curcuna, …) on a drum or darbuka kit |
| **MIDI Tools in Live** | `install_midi_tools` adds three Max for Live MIDI Tools to Live 12's clip view, which work without the server: Usul and Makam Melody under Generate, Makam Snap (notes to the nearest makam pitch) under Transform; seeded by Live's dice, re-applied as their controls move |
| **Harmony** | `analyze_harmony` names the chords in a clip (key, roman numerals, changes) and returns a progression the generators accept, so new parts follow existing ones |
| **MIDI files** | `import_midi_file` (whole file, one track or one channel), `export_clip_to_midi` (with the set's tempo and meter) |
| **Live Set files** | Without opening them in Live: `read_live_set` reads an `.als` (tempo, meter, key, locators, scenes, tracks with mixer, devices, racks, third-party plug-ins and Max for Live devices, clips, samples and which are missing) or compares it with a backup; `find_live_sets` lists your sets newest first and finds the ones using a plug-in or track name; `import_from_live_set` brings a MIDI clip from an old set into the open one or a `.mid` file |
| **Device chains** | `add_device` inserts any of Live's own devices by name with settings; `apply_device_chain` adds ready-made chains (vocal, drum bus, bass, pad, master, sidechain duck) — Live 12.3+ |
| **Browser** | `browser_search` finds presets, devices, plug-ins, clips and samples by name (yours too; ~12,000 samples in about 0.1 s), `browser_load` puts one on a track or a Drum Rack pad, `browser_preview` plays it in Live's preview |
| **Build from a spec** | `build_tracks` creates MIDI tracks with their instrument (a device or a sample), device settings, effects, Compressor sidechains, faders and Arrangement clips with notes in one call — a set the Trial cannot save comes back from a JSON file |
| **Drum kits** | `build_drum_kit` fills a Drum Rack's General MIDI pads with one-shots from Drums › Drum Hits — by style ("808", "909"), from the Core Library or your own samples |
| **Safety net** | `agent_changes` lists what the assistant changed this session (tool, arguments, undo steps) and `agent_changes(undo=N)` takes back exactly those steps with Live's undo; parameter changes also leave an automatic snapshot; generators, `transform_clip_notes` and `apply_midi_tool` take `preview=true` to show the notes without writing; deleting a track, scene, clip or device asks you first in clients that support MCP elicitation (`ABLETON_MCP_CONFIRM=off` turns it off) |
| **Project memory** | `memory` keeps what was decided about a song (references, levels, structure) inside the Live set, saved with it, and the user's taste in `~/.ableton-mcp` for every set; `get_session_overview` shows both, so a later session picks up where the last one ended |
| **Snapshots & A/B** | `snapshot` saves the mixer and every device parameter (racks included), shows what changed in Live's own units, and restores it in one undo step; automated parameters are left alone |
| **Listening** | `measure_levels` reads Live's meters while the set plays (peak/average dBFS per track, no recording); `render_and_analyze` records a Session scene, the clips playing or an Arrangement section and measures it; `analyze_audio_file`, `analyze_clip_audio` |
| **Mix diagnosis** | `mix_doctor` records every track and the master in one pass and reports masking (which parts fight in which band, how often, what to do), stray low end, phase, overs and the master against a streaming target; `capture_stems` returns the stem files; `compare_to_reference` compares tonal balance, loudness, dynamics and width with a reference track and returns EQ Eight settings that move the mix towards it |
| **Real-time analyzer** | `install_analyzer` puts the AbletonMCP Analyzer, a Max for Live device this server generates, on a track or the master; `read_analyzer` then reports per track, while the set plays and without recording, octave-band levels, loudness (within ~0.1 LU of an offline BS.1770 measurement in our test), stereo correlation and width, peak and RMS. Needs Max for Live (Suite, Standard with the add-on, or the Trial) |
| **Live companion** | An optional Extensions SDK add-on for Live 12.4.5+ Suite (`extension/`): "Share … with AbletonMCP" in Live's right-click menus tells `get_live_selection` which bars, clips or tracks you mean; `confirm_in_live` asks yes/no in a dialog inside Live; `render_track_audio` renders an audio track without playing the set. Not yet run inside Live (it is checked against the SDK's types and a simulated host) |
| **Machine learning** | `separate_stems` splits a mix into drums, bass, vocals and other with Demucs, optionally onto new tracks where the source clip was; `audio_to_midi` turns a melody, bass line or chords into a MIDI clip with Spotify's Basic Pitch; `find_similar_sounds` finds samples by description ("dusty vinyl kick") or by example with CLAP; `generate_audio` makes a sound or a loop from a text prompt with MusicGen small and puts it on an audio track, a Drum Rack pad or a Simpler, with the set's tempo and key in the prompt (MusicGen's weights are CC-BY-NC 4.0: non-commercial use). Each runs in its own environment that uv sets up the first time (Demucs ~0.5 GB, Basic Pitch ~150 MB, CLAP ~1.2 GB, MusicGen ~2.5 GB) and leaves the server's memory when done. On an M-series Mac: a 20-second mix separates in ~9 s, a phrase transcribes in ~4 s, a search over indexed sounds takes ~4 s, an 8-second loop is generated in ~19 s |
| **Live events** | `watch_live`, `get_live_events`, `unwatch_live`; resources `ableton://session/overview` and `ableton://events`, pushed to clients that subscribe (both MCP protocol versions) |
| **Panels** | In clients that support MCP Apps (Claude Desktop, claude.ai), 22 tools show an interactive panel with their result: mixer and Session grid with scene launch, level meters, the mix doctor's masking heat map, the reference curve with a one-click EQ, snapshot A/B buttons, a piano roll of generated clips, a Drum Rack pad grid with sample swapping, and a makam keyboard. Other clients get the same results as text |
| **Diagnostics** | `doctor` (also `ableton-mcp-doctor` in a terminal): versions, stale script copies, connection, open dialogs, CPU, errors in Live's log |
| **Original tools** | browser loading, drum kits, audio import, Arrangement duplication, locators; the rest are covered by the tools above (see Tool profiles) |

76 tools in the default profile (104 with `ABLETON_MCP_PROFILE=full`), plus
workflow prompts: `mix_check`, `beat_from_scratch`, `follow_the_harmony`,
`arrange_from_loop`, `release_check`, `explain_my_set`, `match_reference`.

### Audio analysis

`render_and_analyze` and `analyze_audio_file` report integrated loudness
(ITU-R BS.1770 LUFS, verified against the reference tone), true peak, loudness
range, crest factor, clipping, DC offset, energy per band (sub → air), spectral
centroid, stereo correlation and width (including below 150 Hz), and tempo and
key estimates — plus plain-language observations such as *"Heavy low-mids
(250–500 Hz); a source of muddiness"*.

### Tool profiles and safety

Every tool carries MCP annotations (read-only, destructive, idempotent), so
clients can run reads without asking and confirm deletions. Failures come back
as MCP tool errors with the reason, so the assistant can correct its next
call. Tool calls run off the event loop, so a long Live operation does not
stall the server, and the recording tools report progress. Built on the MCP
Python SDK 2.x; clients on the 2025-11-25 and 2026-07-28 protocols both work.

The original upstream tools that newer ones replace (`set_tempo`,
`fire_clip`, `get_clip_notes`, `create_midi_track`, …) are left out by
default to keep the tool list short. Set `ABLETON_MCP_PROFILE=full` to get
them back.

### Built with it

[**reference-dissector**](https://github.com/CanArslanDev/reference-dissector)
takes a reference track apart: the tempo grid, the tuning, every drum hit, the
exact samples from your own library and a synthesized 808. It rebuilds the
track in Live through this server's tools, with the original stems muted
underneath for A/B. Its docs also cover the Live behaviour measured along the
way and how a model that cannot hear can still tell whether two sounds are the
same.

---

## Install

**Requirements:** Ableton Live 11 or 12 (the Trial works),
[uv](https://docs.astral.sh/uv/getting-started/installation/) (`brew install uv`).

### 1. Add the server to your client

<details open>
<summary><b>Claude Desktop</b> — one-click extension</summary>

Download `ableton-live-mcp-<version>.mcpb` from the
[Releases](https://github.com/CanArslanDev/ableton-live-mcp/releases) page and
open it, or drag it into Settings → Extensions. Claude Desktop installs the
Python dependencies with uv the first time it starts the server. To build the
file yourself: `uv run python packaging/build_mcpb.py` (needs Node.js).
</details>

<details>
<summary><b>Claude Code</b> — plugin with skills</summary>

```
/plugin install ableton-live --marketplace CanArslanDev/ableton-live-mcp
```

(Before Claude Code 2.1.275: `/plugin marketplace add CanArslanDev/ableton-live-mcp`,
then `/plugin install ableton-live@ableton-live-mcp`.) Besides the MCP server,
the plugin brings five skills Claude uses when they fit: working in Live, mixing
and mastering, composing and arranging, Turkish makams and usuls, and setup and
troubleshooting. Only the MCP server, without the skills:

```bash
claude mcp add ableton -- uvx --from git+https://github.com/CanArslanDev/ableton-live-mcp ableton-mcp
```
</details>

<details>
<summary><b>Claude Desktop (config file) and other MCP clients</b></summary>

```json
{
  "mcpServers": {
    "AbletonMCP": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/CanArslanDev/ableton-live-mcp", "ableton-mcp"]
    }
  }
}
```
</details>

<details>
<summary><b>From a local clone</b> (for development)</summary>

```bash
git clone https://github.com/CanArslanDev/ableton-live-mcp
cd ableton-live-mcp && uv sync
```

Point the client at `uv --directory /path/to/ableton-live-mcp run ableton-mcp`,
or load the plugin from disk with `claude --plugin-dir /path/to/ableton-live-mcp`.
</details>

### 2. Install the Remote Script into Live

Ask the assistant to *install the Ableton remote script* (the
`install_remote_script` tool), or run it from a terminal:

```bash
uvx --from git+https://github.com/CanArslanDev/ableton-live-mcp ableton-mcp-install-script
```

This copies the script to `<User Library>/Remote Scripts/AbletonMCP`
(`~/Music/Ableton/User Library` on macOS, `Documents\Ableton\User Library` on
Windows). Restart Live, then choose **Settings → Link, Tempo & MIDI → Control
Surface → AbletonMCP**, Input and Output **None**. Restart Live after every
script update.

Run only one MCP client against Live at a time. Ask the assistant to call
`doctor` to confirm the server and the script
versions match.

Environment variables: `ABLETON_MCP_PROFILE=full` (every original tool),
`ABLETON_HOST` / `ABLETON_PORT` (where the server looks for Live),
`ABLETON_MCP_TOKEN` (the session token, when the server cannot read
`~/.ableton-mcp/live-session.json`). In Live's environment:
`ABLETON_MCP_VERBOSE=1` (log every command to Live's Log.txt) and
`ABLETON_MCP_AUTH=0` (accept clients that do not send the token).

---

## How it works

```
AI client ──MCP (stdio)──▶ MCP_Server (Python 3.10+)
                              │  JSON lines over TCP 127.0.0.1:9877,
                              │  session token from ~/.ableton-mcp
                              ▼
                     AbletonMCP Remote Script (inside Live)
                              │  polled on Live's main thread every 10 ms
                              ▼
                       Live Object Model
```

The Remote Script runs inside Live's own Python. It polls its socket from
Live's main thread with Live's own timer, so a command is read, run and
answered in about 10 ms (the earlier socket-thread design needed ~400 ms,
because Live only lets background threads run on its 100 ms tick). Besides the original fixed
commands it serves a small generic protocol — `lom_get`, `lom_set`, `lom_call`,
`batch`, `observe`/`poll_events`, extended note and envelope editing, and
setting a parameter by its display value. Most tools are written on the server
side in terms of those, so new Live features rarely need script changes.

### Addressing objects

Paths follow the Max for Live convention:

```
live_set tracks 2 devices 0 parameters 3
live_set tracks "Lead Synth" mixer_device volume        # select by name
live_set tracks -1                                      # last track
live_set return_tracks 0 devices "Reverb"
id 140234                                               # id from an earlier result
```

Values that must be Live objects are passed as `{"$ref": "<path>"}` (for example
a routing type: `{"$ref": "live_set tracks 2 available_input_routing_types \"Resampling\""}`);
enum arguments as `{"$enum": "Live.Song.RecordingQuantization", "value": 5}`.
Tools take tracks as an index, an exact name, `"master"` or `"return A"`.

### Undo

Live keeps appending changes made by a script to one open undo step until the
user touches Live — one Cmd+Z would revert everything the assistant did. The
script closes the undo step after every modifying command, so each tool call is
its own entry in the undo history. Live still files parameter changes (volume,
tempo, mute, device values) separately from structural edits (names, colors,
new tracks, clips, notes), so a call that does both can take two Cmd+Z.

---

## Limitations

- Live's API cannot draw track automation, and it refuses envelopes on
  Arrangement clips. `write_arrangement_automation` builds the envelope on a
  Session clip and lays it over the stretch (MIDI tracks; the copy keeps it);
  `record_automation` records real track automation, but in real time, out
  loud.
- Live has no export/render API. `render_and_analyze`, `capture_stems` and
  `mix_doctor` record in real time on temporary tracks (deleted afterwards; the
  audio files stay in the project), so they take as long as the section plays.
- Arrangement clip creation and `move_device` need Live 12; `add_device` and
  `apply_device_chain` need Live 12.3; the extended note API needs Live 11.
- Live's API cannot map parameters to rack macros, group tracks, bounce,
  freeze or export.
- The socket only listens on `127.0.0.1` and wants the token Live writes to
  `~/.ableton-mcp/live-session.json` at every launch (readable only by your
  user). That keeps other users and web pages out, not other programs running
  as you.

## Troubleshooting

Start with `uv run ableton-mcp-doctor` in a terminal, or ask the assistant to
run `doctor`: it checks versions, installed copies of the script, the
connection, open dialogs and Live's log, and says how to fix what it finds.

| Symptom | Fix |
|---|---|
| New tools answer *"Remote Script … is older than this server"* | Run the installer again and restart Live. If it persists, check that no copy named `AbletonMCP` sits inside the Live app bundle (`…/Contents/App-Resources/MIDI Remote Scripts/`) — it shadows the User Library copy, and Live updates have been seen to restore an old one there. |
| Claude Desktop: *"AbletonMCP didn't respond within 2 minutes"* | On macOS, check for a pending *"uv wants to access files in your Documents folder"* prompt and allow it, then restart the client. |
| *"Could not connect to Ableton"* | Live must be running with AbletonMCP selected as a Control Surface; check that port 9877 is free. |
| *"Authentication required"* | The MCP server is older than the Remote Script (2.0 wants a session token). Update the server, or point your client at this version. |
| *"Invalid session token"* | Live restarted and wrote a new token; the server reads it again on the next call. If the server runs as another user or in a container, set `ABLETON_MCP_TOKEN`. |
| Live's log | `~/Library/Preferences/Ableton/Live <version>/Log.txt` — lines tagged `(AbletonMCP)` |

## Development

```bash
uv sync
uv run --with pytest pytest -q            # 360 tests, no Live or models required
(cd extension && npm ci && npm test)       # the Extensions SDK add-on
uvx ruff check .
uv run python packaging/build_mcpb.py     # dist/ableton-live-mcp-<version>.mcpb
claude plugin validate .                  # the Claude Code plugin and its skills
```

CI (`.github/workflows/ci.yml`) runs the tests on macOS and Linux with Python
3.10 and 3.13 (Windows reports without blocking), lint, and builds the wheel,
the sdist and the `.mcpb`. Pushing a `v<version>` tag publishes them as a GitHub
release.

`MCP_Server/fake_live.py` is an in-memory imitation of the Live Object Model;
the end-to-end tests run the real Remote Script TCP server and the real MCP
tools against it. See [CLAUDE.md](CLAUDE.md) for the conventions (the bundled
script copy, version bumps, main-thread rules, Live quirks).

### Without Live: the simulator and the eval suite

`uv run ableton-mcp-simulator` serves that imitation through the real Remote
Script, the way Live does, so the server and its tools run without Ableton
(no sound; the browser holds a few samples). Point a server at it with
`ABLETON_MCP_SESSION_FILE=~/.ableton-mcp/simulator-session.json`.

[`evals/`](evals/README.md) holds 32 `claude plugin eval` cases (mixing,
composing, makams and usuls, arrangement and transitions, memory, safety) that run the plugin's real
server against a fresh simulated set each time and check the resulting set,
not the reply:

```bash
claude plugin eval . --scaffold --allow-real-servers \
  --allow-tools "mcp__plugin_ableton-live_ableton__*" --ablation none -j 4
```

## Privacy

Nothing leaves your machine. The upstream project's optional telemetry and
dataset modules are still in the code but inactive here: they need a
credentials file (`MCP_Server/config.py`) that is not part of this repository,
and without it no consent question is shown and nothing is sent. Set
`ABLETON_MCP_DISABLE_TELEMETRY=true` to switch them off explicitly.

## License

MIT — see [LICENSE](LICENSE). Original work © Siddharth Ahuja; extensions © Can Arslan.
Not affiliated with or endorsed by Ableton.
