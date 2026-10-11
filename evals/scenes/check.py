"""Creates named scenes."""


def check(state, initial):
    names = [s["name"] for s in state["scenes"]]
    return {"verse": "Verse" in names, "chorus": "Chorus" in names,
            "kept": names[:2] == ["Intro", "Drop"]}
