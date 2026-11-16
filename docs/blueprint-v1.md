# Blueprint, version 1

A blueprint is a beat to build in Ableton Live, written as one JSON document.
It decides everything without Live: tempo, time signature, locators, the
tracks with their instruments, settings and notes, the sample files to copy
into the User Library, and which read-backs to run once Live has built it.
`build_from_blueprint` (full profile) builds one in the open, empty set over
the server's own connection and reports what Live did and did not take.
Checked in Live 12.4.6 (2026-11-14) with refdissect's blueprints: clean
read-backs, and recordings equal sample for sample to refdissect's own
build through the browser.

[reference-dissector](https://github.com/CanArslanDev/reference-dissector)
writes one per analysed song (`refdissect blueprint WORK` → `WORK/blueprint.json`,
run by `dissect_reference` as its last step); anything else may write one too.

- JSON Schema: [`MCP_Server/schemas/blueprint-v1.schema.json`](../MCP_Server/schemas/blueprint-v1.schema.json)
  (Draft 2020-12; the structure).
- Validator: `MCP_Server.blueprint.validate(data)` returns every problem with
  where it is: the schema, then what a schema cannot say (a sampler's `file`
  names a key of `files`, track names are unique, …).
  `python -m MCP_Server.blueprint FILE.json` also checks the files on disk.
- Examples: [`tests/fixtures/blueprints/`](../tests/fixtures/blueprints/)
  (a short beat; a choke group and a file scaled to full scale in 3/4; Glide,
  a reference stem and a pad loaded by browser name).

## The document

```json
{
  "format": "ableton-mcp-blueprint",
  "version": 1,
  "name": "Short beat",
  "made_by": "refdissect 0.1.0 2245889",
  "tempo": 120,
  "time_signature": "4/4",
  "length_beats": 8,
  "locators": [{"name": "A (1)", "beat": 0}, {"name": "B (2)", "beat": 4}],
  "files": {
    "808": {"source": "/abs/work/synth808.wav", "name": "Short beat 808 719984e5.wav", "sha1": "719984e5"},
    "pad-39": {"source": "/abs/library/clap.wav", "name": "clap ccc9a8ab -3.10dB.wav", "gain_db": -3.1}
  },
  "tracks": [
    {"name": "808 (synth)",
     "instrument": {"file": "808", "warping": false, "playback_mode": "classic"},
     "settings": {"Volume": "0.0 dB", "Transpose": 36, "Detune": -12, "Vol < Vel": 0.35,
                  "Snap": "Off", "F On": "Off", "Ve Release": "50.0 ms"},
     "voices": 1,
     "volume_db": -2.0,
     "clips": [{"start": 0, "length": 8, "name": "808",
                "notes": [{"pitch": 24, "start_time": 0, "duration": 1.5, "velocity": 115}]}]},
    {"name": "Drums (library)",
     "instrument": {"drum_rack": {
       "39": {"file": "pad-39", "warping": false, "playback_mode": "one_shot", "choke_group": 1,
              "settings": {"Volume": -6.6, "Transpose": 0, "Detune": 0, "Vol < Vel": 0.35, "Snap": "Off",
                           "F On": "Off", "Fade In": 0.0}}}},
     "volume_db": -4.0,
     "clips": [{"start": 0, "length": 8, "name": "Drums", "notes": []}]},
    {"name": "REF drums", "kind": "audio", "mute": true,
     "audio_clips": [{"file": "/abs/work/stems/drums.wav", "start": 0, "warping": false}]}
  ],
  "checks": {"samples": true, "volumes": true, "choke_groups": true,
             "locators": true, "time_signature": true, "voices": true}
}
```

### Top level

| Field | | |
|---|---|---|
| `format`, `version` | required | `"ableton-mcp-blueprint"`, `1` |
| `name`, `made_by` | optional | for the report |
| `meta` | optional | free-form; the build ignores it (an excerpt records itself in `meta.excerpt`) |
| `tempo` | required | BPM, 20–999 |
| `time_signature` | required | `"N/D"`: N 1–99 over 1, 2, 4, 8 or 16 |
| `length_beats` | optional | the song's length in quarter beats; `bars=` cuts within it (without it: the end of the last MIDI clip). Locators must fall before it |
| `locators` | optional | `{"name", "beat"}`, beats from 0, one per beat. Whole beats are safest: Live snaps a new cue to the Arrangement's grid, which follows the zoom, so the build zooms in first (in Live 12.4.16, about 30 steps in, an eighth of a beat landed exactly); one Live does not place is reported under `locators_moved` |
| `files` | optional | the files to copy into the User Library, by a key the samplers name (below) |
| `tracks` | required | at least one; names unique |
| `checks` | optional | read-backs after the build, each on unless `false` |

No other field is allowed anywhere except inside `meta`: a misspelt field is an
error, not something quietly ignored.

### `files`

Each entry: `{"source", "name", "gain_db"?, "sha1"?}`.

- `source`: the file, absolute, or relative to the blueprint file's folder (a
  blueprint passed as an object needs absolute paths).
- `name`: its name in `<User Library>/Samples/AbletonMCP Generated/` (no
  folder; an audio extension in any case: `.wav`, `.aif`, `.aiff`, `.flac`,
  `.mp3`, `.ogg`; an unscaled copy keeps its source's). **Name a file after
  its content** (a digest in the name): Live keeps playing what it loaded
  through the browser under a name after the file is overwritten, until it
  restarts.
- `gain_db` (≤ 0): under 0, the copy is the file scaled by it, written as a
  32-bit float WAV at the file's own rate and channels; the name must end
  `.wav`. For a file that peaks over full scale: the gain that brings it to
  0 dBFS, made up in the pad's Volume and the track's fader.
- `sha1`: the start (8–40 hex digits) of the source's SHA-1. The build
  refuses a source whose content changed since the blueprint was written.

A copy already there with the same content is kept (a scaled copy: the same
audio, since libsndfile stamps the time into a float WAV's header); one with
other content is replaced, and the report says so (`files.replaced`, with
Live's caveat). Names are compared without case (one file on macOS's and
Windows' disks): two entries may share a name only as one file, from the
same source or with the same `sha1` (one file in two library folders), and
the same `gain_db`.
Without a User Library the copies go to `~/.ableton-mcp/generated`, as
`generate_audio`'s do.

### `tracks`

The [`build_tracks`](../MCP_Server/extended_tools.py) spec, with two
additions:

- A sampler (a track's `instrument` object, or a Drum Rack pad) names its
  sample in one of three ways: `"file": "<key of files>"` (the copy: loaded
  by path on Live 12.4+, by its name through the browser before), or
  `"sample_path"` (an absolute file loaded as it is, not copied), or
  `"sample"` (a browser name). `file` excludes the other two.
- `"voices"` (1–32) on a track whose instrument is a Simpler: set on the
  device after the build (it is a property, not a parameter). One voice with
  Glide Mode "Glide" slides overlapping notes.

The rest is `build_tracks`':

| Track field | |
|---|---|
| `name` | required |
| `kind` | `"midi"` (default) or `"audio"`; an audio track takes `audio_clips` only |
| `instrument` | a device name (`"Drift"`), a sampler `{file \| sample_path \| sample, warping, playback_mode, start_marker}`, or `{"drum_rack": {"<note 0-127>": sampler + settings + choke_group}}` |
| `settings` | the instrument's parameters by name: a number sets the raw value, text goes by Live's display (`"-6.0 dB"`, `"Off"`); Live 12.3 and 12.4 names both work |
| `effects` | `[{"device", "settings"?, "sidechain_from"?}]` |
| `volume_db` (≤ 6), `pan` (−1…1), `mute` | the mixer |
| `clips` | `[{"start", "length", "name"?, "notes": [{"pitch", "start_time", "duration", "velocity"?, …}]}]`, Arrangement clips; note times from the clip's start; a note must start inside its clip |
| `audio_clips` | `[{"file", "start"?, "warping"?}]`: the clip plays the file from its start (relative files: from the blueprint's folder) |

A pad's `choke_group` is 0 (none) to 16. A pad's `Volume` given as a number
(dB) is read back after the build (below).

Where settings go: a Drum Rack pad's in the pad (`drum_rack.<note>.settings`),
a track's Simpler's (and any other instrument's) in the track's `settings`;
the validator refuses the other places, which `build_tracks` would ignore.
A pitch is a MIDI number or a note name in Live's octaves (C3 = 60) within
0–127. `sidechain_from` names another track of the blueprint. Locators take
one beat each (Live keeps one cue per beat).

### `checks`

| Check | What is read back |
|---|---|
| `time_signature` | Live's signature against the blueprint's |
| `tempo` | Live's tempo against the blueprint's, within 0.001 BPM |
| `samples` | every sampler with `file` or `sample_path`: the file its Simpler holds, compared by content (SHA-1) with the copy (or the `sample_path`) |
| `volumes` | every sampler whose `settings.Volume` is a number: the parameter's value within 0.001 dB, and its display the dB rounded to the digits it shows (two decimals from −10 dB up, one below). One that does not hold is sent again as display text (`"-6.6 dB"`), Live's way before, and counts as held by text within 0.06 dB (Live 12.4.6 held "-6.6 dB" as −6.605) |
| `choke_groups` | every pad with a `choke_group`: its chain's group, set again once where it differs |
| `locators` | a cue Live put at a beat nobody asked for (it snaps new cues to the Arrangement's grid, which depends on the zoom) is deleted, still zoomed in, and listed under `locators_moved` with the beat asked and the beat Live chose; one whose playhead Live refused (past the song's end) is listed with `live` null |
| `voices` | the Simpler's voices after setting them |

## What `build_from_blueprint` does

1. Validates the blueprint and checks its files on disk (every source and
   audio clip there, `sha1` matching) before touching Live. A blueprint
   file whose `meta.plan` changed after it was made (`meta.plan_sha1`, as
   refdissect writes them) is refused: make it again.
2. With `bars="A-B"` (from 1, as Live's ruler counts): makes an excerpt that
   starts at 0. Notes that start in those bars move back by bar A's start and
   end by bar B's end (a note starting before bar A is left out), clips are
   cut to the bars, locators kept if they fall in them, and unwarped audio
   clips are cut at the same instants into `excerpt-A-B/` beside the
   blueprint, each named by its audio (a file that ends before the excerpt
   is left out). A warped audio clip cannot be cut at the tempo alone, so
   `bars` refuses a blueprint that has one.
3. Refuses a set that is not empty (a track with a device or a clip).
4. Sets the tempo and the time signature (before any locator: Live snaps
   cues to the meter's grid).
5. Copies the files into the User Library.
6. Calls `build_tracks` once with the spec (samplers pointed at their copies).
   If it stops, the error says where and which tracks it left in the set.
7. Sets `voices` and places the locators: it shows the Arrangement, zooms it
   in 30 `zoom_view` steps, then for each locator moves the playhead in a
   call of its own, reads it back and toggles the cue in the next call;
   afterwards it zooms out by as many steps, which restores the view only
   roughly (Live's zoom stops at its limits). Then it runs the checks, moves
   the playhead to 0 and shows the Arrangement. An audio clip Live left warped (when the
   blueprint says `"warping": false`) or put elsewhere is one of its track's
   `problems`.

The report:

```json
{"blueprint": "Short beat", "tempo": 120, "time_signature": "4/4",
 "tracks": [{"track": "808 (synth)", "devices": ["Simpler"], "notes": 3}, …],
 "files": {"folder": "…/User Library/Samples/AbletonMCP Generated", "copied": 4},
 "locators": 2,
 "checks": {"time_signature": {"want": "4/4", "live": "4/4", "same": true},
            "tempo": {"want": 120.0, "live": 120.0, "same": true},
            "samples": {"checked": 4, "wrong": {}},
            "volumes": {"checked": 3, "held": 3},
            "choke_groups": {"checked": 2, "wrong": {}},
            "voices": {"808 (synth)": {"want": 1, "live": 1, "same": true}}},
 "ok": true}
```

`ok` is false when a track has `problems`, a locator moved, or a check found a
difference; the details are beside it (`loaded_by_name` says which samplers
went through the browser and why).

## From a refdissect plan

What `refdissect blueprint WORK [--out FILE]` writes, field by field, from
`plan.json` and what `live-build` computes today (`refdissect.live`):

| Blueprint | From |
|---|---|
| `name` | the song's name: the folder holding the stems' folder (the name the plan already gives the 808 one-shot), else the work folder's |
| `made_by` | `"refdissect <version> <commit>"` (the commit only from a git checkout) |
| `tempo`, `length_beats` | `plan.tempo`, `plan.length_beats` |
| `time_signature` | `meter_of(plan)` |
| `locators` | `[{"name": n, "beat": float(round(b))} for n, b in plan.locators]` |
| `files["808"]` | `{"source": t808.sample_path, "name": t808.sample, "sha1": one_shot_digest(t808.sample_path)}`, when the 808 track has a one-shot |
| `files["pad-<note>"]` | `{"source": pad.path, "name": pad_sample(pad.path, file_db), "gain_db": file_db}` (`gain_db` only under 0), and `"sha1"` of the source (refdissect always writes it) |
| track `808 (synth)` | `build_spec`'s, with `"instrument": {"file": "808", "warping": false, "playback_mode": "classic"}` instead of `sample`, `settings` = `simpler_808(t808)`, `"voices"` only when the plan has one (never `null`) |
| track `Drums (library)` | `build_spec`'s, each pad `{"file": "pad-<note>", "warping": false, "playback_mode": "one_shot", "settings": {Volume (a number), Transpose, Detune, Vol < Vel, Snap, "F On": "Off", "Fade In": 0}, "choke_group"?}` (`choke_group` only when 1–16). A Simpler loaded by path starts Classic with its filter on and a 0.1 ms Fade In, where the browser's pad is One-Shot, unfiltered, without one: the blueprint says what it wants |
| melodic and `REF` tracks | `build_spec`'s, the stems' paths absolute |
| `meta` | `{"plan": "plan.json", "plan_sha1": <its SHA-1's first 12 hex digits>}`: the build refuses the blueprint once that plan changed |
| `checks` | left out (all on) |

The checks that need no Live stay in refdissect's command (the 808's digest
against the plan, every pad's file readable); the build repeats the ones it
can (`sha1`, files on disk).

## Versions

A change that an existing blueprint survives (a new optional field) keeps
version 1 and updates the schema here. One that changes what a field means,
or makes one required, is version 2; `build_from_blueprint` names the versions
it reads in its error.
