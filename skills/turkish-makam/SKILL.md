---
name: turkish-makam
description: Turkish makam music in Ableton Live with the ableton MCP tools. Covers makam scales in the Arel-Ezgi-Uzdilek 53-comma system, Live tuning files for the exact comma pitches, melodies and chords in a makam, and usul rhythms (aksak, sofyan, curcuna, …) on drum tracks. Use when the user mentions a makam (hicaz, rast, uşşak, saba, kürdi, nihavend, …), an usul, aksak meters, darbuka, or Turkish/Ottoman music.
---

# Makams and usuls

## Makams

- `turkish_makam()` lists the 24 makams (steps in commas, karar, güçlü) and
  the usuls.
- `turkish_makam(name="hicaz", tonic="A")` gives each degree in commas, in
  cents and as an offset from the nearest 12-TET key, plus the scale to use in
  the generators.
- Generators take a makam as their scale: `scale="hicaz"`, `key="A"`. Notes
  land on the nearest 12-TET keys, so koma pitches (e.g. segâh, eviç) are
  approximated until a tuning is loaded.
- Exact pitches: `turkish_makam(name="hicaz", tonic="A", tuning_file=true)`
  writes an .ascl tuning into the User Library. Live's API cannot load
  tunings, so tell the user to open Tunings (or User Library › Tunings) in
  Live's browser and double-click the file (Live 12's Tuning System).

Write with the makam's karar (final) and güçlü (dominant): phrases lean on
the güçlü and resolve to the karar. Mind the seyir (melodic course): rast and
uşşak are ascending (start near the karar), hüseynî and hicaz start around
the güçlü and descend to the karar, and muhayyer starts from the upper
octave.

## Usuls

`generate_usul(track="Darbuka", usul="aksak", cycles=4, set_meter=true)` writes
düm and tek strokes on a drum track (düm → kick pad, tek → snare, light
te/ke/ka → rim, or pads named doum/tek/ka on a darbuka kit). `set_meter=true`
also sets the set's time signature. `ornament` (0..1) fills long strokes, and
`mapping` overrides the pads.

| Usul | Meter | Usul | Meter |
|---|---|---|---|
| nim_sofyan | 2/4 | duyek | 8/8 |
| semai | 3/4 | musemmen | 8/8 |
| sofyan | 4/4 | aksak | 9/8 (2+2+2+3) |
| turk_aksagi | 5/8 | aksak_semai | 10/8 |
| yuruk_semai | 6/8 | curcuna | 10/16 |
| devr_i_hindi, devr_i_turan | 7/8 | | |

A kit to play them on: `build_drum_kit(track=...)` (kick, snare and rim carry
düm, tek and the light strokes). Live's own library has no darbuka samples; if
the user has a darbuka kit (`browser_search(query="darbuka")`), pads named
doum/tek/ka are mapped automatically. For aksak grooves outside the usul
patterns, `generate_euclidean` with E(4,9) gives 2+2+2+3.
