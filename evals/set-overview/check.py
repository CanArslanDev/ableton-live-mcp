"""Describes the set without changing it."""

from checklib import no_changes


def check(state, initial):
    return {"nothing-changed": no_changes(state, initial)}
