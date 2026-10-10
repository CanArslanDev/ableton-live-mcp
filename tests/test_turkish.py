"""Makam scales, the .ascl tuning and usul rhythms."""

import pytest

from MCP_Server import music, turkish


def test_every_makam_fills_an_octave_of_53_commas():
    for name, (steps, karar, guclu, _built) in turkish.MAKAMS.items():
        assert sum(steps) == 53, name
        assert len(steps) == 7, name
        assert karar in music.NOTE_NAMES and guclu in music.NOTE_NAMES, name


def test_twelve_tone_approximations():
    assert turkish.intervals_12tet("rast") == [0, 2, 4, 5, 7, 9, 11]       # Rast ≈ major
    assert turkish.intervals_12tet("Uşşak") == [0, 2, 3, 5, 7, 8, 10]      # ≈ natural minor
    assert turkish.intervals_12tet("Hüseynî") == [0, 2, 3, 5, 7, 9, 10]    # ≈ dorian
    assert turkish.intervals_12tet("kürdî") == [0, 1, 3, 5, 7, 8, 10]      # ≈ phrygian
    assert turkish.intervals_12tet("Hicaz") == [0, 1, 4, 5, 7, 9, 10]
    assert turkish.intervals_12tet("Zirgüleli Hicaz") == [0, 1, 4, 5, 7, 8, 11]
    assert turkish.intervals_12tet("Saba") == [0, 2, 3, 4, 7, 8, 10]
    assert turkish.intervals_12tet("Hüzzam makamı") == [0, 1, 3, 4, 7, 8, 10]


def test_makam_names_are_forgiving():
    assert turkish.find_makam("Kürdîlihicazkâr") == "kurdilihicazkar"
    assert turkish.find_makam("NİHAVEND") == "nihavend"
    assert turkish.find_makam("Beyati") == "bayati"
    assert turkish.find_makam("bebop") is None


def test_makams_work_wherever_a_scale_does():
    root, intervals = music.parse_key("A hicaz")
    assert root == 9 and intervals == [0, 1, 4, 5, 7, 9, 10]
    assert music.scale_intervals("Uşşak") == [0, 2, 3, 5, 7, 8, 10]
    melody = music.melody("D", "kurdi", bars=2, seed=1)
    assert {n["pitch"] % 12 for n in melody} <= {2, 3, 5, 7, 9, 10, 0}
    with pytest.raises(ValueError, match="Turkish makam"):
        music.scale_intervals("no_such_scale")


def test_ascl_keeps_the_karar_and_moves_degrees_to_comma_pitches():
    name, text = turkish.ascl("hicaz", 9, music.NOTE_NAMES)
    assert name == "Makam Hicaz (A).ascl"
    lines = text.splitlines()
    count = lines.index("12")
    values = [line.split("!")[0].strip() for line in lines[count + 2:count + 14]]
    cents = {music.NOTE_NAMES[i + 1]: float(v) for i, v in enumerate(values[:11])}
    assert values[11] == "2/1"
    assert all("." in v for v in values[:11])          # cents need a dot in Scala files
    assert cents["A"] == pytest.approx(900.0)            # karar stays put
    assert cents["A#"] == pytest.approx(1013.207, abs=0.01)    # B♭: 5 commas above A
    assert cents["C#"] == pytest.approx(84.906, abs=0.01)      # 17 commas above A
    assert cents["B"] == pytest.approx(1100.0)                 # not in the makam
    assert "! @ABL REFERENCE_PITCH 3 9 440.000" in lines
    assert lines[-2].startswith('! @ABL NOTE_NAMES "C" "C#"')


def test_every_usul_cycle_fills_its_meter():
    for name, (numerator, _denominator, strokes, _vocal) in turkish.USULS.items():
        assert sum(units for _stroke, units in strokes) == numerator, name
        assert {stroke for stroke, _ in strokes} <= set(turkish.STROKES), name


def test_aksak_is_two_two_two_three():
    pads = {"düm": 36, "tek": 38, "te": 37, "ke": 37, "ka": 37}
    notes = turkish.usul_notes("Aksak", 1, pads)
    assert [(n["pitch"], n["start_time"]) for n in notes] == [
        (36, 0.0), (37, 1.0), (37, 1.5), (36, 2.0), (38, 3.0), (38, 4.0)]
    assert notes[0]["velocity"] > notes[4]["velocity"] > notes[2]["velocity"]
    two = turkish.usul_notes("aksak", 2, pads)
    assert two[6]["start_time"] == 4.5                  # a 9/8 cycle is 4.5 beats
    busy = turkish.usul_notes("aksak", 1, pads, ornament=1.0, seed=1)
    assert len(busy) == 6 + 3                            # every long stroke decorated


def test_usul_names_are_forgiving():
    assert turkish.usul("Türk Aksağı")["meter"] == "5/8"
    assert turkish.usul("Devr-i Hindî")["units"] == 7
    assert turkish.usul("yürük semai")["vocal"] == "Düm Tek Tek Düm Teek"
    with pytest.raises(ValueError, match="Unknown usul"):
        turkish.usul("waltz")
