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

## Features

| Area | Tools |
|---|---|
| **Any LOM object** | `lom_get_object`, `lom_set_property`, `lom_call_method` reach anything Max for Live can, by path |
| **Orientation** | `get_session_overview`: tracks, groups, mixer, sends, devices, clips, scenes, cue points, key/scale |
| **Batching** | `run_batch` applies many commands in one round-trip on Live's main thread |
| **Mixer & tracks** | `set_track_mixer` (dB, pan, sends, mute/solo/arm, monitoring), `manage_track`, `set_track_routing` |
| **Scenes & clips** | `manage_scene`, `set_clip_properties` (loop, markers, warp, gain, pitch, launch), `manage_clip` (duplicate, crop, quantize, Arrangement clips) |
| **Notes** | `read_clip_notes`, `edit_clip_notes` (probability, velocity deviation, release velocity, edit by note id), `transform_clip_notes` (transpose, snap to scale, quantize, humanize, reverse, invert, legato, …) |
| **Automation** | `write_clip_automation` draws envelopes for any device or mixer parameter, from points or shapes |
| **Devices & racks** | `set_device_parameter_by_name` accepts what Live displays (`"800 Hz"`, `"-6 dB"`, `"25L"`, `"Lowpass"`), `manage_device`, `rack_control` (macros, variations), `get_drum_pads` |
| **Song & transport** | `set_song_properties` (time signature, swing, loop, Live 12 key/scale, quantization), `transport` (undo/redo, capture MIDI, cues), `select_in_live` |
| **Generators** | `music_theory`, `generate_chord_progression`, `generate_drum_pattern` (maps your Drum Rack pads by name), `generate_bassline`, `generate_arpeggio`, `generate_melody` — they follow the set's time signature and, unless told otherwise, its Key & Scale (Live 12) |
| **Harmony** | `analyze_harmony` names the chords in a clip (key, roman numerals, changes) and returns a progression the generators accept, so new parts follow existing ones |
| **MIDI files** | `import_midi_file` (whole file, one track or one channel), `export_clip_to_midi` (with the set's tempo and meter) |
| **Device chains** | `add_device` inserts any of Live's own devices by name with settings; `apply_device_chain` adds ready-made chains (vocal, drum bus, bass, pad, master, sidechain duck) — Live 12.3+ |
| **Listening** | `measure_levels` reads Live's meters while the set plays (peak/average dBFS per track, no recording); `render_and_analyze` records a section through Resampling and measures it; `analyze_audio_file`, `analyze_clip_audio` |
| **Live events** | `watch_live`, `get_live_events`, `unwatch_live`; resources `ableton://session/overview`, `ableton://events` |
| **Diagnostics** | `doctor` (also `ableton-mcp-doctor` in a terminal): versions, stale script copies, connection, open dialogs, CPU, errors in Live's log |
| **Original tools** | browser loading, drum kits, audio import, Arrangement duplication, locators; the rest are covered by the tools above (see Tool profiles) |

50 tools in the default profile (72 with `ABLETON_MCP_PROFILE=full`), plus
workflow prompts: `mix_check`, `beat_from_scratch`, `follow_the_harmony`,
`arrange_from_loop`, `release_check`, `explain_my_set`.

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
as MCP tool errors. Tool calls run off the event loop, so a long Live
operation does not stall the server.

The original upstream tools that newer ones replace (`set_tempo`,
`fire_clip`, `get_clip_notes`, `create_midi_track`, …) are left out by
default to keep the tool list short. Set `ABLETON_MCP_PROFILE=full` to get
them back.

---

## Install

**Requirements:** Ableton Live 11 or 12, [uv](https://docs.astral.sh/uv/getting-started/installation/) (`brew install uv`).

### 1. Install the Remote Script into Live

```bash
uvx --from git+https://github.com/CanArslanDev/ableton-live-mcp ableton-mcp-install-script
```

This copies the script to `<User Library>/Remote Scripts/AbletonMCP`
(`~/Music/Ableton/User Library` on macOS, `Documents\Ableton\User Library` on
Windows). Then in Live: **Settings → Link, Tempo & MIDI → Control Surface →
AbletonMCP**, Input and Output **None**. Restart Live after every script update.

### 2. Add the server to your MCP client

<details open>
<summary><b>Claude Desktop</b> — Settings → Developer → Edit Config</summary>

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
<summary><b>Claude Code</b></summary>

```bash
claude mcp add ableton -- uvx --from git+https://github.com/CanArslanDev/ableton-live-mcp ableton-mcp
```
</details>

<details>
<summary><b>From a local clone</b> (for development)</summary>

```bash
git clone https://github.com/CanArslanDev/ableton-live-mcp
cd ableton-live-mcp && uv sync
uv run ableton-mcp-install-script
```

Point the client at `uv --directory /path/to/ableton-live-mcp run ableton-mcp`.
</details>

Run only one MCP client against Live at a time. Ask the assistant to call
`doctor` (or `get_remote_script_info`) to confirm the server and the script
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

- Live's API cannot write track-level Arrangement automation, and it refuses
  envelope edits on Arrangement clips. Automation tools write clip envelopes on
  Session clips; `duplicate_to_arrangement` carries them into the Arrangement.
- Live has no export/render API. `render_and_analyze` records in real time on a
  temporary track (deleted afterwards), so it takes as long as the section plays.
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
uv run --with pytest pytest -q        # 160 tests, no Live required
```

`tests/fake_live.py` is an in-memory imitation of the Live Object Model; the
end-to-end tests run the real Remote Script TCP server and the real MCP tools
against it. See [CLAUDE.md](CLAUDE.md) for the conventions (the bundled script
copy, version bumps, main-thread rules, Live quirks).

## Privacy

Nothing leaves your machine. The upstream project's optional telemetry and
dataset modules are still in the code but inactive here: they need a
credentials file (`MCP_Server/config.py`) that is not part of this repository,
and without it no consent question is shown and nothing is sent. Set
`ABLETON_MCP_DISABLE_TELEMETRY=true` to switch them off explicitly.

## License

MIT — see [LICENSE](LICENSE). Original work © Siddharth Ahuja; extensions © Can Arslan.
Not affiliated with or endorsed by Ableton.
