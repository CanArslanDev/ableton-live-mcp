"""Snapshots of how a set sounds: mixer settings and every device parameter
(racks included), for A/B comparisons and for going back after an experiment.

Reading and planning are pure functions over batch results, so they are
testable without Live. Restoring matches tracks by name, devices by position
and name, and parameters by position and name, so a snapshot survives tracks
being added or reordered. Automated parameters are never restored: setting
one would switch its automation off.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

MAX_RACK_DEPTH = 3
PARAM_PROPS = ["name", "value", "automation_state"]
DEVICE_PROPS = ["name", "class_name", "can_have_chains"]

RunBatch = Callable[[list[dict]], list[dict]]


def snapshot_dir() -> Path:
    override = os.environ.get("ABLETON_MCP_SNAPSHOT_DIR")
    return Path(override) if override else Path.home() / ".ableton-mcp" / "snapshots"


def _get(path: str, properties: list[str]) -> dict:
    # ids: False keeps thousands of parameters out of the script's id registry
    return {"type": "lom_get", "params": {"path": path, "properties": properties,
                                          "ids": False}}


def _ok(step: dict) -> Any:
    return step.get("result") if step.get("status") == "success" else None


def _setting(item: dict | None) -> dict | None:
    if not isinstance(item, dict) or not isinstance(item.get("value"), (int, float)):
        return None
    auto = item.get("automation_state")
    return {"value": item["value"], "auto": auto if isinstance(auto, int) else 0}


def _items(result: Any) -> list[dict]:
    return result.get("items", []) if isinstance(result, dict) else []


def capture(run_batch: RunBatch, track_paths: list[str]) -> list[dict]:
    """Mixer and device parameters of the tracks at track_paths."""
    tracks: list[dict] = []
    steps: list[dict] = []
    for path in track_paths:
        steps += [_get(path, ["name", "mute"]),
                  _get(f"{path} mixer_device volume", ["value", "automation_state"]),
                  _get(f"{path} mixer_device panning", ["value", "automation_state"]),
                  _get(f"{path} mixer_device sends", ["value", "automation_state"]),
                  _get(f"{path} devices", DEVICE_PROPS)]
    results = run_batch(steps)
    device_lists: list[tuple[list, str, list[dict]]] = []
    for i, path in enumerate(track_paths):
        head, volume, panning, sends, devices = (_ok(s) for s in results[i * 5:i * 5 + 5])
        if not isinstance(head, dict):
            continue
        mute = head.get("mute")
        track = {"path": path, "name": head.get("name"),
                 "mixer": {"volume": _setting(volume), "panning": _setting(panning),
                           "sends": [_setting(s) for s in _items(sends)],
                           "mute": mute if isinstance(mute, bool) else None},
                 "devices": []}
        tracks.append(track)
        device_lists.append((track["devices"], f"{path} devices", _items(devices)))
    _capture_devices(run_batch, device_lists, depth=0)
    return tracks


def _capture_devices(run_batch: RunBatch, device_lists: list, depth: int) -> None:
    """Fill device lists level by level: one batch per rack nesting level."""
    pending: list[tuple[dict, str]] = []
    for target, list_path, items in device_lists:
        for index, item in enumerate(items):
            device = {"path": f"{list_path} {index}", "name": item.get("name"),
                      "class_name": item.get("class_name"), "parameters": [],
                      "chains": []}
            target.append(device)
            pending.append((device, bool(item.get("can_have_chains")) and depth < MAX_RACK_DEPTH))
    if not pending:
        return
    steps: list[dict] = []
    for device, racks in pending:
        steps.append(_get(f"{device['path']} parameters", PARAM_PROPS))
        if racks:
            steps.append(_get(f"{device['path']} chains", ["name"]))
    results = iter(run_batch(steps))
    chain_reads: list[tuple[dict, str]] = []
    for device, racks in pending:
        device["parameters"] = [dict(_setting(p) or {"value": None, "auto": 0},
                                     name=p.get("name")) for p in _items(_ok(next(results)))]
        if racks:
            for index, chain in enumerate(_items(_ok(next(results)))):
                entry = {"path": f"{device['path']} chains {index}", "name": chain.get("name"),
                         "mixer": {}, "devices": []}
                device["chains"].append(entry)
                chain_reads.append((entry, entry["path"]))
    if not chain_reads:
        return
    steps = []
    for _entry, path in chain_reads:
        steps += [_get(f"{path} mixer_device volume", ["value", "automation_state"]),
                  _get(f"{path} mixer_device panning", ["value", "automation_state"]),
                  _get(f"{path} devices", DEVICE_PROPS)]
    results = run_batch(steps)
    nested = []
    for i, (entry, path) in enumerate(chain_reads):
        volume, panning, devices = (_ok(s) for s in results[i * 3:i * 3 + 3])
        entry["mixer"] = {"volume": _setting(volume), "panning": _setting(panning)}
        nested.append((entry["devices"], f"{path} devices", _items(devices)))
    _capture_devices(run_batch, nested, depth + 1)


# ── Comparing ────────────────────────────────────────────────────────────────

def _match_tracks(saved: list[dict], current: list[dict]) -> list[tuple[dict, dict | None]]:
    names = [t.get("name") for t in current]
    by_path = {t["path"]: t for t in current}
    pairs = []
    for track in saved:
        name = track.get("name")
        if name and names.count(name) == 1:
            pairs.append((track, current[names.index(name)]))
        else:
            pairs.append((track, by_path.get(track["path"])))
    return pairs


def _match_devices(saved: list[dict], current: list[dict]) -> list[tuple[dict, dict | None]]:
    used: set[int] = set()
    pairs = []
    for index, device in enumerate(saved):
        match = None
        if index < len(current) and current[index].get("name") == device.get("name") \
                and current[index].get("class_name") == device.get("class_name"):
            match = index
        else:
            for j, candidate in enumerate(current):
                if j not in used and candidate.get("name") == device.get("name") \
                        and candidate.get("class_name") == device.get("class_name"):
                    match = j
                    break
        if match is not None:
            used.add(match)
        pairs.append((device, current[match] if match is not None else None))
    return pairs


def _changed(saved: dict | None, now: dict | None) -> bool:
    if not saved or not now or saved.get("value") is None or now.get("value") is None:
        return False
    return abs(float(saved["value"]) - float(now["value"])) > 1e-6


def compare(saved: list[dict], current: list[dict]) -> dict:
    """Differences between two captures, each with the path to set in `current`.

    Returns {"changes": [...], "automated": [...], "missing": [...]}; a change
    is {"track", "where", "parameter", "path", "property", "saved", "now"}.
    """
    report: dict[str, list] = {"changes": [], "automated": [], "missing": []}

    def setting(track, where, label, path, old, new):
        if not _changed(old, new):
            return
        entry = {"track": track, "where": where, "parameter": label, "path": path,
                 "property": "value", "saved": old["value"], "now": new["value"]}
        if old.get("auto") or new.get("auto"):
            report["automated"].append(entry)
        else:
            report["changes"].append(entry)

    def mixer(track, where, old, new, base):
        for key, label in (("volume", "volume"), ("panning", "pan")):
            setting(track, where, label, f"{base} mixer_device {key}", old.get(key), new.get(key))
        for i, (o, n) in enumerate(zip(old.get("sends") or [], new.get("sends") or [])):
            setting(track, where, f"send {chr(65 + i)}", f"{base} mixer_device sends {i}", o, n)

    def devices(track, saved_list, current_list):
        for old, new in _match_devices(saved_list, current_list):
            if new is None:
                report["missing"].append(f"{track}: device {old.get('name')!r}")
                continue
            names = [p.get("name") for p in new["parameters"]]
            for index, param in enumerate(old["parameters"]):
                if index < len(names) and names[index] == param.get("name"):
                    target = index
                elif param.get("name") in names:
                    target = names.index(param.get("name"))
                else:
                    continue
                setting(track, new["name"], param.get("name"),
                        f"{new['path']} parameters {target}", param, new["parameters"][target])
            chains = {c.get("name"): c for c in new["chains"]}
            for chain in old["chains"]:
                twin = chains.get(chain.get("name"))
                if twin is None:
                    continue
                mixer(track, f"{new['name']} / {chain.get('name')}", chain["mixer"],
                      twin["mixer"], twin["path"])
                devices(track, chain["devices"], twin["devices"])

    for old, new in _match_tracks(saved, current):
        name = old.get("name") or old["path"]
        if new is None:
            report["missing"].append(f"track {name!r}")
            continue
        mixer(name, "mixer", old["mixer"], new["mixer"], new["path"])
        o_mute, n_mute = old["mixer"].get("mute"), new["mixer"].get("mute")
        if isinstance(o_mute, bool) and isinstance(n_mute, bool) and o_mute != n_mute:
            report["changes"].append({"track": name, "where": "mixer", "parameter": "mute",
                                      "path": new["path"], "property": "mute",
                                      "saved": o_mute, "now": n_mute})
        devices(name, old["devices"], new["devices"])
    return report


def restore_ops(changes: list[dict]) -> list[dict]:
    return [{"type": "lom_set", "params": {"path": c["path"], "property": c["property"],
                                           "value": c["saved"]}} for c in changes]


# ── Storage ──────────────────────────────────────────────────────────────────

def _file(name: str) -> Path:
    clean = re.sub(r"[^\w\- ]+", "_", name.strip())[:80].strip()
    if not clean:
        raise ValueError("Give the snapshot a name")
    return snapshot_dir() / f"{clean}.json"


def save(name: str, tracks: list[dict]) -> dict:
    folder = snapshot_dir()
    folder.mkdir(parents=True, exist_ok=True)
    data = {"name": name, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "tracks": tracks}
    _file(name).write_text(json.dumps(data), encoding="utf-8")
    return data


def load(name: str) -> dict:
    path = _file(name)
    if not path.is_file():
        known = ", ".join(s["name"] for s in list_all()) or "none"
        raise ValueError(f"No snapshot named {name!r} (saved: {known})")
    return json.loads(path.read_text(encoding="utf-8"))


def list_all() -> list[dict]:
    folder = snapshot_dir()
    if not folder.is_dir():
        return []
    entries = []
    for path in sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        entries.append({"name": data.get("name"), "created": data.get("created"),
                        "tracks": [t.get("name") for t in data.get("tracks", [])],
                        "parameters": sum(count_parameters(t) for t in data.get("tracks", []))})
    return entries


def delete(name: str) -> bool:
    path = _file(name)
    if path.is_file():
        path.unlink()
        return True
    return False


def count_parameters(track: dict) -> int:
    def devices(items):
        return sum(len(d["parameters"]) + sum(devices(c["devices"]) for c in d["chains"])
                   for d in items)
    return devices(track.get("devices", []))
