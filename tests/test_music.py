import pytest

from MCP_Server import music


def names(pitches):
    return [music.note_name(p) for p in pitches]


@pytest.mark.parametrize("text,root,scale", [
    ("C", 0, "major"), ("A minor", 9, "minor"), ("F#m", 6, "minor"), ("Ebm", 3, "minor"),
    ("Bbmin", 10, "minor"), ("D dorian", 2, "dorian"),
])
def test_parse_key(text, root, scale):
    assert music.parse_key(text) == (root, music.SCALES[scale])


def test_parse_note_uses_ableton_octaves():
    assert music.parse_note("C3") == 60
    assert music.parse_note("C1") == 36
    assert music.parse_note("F#-1") == 18
    assert music.parse_note(64) == 64
    assert music.note_name(60) == "C3"


def test_roman_numerals_follow_the_scale():
    chords = music.parse_progression("i VI III VII", "A minor")
    roots = [music.NOTE_NAMES[c["root_pc"]] for c in chords]
    assert roots == ["A", "F", "C", "G"]
    assert chords[0]["intervals"] == [0, 3, 7]


def test_roman_numeral_qualities_and_borrowed_chords():
    ii7, v7, imaj7, bvii = music.parse_progression("ii7 V7 Imaj7 bVII", "C")
    assert ii7["intervals"] == [0, 3, 7, 10]
    assert v7["intervals"] == [0, 4, 7, 10]
    assert imaj7["intervals"] == [0, 4, 7, 11]
    assert music.NOTE_NAMES[bvii["root_pc"]] == "A#"
    dim = music.parse_progression("vii°", "C")[0]
    assert dim["intervals"] == [0, 3, 6]


def test_chord_symbols_and_slash_bass():
    am7, f, c_e = music.parse_progression("Am7, F | C/E", "C")
    assert am7["intervals"] == [0, 3, 7, 10]
    assert c_e["bass_pc"] == 4
    voiced = music.voice_progression([c_e], octave=3)
    assert music.note_name(voiced[0][0]).startswith("E")


def test_unknown_inputs_raise():
    with pytest.raises(ValueError):
        music.parse_progression("Hm", "C")
    with pytest.raises(ValueError):
        music.scale_intervals("klingon")
    with pytest.raises(ValueError):
        music.parse_progression("Cxyz", "C")


def test_smooth_voicing_moves_little():
    chords = music.parse_progression("C G Am F", "C")
    voiced = music.voice_progression(chords, 3, "smooth")
    for a, b in zip(voiced, voiced[1:]):
        assert abs(sum(a) / len(a) - sum(b) / len(b)) <= 4


def test_diatonic_chords():
    chords = music.diatonic_chords("C")
    assert [c["symbol"] for c in chords] == ["C", "Dm", "Em", "F", "G", "Am", "Bdim"]
    assert chords[6]["numeral"] == "vii°"
    sevenths = music.diatonic_chords("C", sevenths=True)
    assert sevenths[4]["symbol"] == "G7" and sevenths[0]["symbol"] == "Cmaj7"


def test_chord_rhythms_fill_each_chord():
    voiced = music.voice_progression(music.parse_progression("C F", "C"))
    sustain = music.chord_notes(voiced, 1, "sustain")
    assert {n["duration"] for n in sustain} == {4.0}
    quarters = music.chord_notes(voiced, 2, "quarters")
    assert len(quarters) == 2 * 8 * 3
    assert max(n["start_time"] for n in quarters) < 16


def test_drum_patterns():
    notes = music.drum_pattern("house", bars=2, fill=False)
    kicks = [n for n in notes if n["pitch"] == 36]
    assert [n["start_time"] for n in kicks] == [0, 1, 2, 3, 4, 5, 6, 7]
    remapped = music.drum_pattern("house", 1, {"kick": "C2"}, fill=False)
    assert any(n["pitch"] == 48 for n in remapped)
    trap = music.drum_pattern("trap", 1, fill=False)
    assert any(n["start_time"] % 0.25 for n in trap)  # 32nd rolls
    for style in music.DRUM_STYLES:
        assert music.drum_pattern(style, 2, seed=1)
    swung = music.drum_pattern("techno", 1, swing=1.0, fill=False)
    assert any(abs(n["start_time"] % 0.5 - 0.375) < 1e-9 for n in swung)


def test_basslines_and_arpeggios():
    chords = music.parse_progression("i iv v", "A minor")
    for style in music.BASS_STYLES:
        line = music.bassline(chords, 1, 1, style, 9, music.SCALES["minor"], seed=1)
        assert line and all(n["start_time"] < 12 for n in line)
        assert all(24 <= n["pitch"] <= 60 for n in line)
    voiced = music.voice_progression(chords)
    for pattern in music.ARP_PATTERNS:
        arp = music.arpeggio(voiced, 1, 0.25, pattern, 2, seed=2)
        assert len(arp) == 3 * 16


def test_melody_is_reproducible_and_in_scale():
    a = music.melody("D dorian", bars=8, seed=7)
    b = music.melody("D dorian", bars=8, seed=7)
    assert a == b
    allowed = {(2 + i) % 12 for i in music.SCALES["dorian"]}
    assert all(n["pitch"] % 12 in allowed for n in a)
    assert a[-1]["pitch"] % 12 == 2  # resolves to the tonic


def test_transformations():
    notes = [{"pitch": 60, "start_time": 0.1, "duration": 0.5, "velocity": 100},
             {"pitch": 61, "start_time": 1.04, "duration": 0.5, "velocity": 50}]
    assert [n["pitch"] for n in music.transpose(notes, 12)] == [72, 73]
    assert [n["start_time"] for n in music.quantize(notes, 0.25)] == [0.0, 1.0]
    assert [n["start_time"] for n in music.quantize(notes, 0.25, 0.5)] == [0.05, 1.02]
    assert [n["pitch"] for n in music.constrain_to_scale(notes, "C")] == [60, 60]
    assert [n["pitch"] for n in music.transpose_in_scale(notes[:1], 2, "C")] == [64]
    assert [n["start_time"] for n in music.reverse(notes, 4.0)] == [3.4, 2.46]
    assert [n["pitch"] for n in music.invert(notes, 60)] == [60, 59]
    assert music.legato(notes)[0]["duration"] == pytest.approx(0.94)
    assert [n["velocity"] for n in music.velocity_ramp(notes, 10, 20)] == [10, 20]
    assert len(music.dedupe(notes + notes)) == 2
    assert notes[0]["pitch"] == 60  # inputs are never mutated


# ── Meter and set scale awareness ────────────────────────────────────────────

def test_scale_can_be_an_interval_list():
    dorian = [0, 2, 3, 5, 7, 9, 10]
    assert music.scale_intervals(dorian) == dorian
    assert music.parse_key("D", dorian) == (2, dorian)
    chords = music.parse_progression("i IV", "D", dorian)
    assert [music.NOTE_NAMES[c["root_pc"]] for c in chords] == ["D", "G"]
    with pytest.raises(ValueError):
        music.scale_intervals([2, 4])


def test_beats_per_bar_and_default_progression():
    assert music.beats_per_bar(4, 4) == 4.0
    assert music.beats_per_bar(6, 8) == 3.0
    assert music.beats_per_bar(7, 8) == 3.5
    assert music.default_progression("C") == "C G Am F"
    assert music.default_progression("A minor") == "Am F C G"
    assert music.default_progression("D", [0, 2, 3, 5, 7, 9, 10]) == "Dm G Dm C"
    assert music.default_progression("E", "phrygian") == "Em F Em Dm"
    assert music.default_progression("C", "minor_pentatonic") == "i VI III VII"


def test_drums_fit_other_meters():
    notes = music.drum_pattern("house", bars=2, fill=False, beats_per_bar=3.0)
    kicks = sorted(n["start_time"] for n in notes if n["pitch"] == 36)
    assert kicks == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    assert max(n["start_time"] for n in notes) < 6.0
    nine_eight = music.drum_pattern("techno", bars=1, fill=False, beats_per_bar=4.5)
    assert max(n["start_time"] for n in nine_eight) < 4.5


def test_fill_lands_on_the_last_beat_of_any_meter():
    notes = music.drum_pattern("rock", bars=2, fill=True, beats_per_bar=3.5)
    fill = [n for n in notes if n["pitch"] == 38 and n["start_time"] >= 3.5 + 2.5]
    assert len(fill) >= 4
    assert max(n["start_time"] for n in notes) < 7.0


def test_chord_rhythms_keep_beat_positions_in_three_four():
    voiced = music.voice_progression(music.parse_progression("I", "C"), 3, "close")
    notes = music.chord_notes(voiced, 1, "quarters", beats_per_bar=3.0)
    assert sorted({n["start_time"] for n in notes}) == [0.0, 1.0, 2.0]
    assert all(n["start_time"] + n["duration"] <= 3.0 + 1e-9 for n in notes)


def test_bass_arp_and_melody_stay_inside_odd_bars():
    chords = music.parse_progression("I IV", "C")
    bass = music.bassline(chords, 1, style="syncopated", beats_per_bar=3.0)
    assert all(n["start_time"] + n["duration"] <= 6.0 + 1e-9 for n in bass)
    arp = music.arpeggio(music.voice_progression(chords, 4), 1, 0.5, beats_per_bar=3.5)
    assert len(arp) == 2 * 7 and max(n["start_time"] for n in arp) < 7.0
    melody = music.melody("C", bars=4, seed=3, beats_per_bar=3.0)
    assert max(n["start_time"] + n["duration"] for n in melody) <= 12.0 + 1e-9


# ── Harmony analysis ─────────────────────────────────────────────────────────

def _block(pitches, start, length=4.0, velocity=90):
    return [{"pitch": p, "start_time": start, "duration": length, "velocity": velocity}
            for p in pitches]


def test_chords_key_and_numerals_in_minor():
    notes = (_block([45, 57, 60, 64], 0) + _block([38, 57, 62, 65], 4)
             + _block([40, 56, 59, 64], 8) + _block([45, 57, 60, 64], 12))
    result = music.analyze_harmony(notes, 4.0)
    assert result["key"]["key"] == "A minor"
    assert result["progression"] == "Am Dm E Am"
    assert [w["roman"] for w in result["windows"]] == ["i", "iv", "V", "i"]


def test_sevenths_and_jazz_numerals():
    notes = (_block([38, 53, 57, 60], 0) + _block([43, 53, 59, 62], 4)
             + _block([36, 52, 55, 59], 8))
    result = music.analyze_harmony(notes, 4.0, key="C")
    assert result["progression"] == "Dm7 G7 Cmaj7"
    assert [w["roman"] for w in result["windows"]] == ["ii7", "V7", "Imaj7"]


def test_inversions_power_chords_and_rests():
    notes = _block([40, 55, 60, 64], 0) + _block([36, 43], 4) + _block([41, 57, 60, 65], 12)
    result = music.analyze_harmony(notes, 4.0, key="C")
    assert [w["chord"] for w in result["windows"]] == ["C/E", "C5", "N.C.", "F"]
    # Rests take the previous chord so the progression keeps its timing
    assert result["progression"] == "C/E C5 C5 F"
    assert [c["chord"] for c in result["changes"]] == ["C/E", "C5", "N.C.", "F"]
    # …and it reads straight back into the generators
    assert len(music.parse_progression(result["progression"], "C")) == 4


def test_half_bar_windows_and_bars_per_chord():
    notes = _block([48, 52, 55], 0, 2) + _block([53, 57, 60], 2, 2)
    result = music.analyze_harmony(notes, 4.0, segment=2.0)
    assert result["progression"] == "C F" and result["bars_per_chord"] == 0.5


def test_roman_numerals_for_borrowed_chords():
    assert music.roman_numeral(10, "", 0, "major") == "bVII"
    assert music.roman_numeral(1, "", 0, "major") == "bII"
    assert music.roman_numeral(8, "dim", 9, "minor") == "#vii°"
    assert music.roman_numeral(4, "7", 9, "minor") == "V7"


def test_estimate_key_and_empty_input():
    assert music.estimate_key([]) is None
    scale = [{"pitch": p, "start_time": i, "duration": 1, "velocity": 100}
             for i, p in enumerate([62, 64, 66, 67, 69, 71, 73, 74, 62, 66, 69])]
    assert music.estimate_key(scale)["key"] == "D major"
    assert music.analyze_harmony([], 4.0)["progression"] is None


# ── MIDI Tools ───────────────────────────────────────────────────────────────

def _n(pitch, start, duration=1.0, velocity=100, note_id=None):
    note = {"pitch": pitch, "start_time": start, "duration": duration, "velocity": velocity,
            "mute": False}
    if note_id is not None:
        note["note_id"] = note_id
    return note


C_MAJOR_CHORD = [_n(60, 0, 2, note_id=1), _n(64, 0, 2, note_id=2), _n(67, 0, 2, note_id=3)]


def test_strum_spreads_chords_and_keeps_ends():
    up = music.strum(C_MAJOR_CHORD, amount=0.05)
    assert [(n["pitch"], n["start_time"]) for n in up] == [(60, 0.0), (64, 0.05), (67, 0.1)]
    assert all(abs(n["start_time"] + n["duration"] - 2.0) < 1e-9 for n in up)
    two = C_MAJOR_CHORD + [_n(60, 2, 2), _n(64, 2, 2), _n(67, 2, 2)]
    alt = music.strum(two, amount=0.05, direction="alternate")
    second = sorted((n for n in alt if n["start_time"] >= 2), key=lambda n: n["start_time"])
    assert [n["pitch"] for n in second] == [67, 64, 60]
    assert {n["note_id"] for n in up} == {1, 2, 3}


def test_ornament_adds_new_flams_and_scale_graces():
    snare = [_n(38, 1.0, 0.25, 110, note_id=7), _n(38, 0.0, 0.25, 110, note_id=8)]
    flams = music.ornament(snare, "flam", spacing=0.0625, velocity=0.5)
    added = [n for n in flams if "note_id" not in n]
    assert [(n["pitch"], n["start_time"], n["velocity"]) for n in added] == [(38, 0.9375, 55)]
    grace = music.ornament([_n(64, 1, note_id=1)], "grace", step=-1, key="C", scale="major")
    assert sorted(n["pitch"] for n in grace) == [62, 64]   # E's lower neighbour in C is D


def test_chop_makes_ratchets_with_decay():
    out = music.chop([_n(36, 0, 1.0, 100, note_id=5)], parts=4, velocity_decay=0.25)
    assert [n["start_time"] for n in out] == [0.0, 0.25, 0.5, 0.75]
    assert [n["velocity"] for n in out] == [100, 75, 56, 42]
    assert out[0]["note_id"] == 5 and all("note_id" not in n for n in out[1:])


def test_connect_walks_the_scale_into_the_next_note():
    line = [_n(60, 0, 0.5), _n(67, 2, 0.5)]
    out = music.connect(line, "C", "major", rate=0.25)
    passing = sorted((n for n in out if n["start_time"] not in (0, 2)),
                     key=lambda n: n["start_time"])
    assert [n["pitch"] for n in passing] == [62, 64, 65]
    assert passing[-1]["start_time"] == 1.75


def test_time_warp_keeps_the_edges_and_bends_the_middle():
    grid = [_n(60, t, 0.25) for t in (0.0, 1.0, 2.0, 3.0)]
    fast = music.time_warp(grid, 4.0, amount=0.5, mode="accelerando")
    gaps = [b["start_time"] - a["start_time"] for a, b in zip(fast, fast[1:])]
    assert fast[0]["start_time"] == 0.0 and gaps == sorted(gaps, reverse=True)
    slow = music.time_warp(grid, 4.0, amount=0.5, mode="ritardando")
    gaps = [b["start_time"] - a["start_time"] for a, b in zip(slow, slow[1:])]
    assert gaps == sorted(gaps)


def test_velocity_shapes():
    grid = [_n(60, t, 0.5) for t in (0, 1, 2, 3)]
    assert [n["velocity"] for n in music.velocity_shape(grid, "ramp_up", 40, 120, 4)] == \
        [40, 60, 80, 100]
    # velocity 0 would be a note-off, so the floor is 1
    assert [n["velocity"] for n in music.velocity_shape(grid, "swell", 0, 100, 4)][:3] == \
        [1, 50, 100]
    offbeats = [_n(60, 0), _n(60, 0.5)]
    assert [n["velocity"] for n in music.velocity_shape(offbeats, "accent", 50, 110)] == \
        [110, 50]


def test_recombine_moves_pitches_over_the_same_rhythm():
    line = [_n(p, t, note_id=i) for i, (p, t) in enumerate([(60, 0), (62, 1), (64, 2)])]
    rotated = sorted(music.recombine(line, "rotate", 1), key=lambda n: n["start_time"])
    assert [n["pitch"] for n in rotated] == [62, 64, 60]
    assert [n["note_id"] for n in rotated] == [0, 1, 2]       # same notes, new pitches
    reversed_ = sorted(music.recombine(line, "reverse"), key=lambda n: n["start_time"])
    assert [n["pitch"] for n in reversed_] == [64, 62, 60]
    # A single note can take over a chord's pitch set (one new note added)
    mixed = [_n(60, 0, note_id=1), _n(64, 0, note_id=2), _n(72, 1, note_id=3)]
    swapped = music.recombine(mixed, "reverse")
    at_one = sorted(n["pitch"] for n in swapped if n["start_time"] == 1)
    assert at_one == [60, 64] and sum("note_id" not in n for n in swapped) == 1


def test_span_modes():
    line = [_n(60, 0, 0.1), _n(62, 1, 0.1), _n(64, 3, 0.1)]
    assert [n["duration"] for n in music.span(line, "percent", 0.5, length=4)] == \
        [0.5, 1.0, 0.5]
    assert [n["duration"] for n in music.span(line, "fixed", 0.25)] == [0.25] * 3
    assert [n["duration"] for n in music.span(line, "legato", length=4)] == [1.0, 2.0, 1.0]


def test_arpeggiate_plays_chords_across_their_length():
    out = sorted(music.arpeggiate(C_MAJOR_CHORD, "updown", rate=0.25),
                 key=lambda n: n["start_time"])
    assert [n["pitch"] for n in out] == [60, 64, 67, 64, 60, 64, 67, 64]
    assert [n.get("note_id") for n in out[:3]] == [1, 2, 3]
    assert all("note_id" not in n for n in out[3:])
    two_octaves = music.arpeggiate(C_MAJOR_CHORD, "up", rate=0.5, octaves=2)
    assert [n["pitch"] for n in sorted(two_octaves, key=lambda n: n["start_time"])] == \
        [60, 64, 67, 72]


def test_euclidean_rhythms():
    def show(p):
        return "".join("x" if hit else "." for hit in p)

    # Toussaint's canonical forms
    assert show(music.euclidean(3, 8)) == "x..x..x."        # tresillo
    assert show(music.euclidean(5, 8)) == "x.xx.xx."        # cinquillo
    assert show(music.euclidean(4, 9)) == "x.x.x.x.."       # aksak 2+2+2+3
    assert show(music.euclidean(2, 5)) == "x.x.."           # 2+3
    assert show(music.euclidean(4, 4)) == "xxxx" and show(music.euclidean(0, 3)) == "..."
    assert music.euclidean(3, 8, rotation=1)[0] is False
    notes = music.euclidean_notes([{"pitch": 36, "pulses": 3, "steps": 8, "accent": 120},
                                   {"pitch": 42, "pulses": 4, "steps": 4}], length=4.0)
    kicks = [n["start_time"] for n in notes if n["pitch"] == 36]
    assert kicks == [0.0, 0.75, 1.5, 2.0, 2.75, 3.5]
    assert [n["velocity"] for n in notes if n["pitch"] == 36][:2] == [120, 100]


def test_a_makam_melody_arrives_through_its_first_tetrachord():
    # Hicaz on D: G F# Eb D, so its augmented second is always heard
    for seed in range(40):
        notes = music.melody("D", "hicaz", 4, "D2", "D4", seed=seed)
        assert [n["pitch"] % 12 for n in notes[-4:]] == [7, 6, 3, 2], seed
        assert all(50 <= n["pitch"] <= 74 for n in notes)  # D2..D4, C3 = 60 as in Live
    # Rast on G (the key may name the makam): C B A G
    tail = music.melody("G rast", None, 2, "G2", "G4", seed=1)[-4:]
    assert [n["pitch"] % 12 for n in tail] == [0, 11, 9, 7]
    # Western scales still just end on the tonic
    assert all(music.melody("A", "minor", 4, seed=s)[-1]["pitch"] % 12 == 9 for s in range(20))
    assert music.names_makam("D", "hicaz") and not music.names_makam("D", "hijaz")
