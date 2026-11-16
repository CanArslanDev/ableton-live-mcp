---
name: compose-in-live
description: Writing music in Ableton Live with the ableton MCP tools, covering drum kits and grooves, Euclidean rhythms, chord progressions, basslines, arpeggios and melodies that follow the set's key, Live 12 MIDI Tools, finding sounds in the browser, and laying Session clips out as a song. Use when the user wants to make a beat, write chords or a part, change notes, pick sounds, or arrange a loop into a song.
---

# Composing in Live

## Context first

`get_session_overview()` gives tempo, meter and the set's Key & Scale. The
generators use the set's key, scale and meter unless `key`/`scale` are passed,
so set them once with `set_song_properties(tempo=..., root_note=...,
scale_name=..., time_signature="4/4")` when starting from scratch.

## Drums

1. A track with sounds: `manage_track(action="create_midi", name="Drums")`, then
   `build_drum_kit(track="Drums", style="909")` fills a Drum Rack with one-shots
   from Live's library (`source="user"` for the user's own samples, `seed` for
   another pick, `sounds={"kick": "808 long"}` per role).
2. A groove: `generate_drum_pattern(track="Drums", style="house", bars=4)`
   (house, techno, trap, drill, boom_bap, lofi, dnb, breakbeat, uk_garage,
   reggaeton, afrobeats, rock, funk, disco, halftime; `swing`, `humanize`,
   `fill`).
3. Or Euclidean rhythms: `generate_euclidean(track="Drums", voices=[{"pad": "kick",
   "pulses": 3, "steps": 8}, ...])`.

## Harmony and parts

- `music_theory(key="A", scale="minor", progression="i VI III VII")` lists
  scale notes and diatonic chords before you write.
- `generate_chord_progression(track=..., progression="i VI III VII", bars_per_chord=1)`
  (`voicing`, `rhythm`, `octave`).
- Parts that follow the same chords: `generate_bassline(...)`,
  `generate_arpeggio(...)`, `generate_melody(...)` with the same `progression`.
- For chords already in a clip, `analyze_harmony(track=..., clip_index=...)`
  names them; then write parts over that progression.
- Every generator takes `clip_index` (Session) or `arrangement_time`
  (Arrangement), plus `replace=true` to overwrite.

## Edit notes

- `transform_clip_notes(track=..., operation=...)`: transpose (also within the
  scale), constrain to a scale, quantize, humanize, velocity scale or ramp,
  reverse, invert, legato, double or half speed, probability. It can work on
  a pitch or time range only.
- `apply_midi_tool(track=..., tool="strum")`: Live 12's MIDI Tools
  (Transformations such as strum, ornament, chop, connect) on a clip.
- `read_clip_notes(...)` / `edit_clip_notes(...)` for exact changes.
- `.mid` files: `import_midi_file(path=..., track=...)` and
  `export_clip_to_midi(track=..., clip_index=..., path=...)`.
- From an older set: `read_live_set(path=..., detail="full")` lists its clips,
  `import_from_live_set(path=..., track=..., clip_index=..., to_track=...)`
  copies one MIDI clip's notes into the open set.

## Sounds

- `browser_search(query="warm pad", category="instruments")`, then
  `browser_load(track=..., item=<id or name>)`; `browser_preview(item=...)`
  lets the user hear a sample first.
- Live's own devices without the browser: `add_device(track=..., device="Drift")`.
- By description or example: `find_similar_sounds(text="dusty vinyl kick")` or
  `find_similar_sounds(like="/path/to/sound.wav")`; the first search in a
  folder indexes it, so warn that it takes a minute or two.
- When nothing in the library fits, make it: `generate_audio(prompt="metallic
  snare hit", seconds=1.5, track="Drums", drum_pad="snare")` or a loop onto an
  audio track (MusicGen, on this computer; the set's tempo and key go into
  the prompt). Say once that MusicGen's weights are licensed for
  non-commercial use only, and that the first use downloads ~2.5 GB.

## From audio

- `audio_to_midi(track=..., clip_index=..., to_track=...)` turns a melody,
  bass line or chords in an audio clip (or a file) into a MIDI clip, where the
  clip plays them (through its warp); `preset="chords"` for soft or
  reverberant pads and sine-like leads, whose onsets the default misses
  above MIDI 52. 808s and sub basses below it come out, but Basic Pitch
  starts the lowest (under ~50 Hz) about 0.1 s late. For a file warped from
  another tempo, pass the audio's own `tempo`.
- `separate_stems(track=..., clip_index=..., to_tracks=true)` splits a mix
  into drums, bass, vocals and other on new tracks, e.g. to sample a part or
  to transcribe just the bass. The first use of each downloads its model.

## A beat like a reference song

With the full profile (`ABLETON_MCP_PROFILE=full`), reference-dissector takes
a song apart on this computer and the result is built in Live (measured on
macOS with Apple Silicon only; the first run downloads ~2 GB):

1. `index_sound_library()` once (and again when the library changes): a job
   that indexes the User Library and the Core Library's samples, so drum
   hits and 808s are matched against sounds the user owns.
2. `dissect_reference(path="/path/to/song.mp3")` (or `track=`, `clip_index=`)
   starts the analysis and returns a job id at once: stems, tempo grid,
   sections, drum hits matched to samples, the 808, melodic notes and a
   blueprint. About 4 minutes for a 2-minute song (7 the first time).
3. `reference_job(job_id=..., wait=20)` until it is done; it sends progress
   while it waits. The summary gives tempo, meter, sections, each pad's file
   with its match verdict ("same sample", "same sample (likely)", "candidate:
   maybe the same sample, processed", "same sound as …", "same family", "no
   close match") and the 808's notes: say plainly which sounds are only near.
   `reference_job(job_id=..., cancel=True)` stops it;
   `dissect_reference(path=..., steps=["match", "plan"])` reruns part of the
   chain (after a new index, say; `plan` brings `blueprint` along).
4. In an empty set: `build_from_blueprint(blueprint=<job id>)`, or
   `bars="9-16"` for a quick excerpt. It reports what Live did not take
   (a Simpler holding another file, Volumes, choke groups, moved locators).
5. Listen: `render_and_analyze(...)` over a section and
   `compare_to_reference(reference_path=..., ...)` over the same bars.

The prompt `beat_like_reference` walks through this. The song, its stems and
the plan stay on the user's computer.

## Arrange

When the scenes are ready, `build_arrangement(sections=[{"name": "Intro",
"bars": 8, "scene": 0, "except": ["Drums"]}, {"name": "Drop", "bars": 16,
"scene": 1}], locators=true)` lays them out with a locator per section. It
overwrites those bars of the Arrangement, so ask first if it is not empty.

Then the joins: `design_transition(kind=..., at_bar=<first bar of the new
section>)` with `riser`, `reverse_cymbal`, `drum_fill` (fill=snare_roll |
toms | build), `gap` (a beat of silence before a drop) or `filter_sweep`
(MIDI tracks). Automation over a stretch: `write_arrangement_automation` on
MIDI tracks (instant); `record_automation` on audio tracks, returns or the
main track plays the stretch in real time, so say so before. Each is one
`agent_changes(undo=1)` away from gone.

Listen as you go: fire a clip with `manage_clip(action="fire", ...)` or a
scene with `manage_scene(action="fire", scene=...)`, and use
`render_and_analyze` for a check of the result. The prompts
`beat_from_scratch`, `follow_the_harmony` and `arrange_from_loop` walk through
these steps.
