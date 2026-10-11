"""A processing chain for a bass."""

from checklib import track


def check(state, initial):
    names = [d["name"] for d in track(state, "Bass")["devices"]]
    return {"chain": len(names) >= 3,
            "compression": "Compressor" in names or "Glue Compressor" in names,
            "eq": "EQ Eight" in names}
