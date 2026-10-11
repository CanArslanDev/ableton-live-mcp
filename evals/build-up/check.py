"""A riser and a filter sweep into a drop."""

from checklib import device, played_notes, track


def check(state, initial):
    others = [t for t in state["tracks"] if t["name"] not in ("Drums", "Bass", "Vox")]
    riser = [c for t in others for c in t.get("arrangement_clips", [])
             if abs(c["end_time"] - 32) < 0.01 and c["start_time"] <= 16.01
             and len(c.get("notes", [])) >= 8 and c["notes"][0][0] < c["notes"][-1][0]]
    bass = track(state, "Bass")
    # A clip envelope over bars 5-8, or recorded automation there
    swept = [c["envelopes"]["Frequency"] for c in bass.get("arrangement_clips", [])
             if c["start_time"] <= 16.01 and c["end_time"] >= 31.99
             and len((c.get("envelopes") or {}).get("Frequency", [])) >= 2]
    lane = [v for t, v in bass.get("automation", {}).get("Auto Filter/Frequency", [])
            if 15.9 <= t <= 32.01]
    rising = (bool(swept) and swept[0][0][2] < swept[0][-1][2]) or \
        (len(lane) >= 2 and lane[0] < max(lane))
    return {"riser": bool(riser),
            "bass-sweep": device(bass, "Auto Filter") is not None and rising,
            "bass-notes-kept": played_notes(bass) == played_notes(track(initial, "Bass"))}
