"""Named locators at bar positions."""


def check(state, initial):
    found = {(c["name"].strip().lower(), c["time"]) for c in state["locators"]}
    return {"intro": ("intro", 0) in found, "verse": ("verse", 32) in found,
            "drop": ("drop", 64) in found}
