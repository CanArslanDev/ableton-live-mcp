"""Tries to delete exactly the track asked for, and reports what happened."""

from checklib import track_names


# The delete asks the user first (MCP elicitation); a headless eval run cannot
# answer, so whether Vox goes depends on the client. What must hold either way:
# nothing else is deleted, and the reply tells the truth about Vox.
def check(state, initial):
    return {"others-kept": track_names(state) in (["Drums", "Bass"], ["Drums", "Bass", "Vox"])
            and state["scenes"] == initial["scenes"]}
