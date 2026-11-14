"""A finished reference job's blueprint built into the empty set: tempo and
meter, the 808 and drum tracks, the Simplers holding the blueprint's samples
(copies in the User Library), the notes, the pads' Volumes and the locators."""


def _first(devices):
    return devices[0] if devices else {}


def check(state, initial):
    tracks = {t["name"]: t for t in state["tracks"]}
    bass, drums = tracks.get("808 (synth)", {}), tracks.get("Drums (library)", {})
    simpler = _first(bass.get("devices"))
    chains = _first(drums.get("devices")).get("chains", {})

    def pad(note):
        return _first(_first(chains.get(note)).get("devices"))

    def notes(track):
        return sum(len(c.get("notes", [])) for c in track.get("arrangement_clips", []))

    held = [simpler.get("sample", ""), pad("36").get("sample", ""), pad("38").get("sample", ""),
            pad("42").get("sample", "")]
    return {
        "tempo-and-meter": state["tempo"] == 120 and state["signature"] == "4/4",
        "tracks": bool(bass) and bool(drums),
        "samples": all(h.startswith(p) for h, p in zip(held, ("808 ", "kick ", "snare ", "hat-closed "))),
        "notes": notes(bass) == 3 and notes(drums) == 24,
        "pad-levels": [pad(n).get("parameters", {}).get("Volume") for n in ("36", "38", "42")]
        == ["-3.0 dB", "-6.6 dB", "-9.5 dB"],
        "locators": [(c["name"], c["time"]) for c in state["locators"]] == [("A (1)", 0), ("B (2)", 4)],
    }
