# Changelog

## 2.5.0 — 2026-11-24

Remote Script 2.2.0: reinstall it (`ableton-mcp-install-script`) and restart
Live. Checked in Live 12.4.16: the script loads, and the plugin's server
2.4.3 (which expects 2.1.0) still works with it.

**Note for anyone also running an older server:** its `doctor` (2.4.3 and
earlier) calls script 2.2.0 "Outdated copy" and suggests
`install_remote_script`. Don't follow it: that installs the older 2.1.0
and undoes the fixes below. Install from this version instead.

### Fixed

- **A stopped `build_from_blueprint` no longer doubles a period.** Live's
  own messages end in one ("Device NoSuchSynth not found."), and the
  build's "build_tracks stopped: … It left … in the set" put a second
  after it (seen in Live 12.4.16, 2026-11-18). The fake's unknown-device
  error now reads as Live's does.
- **`browser_load` and `build_drum_kit` no longer load onto the wrong
  track** (this and the fixes below were met while remaking songs in Live
  12.4.16). When selecting the target track failed, Live's browser still
  loaded the item onto whatever track was selected (a preset replaced a
  Drums track's Drum Rack). The track (and its Drum Rack) is now found
  before anything loads (and before the browser is searched), an item
  loads only once its track reads back as selected and its pad's selection
  was accepted, and the result says what the track holds afterwards
  (`track`, `devices`, `pad_name`; `build_drum_kit`'s `track` too; a pad
  still empty is an error, a track whose devices did not change a
  `warning`). A track Live renames as it loads ("1-MIDI" becomes "1-BNYX
  Boot Kit") is followed by id, where reading it back by its old name used
  to fail after the load. The checks cost round trips: about 10 for one
  item instead of about 4, and 2 more per further pad (a few ms each).
- **Sounds named explicitly load that sound.** `sounds={"crash": "Impact
  Layered"}` in `build_drum_kit` (and an item query in `browser_load`,
  `browser_preview`) took the browser's first hit, "Impact Layered
  Reversed". The exact name (case and extension aside) now wins, then names
  that are not reversed or loops unless asked for, then the closest and
  shortest. The walk ends at the exact name, else at 25 hits with every
  word in the name and one in its folders, or 3 s after the first hit with
  every word in the name, and after 20 s in any case; a walk that did not
  finish and found nothing says so in the error ("searched N items in S s
  without finishing"), as `build_drum_kit` does under
  `searches_unfinished`.
- **`design_transition(kind="reverse_cymbal")` reverses once.**
  `Simpler.reverse()` toggles, so a second call played the crash forwards
  again. Live keeps the reversed copy in `Samples/Processed/Reverse`, and a
  sample there (or named reversed) is left as it is; the crash it picks is
  a plain one from Drum Hits › Cymbal, not one already reversed. A Simpler
  on that track holding something other than a crash (by its file name) is
  replaced by one; one holding Live's reversed copy is kept, whatever its
  name.
- **Arms and the selection are put back.** When a device lands on a MIDI
  track that has none, through `insert_device` or the browser, Live
  12.4.16 arms that track (`arm`, not `implicit_arm`), takes the arm from
  every other track (exclusive arm, Live's default) and selects it. So
  `add_device`, `apply_device_chain`, `build_drum_kit`, `browser_load`,
  `build_tracks`, `design_transition` and the legacy
  `load_instrument_or_effect` and `load_drum_kit` left the new track armed,
  disarmed the user's armed track and moved the selection. They now read
  every track's arm and the selection before, and afterwards disarm tracks
  Live armed, re-arm the ones it disarmed (arms set by a script are not
  exclusive) and reselect the user's track (Live moves the selection only
  after the command, so it is always set back; the legacy tools keep
  their selection), in one batch that leaves no undo step. Live also
  selects a track a device is inserted on, a new track, and the track of a
  deleted device when another device stays on it: `build_tracks` and
  `design_transition` put the selection back at the end, and
  `manage_device(action="delete")` after any delete. An effect onto the
  selected track that already has devices needs no put-back (inserting
  still adds one read before the insert, against 2.4.3), and a failed
  put-back never hides the error of the insert or load it followed.
- **Tools keep hold of a track Live renames.** A track still named as Live
  named it ("1-MIDI") takes the name of an instrument that lands on it
  ("1-Drift"), by `insert_device` or the browser. `build_drum_kit` then
  failed after inserting its Drum Rack ("No item named '1-MIDI'"), leaving
  an empty rack; `add_device` could not disarm the track. Tools that insert
  or load (`add_device`, `apply_device_chain`, `build_drum_kit`,
  `browser_load`, `generate_audio`, `install_analyzer`) now address the
  track by its id and report its current name as `track`.
- **Limiter "Gain" sets its Input Gain**, not its Output. Live 12.3's
  Limiter "Gain" is 12.4's "Input Gain"; the general rename "Gain" →
  "Output" (right for Utility) is no longer applied to it. Renames can now
  belong to one device (`DEVICE_PARAMETER_RENAMES`).
- **A Drum Rack inside an Instrument Rack is found.** `get_drum_pads` said
  "No Drum Rack" for kits like "BNYX Boot Kit"; it,
  `browser_load(drum_pad=…)`, `build_drum_kit`, drum fills and darbuka
  mapping now look inside racks' chains (the track's own devices first).
  `get_drum_pads` names the `rack` when it is nested.
- **Devices inside racks are found by name** in
  `set_device_parameter_by_name`, `manage_device`, `rack_control` and the
  other tools that take a device: the track's own devices first, then
  rack chains level by level. A name that several nested devices share is
  an error listing their paths, and a LOM
  path on that track is accepted as the device ("live_set …" or "id N …";
  one on another track is refused; a device named like one wins when found
  at the same or a shallower rack level than the path's device), as
  are names made only of digits ("808"; else the index, and an error when
  both an index and a nested device's name fit). `manage_device`
  deletes a device given by path, id or name, also inside a rack (through
  its chain).
- **Failed settings are reported at the top.** `add_device` and
  `apply_device_chain` return `ok: false` and `errors` (setting → Live's
  message) when a setting fails, e.g. an EQ Eight filter type Live does not
  have; before, `add_device` looked successful with the failure inside
  `settings`. `settings` is now a count ("1 of 2 applied"). `build_tracks`
  returns a top-level `ok` too.
- **`run_batch` checks every step before running any**: a step without a
  `type`, with `params` that are not an object, or a `lom_get`/`lom_set`/
  `lom_call`/`param_set_display`/`observe` step without `params.path` is
  refused, naming the step (it ran and answered "Empty path").
- **`build_arrangement` silences what does not play in a section.** Old
  Arrangement clips on tracks left out of a section kept playing there
  (old drums in a rebuilt intro). Clips inside a section's bars on such
  tracks are deleted, and clips across its edges are cut there first by
  laying a short copy over them and deleting it (a copy laid over a clip
  splits it, MIDI and audio alike, in Live 12.4.16). A track with Session
  clips is cleared wherever it doesn't play; one without (Arrangement-only
  material such as recorded vocals) only in the sections that name it.
  Group tracks are skipped; a track that could not be cleared is an error
  ("Drums: not cleared: …").
- **`build_arrangement` replaces the old locators**: locators inside the
  built bars that are not at a section start are deleted.
- **A clip longer than what is left of a section lands at that length.**
  Live makes a copy as long as the clip's loop (`loop_end - loop_start`),
  so loop_end is moved for the copy and put back. An audio clip used to be
  placed whole (a 16-bar file ran 48 beats past a 4-bar outro), and the
  rest of a MIDI section was rebuilt from notes only, losing its clip
  envelopes; both are now copies of the Session clip, envelopes and all.
  An unwarped audio clip keeps the length Live's warp tempo guess gave it
  whatever its loop, so its copy is trimmed in the Arrangement instead
  (each scratch copy past everything on the track, at a place of its own,
  and deleted whatever fails; one left behind is an error).
  `build_arrangement`'s errors name the section and track.
- **`import_midi_file` no longer adds a bar for a release tail**: a last
  note ending less than 1/4 beat past a bar line made a 9-bar clip of an
  8-bar file (a note starting on that bar line still keeps the bar).
- **New audio clips are checked against the file.** Live 12.4.16 warps a
  new clip at the tempo it guesses (a 40 s click track made at 120 BPM
  came in as 96 BPM, 64 beats instead of 80), and an unwarped Session clip
  keeps that guess as its length, so its copies were cut short. The legacy
  `create_audio_clip`, `build_tracks`' `audio_clips`, `generate_audio` and
  `separate_stems(to_tracks=True)` (stems of an unwarped source; a warped
  source's stems are left as Live makes them) report the length in beats,
  Live's first reading of the file's tempo (`live_guess_bpm`), the tempo
  its warp markers give it now (`warp_bpm`, warped clips) and the warp
  markers. A `warning` comes when the length is not
  what was asked, or when the file lasts whole bars at the set's tempo (as
  one rendered from a song does) and Live made it another length; a loop
  made at another tempo is left to Live.
- **Tools that make Arrangement clips put the playhead back.** Live
  12.4.16 moves the stopped playhead to every clip that
  `duplicate_clip_to_arrangement`, `create_midi_clip` or
  `create_audio_clip` makes in the Arrangement, so `build_arrangement`
  left it at the last section and `place_locators` restored it there. It
  goes back after `build_arrangement`, `duplicate_to_arrangement`,
  `build_tracks`, `build_from_blueprint`, `design_transition`,
  `write_arrangement_automation`, the generators, `import_midi_file`,
  `import_from_live_set`, `audio_to_midi` and `manage_clip` with an
  Arrangement time, and `generate_audio` / `separate_stems` placing audio
  in the Arrangement.

- **Only the recording tracks record.** `render_and_analyze`,
  `capture_stems`, `mix_doctor` and `compare_to_reference` stopped the
  transport and put arms back in one batch, arms first, so re-armed tracks
  recorded and split their clips (Crank). The transport now stops and
  record mode goes off first; arms come back in a later command. Tracks
  with `implicit_arm` (Push's auto-arm, which records in Live 12.4.16) are
  disarmed for the take too, every arm is read back before anything plays
  (a track that stays armed stops the render), and the result names the
  tracks it disarmed (`disarmed_for_take`). `record_automation` turns
  implicit arms off as well, by track id, and reads the arms back before
  it plays.
- **The selection comes back after a recording**; Live had moved it to a
  neighbour of the deleted temporary track. A scene render stops the clips
  it launched and puts Back to Arrangement back (Live 12.4.16 left them
  playing on the stopped transport, overriding the Arrangement).
- **An empty recording is an error that names Live's audio device.** With
  "No Device" as Live's output, Live wrote 92-byte WAVs (2 frames) and the
  render analysed them or said "no audio file was found" (Nerdeysen). A
  recording must now hold the section: almost no frames is an error that
  quotes Live's Log.txt about the output device; a take Live cut short
  (its clip under the section's beats), playback stopping before the end
  and a take that never reaches it (formerly a silent return at the
  deadline; it waits as long as a slower scene tempo and 8 bars of the
  meter need) are errors
  too. The clip's beats, not the set's tempo, measure
  a take, so a scene with its own tempo or tempo automation renders.
  Files are read once their size has held for a second.
- **Analyses cover the section only**: Live records on until the stop
  reaches it, so the recording's tail is left out (by the clip's span),
  and at most 600 s are read (the length reported is the real one). A
  kept track is disarmed also when the setup stops early.
- **A scene's tempo no longer stays.** A scene with a tempo of its own
  changed the set's for good; the set's tempo is put back
  (`tempo_put_back`). The metronome comes back also when a render stops
  before playing, and a render whose arms cannot be read back does not
  record.
- **No more "Post Mixer" errors in Live's log.** Setting the input
  channel in the batch that set the inputs failed on all but the first
  new track (Live still listed Ext. In's channels); Live had set Post
  Mixer with the input anyway. The channel is now read in a later command
  and set only where it is not Post Mixer.
- **`separate_stems` no longer overwrites another file's stems.** An
  `out_dir` holding stems of another (or an unrecorded) source is refused
  unless `overwrite=true`; the default folder moves on to "<name> 2" for a
  second file of the same name, chosen when the job's turn comes (two
  queued calls cannot pick the same one).
- **`read_live_set` finds the samples of a moved project.** Live 12.4
  saves project samples as `RelativePathType` 3, relative to the project;
  they were looked up only at their old absolute path and reported missing.
- **Tempo estimates weigh double, half and 3:4 tempos** (a 136 BPM song
  read as 102): the peak's relations are scored by how well a pulse at
  that period fits, with a preference around 120 BPM, and replace the
  peak's tempo only by a clear margin. On 206 synthetic grooves: 140 right
  against the peak's 119, none lost; the remake song's pattern still reads
  102, with 136 among the alternatives.
- **Machine-learning jobs run one at a time**, below Live's priority:
  four `find_similar_sounds` at once never finished and starved Live
  until its connection timed out. A queued call says what it waits for.

- **Display values in the wrong unit or out of range are refused**
  (Remote Script 2.2.0). `param_set_display` (behind setting parameters
  by text, `"value": "-6 dB"`) clamped them: "40 %" on Drum Buss
  Transients, which shows -1.00 to 1.00, set 1.00. A unit the parameter
  does not show is now an error naming what it shows and its range ("'40
  %' does not fit Transients: it shows plain numbers (no unit), from -1.00
  to 1.00"), and so is a value past either end ("'5 s' is outside the
  range of Release, 1.00 ms to 3.00 s"); a value past an end by at most
  one display step there (to the next display inward) sets the end and
  says so (`note`:
  "Attack's lowest is 0.01 ms: set to 0.01 ms" for "0 ms"), however the
  target is written; a stepped control takes half its last digit, and
  an end that is not a plain number nothing past it (Hybrid Reverb's Ti
  Rate "1/128 T", "4 D"; Expansion Ratio "1 : 2.00"). Hz and kHz, ms
  and s, dB, %, pan ("25L", "C"), ratios ("4:1"), "-inf dB", plain
  numbers and choice names work as before, and
  so do other spellings of a unit the parameter shows ("1.5 sec", "300
  msec", "-6 dBFS", "-1 dBTP", "12 semitones", "11 cents"). Note values
  ("1/16") and Compressor's expansion ("1:1.5") are now read right; script
  2.1.0 set them to 1/64 and 1:1.00. A minus typed with a space ("- 6
  dB", read as +6 dB) or as "−" or "–" is a minus, and "1/0" is
  refused instead of read as 1.
- **`set_track_mixer` fails when a part fails.** A refused change came back
  as a success with `"ok": false` in its text; the call is now an error
  that names what failed and what was applied.
- **Tools pass on what was set instead.** A setting that landed on −inf,
  on the end of the range or on the upper of two displays carries a
  `note`; `add_device` and `apply_device_chain` list them under `notes`
  (setting → note), `build_tracks` under each track's `setting_notes`,
  `set_track_mixer` and the batch summaries on the step, and
  `set_device_parameter_by_name` and `run_batch` return them as Live's
  script gave them.
- **Glue Compressor's Release takes numbers** (Remote Script 2.2.0).
  It shows ".1" to "1.2", then "A" (Auto), and every number was refused
  ("Parameter display … is not numeric"); "A" now sets Auto, and a
  target that is neither says what the parameter shows.
- **Display values land on the display asked for** (Remote Script 2.2.0).
  Checked by setting 7 displays of each of 629 parameters in Live 12.4.16
  (4,403 sets): "12 dB" on Limiter's Input Gain read back "11 dB" (Live
  keeps a float32, a hair below the edge found), "0.0 %" read "-0.0 %",
  Operator's Freq<Vel "-50" read "-50.0", "25 lp/bp" on a Filter Morph
  set "25 no/lp" (its number restarts in each section). Each now reads
  back as asked. Roar's FB Note takes note names ("F#3"), its Blend
  splits ("75 / 25"). A level below the lowest a fader shows above −inf
  ("-80 dB" on a send, "-70 dB" on Compressor's Threshold, lowest −69.7
  dB) sets −inf, as Live's faders do, and the result says so (`note`);
  it landed on the lowest number. The crossfader's "25B" sets B (it set
  A). A value halfway between two displays goes to the larger ("1" on
  Glue Compressor's Release, between ".8" and "1.2", sets 1.2), with a
  note. The value set sits in the middle of its display ("C" pan is
  0.0, not −0.01), and a refused value writes nothing.
  Resonators' note names with "♯" ("C3" set E4 and reported success),
  Hybrid Reverb's Ti Rate ("1/32 T", "1 D") and Vintage (Off … Extreme)
  are read, and "1:4" on Compressor's Ratio is refused (it set 4:1). A
  result whose display is not what was asked, nor the nearest the
  parameter shows, is now refused instead of reported as set. A second
  check set 11 displays of each of 883 parameters (26 effects, 6
  instruments; 9,713 sets): every one read back as asked.
- **`{"$ref": <id>}` reaches the object** (Remote Script 2.2.0). An id
  from an earlier result passed as a bare number ("Unknown root") or as
  digits now works like `{"$id": N}` and `"id N"`; the error for an
  unknown root names those forms, and `lom_call_method` mentions `$id`.
- **Browser searches leave Live half its main thread** (Remote Script
  2.2.0). Each slice of a walk ran 120 ms, and the next was asked for at
  once: with a large User Library, `browser_search` and loads by name
  kept Live unresponsive for minutes. The script now gives a slice no
  more time than Live had since the last one, and says how long to rest
  (`rest_ms`); the server asks for 60 ms slices and waits that long. Its
  time limits (20 s, 3 s after a strong hit) leave those waits out, so a
  search covers as much ground as before and can take up to twice as
  long before it reports an unfinished walk. A folder is read item by
  item instead of copied into a list first.
  Measured in Live 12.4.16 with a test folder of 100,000 samples in the
  User Library: with script 2.1.0 a walk of it ran slices of 308 and 413
  ms and another client's commands waited 396 ms (median, 710 at most);
  with 2.2.0 a walk of every category (208,705 items, 2.2 s) took 48 % of
  the main thread and other commands answered in 8 ms (median; 78 ms at
  the 90th percentile). Listing one folder is still one call in Live
  (~3 µs an item: 0.33 s for those 100,000), the longest wait left.
- **Transitions and Arrangement copies leave the selection alone.**
  `design_transition` onto a "Riser" or "Reverse Cymbal" track that
  already existed left that track selected (Live 12.4.16, 3 runs of 3),
  and with such a track around `duplicate_to_arrangement` at 64 and 32
  left it selected too. Live pins a new track once a clip was copied onto
  it and the playhead set: a later copy over that clip's span, or the
  playhead set back into it from outside, selects it again (seen from the
  next command), and setting the selection back to the track before is
  ignored until another track is set (deleting that track or its clip ends
  it). Tools that put the playhead back (`duplicate_to_arrangement`,
  `build_arrangement`, `write_arrangement_automation`, transitions, clips
  made in the Arrangement) now also read the selection in the command
  after and, when it moved, set the selected track and then the user's;
  the arm put-back retries the same way. Such a tool now costs one
  selection read more, and one batch more when the selection moved.
- **`browser_load(item="808")` searches for "808".** Any item made of
  digits was taken for an id from `browser_search` and failed with
  "Unknown id 808"; digits that are no browser item's id are now a name
  (also in `browser_preview` and for samples given to `build_drum_kit`).
- **`create_locator` as the first call no longer reports the script
  missing** (`loaded=None`): a capability check made before any
  connection now connects and asks the script first, and when Live cannot
  be reached it says so instead of asking for a reinstall.

### Added

- **`set_device_parameter_by_name(pad=…)`** reaches a Drum Rack pad's
  chain: `device="mixer"` its volume, pan or sends, otherwise the device on
  that pad (by index or name); an empty pad is an error that says so, and
  so is a send the Drum Rack has no return chain for (pad sends go to the
  rack's own returns).
- **Settings say which parameter they set**: `renamed` in `add_device` and
  `apply_device_chain`, `asked` and `parameter` in
  `set_device_parameter_by_name`, when a name was mapped to another.
- **`build_arrangement` options and reports**: `clear_others=False`
  keeps every track as it is, `clear_locators=False` the old locators;
  `cleared` gives per track the clips `removed` and `trimmed`, `kept` the
  Arrangement-only clips left in sections that don't name their track,
  `locators_deleted` the locators that went.
- **`duplicate_to_arrangement(length=…)`** sizes the copy, and the result
  says where it landed (`start_time`, `end_time`), warning when that is
  not what was asked, when an unlooped clip's end_marker is before its
  loop_end (Live copies to loop_end), and when an unwarped clip's length
  is Live's warp guess rather than what it plays. A `length` of 0 or less
  is refused.
- **`import_midi_file(length_bars=…)`** sets the clip's length; notes are
  cut at its end (`notes_after_the_end` counts those left out).
- **`create_audio_clip(warping=…, bars=…)`**: `warping=false` plays the
  file as recorded (it is first warped to its length at the set's tempo,
  so copies last as long as it plays); `bars` warps the file straight to
  that length, with a warning when that changes its speed. `bars` with
  `warping=false` is refused before the clip is made; a clip Live left
  unwarped is left as it is and the result says so under `notes` (warping
  it again would drop its end marker). `build_tracks` audio clips take
  `bars` with `"warping": true`, and file such notes under `notes`.
- **`design_transition(kind="gap")` cuts audio tracks too** (named in
  `tracks`; MIDI tracks still get an empty "Gap" clip) and returns each
  track's clips around the gap (`clips`: `arrangement_clip_index`, start,
  end), so they need not be read again.

- **`manage_track(action="delete_recording_tracks")`** deletes the
  "MCP Rec …" tracks `render_and_analyze(keep_track=True)` left; their
  files stay in Samples/Recorded. It deletes by track id and is refused
  while one of them is armed or in a take (a render running in this or
  another session). `render_and_analyze` returns the recording's path
  (`file`, with `render.played` saying what played), `mix_doctor` its
  `files`.
- **Tempo and key confidence**: `analyze_audio_file` and the renders add
  `tempo_confidence` and `tempo_alternatives_bpm`; `key_estimate` gets
  `weak` and `or` (the runner-up) when the key is a guess. `observations`
  note a weak key, an uncertain tempo (confidence under 0.1) and a 4:3 or
  3:4 tempo scoring 0.75 of the pick's or more ("Tempo 102 BPM; could be
  136 (4:3): check by ear."; on the benchmark it named one for 7 of 73
  right picks, none of them click tracks or four on the floor).
- **`find_similar_sounds(min_score=…, min_seconds=…, max_seconds=…)`**
  filters weak matches and sustained sounds from one-shots, and the
  result warns when the best match is weak.
- **`doctor` checks Live's audio output device** in its Log.txt and warns
  on "No Device".

### Docs

- CLAUDE.md: parameter displays across Live 12.4.16's devices, the
  browser's listing costs, and its quit dialog (Cmd+D is Don't Save).
- The ableton-live skill: read the track's devices before retrying an
  interrupted call (it may have reached Live), and what `ok`, `errors` and
  `renamed` mean; pad chain mixers and devices in racks. The mix-and-master
  skill: check `errors` after `add_device`. CLAUDE.md: Limiter's 12.4
  parameters, racks and chain mixers, track names following the
  instrument, and Simpler's reverse, as measured in Live 12.4.16.
- `run_batch`: a playhead moved in a batch takes effect after it
  (`set_or_delete_cue` in the same batch toggles at the old position).
  CLAUDE.md: how Live 12.4.16 sizes copies into the Arrangement, warps and
  unwarps new audio clips, trims and deletes Arrangement clips, moves the
  playhead, and names audio tracks after their first clip.

- README Limitations and the ableton-live skill: Live's API cannot save
  the set; ask the user to save (on macOS, File › Save Live Set with Live
  in front; a keystroke sent while another app is in front is lost) and
  check the .als modification time. Recordings stay in the project's
  Samples/Recorded. CLAUDE.md: implicit arm, recording files, Live's
  audio device in Log.txt, RelativePathType values in Live 12.4 sets, and
  CLAP scores, as measured on 2026-11-23.

### Tests

- The ML jobs' priority is set and read with `os.setpriority` /
  `os.getpriority`: `os.nice(0)` answered 0 on GitHub's macOS runners
  (BSD's nice()), failing the priority test there.
- The script's display units, unit and range refusals, bare ids in
  `$ref`, a walk's share of the main thread and folders read by index
  (all failed on script 2.1.0), other spellings of units ("1.5 sec" is
  1.5 s), and the server resting between slices without counting the
  rests against its time limits.
- `fake_live`: Live 12.4.16's Limiter parameters, Instrument Racks
  (`FakeRack`) and chain mixers, Simpler's reverse as a copy in
  `Processed/Reverse`, tracks named after what loads onto them, the
  browser loading onto a Drum Rack inside a rack, an "FX Hit" folder that
  lists "Impact Layered Reversed" first and a "Crash Reverse Swell" listed
  before "Crash 505". Tests for each fix above.
- `fake_live`: a clip's length is its loop (copies take it), unlooped
  Arrangement clips end at loop_end, warping off turns the loop off and
  keeps times in seconds (an unwarped Session clip keeping its length),
  warp markers with Live's shadow marker, `move_warp_marker`,
  `remove_warp_marker`, the tempo Live reads a file at (`FILE_TEMPOS`, WAV
  length read from the file), `Track.delete_clip`, copies replacing the
  clips they cover (a covered or deleted clip can no longer be read or
  changed, until undo brings it back), and the playhead following new
  Arrangement clips.
- `tests/test_render.py` answers recording like Live 12.4.16 (arm and
  implicit arm, the selection, tracks recording while Live plays with
  record mode on) and checks each recording fix above; a moved project
  for `read_live_set`; a tempo benchmark (`tests/tempo_bench.py`: click
  tracks, house, trap, drill, boom bap, half-time, drum and bass,
  reggaeton, waltzes, the remake song's pattern) that must never lose a
  tempo the peak alone got right; two ML jobs at once; stems of two
  sources in one folder.
  `fake_live` tracks have `implicit_arm`.
- `fake_live` pins a selection as measured in Live 12.4.16
  (`FakeSongView`): a new track, a clip copied onto it and the playhead
  set; from outside its span, a copy over it or a playhead set into it
  selects it after the command, and a set back to the track before is
  ignored; deleting the track or its clip ends it. Tests for the three fixes
  above and capability checks before any connection, with and without
  Live.

## 2.4.3 — 2026-11-16

The Remote Script is unchanged (2.1.0): no reinstall or Live restart needed.
2.4.2's jobs pin a reference-dissector commit that is on no branch any
more: update to keep `dissect_reference` and `index_sound_library` working.

### Changed

- **The reference jobs ran with the real refdissect** (the pinned f907dd2,
  Apple M4, 2026-11-16): `index_sound_library` over 12,093 sounds took
  33.8 s; `dissect_reference` of a 126 s song 442 s the first time (setup
  and model downloads included) and 224 s once set up, with the same
  result; `build_from_blueprint` of the finished job built it in Live
  12.4.16 in under 5 s with clean read-backs. Progress and the estimate
  `dissect_reference` and `index_sound_library` give now follow those
  runs: about 4 minutes for a 2-minute song (5 before), about 35 s for
  12,000 sounds (a minute per 9,000 before).
- **The jobs run reference-dissector f907dd2** (was f192456). Its history
  was rewritten on 2026-11-16 to drop the Co-Authored-By and Claude-Session
  lines from its commit messages; every commit kept its files, author and
  date, so f907dd2 holds exactly f192456's files and the jobs install the
  same code. The old ids are on no branch any more. Those named in this
  changelog are now: f192456 → f907dd2, 11a264c → 76ce377, 2245889 →
  ca048c4, 21d82d7 → f8eff5a.

## 2.4.2 — 2026-11-16

The Remote Script is unchanged (2.1.0): no reinstall or Live restart needed.
The Claude Code plugin's new "Tool profile" option needs a new session.

### Fixed

- **A cue Live moved is deleted only where the playhead got to.**
  `build_from_blueprint` used to move the playhead to such a cue and toggle
  without reading it back: where Live kept the playhead (past the song's
  end) the toggle hit the cue there instead, and a locator placed right
  could go. The playhead is now read back first; a cue it cannot reach is
  left and reported (`playhead`, `deleted: false`).
- `create_locator` and `build_arrangement` delete a cue Live put at
  another beat too, and say where it went; zooming the Arrangement in
  reports its errors (`zoom_errors` in `build_from_blueprint`'s report).
- `build_from_blueprint` refuses a set that holds a locator, which it
  would otherwise rename where the blueprint asks for one.
- **A track named only with digits** ("808") is that track, not track
  808: as a tool's `track`, a device's name, an input routing, and a
  sidechain source (`set_track_routing`, `apply_device_chain`,
  `build_tracks`' `sidechain_from`, `capture_stems`). An integer, or digits
  no track has as its name, are still an index. Checked in Live 12.4.16
  (the mixer and an input routing by "808").
- **New audio clips play from the file's start** in every path, not from
  where Live puts the start when warping goes off after its warp analysis
  (the file's first transient, up to seconds in; in Live 12.4.16 a 150 s
  file's went from 0 to 0.53, and the reset in the same batch held): stems placed by `separate_stems(to_tracks=True)` play
  from where their (unwarped) source clip does, and `generate_audio`'s
  clips and the original `create_audio_clip` from the file's start, as
  `build_tracks`' already did. The simulator's Live now moves a new clip's
  start, so a path that leaves it is caught.
- `doctor` says how to turn the full profile on in each client (in Claude
  Desktop the extension's Tool set, which overrides the environment), and
  warns when uv is missing.

### Added

- **A "Tool profile" option in the Claude Code plugin** (`/plugin`,
  Configure options): `full` turns the reference-song tools on without a
  shell variable. The plugin passes it as `ABLETON_MCP_PROFILE_SAVED`, so
  `ABLETON_MCP_PROFILE` in Claude Code's environment (the evals') still
  wins.

### Changed

- The jobs run reference-dissector f192456 (was 11a264c). For the jobs
  only its `blueprint` step changed: a locator that rounds to the plan's
  end is left out and logged, where it used to fail the job's last step.
  Everything else they run writes the same files (`plan --live-sr` and
  `candidates`' per-bar rule are off the jobs' path; the same plan made a
  byte-identical blueprint at both commits).
- The fake Live, and so the simulator, reports Live 12.4.16, where the
  latest measurements were made, and a pad the browser loads has 3 voices
  (a Simpler made by path 6), as Live 12.4.16 did on 2026-11-14.
- CI fails on Windows too (it has passed there; the jobs' process-group
  tests skip). The .mcpb's tool list is always the core profile's, whatever
  the shell that builds it says; the release notes take only their own
  version's CHANGELOG section (`2.4.1`, never `2.4.10`).

### Docs

- The checks of 2026-11-14 (`build_from_blueprint` in Live, a Simpler's
  0.1 ms Fade In, 13 of 13 Volumes, the recordings equal to the browser's
  build) ran on Live 12.4.16, not 12.4.6: Live's own log has 12.4.16 first
  started that day at 16:02 and 12.4.6 last on 2026-11-08. Corrected in the
  2.4.0 notes, README, CLAUDE.md and docs/blueprint-v1.md.
- How to turn the full profile on, per client (README, the setup skill);
  `made_by` in docs/blueprint-v1.md (an install names its commit too).

### Tests

- `tests/test_blueprint_readback.py`: `build_from_blueprint` against a
  simulator made to refuse a file by path (the browser's item of that name
  holding another), a choke group, a Volume as a number and as text, and
  voices; each is reported and makes `ok` false, and a check turned off is
  not run. A blueprint passed as an object cuts an excerpt's stems into the
  data folder. Before, only the clean read-backs were tested.
- Jobs: the runner's heartbeat while a step runs, a runner alive but silent
  for `STALE_S` (lost, holding the machine), and "invalid choice" skipping
  only an optional step.

## 2.4.1 — 2026-11-16

The Remote Script is unchanged (2.1.0): no reinstall or Live restart needed.

### Fixed

- **Locators land where they are asked.** Live snaps a new cue to the
  Arrangement's grid, which follows its horizontal zoom: at the default zoom
  beat 9 of a 3/4 song became 12. `create_locator`, `build_arrangement` and
  `build_from_blueprint` now show the Arrangement and zoom it in 30
  `zoom_view` steps before placing cues (in Live 12.4.16, about 30 steps in,
  an eighth of a beat landed exactly), then zoom out by as many, which
  restores the view only roughly. Each cue's playhead is read back before
  the cue is toggled; one Live refuses (past the song's end) places nothing
  and says where the playhead stayed. `build_from_blueprint` deletes the
  locators Live still moved while zoomed in (`locators_moved`, unchanged).
  Checked in Live 12.4.16: a 3/4 build placed locators on beats 0, 9, 21,
  33 and 45 exactly, and `create_locator` with the Session view in front
  placed 15 and 40.5.
- The simulator's Live snaps cues to a grid that follows `zoom_view`
  (a model of the measurements, not one of them).

## 2.4.0 — 2026-11-15

The Remote Script is unchanged (2.1.0): no reinstall or Live restart needed.
The new tools are in the full profile: start the client with
`ABLETON_MCP_PROFILE=full` in its environment (`ABLETON_MCP_PROFILE=full
claude` in Claude Code; "full" under the extension's Tool set in Claude
Desktop). `build_from_blueprint` was checked in Live 12.4.16 on 2026-11-14,
through reference-dissector's `live-build`: an 8-bar excerpt and a whole
song (10 tracks, 1,881 notes) built with clean read-backs and recorded equal,
sample for sample, to its build through the browser. The jobs
(`dissect_reference`, `index_sound_library`, `reference_job`) have run
only with a stand-in for refdissect, not with the pinned one and its models.

### Added

- **"Make a beat like this song", end to end (full profile).** Four tools
  run [reference-dissector](https://github.com/CanArslanDev/reference-dissector)
  (refdissect) and build its result in Live:
  - `index_sound_library(folders=None)`: a job that indexes the user's
    samples for matching (refdissect `inventory`, `index-library` and its
    808 index); by default the User Library and Live's Core Library samples.
  - `dissect_reference(path | track/clip, model, melodic, slides, steps)`:
    a job that runs refdissect's steps (separate, analyze, melodic, match,
    fit-808, plan, blueprint) on a song, or part of them again on its folder,
    and returns the job's id at once.
  - `reference_job(job_id=None, wait=20, cancel=False)`: waits up to `wait`
    seconds for the next step with progress notifications, then gives the
    state; once done, a summary from plan.json (tempo, meter, sections, pads
    with their match verdicts, 808 notes, files). Without an id it lists the
    recent jobs; `cancel` stops the job's whole process group.
  - `build_from_blueprint(blueprint, bars=None)`: builds a blueprint in the
    open, empty set over this server's own connection (so the Remote
    Script's 8-client limit no longer gets in the way): tempo and meter,
    the files copied into the User Library (scaled to full scale where a
    file peaks over it), one `build_tracks` call, voices, locators, then
    read-backs of every Simpler's file, pad Volumes (sent again as text where
    the number did not hold), choke groups, locators Live moved, the time
    signature and the tempo. `bars="9-16"` builds a short excerpt. A
    blueprint older than the plan beside it (`meta.plan_sha1`) is refused,
    and a `plan` run again brings `blueprint` along.
- refdissect is not a dependency: jobs run it in a uv environment of its own
  (Python 3.12, pinned to reference-dissector 11a264c;
  `ABLETON_MCP_REFDISSECT=<checkout>` runs a local one), detached from the
  server in their own process group, with records in `~/.ableton-mcp/jobs/`
  (`ABLETON_MCP_JOBS_DIR`) and a `job.json` in the work folder. A job
  outlives the server; one whose runner is gone reads as `lost` (and, while
  processes it started still run, keeps the next job from starting until
  `cancel` stops them); one job runs at a time. A pid another process took
  since is never taken for the job's. Data goes to `~/Music/Ableton
  MCP/Reference Dissector/` (`ABLETON_MCP_REFERENCE_DIR`), one folder per
  song named after its file (`(2)` for another file of the same name).
  Paths reach refdissect absolute. Measured on macOS with Apple Silicon only.
- **Blueprint v1** ([docs/blueprint-v1.md](docs/blueprint-v1.md)): a JSON
  format for a beat to build, with a JSON Schema
  (`MCP_Server/schemas/blueprint-v1.schema.json`), a validator
  (`MCP_Server.blueprint.validate`, `python -m MCP_Server.blueprint FILE`)
  and three example blueprints in `tests/fixtures/blueprints/`. Besides the
  schema, the validator refuses what the build would ignore or fail on:
  settings where `build_tracks` does not read them, a pitch outside MIDI's
  0–127, two locators on one beat, a sidechain from no track of the
  blueprint, a copy renamed to another format. File names take their
  extension in any case; one file under two paths (same `sha1`) is one copy.
  Scaled copies and cut stems are compared and named by their audio (a float
  WAV's header carries the time it was written).
- The `beat_like_reference` prompt (full profile) and a section of the
  compose-in-live skill chain the four tools with `render_and_analyze` and
  `compare_to_reference`.
- `doctor` reports reference-dissector: the pinned commit or the local
  checkout, uv, the library index's size and age, a job running, and that
  its tools are off when the server runs the core profile.
- The simulator's state shows each Drum Rack pad's chains (choke group,
  devices) and a Simpler's voices; the fake Simpler has the parameters the
  build sets (`Vol < Vel`, `Snap`, `F On`, `Ve Release`, `Glide Mode`,
  `Glide Time`, `Fade In`, `Fade Out`) and `voices`. As in Live 12.4.16, a
  Simpler made with `insert_device` starts with a 0.1 ms Fade In and a pad
  the browser loads at 0, playing at C3 (it played the pad's own note), so
  a blueprint from reference-dissector (pads at `"Fade In": 0`) builds
  cleanly in the simulator; before, the parameter was missing.

### Changed

- The setup skill, the README and Claude Desktop's Tool set option say that
  the full profile also holds the reference-song tools, and how to turn it
  on (`ABLETON_MCP_PROFILE=full claude` in Claude Code).
- `jsonschema` is a dependency of its own (it came only through `mcp`).

### Tests

- `tests/test_blueprint.py`: the schema and the validator, files copied,
  scaled and kept, excerpts, and every fixture built against the simulator
  (read-backs, a Volume set again as text, a locator Live moved, a set that
  is not empty, a changed file, a build that stops part-way).
- `tests/test_reference_jobs.py`: jobs with a stand-in for refdissect (real
  processes, no uv or models): every step and the summary, steps run again,
  inputs checked first, one job at a time, cancel (the steps' processes go
  too), a failed step, a refdissect without the `blueprint` command, lost
  jobs, a runner killed alone (its steps stopped by cancel), a pid another
  process took, songs of one name, the library index, a job built by its
  id, doctor, the profile; another server seeing a running job (through
  `ps`) and cancelling it, a runner deaf to SIGTERM, an unreaped runner,
  a job still starting, relative paths, the default folders.
- `tests/test_blueprint.py` also: copies and stems kept across a second
  (float WAV headers), a Volume that really refuses a number, the tempo and
  an audio clip left warped read back, the validator's new refusals, a
  blueprint older than its plan, clips that start inside an excerpt.
- An eval case, `build-from-blueprint` (needs `ABLETON_MCP_PROFILE=full`):
  a finished job in the run's home, built into an empty simulated set.

### Fixed

- `separate_stems` pins torch 2.14.1 in its Demucs environment. Unpinned, a
  fresh environment resolves torch 2.15.0, whose stems on Apple GPUs (MPS)
  put the bass into "other" (measured by reference-dissector, which pins the
  same version).
- `build_tracks` reports an audio clip Live left warped (asked unwarped) or
  put at another beat, and the errors of setting it up, which it dropped.

## 2.3.0 — 2026-10-30

The Remote Script is unchanged (2.1.0): no reinstall or Live restart needed.
The new `build_tracks` options were checked in Live 12.4.6 (2026-10-19);
`audio_to_midi`'s placement through a clip's warp only against the fake Live.

### Added

- `audio_to_midi` takes a `preset`: `melodic` (the default: onset 0.6,
  frame 0.3, 35 ms, melodia only below MIDI 52), `chords` (melodia at every
  pitch) or `legacy` (what it did before: 0.5, 0.3, 127.7 ms, melodia on),
  and `melodia_trick`, which it never passed (it was always on); given, it
  holds at every pitch. On 63 synthetic renders (plucks, 32nd runs, leads,
  pads; with reverb, an 808 and a bleed loop), onset F1 went from 0.583
  (legacy) to 0.687 with melodia off at every pitch, 0.706 with `lowest`
  just above the 808 (measured by an earlier spike, not again with melodia
  below MIDI 52). legacy keeps only notes of 139 ms or more: on 32nd-note
  runs at 140 BPM its F1 was 0.36, melodic's 0.90 (4 clean renders through
  this server's worker). Without melodia a note needs an attack Basic Pitch
  sees: it found 3 of 16 pure sines with a 10 ms fade-in (chords 15), 13 of
  124 notes on synthetic 808 lines and 2 notes for the 336 of six 808 stems
  (MIDI 22-32; "No notes found" on five of them). So melodic decodes the
  model's output twice, melodia off from MIDI 52 (165 Hz) up and on below,
  and drops the low notes melodia reads an octave over a sounding one (a
  bass's second harmonic: 83 of the 106 extra notes on 3 saw bass lines).
  Onset F1, melodia off everywhere → melodic (3 renders each, 4 above MIDI
  52): 808 lines 0.18 → 0.86, with repeated notes 0.18 → 0.49, sine bass
  0.00 → 0.45, saw bass 0.84 → 0.82, sine bass at MIDI 40-55 0.34 → 0.82;
  plucks, runs, a lead and pads above MIDI 52 0.915 → 0.916, plucks and
  pads at MIDI 40-60 0.900 → 0.873. On the 808 stems F1 went from 0.006 to
  0.17: the notes have their pitch, but Basic Pitch starts them a median
  109 ms late (IQR 48–187 ms) at 29–52 Hz. When it finds no notes, the
  error suggests `preset="chords"`.
- `audio_to_midi(bends=true)` lists the pitch bend of each bent note in
  cents, one value per model frame (11.6 ms). Basic Pitch 0.4.0 reads
  in-tune notes a third of a semitone sharp (+1 in 99.4–100 % of the frames
  of sines at 110, 220, 440 and 880 Hz); the values are corrected by that
  bin, after which in-tune and ±33-cent sines read 0 and ±33 cents. Live's
  API has no per-note pitch bend, so bends are returned, not written.
- `audio_to_midi` velocities follow each note's level
  (`audio_analysis.note_levels`): a band at the note's pitch, one semitone
  wide but at most 100 ms long, the loudest of its readings from 20 ms
  before the note's start to 60 ms after it, none past its end; the
  loudest note gets 127 and the others 127 × 10^(dB below it / 40). Rank
  correlation with the true level (Spearman, Basic Pitch on 4 clean
  synthetic renders each), against 127 × Basic Pitch's amplitude before:
  plucks 0.62 → 0.98, a legato lead 0.71 → 0.99, triad pads 0.27 → 0.95.
  On 3 driven 808 lines (MIDI 28-43, -24 to 0 dB, 82 notes) it is 0.98,
  and equal-level 808 lines get velocities 125–127. Centred on the note's
  start, a one-semitone window (0.51 s at MIDI 24) read the silence or the
  note before it: 0.95 and velocities 102–127, and equal-level 808 lines
  (MIDI 24-45) spread 3.6, 4.1 and 10.0 dB in quarters, 8ths and 16ths at
  140 BPM, against 0.08, 0.12 and 0.18 dB now. Below MIDI 53 the band is
  wider than a semitone, so a kick on an 808 note adds 0.9 dB on average.
  With reverb and detuned pad voices a similar measure ranked plucks at
  0.81–0.83, a lead at 0.57 and pads at 0.20. `legacy` keeps the old
  velocity.
- `audio_to_midi` places notes where the source clip plays them: a warped
  clip through Live's own warp (`Clip.sample_to_beat_time`, in batches), an
  unwarped one from its start marker; notes the clip never plays are left
  out. `offset_beats` moves every note, and `timing` in the result says
  what applied (or that Live could not map a warped clip). Checked against
  the fake Live, not yet in Live. `lowest` and `highest` also take Hz
  ("82.4 Hz").
- `build_tracks` loads a Simpler's or a drum pad's sample from a file
  (`sample_path`, absolute) with `SimplerDevice.replace_sample` (Live
  12.4+), builds those pads without the browser (`insert_chain`, `in_note`,
  a Simpler per chain) and reads each file back from its Simpler. By name,
  the browser's first hit could be another file with the same name. On
  older Live, or when Live refuses a file, the file's name goes to the
  browser (`loaded_by_name` says why), and the call fails if neither
  works. Simplers and pads take `playback_mode` (classic, one_shot,
  slicing) and `start_marker` (sample frames), pads `choke_group` (0–16),
  and `signature` sets the time signature first. Settings such as
  `"Detune": "-12 ct"` resolve by name as before. If Live refuses a call
  that builds pads by path (`insert_chain`, `in_note`,
  `Chain.insert_device`), the rack is deleted and every pad comes from the
  browser on a new one, as `loaded_by_name` says. `sample` may come with
  `sample_path`: it is the name the browser looks up when the path cannot
  be used, and what 2.2.0, which ignores `sample_path` and the new options
  without an error, loads. Everything goes through the Remote Script's
  generic LOM bridge: script 2.1.0 works unchanged. Measured in Live 12.4.6
  on 2026-10-19 with script 2.1.0, every option passed: files by path on a
  Simpler and on Drum Rack pads (each read back as the exact path),
  `playback_mode` classic and one_shot, `start_marker` (frames),
  `choke_group`, `warping: false`, `"Detune": "-12 ct"`, `signature` "7/8",
  the browser fallback for a file Live refuses (on a Simpler and on one pad
  of a rack) and the refusals before anything is built; a 5-track build (an
  808 Simpler and drum pads, by path) took 7.7 s. Not verified in Live: the
  rack rebuilt by name (Live 12.4.6 refused none of those calls; the fake
  Live was made to refuse each), loading on Live before 12.4, and the
  slicing mode.

### Changed

- `audio_to_midi` defaults to the `melodic` preset and to velocities from
  each note's level; `preset="legacy"` gives what it gave before.
- The tempo estimate of the audio analysis tools is found between frames: a
  parabola through the autocorrelation peak at one beat, then at 2 to 32
  beats, and it is reported to 0.01 BPM (0.1 before). Whole-frame lags are
  3.9 BPM apart at 140 BPM (44.1 kHz), and a 137 BPM click track read
  136.0; it reads 137.0 now. Click tracks from 70 to 175 BPM at 44.1 and
  48 kHz came within 0.01 BPM wherever the tempo octave was right; the
  octave choice is unchanged (150 BPM still reads 75).

### Documentation

- `capture_stems` says its recordings are at the audio device's sample rate
  (often 48 kHz). Read as 44.1 kHz, a capture drifts by 9 %.
- CLAUDE.md: recordings' sample rate; Live keeps playing a sample it loaded
  after the file is overwritten on disk, until it restarts.
- CLAUDE.md: what Live 12.4.6 does with the Simplers and pads
  `build_tracks` makes, measured on 2026-10-19. `replace_sample` reads an
  overwritten file again, where a browser load keeps the old content.
  Transpose keeps whole semitones and Detune whole cents from −50 to +50,
  truncating numbers (display text such as "11.5 ct" rounds). A Simpler's
  level follows dB = 0.4 × A × (velocity − 70) − 0.096 over its Volume, A
  being "Vol < Vel" as a fraction (35 % on a Simpler made with
  `insert_device`, 45 % on a pad loaded through the browser). A choke
  group cuts with a 1 ms fade; "Snap", on by default, ends a sample
  10–30 ms early; Glide slides linearly in semitones over the Glide Time
  (one voice, overlapping notes); notes start within 0.04 ms of their
  time, and recordings add no offset.
- The compose-in-live skill says when `preset="chords"` fits and that Basic
  Pitch starts the lowest notes (under ~50 Hz) about 0.1 s late.
- CLAUDE.md: what Live 12.4.6 did in reference-dissector's live checks of
  2026-10-29, which built 8 plans and a song with 2.2.0's `build_tracks`
  and recorded every pad. One-Shot pads hit again start the new hit 1.000
  ms late (391 hits) and fade the old one out over 1 ms; the Classic 808
  Simpler releases at −1.683 dB/ms, restarts a note of its own pitch ~1 ms
  late, and its filter delays notes 0.13–0.46 ms; Simpler loses top end
  (more for 44.1 kHz files); a Volume sent as display text is held a step
  low; loading by file name took another file of the same name, and a
  name the browser never listed made the build stop half-made; cues land
  on the Arrangement's zoom-dependent grid.
- README and the ableton-setup skill: "Connection reset by peer" means the
  Remote Script already serves its 8 clients (one per MCP server).

### Tests

- The fake Live, and so the simulator, reports Live 12.4.6 and has Drum
  Rack chains that a pad lists by their `in_note` (as `DrumPad.chains`
  does), `SimplerDevice.replace_sample`, Simpler's Detune and
  `Clip.sample_to_beat_time`. 28 new tests, 370 in all; 2 of them run the
  real Basic Pitch model, only with `ABLETON_MCP_ML_TESTS=1`.
- The companion tools' test gives them a connection that raises. It used the
  server's own, which tried localhost:9877, where a running Live listens,
  3 times per run (2.5 s; 0.45 s now). No test tries that port now (checked
  over the whole suite with Live open).

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
