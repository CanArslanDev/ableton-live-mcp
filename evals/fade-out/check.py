"""Track automation on the main track, recorded in time."""


def fades(t):
    lane = [v for at, v in t.get("automation", {}).get("mixer/Track Volume", [])
            if 47.5 <= at <= 64.01]
    return len(lane) >= 4 and lane[0] > lane[-1] and min(lane) <= 0.1


def check(state, initial):
    # The main track's fader, or every track's
    whole_mix = fades(state["master"]) or all(fades(t) for t in state["tracks"])
    return {"fades-out": whole_mix,
            "rest-untouched": state["tempo"] == initial["tempo"]
            and [t["name"] for t in state["tracks"]] == [t["name"] for t in initial["tracks"]]}
