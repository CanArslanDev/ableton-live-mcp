"""Keeps a decision and a reference inside the set."""

from checklib import set_notes


def check(state, initial):
    kept = " | ".join(n["text"].lower() for n in set_notes(state))
    return {"kick-decision": "kick" in kept and "-6" in kept,
            "reference": "archangel" in kept}
