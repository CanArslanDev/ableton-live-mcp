"""Sets Live 12's Key & Scale."""


def check(state, initial):
    return {"root": state["key"]["root_note"] == 2, "scale": state["key"]["scale_name"] == "Dorian"}
