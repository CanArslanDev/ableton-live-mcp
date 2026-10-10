---
name: mix-and-master
description: Mixing and release workflow in Ableton Live with the ableton MCP tools. It diagnoses a section with mix_doctor, fixes masking and balance, matches a reference track, checks loudness and true peak for streaming platforms, and A/Bs with snapshots. Use when the user asks about their mix, levels, loudness, LUFS, EQ, masking, muddiness, a reference track, or getting a song ready for release.
---

# Mixing and mastering in Live

Diagnose by listening, change one thing at a time, and let the user compare.

## 1. Pick the section

Agree on what to listen to: an Arrangement range (`start_beat`,
`length_beats`, e.g. the drop) or a Session scene (`scene=0`). The busiest
8–16 bars are the most useful. Every analysis records in real time.

## 2. Save the starting point

`snapshot(action="save", name="before mix")`. It stores mixer and device
settings, so every later change can be compared and undone as a whole.

## 3. Diagnose

`mix_doctor(start_beat=..., length_beats=..., platform="spotify")`, or
`mix_doctor(scene=0)`. It records every track and the master in one pass and
reports:

- the master's loudness, true peak, crest factor and tonal balance against
  the platform's target;
- masking: which two parts collide in which band, and how often;
- per track: stray low end, phase problems, overs, level, main band.

Explain the findings in plain words, most audible first, and propose concrete
moves before making them.

## 4. Fix

- Balance and space: `set_track_mixer(track=..., volume_db=..., pan=..., sends_db={...})`.
- EQ and dynamics: `add_device(track=..., device="EQ Eight", settings={...})`,
  then `set_device_parameter_by_name(...)` to adjust. Live shows parameter
  names like "1 Frequency A", "1 Gain A".
- Ready-made starting points: `apply_device_chain(track=..., chain="drum_bus")`
  (vocal, drum_bus, bass, pad, master, sidechain_duck with `sidechain_from`).
- Masking: cut the less important part 2–4 dB in the shared band, or duck it
  from the other (sidechain_duck), rather than boosting the other part.

Run `mix_doctor` again on the same section and report what changed. The user
can A/B with `snapshot(action="restore", name="before mix")` and a second
snapshot of the new state.

## 5. Match a reference

`compare_to_reference(reference_path="/path/to/reference.wav")` compares
loudness, true peak, dynamics, width and 1/3-octave tonal balance with levels
matched, using the reference's loudest stretch. It also returns
`eq_eight_settings` that move the mix about 60% of the way. Apply them only if
the user agrees: `add_device(track="master", device="EQ Eight", settings=...)`.

## 6. Release check

Targets (integrated loudness / true peak): Spotify, YouTube, Tidal,
SoundCloud -14 LUFS / -1 dBTP; Apple Music -16 LUFS; Amazon Music -14 LUFS /
-2 dBTP; club masters often -6 to -9 LUFS / -0.3 dBTP. Louder than the target
only gets turned down by the platform, at the cost of punch. Measure the whole
song with `render_and_analyze(start_beat=0, length_beats=...)`; for the
master chain, `apply_device_chain(track="master", chain="master")` ends in a
true-peak limiter at -1 dB.

The MCP prompts `mix_check`, `match_reference` and `release_check` run these
workflows step by step.
