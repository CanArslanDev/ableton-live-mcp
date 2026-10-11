"""Read Ableton Live Set files (.als) without Live.

An .als file is gzip-compressed XML. This module answers what a producer asks
about a set on disk: tempo, meter, key, tracks with their devices (racks,
third-party plugins, Max for Live), mixer, clips with their MIDI notes,
scenes, locators, and the samples it uses, including the missing ones. It
never writes sets.

Layout, as checked on Live 12.4's own sets (Core Library templates, lessons,
the "Patience" demo) and sets saved by 12.3:

- Live 12 calls the master track MainTrack; earlier versions MasterTrack.
  Tempo and time signature live in its mixer.
- Device elements are named after Live's device class (`Device.class_name`
  in the LOM): Eq8 is EQ Eight, StereoGain is Utility (DEVICE_NAMES, read
  from Live 12.4 by inserting each device).
- A time signature is stored as (numerator - 1) + 99 * log2(denominator).
- ScaleInformation stores the root (0 = C) and the scale as an index into
  Live's scale list (LIVE_SCALES; 0 Major and 1 Minor confirmed against the
  notes of the demo set, the rest in the order Live and Push list them).
- A Drum Rack pad's note is 128 - BranchInfo/ReceivingNote.
- Note times are in the clip's own beats, as the LOM reports them.
"""

from __future__ import annotations

import gzip
import io
import math
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any

# Device class (XML element) → the name Live shows, where they differ
DEVICE_NAMES = {
    "AudioEffectGroupDevice": "Audio Effect Rack", "AutoFilter2": "Auto Filter",
    "AutoPan2": "Auto Pan-Tremolo", "AutoShift": "Auto Shift", "BeatRepeat": "Beat Repeat",
    "ChannelEq": "Channel EQ", "Chorus2": "Chorus-Ensemble", "Compressor2": "Compressor",
    "DrumBuss": "Drum Buss", "DrumGroupDevice": "Drum Rack", "Eq8": "EQ Eight",
    "Erosion2": "Erosion", "FilterDelay": "Filter Delay", "FilterEQ3": "EQ Three",
    "GlueCompressor": "Glue Compressor", "GrainDelay": "Grain Delay", "Hybrid": "Hybrid Reverb",
    "InstrumentGroupDevice": "Instrument Rack", "InstrumentImpulse": "Impulse",
    "InstrumentMeld": "Meld", "InstrumentVector": "Wavetable", "LoungeLizard": "Electric",
    "MidiArpeggiator": "Arpeggiator", "MidiCcControl": "CC Control", "MidiChord": "Chord",
    "MidiEffectGroupDevice": "MIDI Effect Rack", "MidiNoteLength": "Note Length",
    "MidiPitcher": "Pitch", "MidiRandom": "Random", "MidiScale": "Scale",
    "MidiVelocity": "Velocity", "MultiSampler": "Sampler",
    "MultibandDynamics": "Multiband Dynamics", "OriginalSimpler": "Simpler",
    "PhaserNew": "Phaser-Flanger", "ProxyAudioEffectDevice": "External Audio Effect",
    "ProxyInstrumentDevice": "External Instrument", "Redux2": "Redux",
    "Resonator": "Resonators", "Spectral": "Spectral Time", "SpectrumAnalyzer": "Spectrum",
    "StereoGain": "Utility", "StringStudio": "Tension", "Transmute": "Spectral Resonator",
    "Tube": "Dynamic Tube", "UltraAnalog": "Analog", "Vinyl": "Vinyl Distortion",
    # Older versions of devices that Live has since replaced
    "AutoFilter": "Auto Filter (legacy)", "AutoPan": "Auto Pan (legacy)",
    "Chorus": "Chorus (legacy)", "Erosion": "Erosion (legacy)", "Redux": "Redux (legacy)",
    "Phaser": "Phaser (legacy)", "Flanger": "Flanger (legacy)",
    "FrequencyShifter": "Frequency Shifter (legacy)", "SimpleDelay": "Simple Delay (legacy)",
    "PingPongDelay": "Ping Pong Delay (legacy)",
}
MAX_DEVICES = {"MxDeviceAudioEffect": "Max Audio Effect", "MxDeviceInstrument": "Max Instrument",
               "MxDeviceMidiEffect": "Max MIDI Effect"}
PLUGIN_DEVICES = ("PluginDevice", "VstDevice", "AuPluginDevice")
PLUGIN_FORMATS = {"VstPluginInfo": "VST2", "Vst3PluginInfo": "VST3", "AuPluginInfo": "AU"}

LIVE_SCALES = [
    "Major", "Minor", "Dorian", "Mixolydian", "Lydian", "Phrygian", "Locrian", "Whole Tone",
    "Half-whole Dim.", "Whole-half Dim.", "Minor Blues", "Minor Pentatonic", "Major Pentatonic",
    "Harmonic Minor", "Harmonic Major", "Dorian #4", "Phrygian Dominant", "Melodic Minor",
    "Lydian Augmented", "Lydian Dominant", "Super Locrian", "8-Tone Spanish", "Bhairav",
    "Hungarian Minor", "Hirajoshi", "In-Sen", "Iwato", "Kumoi", "Pelog Selisir",
    "Pelog Tembung", "Messiaen 3", "Messiaen 4", "Messiaen 5", "Messiaen 6", "Messiaen 7",
]
SHARPS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
FLATS = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
TRACK_TYPES = {"MidiTrack": "midi", "AudioTrack": "audio", "GroupTrack": "group",
               "ReturnTrack": "return"}
MIN_GAIN = 0.0003163  # Live's fader floor, shown as -inf dB
_UNREAD = frozenset({
    "AutomationTarget", "ModulationTarget", "MidiControllerRange", "MidiCCOnOffThresholds",
    "AutomationEnvelopes", "Envelopes", "AutomationLanes", "PerNoteEventStore", "ParameterList",
    "ViewStates", "ExpressionLanes", "ContentLanes", "WarpMarkers",
    "ClipEnvelopeChooserViewState", "TakeLanes", "FreezeSequencer"})


# ── Reading the file ─────────────────────────────────────────────────────────

def load(path: str | Path) -> tuple[Path, ET.Element]:
    """The set's path and its <LiveSet> element."""
    file = Path(path).expanduser()
    if file.suffix.lower() != ".als":
        raise ValueError(f"Not a Live Set (.als): {file}")
    if not file.is_file():
        raise FileNotFoundError(f"No such Live Set: {file}")
    raw = file.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    # Live never writes a DTD; refusing one rules out entity expansion
    if b"<!DOCTYPE" in raw[:4096] or b"<!ENTITY" in raw:
        raise ValueError(f"{file.name} is not a Live Set")
    # Automation, modulation and view state are most of a set and never read
    # here: dropping them while parsing keeps a 27 MB set at ~80 MB of memory
    # instead of ~215 MB, at the same speed.
    events = ET.iterparse(io.BytesIO(raw), events=("end",))
    for _event, element in events:
        if element.tag in _UNREAD:
            element.clear()
    root = events.root
    live_set = root.find("LiveSet")
    if root.tag != "Ableton" or live_set is None:
        raise ValueError(f"{file.name} is not a Live Set")
    live_set.set("_creator", root.get("Creator", ""))
    return file, live_set


def _value(el: ET.Element | None, path: str = "", default: Any = None) -> Any:
    node = el.find(path) if (el is not None and path) else el
    if node is None:
        return default
    return node.get("Value", default)


def _float(el: ET.Element | None, path: str = "", default: float | None = None) -> float | None:
    value = _value(el, path)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bool(el: ET.Element | None, path: str = "") -> bool:
    return _value(el, path) == "true"


def _main_track(live_set: ET.Element) -> ET.Element | None:
    main = live_set.find("MainTrack")
    return main if main is not None else live_set.find("MasterTrack")


def decode_time_signature(value: int) -> tuple[int, int]:
    return value % 99 + 1, 2 ** (value // 99)


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def _gain_db(gain: float | None) -> str | None:
    if gain is None:
        return None
    return "-inf dB" if gain <= MIN_GAIN else f"{20 * math.log10(gain):.1f} dB"


def _pan(value: float | None) -> str | None:
    if value is None:
        return None
    steps = round(abs(value) * 50)
    return "C" if steps == 0 else f"{steps}{'L' if value < 0 else 'R'}"


def _split_camel(tag: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", tag)


# ── Devices ──────────────────────────────────────────────────────────────────

def _plugin_info(el: ET.Element) -> dict:
    desc = el.find("PluginDesc")
    info = desc[0] if desc is not None and len(desc) else None
    if info is None:
        return {"name": _value(el, "UserName") or "Plug-in", "format": "plug-in"}
    plugin = {"name": _value(info, "PlugName") or _value(info, "Name") or "Plug-in",
              "format": PLUGIN_FORMATS.get(info.tag, info.tag.replace("PluginInfo", ""))}
    vendor = _value(info, "Manufacturer")
    if vendor:
        plugin["vendor"] = vendor
    return plugin


def _max_device_name(el: ET.Element) -> str:
    ref = el.find("PatchSlot/Value/MxPatchRef/FileRef")
    for field in ("Path", "RelativePath"):
        value = _value(ref, field)
        if value:
            return Path(value.replace("\\", "/")).stem
    return MAX_DEVICES.get(el.tag, "Max for Live device")


def device(el: ET.Element) -> dict:
    """A device element as {device, name?, on?, plugin?, max_for_live?, chains?}."""
    if el.tag in PLUGIN_DEVICES:
        plugin = _plugin_info(el)
        entry = {"device": plugin["name"], "plugin": plugin}
    elif el.tag in MAX_DEVICES:
        entry = {"device": _max_device_name(el), "max_for_live": MAX_DEVICES[el.tag]}
    else:
        entry = {"device": DEVICE_NAMES.get(el.tag, _split_camel(el.tag))}
    user_name = _value(el, "UserName") or ""
    if user_name and user_name != entry["device"]:
        entry["name"] = user_name
    if _value(el, "On/Manual") == "false":
        entry["on"] = False
    branches = el.find("Branches")
    if branches is not None:
        chains = []
        for branch in branches:
            devices = branch.find("DeviceChain/*/Devices")
            chain = {"name": _value(branch, "Name/EffectiveName") or "",
                     "devices": [device(d) for d in (devices if devices is not None else [])]}
            receiving = _value(branch, "BranchInfo/ReceivingNote")
            if receiving is not None:
                chain["note"] = 128 - int(receiving)
            chains.append(chain)
        entry["chains"] = chains
    return entry


def device_label(entry: dict) -> str:
    """One line for a device: 'Instrument Rack "Strum-o-Matic" (3 chains)'."""
    text = entry["device"]
    if entry.get("name"):
        text += f' "{entry["name"]}"'
    if entry.get("plugin"):
        text += f" ({entry['plugin']['format']})"
    if entry.get("max_for_live"):
        text += " (Max for Live)"
    chains = entry.get("chains")
    if chains is not None:
        if entry["device"] == "Drum Rack":
            text += f" ({len(chains)} pad{'s' if len(chains) != 1 else ''})"
        elif len(chains) == 1:
            inner = [device_label(d) for d in chains[0]["devices"]]
            text += " [" + ", ".join(inner[:6]) + (", …" if len(inner) > 6 else "") + "]"
        else:
            text += f" ({len(chains)} chains)"
    if entry.get("on") is False:
        text += " [off]"
    return text


def _walk_devices(entries: list[dict]):
    for entry in entries:
        yield entry
        for chain in entry.get("chains", []):
            yield from _walk_devices(chain["devices"])


# ── Samples ──────────────────────────────────────────────────────────────────

def _live_resource_dirs() -> list[Path]:
    """App-Resources of the installed Lives (Core Library, Builtin devices)."""
    if sys.platform == "darwin":
        apps = sorted(Path("/Applications").glob("Ableton Live*.app"))
        return [a / "Contents" / "App-Resources" for a in apps]
    if sys.platform == "win32":
        base = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Ableton"
        return sorted(p / "Resources" for p in base.glob("Live*"))
    return []


def _user_libraries() -> list[Path]:
    try:
        from .remote_script_install import discover_user_library_remote_script_dirs
        return [d.parent for d in discover_user_library_remote_script_dirs()]
    except Exception:
        return [Path.home() / "Music" / "Ableton" / "User Library"]


def _sample_roots() -> dict[str, list[Path]]:
    resources = _live_resource_dirs()
    return {
        "core_library": [r / "Core Library" for r in resources],
        "builtin": [r / "Builtin" for r in resources],
        "user_library": _user_libraries(),
        "packs": [Path.home() / "Music" / "Ableton" / "Factory Packs",
                  Path.home() / "Documents" / "Ableton" / "Factory Packs"],
    }


def _sample(ref: ET.Element, set_dir: Path, roots: dict[str, list[Path]]) -> dict:
    path = (_value(ref, "Path") or "").replace("\\", "/")
    relative = (_value(ref, "RelativePath") or "").replace("\\", "/")
    kind = _value(ref, "RelativePathType") or "0"
    pack = _value(ref, "LivePackName") or ""
    if pack == "Core Library":
        where, bases = "core_library", roots["core_library"]
    elif pack:
        where, bases = "pack", [b / pack for b in roots["packs"]]
    elif kind == "1":
        where, bases = "project", [set_dir]
    elif kind == "6":
        where, bases = "user_library", roots["user_library"] + roots["core_library"]
    elif kind == "7":
        where, bases = "builtin", roots["builtin"]
    else:
        where, bases = "elsewhere", []
    candidates = [Path(path)] if path else []
    if relative:
        candidates += [base / relative for base in bases]
    found = next((c for c in candidates if c.is_file()), None)
    if found is not None and where == "elsewhere" and set_dir in found.parents:
        where = "project"
    name = Path(relative or path).name
    return {"file": name, "path": str(found or (path or relative)), "found": found is not None,
            "where": where}


def samples(live_set: ET.Element, set_dir: Path) -> list[dict]:
    """Every distinct sample the set uses (audio clips, Simpler, Sampler, Drum Rack pads)."""
    roots = _sample_roots()
    seen: dict[tuple, dict] = {}
    for ref in live_set.iter("SampleRef"):
        file_ref = ref.find("FileRef")
        if file_ref is None:
            continue
        key = (_value(file_ref, "Path"), _value(file_ref, "RelativePath"))
        if key not in seen and any(key):
            seen[key] = _sample(file_ref, set_dir, roots)
    return list(seen.values())


# ── Clips ────────────────────────────────────────────────────────────────────

def notes(clip: ET.Element) -> list[dict]:
    result = []
    for key_track in clip.findall("Notes/KeyTracks/KeyTrack"):
        pitch = int(_value(key_track, "MidiKey", 0))
        for event in key_track.findall("Notes/MidiNoteEvent"):
            note = {"pitch": pitch, "start_time": float(event.get("Time", 0)),
                    "duration": float(event.get("Duration", 0)),
                    "velocity": float(event.get("Velocity", 100)),
                    "mute": event.get("IsEnabled") == "false"}
            probability = event.get("Probability")
            if probability not in (None, "1"):
                note["probability"] = float(probability)
            deviation = event.get("VelocityDeviation")
            if deviation not in (None, "0"):
                note["velocity_deviation"] = float(deviation)
            result.append(note)
    result.sort(key=lambda n: (n["start_time"], n["pitch"]))
    return result


def clip_summary(el: ET.Element, arrangement: bool) -> dict:
    start, end = _float(el, "CurrentStart", 0.0), _float(el, "CurrentEnd", 0.0)
    entry: dict[str, Any] = {"name": _value(el, "Name") or "", "kind": "midi" if el.tag == "MidiClip" else "audio"}
    if arrangement:
        entry["start"], entry["end"] = _round(start), _round(end)
    else:
        entry["length"] = _round(end - start)
    if _bool(el, "Loop/LoopOn"):
        entry["loop"] = [_round(_float(el, "Loop/LoopStart")), _round(_float(el, "Loop/LoopEnd"))]
    if _bool(el, "Disabled"):
        entry["muted"] = True
    if el.tag == "MidiClip":
        entry["notes"] = len(el.findall("Notes/KeyTracks/KeyTrack/Notes/MidiNoteEvent"))
    else:
        ref = el.find("SampleRef/FileRef")
        sample = _value(ref, "RelativePath") or _value(ref, "Path") or ""
        entry["sample"] = Path(sample.replace("\\", "/")).name
        entry["warped"] = _bool(el, "IsWarped")
    return entry


def session_clips(track: ET.Element) -> list[tuple[int, ET.Element]]:
    slots = track.findall("DeviceChain/MainSequencer/ClipSlotList/ClipSlot")
    found = []
    for index, slot in enumerate(slots):
        value = slot.find("ClipSlot/Value")
        clip = value[0] if value is not None and len(value) else None
        if clip is not None and clip.tag in ("MidiClip", "AudioClip"):
            found.append((index, clip))
    return found


def arrangement_clips(track: ET.Element) -> list[ET.Element]:
    clips = [c for c in track.findall("DeviceChain/MainSequencer/*/ArrangerAutomation/Events/*")
             if c.tag in ("MidiClip", "AudioClip")]
    return sorted(clips, key=lambda c: _float(c, "CurrentStart", 0.0))


# ── The set ──────────────────────────────────────────────────────────────────

def _key(live_set: ET.Element) -> dict | None:
    scale = live_set.find("ScaleInformation")
    if scale is None:
        return None
    root = int(_value(scale, "Root", 0) or 0)
    raw = _value(scale, "Name", "0")
    if str(raw).isdigit():
        index = int(raw)
        name = LIVE_SCALES[index] if index < len(LIVE_SCALES) else f"scale #{index}"
    else:
        name = raw
    names = FLATS if _bool(live_set, "PreferFlatRootNote") else SHARPS
    entry = {"root": names[root % 12], "scale": name}
    if live_set.find("InKey") is not None:
        entry["in_key"] = _bool(live_set, "InKey")
    return entry


def _tracks(live_set: ET.Element) -> list[ET.Element]:
    tracks = live_set.find("Tracks")
    return [t for t in (tracks if tracks is not None else []) if t.tag in TRACK_TYPES]


def track_name(track: ET.Element) -> str:
    return _value(track, "Name/EffectiveName") or _value(track, "Name/UserName") or ""


def find_track(live_set: ET.Element, track: int | str) -> ET.Element:
    """A track (not a return) by index or name, as in the open set."""
    tracks = [t for t in _tracks(live_set) if t.tag != "ReturnTrack"]
    if isinstance(track, int) or re.fullmatch(r"\d+", str(track).strip()):
        index = int(track)
        if not 0 <= index < len(tracks):
            raise ValueError(f"Track index {index} out of range: the set has {len(tracks)} tracks")
        return tracks[index]
    wanted = str(track).strip().lower()
    matches = [t for t in tracks if track_name(t).lower() == wanted]
    if not matches:
        raise ValueError(f"No track named {track!r} in the set. Tracks: "
                         + ", ".join(track_name(t) for t in tracks))
    return matches[0]


def _track(el: ET.Element, groups: dict[str, str], returns: list[str], detail: str,
           beats_per_bar: float) -> dict:
    mixer = el.find("DeviceChain/Mixer")
    entry: dict[str, Any] = {"name": track_name(el), "type": TRACK_TYPES[el.tag],
                             "color": int(_value(el, "Color", 0) or 0)}
    group = groups.get(_value(el, "TrackGroupId", "-1"))
    if group:
        entry["group"] = group
    entry["volume"] = _gain_db(_float(mixer, "Volume/Manual"))
    entry["pan"] = _pan(_float(mixer, "Pan/Manual"))
    if _value(mixer, "Speaker/Manual") == "false":
        entry["muted"] = True
    if _bool(mixer, "SoloSink"):
        entry["solo"] = True
    if _bool(el, "Freeze"):
        entry["frozen"] = True
    sends = [_gain_db(_float(holder, "Send/Manual")) for holder in mixer.findall("Sends/TrackSendHolder")] \
        if mixer is not None else []
    active = {returns[i] if i < len(returns) else f"send {i + 1}": level
              for i, level in enumerate(sends) if level and level != "-inf dB"}
    if active:
        entry["sends"] = active
    output = _value(el, "DeviceChain/AudioOutputRouting/UpperDisplayString")
    if output and detail == "full":
        entry["output"] = output
    devices_el = el.find("DeviceChain/DeviceChain/Devices")
    devices = [device(d) for d in (devices_el if devices_el is not None else [])]
    entry["devices"] = devices if detail == "full" else [device_label(d) for d in devices]
    session = session_clips(el)
    arrangement = arrangement_clips(el)
    if detail == "full":
        if session:
            entry["session_clips"] = [{"slot": i, **clip_summary(c, False)} for i, c in session]
        if arrangement:
            entry["arrangement_clips"] = [
                {"bar": _round(_float(c, "CurrentStart", 0.0) / beats_per_bar + 1, 2),
                 **clip_summary(c, True)} for c in arrangement]
    else:
        if session:
            entry["session_clips"] = len(session)
        if arrangement:
            entry["arrangement_clips"] = len(arrangement)
    return entry


def read_set(path: str | Path, detail: str = "summary") -> dict:
    """Everything a producer wants to know about a set on disk (see the module docstring)."""
    if detail not in ("summary", "full"):
        raise ValueError("detail: summary | full")
    file, live_set = load(path)
    main = _main_track(live_set)
    numerator, denominator = decode_time_signature(
        int(_value(main, "DeviceChain/Mixer/TimeSignature/Manual", 201)))
    beats_per_bar = numerator * 4 / denominator
    tracks = _tracks(live_set)
    groups = {t.get("Id"): track_name(t) for t in tracks if t.tag == "GroupTrack"}
    returns = [track_name(t) for t in tracks if t.tag == "ReturnTrack"]
    entries = [_track(t, groups, returns, detail, beats_per_bar) for t in tracks]
    end = max((_float(c, "CurrentEnd", 0.0) for t in tracks for c in arrangement_clips(t)),
              default=0.0)

    result: dict[str, Any] = {
        "file": str(file),
        "saved_with": live_set.get("_creator") or None,
        "modified": datetime.fromtimestamp(file.stat().st_mtime).isoformat(timespec="minutes"),
        "tempo": _round(_float(main, "DeviceChain/Mixer/Tempo/Manual"), 2),
        "time_signature": f"{numerator}/{denominator}",
        "key": _key(live_set),
        "arrangement_length": {"beats": _round(end), "bars": _round(end / beats_per_bar, 2)},
    }
    if _bool(live_set, "Transport/LoopOn"):
        start = _float(live_set, "Transport/LoopStart", 0.0)
        result["loop"] = {"start_bar": _round(start / beats_per_bar + 1, 2),
                          "bars": _round(_float(live_set, "Transport/LoopLength", 0.0) / beats_per_bar, 2)}
    result["locators"] = [
        {"name": _value(loc, "Name") or "", "bar": _round(_float(loc, "Time", 0.0) / beats_per_bar + 1, 2)}
        for loc in sorted(live_set.findall("Locators/Locators/Locator"),
                          key=lambda loc: _float(loc, "Time", 0.0))]
    scenes = []
    for index, scene in enumerate(live_set.findall("Scenes/Scene")):
        item: dict[str, Any] = {"index": index, "name": _value(scene, "Name") or ""}
        if _bool(scene, "IsTempoEnabled"):
            item["tempo"] = _float(scene, "Tempo")
        if _bool(scene, "IsTimeSignatureEnabled"):
            item["time_signature"] = "%d/%d" % decode_time_signature(
                int(_value(scene, "TimeSignatureId", 201)))
        scenes.append(item)
    result["scenes"] = scenes if detail == "full" else [s for s in scenes if len(s) > 2 or s["name"]]
    result["tracks"] = [e for e in entries if e["type"] != "return"]
    result["return_tracks"] = [e for e in entries if e["type"] == "return"]
    main_devices_el = main.find("DeviceChain/DeviceChain/Devices") if main is not None else None
    main_devices = [device(d) for d in (main_devices_el if main_devices_el is not None else [])]
    result["main_track"] = {
        "volume": _gain_db(_float(main, "DeviceChain/Mixer/Volume/Manual")),
        "devices": main_devices if detail == "full" else [device_label(d) for d in main_devices]}

    # Plug-ins and Max devices, with the tracks that use them
    plugins: dict[tuple, dict] = {}
    max_devices: dict[str, dict] = {}
    for track_el, track_entry in [(t, e) for t, e in zip(tracks, entries)] + [(main, {"name": "Main"})]:
        if track_el is None:
            continue
        devices_el = track_el.find("DeviceChain/DeviceChain/Devices")
        for d in _walk_devices([device(x) for x in (devices_el if devices_el is not None else [])]):
            if d.get("plugin"):
                key = (d["plugin"]["name"], d["plugin"]["format"])
                item = plugins.setdefault(key, {**d["plugin"], "tracks": []})
                if track_entry["name"] not in item["tracks"]:
                    item["tracks"].append(track_entry["name"])
            if d.get("max_for_live"):
                item = max_devices.setdefault(d["device"], {"name": d["device"], "tracks": []})
                if track_entry["name"] not in item["tracks"]:
                    item["tracks"].append(track_entry["name"])
    result["plugins"] = sorted(plugins.values(), key=lambda p: p["name"].lower())
    result["max_for_live"] = sorted(max_devices.values(), key=lambda m: m["name"].lower())

    used = samples(live_set, file.parent)
    missing = [s for s in used if not s["found"]]
    by_place: dict[str, int] = {}
    for s in used:
        by_place[s["where"]] = by_place.get(s["where"], 0) + 1
    result["samples"] = {"count": len(used), "missing": len(missing), "by_location": by_place,
                         "missing_files": [s["path"] for s in missing[:50]]}
    if detail == "full":
        result["samples"]["files"] = used

    notes_out = []
    if missing:
        notes_out.append(f"{len(missing)} of {len(used)} samples are missing; Live will ask to "
                         "search for them (File › Manage Files).")
    if plugins:
        notes_out.append("Third-party plug-ins: " + ", ".join(
            f"{p['name']} ({p['format']})" for p in result["plugins"]) + ".")
    frozen = [e["name"] for e in entries if e.get("frozen")]
    if frozen:
        notes_out.append("Frozen tracks: " + ", ".join(frozen) + ".")
    result["notes"] = notes_out
    return result


def clip(path: str | Path, track: int | str, clip_index: int | None = None,
         arrangement_clip_index: int | None = None) -> dict:
    """A MIDI clip of a set on disk: the notes you hear, from its start.

    Live keeps the played region in Loop/LoopStart..LoopEnd whether the loop
    is on (the loop) or off (start and end markers). Notes outside it are
    left out and the region starts at beat 0, as in a new clip.
    """
    file, live_set = load(path)
    track_el = find_track(live_set, track)
    if arrangement_clip_index is not None:
        clips = arrangement_clips(track_el)
        if not 0 <= arrangement_clip_index < len(clips):
            raise ValueError(f"Track {track_name(track_el)!r} has {len(clips)} Arrangement clips")
        clip_el = clips[arrangement_clip_index]
    else:
        slots = dict(session_clips(track_el))
        index = 0 if clip_index is None else clip_index
        if index not in slots:
            raise ValueError(f"No clip in slot {index} of {track_name(track_el)!r}; slots with "
                             f"clips: {sorted(slots) or 'none'} (or pass arrangement_clip_index)")
        clip_el = slots[index]
    if clip_el.tag != "MidiClip":
        raise ValueError("That is an audio clip; only MIDI clips have notes")
    begin = _float(clip_el, "Loop/LoopStart", _float(clip_el, "CurrentStart", 0.0))
    finish = _float(clip_el, "Loop/LoopEnd", _float(clip_el, "CurrentEnd", 0.0))
    length = max(0.0, finish - begin)
    heard = []
    every = notes(clip_el)
    for note in every:
        offset = note["start_time"] - begin
        if -1e-6 <= offset < length - 1e-6:
            heard.append(dict(note, start_time=_round(max(0.0, offset), 6),
                              duration=_round(min(note["duration"], length - offset), 6)))
    main = _main_track(live_set)
    numerator, denominator = decode_time_signature(
        int(_value(main, "DeviceChain/Mixer/TimeSignature/Manual", 201)))
    return {"file": str(file), "track": track_name(track_el), "clip": _value(clip_el, "Name") or "",
            "length": _round(length), "looping": _bool(clip_el, "Loop/LoopOn"),
            "set_tempo": _round(_float(main, "DeviceChain/Mixer/Tempo/Manual"), 2),
            "set_time_signature": [numerator, denominator],
            "notes_outside": len(every) - len(heard), "notes": heard}


# ── Comparing and finding sets ───────────────────────────────────────────────

def _devices_flat(entry: dict) -> list[str]:
    return [device_label(d) for d in _walk_devices(entry["devices"])]


def compare(path: str | Path, other: str | Path) -> dict:
    """What changed from `other` (e.g. a Backup) to `path`."""
    new, old = read_set(path, "full"), read_set(other, "full")
    changes: list[str] = []
    for field in ("tempo", "time_signature", "key"):
        if new[field] != old[field]:
            changes.append(f"{field.replace('_', ' ')}: {old[field]} → {new[field]}")
    if new["arrangement_length"]["bars"] != old["arrangement_length"]["bars"]:
        changes.append(f"arrangement: {old['arrangement_length']['bars']} → "
                       f"{new['arrangement_length']['bars']} bars")
    old_tracks = {t["name"]: t for t in old["tracks"] + old["return_tracks"]}
    new_tracks = {t["name"]: t for t in new["tracks"] + new["return_tracks"]}
    added = [n for n in new_tracks if n not in old_tracks]
    removed = [n for n in old_tracks if n not in new_tracks]
    if added:
        changes.append("tracks added: " + ", ".join(added))
    if removed:
        changes.append("tracks removed: " + ", ".join(removed))
    for name, track in new_tracks.items():
        before = old_tracks.get(name)
        if before is None:
            continue
        for field in ("volume", "pan", "muted", "solo", "frozen"):
            if track.get(field) != before.get(field):
                changes.append(f"{name}: {field} {before.get(field)} → {track.get(field)}")
        was, now = _devices_flat(before), _devices_flat(track)
        gone = [d for d in was if d not in now]
        new_devices = [d for d in now if d not in was]
        if gone:
            changes.append(f"{name}: devices removed: " + ", ".join(gone))
        if new_devices:
            changes.append(f"{name}: devices added: " + ", ".join(new_devices))
        for kind in ("session_clips", "arrangement_clips"):
            a, b = len(before.get(kind, [])), len(track.get(kind, []))
            if a != b:
                changes.append(f"{name}: {kind.replace('_', ' ')} {a} → {b}")
    old_locators = [(loc["name"], loc["bar"]) for loc in old["locators"]]
    new_locators = [(loc["name"], loc["bar"]) for loc in new["locators"]]
    if old_locators != new_locators:
        changes.append(f"locators: {len(old_locators)} → {len(new_locators)}")
    if new["samples"]["missing"] != old["samples"]["missing"]:
        changes.append(f"missing samples: {old['samples']['missing']} → {new['samples']['missing']}")
    return {"file": new["file"], "compared_to": old["file"],
            "saved": [old["modified"], new["modified"]], "changes": changes or ["no differences"]}


def default_folders() -> list[Path]:
    home = Path.home()
    candidates = [home / "Music" / "Ableton", home / "Documents" / "Ableton",
                  home / "Documents" / "Ableton Files", home / "Documents"]
    return [c for c in candidates if c.is_dir()]


_SKIP_DIRS = {"Backup", "Library", "node_modules", "Ableton Project Info", "Samples",
              "Recorded", "Factory Packs", "User Library"}


def iter_set_files(folders: list[Path], include_backups: bool = False, max_depth: int = 6):
    seen: set[Path] = set()
    for folder in folders:
        base_depth = len(folder.parts)
        for root, dirs, files in os.walk(folder):
            depth = len(Path(root).parts) - base_depth

            def keep(name: str) -> bool:
                if name.startswith(".") or depth >= max_depth:
                    return False
                return include_backups if name == "Backup" else name not in _SKIP_DIRS

            dirs[:] = [d for d in dirs if keep(d)]
            for name in files:
                if name.lower().endswith(".als") and not name.startswith("."):
                    path = (Path(root) / name).resolve()
                    if path not in seen:
                        seen.add(path)
                        yield path


def overview(path: str | Path) -> dict:
    """The few facts that tell sets apart, for lists."""
    info = read_set(path, "summary")
    counts: dict[str, int] = {}
    for track in info["tracks"]:
        counts[track["type"]] = counts.get(track["type"], 0) + 1
    entry = {"file": info["file"], "modified": info["modified"], "saved_with": info["saved_with"],
             "tempo": info["tempo"], "time_signature": info["time_signature"],
             "key": f"{info['key']['root']} {info['key']['scale']}" if info["key"] else None,
             "bars": info["arrangement_length"]["bars"], "tracks": counts,
             "plugins": [p["name"] for p in info["plugins"]],
             "missing_samples": info["samples"]["missing"]}
    entry["_names"] = [t["name"] for t in info["tracks"]]
    return entry


def find_sets(folder: str | Path | None = None, query: str | None = None,
              include_backups: bool = False, limit: int = 30) -> dict:
    folders = [Path(folder).expanduser()] if folder else default_folders()
    for f in folders:
        if not f.is_dir():
            raise FileNotFoundError(f"No such folder: {f}")
    files = sorted(iter_set_files(folders, include_backups),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    wanted = (query or "").strip().lower()
    found, errors, read = [], [], 0
    for path in files:
        if len(found) >= limit:
            break
        try:
            entry = overview(path)
            read += 1
        except Exception as e:  # a damaged or foreign file: report and go on
            errors.append({"file": str(path), "error": str(e)})
            continue
        names = entry.pop("_names")
        haystack = " ".join([path.name, *names, *entry["plugins"]]).lower()
        if wanted and wanted not in haystack:
            continue
        found.append(entry)
    result = {"folders": [str(f) for f in folders], "sets_found": len(files), "read": read,
              "matches" if wanted else "sets": found}
    if errors:
        result["unreadable"] = errors[:10]
    return result
