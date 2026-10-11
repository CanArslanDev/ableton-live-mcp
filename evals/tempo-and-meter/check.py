"""Sets the tempo and an odd meter."""


def check(state, initial):
    return {"tempo": state["tempo"] == 128, "meter": state["signature"] == "7/8",
            "tracks-untouched": state["tracks"] == initial["tracks"]}
