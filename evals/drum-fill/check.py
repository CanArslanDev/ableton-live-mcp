"""A fill in the Arrangement, the groove around it kept."""

from checklib import played_notes, track


def check(state, initial):
    drums = track(state, "Drums")
    fill = played_notes(drums, 28, 32)
    snares = [n for n in fill if n[0] == 38]
    first, last = [n for n in snares if n[1] < 29], [n for n in snares if n[1] >= 31]
    kicks = [n[1] for n in played_notes(drums) if n[0] == 36 and not 28 <= n[1] < 32]
    return {"fill": len(snares) >= 6 and len(last) > len(first),
            "groove-kept": kicks == [b for b in range(64) if not 28 <= b < 32]}
