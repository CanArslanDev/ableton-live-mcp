"""Standard MIDI File reading and writing for clip import/export.

Notes use the same dicts as music.py ({pitch, start_time, duration, velocity},
times in beats). A beat is a quarter note in both Live and MIDI files, so a
tick is 1/ticks_per_beat of a Live beat. No Live access here.
"""

from __future__ import annotations

import re
from pathlib import Path

import mido

MIDI_SUFFIXES = (".mid", ".midi")
DEFAULT_TICKS_PER_BEAT = 480


def _check_suffix(path: Path) -> None:
    if path.suffix.lower() not in MIDI_SUFFIXES:
        raise ValueError(f"Expected a .mid or .midi file, got {path.name!r}")


def read_midi(path: str | Path) -> dict:
    """Tracks, notes, tempo and time signature of a MIDI file."""
    path = Path(path).expanduser()
    _check_suffix(path)
    if not path.is_file():
        raise FileNotFoundError(f"MIDI file not found: {path}")
    midi = mido.MidiFile(str(path))
    tpb = midi.ticks_per_beat or DEFAULT_TICKS_PER_BEAT
    tempo_bpm = None
    time_signature = None
    tracks = []
    for index, track in enumerate(midi.tracks):
        tick = 0
        open_notes: dict[tuple[int, int], list[tuple[int, int]]] = {}
        notes = []
        channels = set()
        name = None
        for message in track:
            tick += message.time
            if message.type == "track_name" and name is None:
                name = message.name
            elif message.type == "set_tempo" and tempo_bpm is None:
                tempo_bpm = round(mido.tempo2bpm(message.tempo), 3)
            elif message.type == "time_signature" and time_signature is None:
                time_signature = f"{message.numerator}/{message.denominator}"
            elif message.type == "note_on" and message.velocity > 0:
                open_notes.setdefault((message.channel, message.note), []).append(
                    (tick, message.velocity))
                channels.add(message.channel)
            elif message.type in ("note_off", "note_on"):
                started = open_notes.get((message.channel, message.note))
                if started:
                    start, velocity = started.pop(0)
                    notes.append(_note(message.note, start, tick, velocity, tpb,
                                       message.channel))
        # Notes never released end with their track
        for (channel, pitch), started in open_notes.items():
            for start, velocity in started:
                notes.append(_note(pitch, start, max(tick, start + 1), velocity, tpb, channel))
        notes.sort(key=lambda n: (n["start_time"], n["pitch"]))
        tracks.append({"index": index, "name": name or f"Track {index + 1}",
                       "channels": sorted(c + 1 for c in channels),
                       "note_count": len(notes), "notes": notes})
    return {"file": str(path), "type": midi.type, "ticks_per_beat": tpb,
            "tempo_bpm": tempo_bpm, "time_signature": time_signature, "tracks": tracks}


def _note(pitch: int, start: int, end: int, velocity: int, tpb: int, channel: int) -> dict:
    return {"pitch": int(pitch), "start_time": round(start / tpb, 6),
            "duration": round(max(end - start, 1) / tpb, 6), "velocity": int(velocity),
            "channel": channel + 1}


def select_notes(info: dict, track: int | str | None = None,
                 channel: int | None = None) -> list[dict]:
    """Notes of one track (index or name) or of all tracks, optionally one channel (1-16)."""
    tracks = info["tracks"]
    if track is not None:
        if isinstance(track, int) or re.fullmatch(r"\d+", str(track)):
            index = int(track)
            if not 0 <= index < len(tracks):
                raise ValueError(f"MIDI track {index} out of range ({len(tracks)} tracks)")
            tracks = [tracks[index]]
        else:
            wanted = [t for t in tracks if t["name"].lower() == str(track).lower()]
            if not wanted:
                raise ValueError(f"No MIDI track named {track!r}. Tracks: "
                                 + ", ".join(t["name"] for t in info["tracks"]))
            tracks = wanted
    notes = [dict(n) for t in tracks for n in t["notes"]
             if channel is None or n["channel"] == channel]
    notes.sort(key=lambda n: (n["start_time"], n["pitch"]))
    return notes


def write_midi(path: str | Path, notes: list[dict], tempo_bpm: float = 120.0,
               numerator: int = 4, denominator: int = 4, name: str = "Clip",
               channel: int = 1, ticks_per_beat: int = DEFAULT_TICKS_PER_BEAT) -> dict:
    """Write notes as a type-1 MIDI file: a tempo/meter track and a note track."""
    path = Path(path).expanduser()
    _check_suffix(path)
    midi = mido.MidiFile(type=1, ticks_per_beat=ticks_per_beat)
    meta = mido.MidiTrack()
    meta.append(mido.MetaMessage("track_name", name=name, time=0))
    meta.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(tempo_bpm), time=0))
    meta.append(mido.MetaMessage("time_signature", numerator=int(numerator),
                                 denominator=int(denominator), time=0))
    midi.tracks.append(meta)

    events = []
    for n in notes:
        pitch = min(127, max(0, int(n["pitch"])))
        velocity = min(127, max(1, int(round(n.get("velocity", 100)))))
        start = max(0, int(round(n["start_time"] * ticks_per_beat)))
        end = max(start + 1, int(round((n["start_time"] + n["duration"]) * ticks_per_beat)))
        # Note-offs sort before note-ons on the same tick so repeated notes re-trigger
        events.append((start, 1, mido.Message("note_on", note=pitch, velocity=velocity,
                                              channel=channel - 1)))
        events.append((end, 0, mido.Message("note_off", note=pitch, velocity=0,
                                             channel=channel - 1)))
    events.sort(key=lambda e: (e[0], e[1], e[2].note))
    track = mido.MidiTrack()
    track.append(mido.MetaMessage("track_name", name=name, time=0))
    previous = 0
    for tick, _order, message in events:
        track.append(message.copy(time=tick - previous))
        previous = tick
    track.append(mido.MetaMessage("end_of_track", time=0))
    midi.tracks.append(track)
    path.parent.mkdir(parents=True, exist_ok=True)
    midi.save(str(path))
    return {"file": str(path), "notes": len(notes), "tempo_bpm": tempo_bpm,
            "time_signature": f"{numerator}/{denominator}"}


def free_path(folder: Path, stem: str) -> Path:
    """folder/stem.mid, or 'stem 2.mid', 'stem 3.mid'… if taken."""
    safe = re.sub(r"[^\w\-. ]+", "_", stem).strip(" .") or "clip"
    candidate = folder / f"{safe}.mid"
    counter = 2
    while candidate.exists():
        candidate = folder / f"{safe} {counter}.mid"
        counter += 1
    return candidate
