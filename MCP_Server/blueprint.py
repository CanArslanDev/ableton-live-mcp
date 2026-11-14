"""Blueprints: a beat to build in Live, as one JSON document (version 1).

A blueprint holds everything build_from_blueprint needs and decides nothing
in Live: tempo, time signature, locators, the build_tracks spec, the files
to copy into the User Library (scaled where they peak over full scale), and
which read-backs to run afterwards. reference-dissector writes one
(`refdissect blueprint`) for a song it analysed; anything else may too.
docs/blueprint-v1.md describes the format, schemas/blueprint-v1.schema.json
is its JSON Schema (structure), and validate() adds the cross-references
the schema cannot say (a sampler's file key, unique track names, ...).

The parts that need no Live are here: validation, resolving files,
excerpts (bars A-B) and the build_tracks spec. The build itself is the
build_from_blueprint tool (extended_tools.py).

    python -m MCP_Server.blueprint path/to/blueprint.json   # prints its problems
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import shutil
import sys
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any

FORMAT = "ableton-mcp-blueprint"
VERSION = 1
SCHEMA_PATH = Path(__file__).with_name("schemas") / "blueprint-v1.schema.json"
CHECKS = ("time_signature", "samples", "volumes", "choke_groups", "locators", "voices")
GENERATED = Path("Samples") / "AbletonMCP Generated"
# A Volume holds when its value is within this of the blueprint's dB, and its
# display (two decimals from -10 dB up) within VOLUME_DISPLAY_DB. Sent as
# display text instead, Live 12.4.6 held a display step under it ("-6.6 dB"
# held -6.605, "-14.9 dB" -14.95): within VOLUME_TEXT_DB counts as held so
VOLUME_VALUE_DB, VOLUME_DISPLAY_DB, VOLUME_TEXT_DB = 1e-3, 0.006, 0.06


@lru_cache(maxsize=1)
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _where(path) -> str:
    parts = []
    for part in path:
        parts.append(f"[{part}]" if isinstance(part, int) else f".{part}")
    return "blueprint" + "".join(parts)


def signature(text: str) -> tuple[int, int]:
    numerator, denominator = (int(v) for v in str(text).split("/"))
    return numerator, denominator


def samplers(blueprint: dict) -> Iterator[tuple[int, str, str | None, dict]]:
    """(track index, track name, pad note or None, options) of every Simpler
    the blueprint makes: a track's own, or a Drum Rack pad's."""
    for index, track in enumerate(blueprint.get("tracks") or []):
        instrument = track.get("instrument")
        if not isinstance(instrument, dict):
            continue
        if "drum_rack" in instrument:
            for note, pad in (instrument.get("drum_rack") or {}).items():
                yield index, track.get("name"), str(note), pad
        else:
            yield index, track.get("name"), None, instrument


def _explained(error):
    """For a value that fits none of oneOf's shapes, the error of the shape
    it was meant to have: not one whose very type differs, then the fewest
    errors, then the deepest (a pad's choke group, not "is not a string";
    a sampler's unknown field, not "drum_rack is required")."""
    weak = ("type", "required")
    while error.validator in ("oneOf", "anyOf") and error.context:
        branches: dict = {}
        for e in error.context:
            branches.setdefault(e.relative_schema_path[0], []).append(e)

        def rank(errors):
            mismatch = any(e.validator == "type" and not e.relative_path for e in errors)
            return (mismatch, len(errors), -max(len(e.absolute_path) for e in errors))

        best = min(branches.values(), key=rank)
        error = max(best, key=lambda e: (len(e.absolute_path), e.validator not in weak))
    return error


def validate(blueprint: Any) -> list[str]:
    """Every problem with a blueprint, each with where it is; [] when it can
    be built (files are checked when it is built, against the disk)."""
    import jsonschema

    if not isinstance(blueprint, dict):
        return ["blueprint: a JSON object"]
    if blueprint.get("format") != FORMAT or blueprint.get("version") != VERSION:
        return [f"blueprint: format must be {FORMAT!r} and version {VERSION} (it says "
                f"{blueprint.get('format')!r}, {blueprint.get('version')!r})"]
    validator = jsonschema.Draft202012Validator(schema())
    problems = []
    for error in sorted(validator.iter_errors(blueprint), key=lambda e: list(map(str, e.absolute_path))):
        error = _explained(error)
        problems.append(f"{_where(error.absolute_path)}: {error.message}")
    if problems:
        return problems
    names = [t["name"] for t in blueprint["tracks"]]
    for name in sorted({n for n in names if names.count(n) > 1}):
        problems.append(f"blueprint.tracks: the name {name!r} is used twice")
    files = blueprint.get("files") or {}
    for index, track in enumerate(blueprint["tracks"]):
        where = f"blueprint.tracks[{index}] ({track['name']})"
        kind = track.get("kind", "midi")
        if kind == "audio" and (track.get("instrument") or track.get("clips") or track.get("voices")):
            problems.append(f"{where}: an audio track takes audio_clips, not an instrument, MIDI "
                            "clips or voices")
        if kind == "midi" and track.get("audio_clips"):
            problems.append(f"{where}: audio_clips belong on a track of kind \"audio\"")
        instrument = track.get("instrument")
        if track.get("voices") is not None and not (isinstance(instrument, dict)
                                                    and "drum_rack" not in instrument):
            problems.append(f"{where}: voices is a Simpler's: give the track a sampler instrument")
        for c, clip in enumerate(track.get("clips") or []):
            late = [n for n in clip.get("notes") or [] if n["start_time"] >= clip["length"]]
            if late:
                problems.append(f"{where}.clips[{c}]: {len(late)} notes start at or after the clip's "
                                f"length ({clip['length']:g})")
    for _index, name, note, options in samplers(blueprint):
        where = f"blueprint.tracks ({name}{f' pad {note}' if note else ''})"
        given = [k for k in ("file", "sample_path", "sample") if options.get(k)]
        if not given:
            problems.append(f"{where}: give file (a key of files), sample_path or sample")
        if "file" in given and len(given) > 1:
            problems.append(f"{where}: file names the sample already; leave out sample_path and sample")
        if options.get("file") and options["file"] not in files:
            problems.append(f"{where}: file {options['file']!r} is not a key of files")
        if options.get("sample_path") and not os.path.isabs(os.path.expanduser(options["sample_path"])):
            problems.append(f"{where}: sample_path must be absolute (or use files)")
        if note is None and options.get("choke_group") is not None:
            problems.append(f"{where}: choke_group is a Drum Rack pad's")
    for key, entry in files.items():
        gain = float(entry.get("gain_db") or 0.0)
        if gain < 0 and not entry["name"].lower().endswith(".wav"):
            problems.append(f"blueprint.files.{key}: a scaled copy (gain_db {gain:g}) is a WAV: "
                            "name it .wav")
    copies = [e["name"].lower() for e in files.values()]  # one file on macOS's and Windows' disks
    for name in sorted({n for n in copies if copies.count(n) > 1}):
        sources = {(e["source"], float(e.get("gain_db") or 0)) for e in files.values()
                   if e["name"].lower() == name}
        if len(sources) > 1:
            problems.append(f"blueprint.files: {name!r} names two different files (names are compared "
                            "without case)")
    length = blueprint.get("length_beats")
    for i, locator in enumerate(blueprint.get("locators") or []):
        if length is not None and locator["beat"] >= length:
            problems.append(f"blueprint.locators[{i}] ({locator['name']}): beat {locator['beat']:g} "
                            f"is past the end ({length:g})")
    return problems


def check(blueprint: Any) -> dict:
    """The blueprint, or ValueError with its problems."""
    problems = validate(blueprint)
    if problems:
        shown = "; ".join(problems[:12]) + (f"; and {len(problems) - 12} more" if len(problems) > 12 else "")
        raise ValueError(f"The blueprint cannot be built: {shown}")
    return blueprint


def checks_on(blueprint: dict) -> dict[str, bool]:
    given = blueprint.get("checks") or {}
    return {name: bool(given.get(name, True)) for name in CHECKS}


# ── Files ────────────────────────────────────────────────────────────────────

def resolve(path: str, base: Path | None) -> Path:
    """A blueprint's path: absolute as written (after ~), else under the
    blueprint's folder."""
    p = Path(path).expanduser()
    if p.is_absolute():
        return p
    if base is None:
        raise ValueError(f"{path!r} is relative, and a blueprint given as an object has no folder: "
                         "give the blueprint's file instead, or absolute paths")
    return base / p


def sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def check_sources(blueprint: dict, base: Path | None) -> list[str]:
    """What the disk says against the blueprint: every file, sample_path and
    audio clip there, each copied file's content the one its sha1 names."""
    problems = []
    for key, entry in (blueprint.get("files") or {}).items():
        try:
            source = resolve(entry["source"], base)
        except ValueError as e:
            problems.append(f"files.{key}: {e}")
            continue
        if not source.is_file():
            problems.append(f"files.{key}: no such file {source}")
        elif entry.get("sha1") and not sha1(source).startswith(entry["sha1"]):
            problems.append(f"files.{key}: {source} is not the file the blueprint was made with "
                            f"(SHA-1 {sha1(source)[:8]}, the blueprint says {entry['sha1'][:8]}): "
                            "make the blueprint again")
    for _index, name, note, options in samplers(blueprint):
        if options.get("sample_path") and not Path(options["sample_path"]).expanduser().is_file():
            problems.append(f"{name}{f' pad {note}' if note else ''}: no such file {options['sample_path']}")
    for track in blueprint.get("tracks") or []:
        for clip in track.get("audio_clips") or []:
            try:
                file = resolve(clip["file"], base)
            except ValueError as e:
                problems.append(f"{track['name']}: {e}")
                continue
            if not file.is_file():
                problems.append(f"{track['name']}: no such audio file {file}")
    return problems


def scaled_samples(source: Path, gain_db: float):
    import numpy as np
    import soundfile as sf

    x, rate = sf.read(str(source), dtype="float64", always_2d=True)
    return (x * 10 ** (gain_db / 20)).astype(np.float32), rate


def put_files(blueprint: dict, base: Path | None, folder: Path) -> tuple[dict[str, Path], list[dict]]:
    """Each file of the blueprint in folder under its name: copied as it is,
    or with gain_db under 0 written scaled by it (a 32-bit float WAV at the
    file's rate and channels). A file there with that content already is
    kept; one with other content is replaced and reported, since Live keeps
    playing what it loaded under a name through the browser until it
    restarts. Returns {key: path} and a row per file."""
    import soundfile as sf

    folder.mkdir(parents=True, exist_ok=True)
    placed, rows = {}, []
    for key, entry in (blueprint.get("files") or {}).items():
        source = resolve(entry["source"], base)
        target = folder / entry["name"]
        gain = float(entry.get("gain_db") or 0.0)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        if gain < 0:
            samples, rate = scaled_samples(source, gain)
            sf.write(str(temporary), samples, rate, subtype="FLOAT", format="WAV")
        else:
            shutil.copyfile(source, temporary)
        existed = target.is_file()
        if existed and sha1(target) == sha1(temporary):
            temporary.unlink()
            action = "kept"
        else:
            os.replace(temporary, target)
            action = "replaced" if existed else ("scaled" if gain < 0 else "copied")
        placed[key] = target
        rows.append({"file": key, "name": entry["name"], "action": action,
                     **({"gain_db": gain} if gain < 0 else {})})
    return placed, rows


def build_spec(blueprint: dict, placed: dict[str, Path], base: Path | None) -> list[dict]:
    """The blueprint's tracks as build_tracks takes them: a sampler's file
    becomes sample_path (its copy) and sample (the copy's name, for Live
    before 12.4, which loads through the browser), audio clips' files
    absolute; voices stays out (build_from_blueprint sets it after)."""
    spec = copy.deepcopy(blueprint["tracks"])
    for track in spec:
        track.pop("voices", None)
        for clip in track.get("audio_clips") or []:
            clip["file"] = str(resolve(clip["file"], base))
        instrument = track.get("instrument")
        if not isinstance(instrument, dict):
            continue
        options = instrument["drum_rack"].values() if "drum_rack" in instrument else [instrument]
        for sampler in options:
            key = sampler.pop("file", None)
            if key is not None:
                sampler["sample_path"] = str(placed[key])
                sampler["sample"] = placed[key].name
            elif sampler.get("sample_path"):
                sampler["sample_path"] = str(Path(sampler["sample_path"]).expanduser())
    return spec


# ── Excerpts ─────────────────────────────────────────────────────────────────

def parse_bars(bars: str | int | list | tuple) -> tuple[int, int]:
    """"9-16", [9, 16] or 9: bars counted from 1, as Live's ruler shows them."""
    if isinstance(bars, (list, tuple)):
        values = [int(v) for v in bars]
    elif isinstance(bars, int):
        values = [bars]
    else:
        values = [int(v) for v in re.split(r"\s*(?:-|\.\.|to)\s*", str(bars).strip()) if v]
    if len(values) == 1:
        values *= 2
    if len(values) != 2:
        raise ValueError(f"bars {bars!r}: give \"A-B\" (from bar 1)")
    return values[0], values[1]


def bar_beats(blueprint: dict) -> float:
    numerator, denominator = signature(blueprint["time_signature"])
    return numerator * 4 / denominator


def song_length(blueprint: dict) -> float:
    if blueprint.get("length_beats"):
        return float(blueprint["length_beats"])
    ends = [c["start"] + c["length"] for t in blueprint["tracks"] for c in t.get("clips") or []]
    return max(ends, default=0.0)


def trim(source: Path, out_dir: Path, seconds: tuple[float, float]) -> Path | None:
    """source's samples from seconds[0] to seconds[1] (fewer where it ends
    first), in its rate and sample format, in out_dir under its name and the
    content's digest (Live keeps playing what it loaded under a name); None
    when the file ends before seconds[0]."""
    import soundfile as sf

    info = sf.info(str(source))
    a, b = (min(info.frames, max(0, int(round(t * info.samplerate)))) for t in seconds)
    if b <= a:
        return None
    x, rate = sf.read(str(source), start=a, stop=b, dtype="float64", always_2d=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    temporary = out_dir / f".{source.stem}.{os.getpid()}.tmp{source.suffix}"
    sf.write(str(temporary), x, rate, subtype=info.subtype, format=info.format)
    target = out_dir / f"{source.stem} {sha1(temporary)[:8]}{source.suffix}"
    os.replace(temporary, target)
    return target


def excerpt(blueprint: dict, bars: tuple[int, int], base: Path | None, out_dir: Path) -> dict:
    """Bars a to b (both in, from 1) of the blueprint as a blueprint of its
    own that starts at 0, for a short check in Live (it records in real
    time). Notes that start in those bars move back by bar a's start and end
    by bar b's end (a note that starts before bar a is left out, also where
    it sounds into it); clips are cut to the bars, locators kept if they
    fall in them. Unwarped audio clips are cut at the same instants into
    out_dir, named by their content (warped ones cannot be, at the song's
    tempo alone)."""
    a, b = bars
    bar = bar_beats(blueprint)
    length = song_length(blueprint)
    last = max(1, math.ceil(length / bar - 1e-9))
    if not 1 <= a <= b <= last:
        raise ValueError(f"bars {a}-{b} are not in the blueprint's 1-{last}")
    start, end = (a - 1) * bar, min(b * bar, length)
    seconds_per_beat = 60.0 / float(blueprint["tempo"])
    out = copy.deepcopy(blueprint)
    for track in out["tracks"]:
        clips = []
        for clip in track.get("clips") or []:
            c0, c1 = clip["start"], clip["start"] + clip["length"]
            lo, hi = max(c0, start), min(c1, end)
            if hi <= lo:
                continue
            notes = []
            for note in clip.get("notes") or []:
                at = c0 + note["start_time"]
                if lo <= at < hi:
                    notes.append({**note, "start_time": round(at - lo, 6),
                                  "duration": round(min(note["duration"], hi - at), 6)})
            clips.append({**clip, "start": round(lo - start, 6), "length": round(hi - lo, 6),
                          "notes": notes})
        if "clips" in track:
            track["clips"] = clips
        audio = []
        for clip in track.get("audio_clips") or []:
            if clip.get("warping", False):
                raise ValueError(f"{track['name']}: an excerpt cuts unwarped audio clips only")
            c0 = float(clip.get("start", 0.0))
            if c0 >= end:
                continue
            source = resolve(clip["file"], base)
            target = trim(source, out_dir, (max(0.0, start - c0) * seconds_per_beat,
                                            (end - c0) * seconds_per_beat))
            if target is not None:  # a file that ends before the excerpt plays nothing in it
                audio.append({**clip, "file": str(target), "start": round(max(c0, start) - start, 6)})
        if "audio_clips" in track:
            track["audio_clips"] = audio
    out["locators"] = [{**loc, "beat": round(loc["beat"] - start, 6)}
                       for loc in blueprint.get("locators") or [] if start <= loc["beat"] < end]
    out["length_beats"] = end - start
    out.setdefault("meta", {})["excerpt"] = {
        "bars": [a, b], "beats": [start, end],
        "seconds": [round(start * seconds_per_beat, 6), round(end * seconds_per_beat, 6)]}
    return out


# ── Read-backs ───────────────────────────────────────────────────────────────

def display_db(text: Any) -> float | None:
    """The number in a parameter's display ("-6.60 dB"), -inf for "-inf dB"."""
    try:
        return float(str(text).replace("dB", "").strip())
    except ValueError:
        return float("-inf") if "inf" in str(text) else None


def volume_held(want: float, value: Any, display: Any) -> bool:
    shown = display_db(display) if isinstance(display, str) else None
    return (isinstance(value, (int, float)) and abs(float(value) - want) <= VOLUME_VALUE_DB
            and (shown is None or abs(shown - want) <= VOLUME_DISPLAY_DB))


def main(argv: list[str] | None = None) -> int:
    paths = (argv if argv is not None else sys.argv[1:])
    if not paths:
        print("usage: python -m MCP_Server.blueprint BLUEPRINT.json [...]", file=sys.stderr)
        return 2
    bad = 0
    for path in paths:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"{path}: {e}")
            bad += 1
            continue
        problems = validate(data) or check_sources(data, Path(path).resolve().parent)
        for problem in problems:
            print(f"{path}: {problem}")
        bad += bool(problems)
        if not problems:
            print(f"{path}: ok")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
