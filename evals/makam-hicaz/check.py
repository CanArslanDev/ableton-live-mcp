"""A melody in makam Hicaz."""

from checklib import new_midi_clips, pitch_classes


HICAZ_ON_D = {2, 3, 6, 7, 9, 10, 11, 0}  # D Eb F# G A, then Bb or B, C


def check(state, initial):
    clips = new_midi_clips(state, initial, "Bass")
    notes = clips[0]["notes"] if clips else []
    classes = pitch_classes(notes)
    # F# over D, without E or F: the raised third that sets Hicaz apart from
    # the minor makams on the same karar
    return {"melody": len(notes) >= 6,
            "hicaz-notes": bool(notes) and classes <= HICAZ_ON_D,
            "hicaz-third": 6 in classes}
