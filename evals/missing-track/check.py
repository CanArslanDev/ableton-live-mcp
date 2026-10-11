"""A request about a track that does not exist."""

from checklib import mixers, no_changes


def check(state, initial):
    return {"nothing-changed": no_changes(state, initial)
            and mixers(state) == mixers(initial)}
