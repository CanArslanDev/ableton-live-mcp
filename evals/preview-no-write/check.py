"""Shows chords without writing them."""

from checklib import no_changes


def check(state, initial):
    return {"nothing-written": no_changes(state, initial)}
