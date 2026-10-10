"""MCP prompts: ready-made workflows a client can offer (e.g. as slash commands).

Each prompt is a short plan written in terms of this server's tools. The
assistant still decides the details and asks before destructive changes.
"""

from __future__ import annotations

# Integrated loudness / true-peak ceilings the platforms normalise to. Spotify
# and SoundCloud publish theirs; the others are the commonly measured values.
LOUDNESS_TARGETS = {
    "spotify": ("-14 LUFS", "-1 dBTP (-2 dBTP if the master is louder than -14 LUFS)"),
    "apple_music": ("-16 LUFS", "-1 dBTP"),
    "youtube": ("-14 LUFS", "-1 dBTP"),
    "tidal": ("-14 LUFS", "-1 dBTP"),
    "amazon_music": ("-14 LUFS", "-2 dBTP"),
    "soundcloud": ("-14 LUFS", "-1 dBTP"),
    "deezer": ("-15 LUFS", "-1 dBTP"),
    "club": ("no normalisation; masters for DJs are often -6 to -9 LUFS", "-0.3 dBTP"),
}


def _target(platform: str) -> str:
    key = platform.strip().lower().replace(" ", "_")
    lufs, peak = LOUDNESS_TARGETS.get(key, LOUDNESS_TARGETS["spotify"])
    name = key if key in LOUDNESS_TARGETS else "spotify"
    return f"{name}: integrated loudness {lufs}, true peak at or below {peak}"


def register_prompts(mcp) -> None:
    @mcp.prompt(name="mix_check", title="Mix check")
    def mix_check(platform: str = "spotify", section: str = "the loop region") -> str:
        """Listen to the mix, then report problems with concrete fixes."""
        return f"""Check the mix of the current Ableton set.

1. get_session_overview to learn the tracks, groups, returns and devices.
2. snapshot action="save" name="before mix check", so every change can be undone
   as a whole or A/B-compared.
3. mix_doctor on {section} with platform="{platform}" (for a Session loop pass
   scene=<its scene>; otherwise start_beat/length_beats in the Arrangement — pick
   the busiest 16 bars if there is no loop).
4. Weigh its findings against the delivery target — {_target(platform)} — and a
   balanced mix: low end mono below ~150 Hz, no clipping, crest factor not
   crushed, no two parts fighting for the same band.
5. Report at most 7 findings, most important first. For each: what was
   measured, why it matters, and the exact change (track, device, parameter,
   value), e.g. "Pad: EQ Eight band 2 bell -3 dB at 200 Hz (it masks the bass)".
6. Ask before changing anything. After the user agrees, apply the changes, run
   mix_doctor on the same section again and show before/after numbers. Offer
   snapshot action="restore" name="before mix check" if the user prefers the old sound."""

    @mcp.prompt(name="beat_from_scratch", title="Beat from scratch")
    def beat_from_scratch(genre: str = "house", bpm: str = "", key: str = "",
                          bars: str = "4") -> str:
        """Build a short loop: drums, bass, chords and a top line."""
        tempo = f"{bpm} BPM" if bpm else "a tempo typical for the genre"
        key_text = key or "a key that suits the genre (set it as the set's Key & Scale)"
        return f"""Make a {bars}-bar {genre} loop in Ableton Live at {tempo}, in {key_text}.

1. get_session_overview, then set_song_properties (tempo, root_note,
   scale_name). Reuse empty tracks before creating new ones.
2. Drums: a MIDI track, build_drum_kit with a style word that suits {genre}
   (e.g. "808" for trap, "909" for techno; source="user" to use the user's own
   samples), then generate_drum_pattern in a {genre}-appropriate style.
3. Chords: an instrument (browser_search + browser_load, or add_device), then
   generate_chord_progression (pick a progression and rhythm idiomatic for {genre}).
4. Bass: analyze_harmony on the chord clip, then generate_bassline with its
   progression and bars_per_chord so the bass follows the chords.
5. Optional top line with generate_melody or generate_arpeggio.
6. Name and colour the tracks and clips (manage_track, set_clip_properties),
   set rough levels with set_track_mixer, put the clips in one scene.
7. mix_doctor with scene=<that scene> and length_beats of the loop; fix anything
   clearly off (levels, masking, clipping). Fire the scene and summarise what you made."""

    @mcp.prompt(name="match_reference", title="Match a reference track")
    def match_reference(reference_path: str, section: str = "the drop") -> str:
        """Move the mix's tonal balance and loudness towards a reference."""
        return f"""Bring this mix closer to the reference track at {reference_path}.

1. snapshot action="save" name="before reference match".
2. compare_to_reference with reference_path="{reference_path}" over {section}
   (scene=... for a Session loop, start_beat/length_beats in the Arrangement).
3. Explain the differences in plain words: loudness, dynamics, width, and
   where the tonal balance differs (which frequencies, how many dB).
4. Propose the EQ moves it suggests (eq_eight_settings, about 60% of the way)
   on the master or the relevant bus, plus any loudness/limiting change. Ask first.
5. Apply with add_device (device="EQ Eight", settings=eq_eight_settings), run
   compare_to_reference again and show what moved. Let the user A/B with
   snapshot restore "before reference match" and decide."""

    @mcp.prompt(name="follow_the_harmony", title="Write a part that follows the chords")
    def follow_the_harmony(source_track: str, part: str = "bassline",
                           target_track: str = "") -> str:
        """Analyse the chords of one clip and write a matching new part."""
        target = target_track or "a suitable track (create one if needed)"
        return f"""Write a {part} that follows the chords on track "{source_track}".

1. read the set with get_session_overview and find the clip on
   "{source_track}" (ask which one if there are several).
2. analyze_harmony on it. Tell the user the key and the progression
   (with roman numerals) in one line.
3. Generate the {part} on {target} with the matching generator, passing the
   detected progression, key and bars_per_chord so it lines up bar for bar.
4. Fire both clips together and offer two variations (different seed or
   style) the user can choose from."""

    @mcp.prompt(name="arrange_from_loop", title="Arrange a loop into a song")
    def arrange_from_loop(structure: str = "intro 8, build 8, drop 16, break 8, "
                                           "drop 16, outro 8") -> str:
        """Lay Session clips out in the Arrangement as a full song."""
        return f"""Turn the Session clips of this set into an Arrangement with this
structure (lengths in bars): {structure}.

1. get_session_overview: list the clips per track and decide which tracks
   play in which section (intro and outro sparse, drops full, breaks
   without drums or bass). Show the plan as a table and wait for approval.
2. build_arrangement with one entry per section: a scene plus "except" for
   the tracks that sit out, or explicit "clips" per track. It repeats loops to
   fill each section, cuts MIDI clips exactly and puts a named locator at
   every section start.
3. Add movement at transitions: draw filter or volume ramps with
   write_clip_automation on a Session copy of the clip that leads into a
   drop, then duplicate_to_arrangement that copy over the transition
   (envelopes can only be written on Session clips; the copy keeps them).
   Add a fill before each new section (generate_drum_pattern with fill=True).
4. Switch to the Arrangement (select_in_live view="arrangement") and
   summarise the layout."""

    @mcp.prompt(name="release_check", title="Release check")
    def release_check(platform: str = "spotify") -> str:
        """Measure the master against a platform's loudness target."""
        return f"""Check whether this set's master is ready to release on {platform}.

Target: {_target(platform)}.

1. render_and_analyze the master over the whole arrangement (song_length
   from get_session_overview; for long songs, the loudest section).
2. Report integrated loudness, true peak, loudness range, crest factor,
   clipping, stereo correlation and low-end mono compatibility against the
   target, each as pass / warn / fail.
3. If something fails, propose the smallest fix on the master chain (e.g.
   limiter ceiling, gain into the limiter) and ask before applying it.
   Re-measure after any change."""

    @mcp.prompt(name="explain_my_set", title="Explain my set")
    def explain_my_set(focus: str = "everything") -> str:
        """Describe how the set is built, as a teacher would."""
        return f"""Explain this Ableton set to me as a producer and teacher would;
focus on: {focus}.

1. get_session_overview for structure, tracks, devices and routing.
2. analyze_harmony on the main harmonic clips, read_clip_notes on drums
   and bass to describe the rhythm.
3. Explain the arrangement, the harmony (key, progression, roman numerals),
   the groove and the sound design / mix choices you can see, in plain
   language. Point out one or two things worth trying next.
Do not change anything in the set."""
