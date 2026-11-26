"""A small in-memory imitation of Live's Object Model, for the tests and the
simulator (simulator.py).

Covers what the bridge and the extended tools touch: song, tracks, mixer,
devices, parameters, clip slots, clips with the Live 11 note API, scenes,
views and property listeners. Objects carry ``_live_ptr`` like real LOM
objects, so the bridge treats them as Live objects. Parameter names follow
Live 12.4 ("Output", "1 Q A"); displays imitate Live's closely enough for
setting values by display.
"""

import math
import os
import re
import sys
import time
import types
import wave
import weakref

from .journal import NO_UNDO_PROPERTIES, PARAMETER_PROPERTIES


class FakeEnum(int):
    names = {}
    values = {}

    def __str__(self):
        for name, member in type(self).names.items():
            if int(member) == int(self):
                return name
        return int.__str__(self)

    @classmethod
    def define(cls, type_name, members):
        enum_type = type(type_name, (cls,), {})
        enum_type.names, enum_type.values = {}, {}
        for index, name in enumerate(members):
            member = enum_type(index)
            enum_type.names[name] = member
            enum_type.values[index] = member
        return enum_type


WarpMode = FakeEnum.define("WarpMode", ["beats", "tones", "texture", "re_pitch", "complex",
                                        "rex", "complex_pro"])
Quantization = FakeEnum.define("Quantization", [
    "q_no_q", "q_8_bars", "q_4_bars", "q_2_bars", "q_bar", "q_half", "q_half_triplet",
    "q_quarter", "q_quarter_triplet", "q_eight", "q_eight_triplet", "q_sixtenth",
    "q_sixtenth_triplet", "q_thirtytwoth"])
RecordingQuantization = FakeEnum.define("RecordingQuantization", [
    "rec_q_no_q", "rec_q_quarter", "rec_q_eight", "rec_q_eight_triplet",
    "rec_q_eight_eight_triplet", "rec_q_sixtenth", "rec_q_sixtenth_triplet",
    "rec_q_sixtenth_sixtenth_triplet", "rec_q_thirtysecond"])


class MidiNoteSpecification(object):
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def install_fake_live_module():
    live = types.ModuleType("Live")
    live.Clip = types.SimpleNamespace(MidiNoteSpecification=MidiNoteSpecification,
                                      WarpMode=WarpMode)
    live.Song = types.SimpleNamespace(RecordingQuantization=RecordingQuantization,
                                      Quantization=Quantization)
    sys.modules["Live"] = live
    return live


class FakeObj(object):
    def __init__(self, **props):
        object.__setattr__(self, "_listeners", {})
        for key, value in props.items():
            object.__setattr__(self, key, value)

    @property
    def _live_ptr(self):
        return id(self)

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)
        for callback in list(self._listeners.get(name, [])):
            callback()

    def __getattr__(self, name):
        if name.startswith("add_") and name.endswith("_listener"):
            prop = name[4:-9]
            return lambda cb: self._listeners.setdefault(prop, []).append(cb)
        if name.startswith("remove_") and name.endswith("_listener"):
            prop = name[7:-9]
            return lambda cb: self._listeners[prop].remove(cb)
        if name.endswith("_has_listener"):
            prop = name[:-13]
            return lambda cb: cb in self._listeners.get(prop, [])
        raise AttributeError(name)


class FakeParam(FakeObj):
    def __init__(self, name, value=0.0, min=0.0, max=1.0, display=None, items=None):
        super(FakeParam, self).__init__(name=name, value=value, min=min, max=max,
                                        is_quantized=items is not None,
                                        value_items=items or [], automation_state=0)
        object.__setattr__(self, "_display", display)

    def str_for_value(self, value):
        if self.value_items:
            return self.value_items[int(round(value - self.min))]
        if self._display:
            return self._display(value)
        return "%.2f" % value

    def __setattr__(self, name, value):
        FakeObj.__setattr__(self, name, value)
        song = _RECORDING[0]
        if name == "value" and song is not None:
            song._record_automation(self, value)


# The song recording the Arrangement now, if any (Live records parameter
# changes made while it does as automation)
_RECORDING = [None]


def volume_display(value):
    if value <= 0:
        return "-inf dB"
    return "%.1f dB" % (40 * math.log10(value / 0.85))


NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note_display(value):
    """A MIDI note as Live names it: 60 is "C3", 12 "C-1"."""
    note = int(round(value))
    return "%s%d" % (NOTE_NAMES[note % 12], note // 12 - 2)


def pan_display(value):
    amount = int(round(abs(value) * 50))
    if amount == 0:
        return "C"
    return "%d%s" % (amount, "L" if value < 0 else "R")


def freq_display(value):
    hz = 20 * (1000 ** value)
    return "%.2f kHz" % (hz / 1000) if hz >= 1000 else "%.0f Hz" % hz


class FakeEnvelope(FakeObj):
    def __init__(self, parameter=None):
        super(FakeEnvelope, self).__init__(steps=[], parameter=parameter)

    def insert_step(self, time, duration, value):
        self.steps.append((time, duration, value))


class FakeNote(FakeObj):
    pass


class FakeClip(FakeObj):
    def __init__(self, name="Clip", length=4.0, midi=True):
        super(FakeClip, self).__init__(
            name=name, loop_start=0.0, loop_end=length, start_marker=0.0,
            end_marker=length, looping=True, is_midi_clip=midi, is_audio_clip=not midi,
            is_playing=False, is_recording=False, warp_mode=WarpMode(0), muted=False,
            color_index=0, file_path=None, is_arrangement_clip=False)
        object.__setattr__(self, "_notes", [])
        object.__setattr__(self, "_next_id", 1)
        object.__setattr__(self, "envelopes", {})
        object.__setattr__(self, "calls", [])

    # A clip deleted or replaced (a copy laid over it) is gone: Live 12.4.16
    # answers reads of it with an error, and refuses to change or copy it
    def __getattribute__(self, name):
        if not name.startswith("_") and object.__getattribute__(self, "__dict__").get("_gone"):
            raise RuntimeError("Object of type Clip no longer exists")
        return object.__getattribute__(self, name)

    # Live 12.4.16 (2026-11-23): a clip's length is loop_end - loop_start,
    # looped or not (end_marker does not shorten an unlooped one), and a copy
    # into the Arrangement takes that length. Unwarped audio keeps its loop in
    # seconds: a Session clip's length is that at the tempo its warp markers
    # give (Live's guess, 64 beats for a 40 s file read as 96 BPM), an
    # Arrangement clip's at the set's tempo.
    def _seconds_per_beat(self):
        if not self.is_audio_clip or vars(self).get("warping", True):
            return 1.0
        if vars(self).get("start_time") is not None:
            return 60.0 / _tempo()
        return 60.0 / self._marker_bpm()

    def _marker_bpm(self):
        real = vars(self).get("warp_markers", [])[:-1]
        if len(real) >= 2 and real[-1].sample_time > real[0].sample_time:
            return 60.0 * (real[-1].beat_time - real[0].beat_time) / (
                real[-1].sample_time - real[0].sample_time)
        return _tempo()

    @property
    def length(self):
        frozen = vars(self).get("_unwarped_length")
        if frozen is not None and vars(self).get("start_time") is None \
                and vars(self).get("warping") is False:
            # an unwarped Session clip keeps the length it had when warping
            # went off, whatever its loop (40 s → 39 s → 40 s left 64 beats)
            return frozen
        return (self.loop_end - self.loop_start) / self._seconds_per_beat()

    @length.setter
    def length(self, value):
        self.loop_end = self.loop_start + value * self._seconds_per_beat()

    def __setattr__(self, name, value):
        if vars(self).get("_gone"):
            raise RuntimeError("Object of type Clip no longer exists")
        arranged = vars(self).get("start_time") is not None
        if name == "looping" and not value and vars(self).get("looping"):
            if arranged:  # an Arrangement clip keeps its span: loop_end becomes its end
                object.__setattr__(self, "loop_end", self.loop_start + (
                    self.end_time - self.start_time) * self._seconds_per_beat())
            else:  # a Session clip's end marker moves to its loop end
                object.__setattr__(self, "end_marker", self.loop_end)
        if name == "warping" and self.is_audio_clip and bool(value) != vars(self).get("warping"):
            # Live turns the loop off with warping, and its times go to
            # seconds (back to beats warped), at the markers' tempo
            to_seconds = 60.0 / self._marker_bpm()
            factor = to_seconds if not value else 1.0 / to_seconds
            object.__setattr__(self, "_unwarped_length", None if value else self.length)
            markers = vars(self).get("warp_markers")
            if value and markers:  # warped again: Live keeps only the first marker
                first, beat = markers[0], 1 / 32.0
                markers[:] = [first, FakeObj(beat_time=first.beat_time + beat,
                                             sample_time=first.sample_time + beat * to_seconds)]
            for key in ("loop_start", "loop_end", "start_marker"):
                object.__setattr__(self, key, vars(self)[key] * factor)
            FakeObj.__setattr__(self, name, value)
            if not value:
                object.__setattr__(self, "looping", False)
                if arranged:  # unwarped it plays the file at its own speed
                    object.__setattr__(self, "end_time", self.start_time + self.length)
            return
        FakeObj.__setattr__(self, name, value)
        if name in ("loop_start", "loop_end") and arranged and not self.looping:
            # an unlooped Arrangement clip ends where its loop_end says
            object.__setattr__(self, "end_time", self.start_time + self.length)

    def move_warp_marker(self, beat_time, beat_time_distance):
        """Live 12.4.16: moves a warp marker (not the shadow marker Live keeps
        1/32 beat after the last one) by a distance in beats; the loop and the
        markers stay where they were."""
        markers = self.warp_markers
        for index, marker in enumerate(markers[:-1]):
            if abs(marker.beat_time - beat_time) < 1e-9:
                marker.beat_time = beat_time + beat_time_distance
                if index == len(markers) - 2:
                    markers[-1].beat_time = marker.beat_time + 1 / 32.0
                return
        if markers and abs(markers[-1].beat_time - beat_time) < 1e-9:
            raise RuntimeError("The shadow marker can't be moved.")
        raise RuntimeError("The specified warp marker doesn't exist")

    def remove_warp_marker(self, beat_time):
        markers = self.warp_markers
        for marker in markers[:-1]:
            if abs(marker.beat_time - beat_time) < 1e-9:
                markers.remove(marker)
                last = markers[-2] if len(markers) > 1 else markers[0]
                markers[-1].beat_time = last.beat_time + 1 / 32.0
                return
        raise RuntimeError("The specified warp marker doesn't exist")

    def get_notes_extended(self, from_pitch, pitch_span, from_time, time_span):
        return [n for n in self._notes
                if from_pitch <= n.pitch < from_pitch + pitch_span
                and from_time <= n.start_time < from_time + time_span]

    def add_new_notes(self, specs):
        ids = []
        for spec in specs:
            kw = dict(probability=1.0, velocity_deviation=0.0, release_velocity=64.0)
            kw.update(spec.kwargs)
            note = FakeNote(note_id=self._next_id, **kw)
            object.__setattr__(self, "_next_id", self._next_id + 1)
            self._notes.append(note)
            ids.append(note.note_id)
        return ids

    def remove_notes_extended(self, from_pitch, pitch_span, from_time, time_span):
        doomed = self.get_notes_extended(from_pitch, pitch_span, from_time, time_span)
        object.__setattr__(self, "_notes", [n for n in self._notes if n not in doomed])

    def remove_notes_by_id(self, ids):
        object.__setattr__(self, "_notes", [n for n in self._notes if n.note_id not in ids])

    def apply_note_modifications(self, vector):
        by_id = dict((n.note_id, n) for n in vector)
        object.__setattr__(self, "_notes", [by_id.get(n.note_id, n) for n in self._notes])

    def automation_envelope(self, param):
        return self.envelopes.get(id(param))

    def create_automation_envelope(self, param):
        env = FakeEnvelope(param)
        self.envelopes[id(param)] = env
        return env

    def clear_envelope(self, param):
        self.envelopes.pop(id(param), None)

    def fire(self):
        self.calls.append("fire")

    def quantize(self, grid, amount):
        if not isinstance(grid, RecordingQuantization):
            raise TypeError("Python argument types did not match C++ signature")
        self.calls.append(("quantize", str(grid), amount))

    def duplicate_loop(self):
        self.loop_end = self.loop_start + self.length * 2

    def sample_to_beat_time(self, sample_time):
        """A warped audio clip's beat at a sample frame of its file: here a
        straight warp at the file's own tempo (file_tempo), beat 0 at frame 0.
        Live raises the same way for an unwarped clip."""
        if self.is_midi_clip or not getattr(self, "warping", False):
            raise RuntimeError("Sample is not warped")
        return sample_time / self.sample_rate * getattr(self, "file_tempo", 120.0) / 60.0


def _copy_clip(clip):
    copy = FakeClip(name=clip.name, length=clip.length, midi=clip.is_midi_clip)
    for key in ("loop_start", "loop_end", "start_marker", "end_marker", "looping",
                "color_index", "file_path"):
        object.__setattr__(copy, key, getattr(clip, key))
    for key in ("warping", "sample_rate", "sample_length"):
        if key in vars(clip):
            object.__setattr__(copy, key, vars(clip)[key])
    if "warp_markers" in vars(clip):
        object.__setattr__(copy, "warp_markers", [FakeObj(beat_time=m.beat_time,
                                                          sample_time=m.sample_time)
                                                  for m in clip.warp_markers])
    for note in clip._notes:
        copy._notes.append(FakeNote(**{k: v for k, v in vars(note).items() if k != "_listeners"}))
    object.__setattr__(copy, "_next_id", clip._next_id)
    for key, envelope in clip.envelopes.items():
        twin = FakeEnvelope(envelope.parameter)
        twin.steps.extend(envelope.steps)
        copy.envelopes[key] = twin
    return copy


def _clip_time_at(clip, position):
    """The clip's own time that plays at an Arrangement position."""
    offset = position - clip.start_time
    first = clip.loop_end - clip.start_marker
    if not clip.looping or offset < first:
        return clip.start_marker + offset
    loop = clip.loop_end - clip.loop_start
    return clip.loop_start + (offset - first) % loop


class FakeClipSlot(FakeObj):
    def __init__(self, clip=None):
        super(FakeClipSlot, self).__init__(clip=clip, has_clip=clip is not None)

    def create_clip(self, length):
        if vars(self).get("_midi") is False:
            raise RuntimeError("MIDI clips can only be created on MIDI tracks")
        self.clip = FakeClip(length=length)
        self.has_clip = True

    def delete_clip(self):
        self.clip = None
        self.has_clip = False

    def stop(self):
        if self.clip is not None:
            self.clip.is_playing = False

    def duplicate_clip_to(self, target):
        target.clip = FakeClip(name=self.clip.name, length=self.clip.length)
        target.has_clip = True

    def create_audio_clip(self, path):
        clip = _new_audio_clip(path)
        self.clip = clip
        self.has_clip = True
        return clip


# The tempo Live's warp analysis reads a file at, by path; others are read
# at the set's tempo. Live 12.4.16 (2026-11-23) read a 40 s click track made
# at 120 BPM as 96 BPM (64 beats, three warp markers) and an 8 s one at
# 120 and a 100 BPM one right.
FILE_TEMPOS = {}


def _tempo():
    for song in (ref() for ref in reversed(_SONGS)):
        if song is not None:
            return float(vars(song).get("tempo", 120.0))
    return 120.0


def _file_seconds(path):
    """(seconds, sample rate) of a PCM WAV; 4 s at 44.1 kHz for anything else."""
    try:
        with wave.open(str(path)) as w:
            return w.getnframes() / float(w.getframerate()), float(w.getframerate())
    except Exception:
        return 4.0, 44100.0


def _new_audio_clip(path):
    """A new audio clip as Live 12.4.16 makes it: warped and looped at the
    tempo its analysis reads (FILE_TEMPOS), warp markers at the file's start
    and end plus the shadow marker 1/32 beat later, sample_length in frames."""
    seconds, rate = _file_seconds(path)
    bpm = FILE_TEMPOS.get(str(path), _tempo())
    beats = seconds * bpm / 60.0
    clip = FakeClip(name=str(path).replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0],
                    length=beats, midi=False)
    markers = [FakeObj(beat_time=0.0, sample_time=0.0),
               FakeObj(beat_time=beats, sample_time=seconds),
               FakeObj(beat_time=beats + 1 / 32.0, sample_time=seconds + 60.0 / bpm / 32.0)]
    for key, value in (("file_path", path), ("warping", True), ("sample_rate", rate),
                       ("sample_length", int(round(seconds * rate))), ("warp_markers", markers)):
        object.__setattr__(clip, key, value)
    _at_first_transient(clip)
    return clip


# Where Live puts a new audio clip's start: at the file's first transient
# (21 ms to 3.6 s in, reference-dissector's first live build, 2026-10-11)
# once warping goes off after its warp analysis (Live 12.4.16 moved it 0 to
# 0.53 then; warped, or reset in the same batch, it stays at 0). Here at
# once and always, a fixed quarter beat: a stricter Live, so a tool that
# leaves it there is caught.
FIRST_TRANSIENT = 0.25


def _at_first_transient(clip):
    object.__setattr__(clip, "loop_start", FIRST_TRANSIENT)
    object.__setattr__(clip, "start_marker", FIRST_TRANSIENT)


class FakeSample(FakeObj):
    def __init__(self, file_path, seconds=0.5):
        super(FakeSample, self).__init__(file_path=file_path, sample_rate=44100.0,
                                         length=int(seconds * 44100), start_marker=0,
                                         end_marker=int(seconds * 44100) - 1)


class FakeDevice(FakeObj):
    def __init__(self, name, class_name="OriginalSimpler", parameters=None, **extra):
        params = [FakeParam("Device On", 1.0)] + list(parameters or [])
        super(FakeDevice, self).__init__(name=name, class_name=class_name,
                                         parameters=params, can_have_chains=False, **extra)


def _compressor():
    routing = [FakeObj(display_name=n) for n in ("Drums", "Bass", "No Input")]
    return FakeDevice("Compressor", "Compressor2", [
        FakeParam("Threshold", 0.85, display=lambda v: "%.2f dB" % (-60 + 60 * v)),
        FakeParam("Ratio", 0.75, display=lambda v: "%.2f : 1" % (1 + 99 * v)),
        FakeParam("Attack", 0.4, display=lambda v: "%.2f ms" % (0.01 * 1000 ** v * 10)),
        FakeParam("Release", 0.2, display=lambda v: "%.1f ms" % (3000 ** v)),
        FakeParam("Model", 1, 0, 2, items=["Peak", "RMS", "Expand"]),
        FakeParam("S/C On", 0, 0, 1, items=["Off", "On"]),
        # Live 12.4's name; Live 12.3 called it "Output Gain"
        FakeParam("Output", 0.0, -36.0, 36.0, display=lambda v: "%.2f dB" % v),
    ], available_input_routing_types=routing, input_routing_type=routing[-1])


def _insert_device(devices, name, index=-1):
    """Track/Chain.insert_device (Live 12.3+): a native device by browser name."""
    if name not in INSERTABLE:
        raise RuntimeError("Device %s not found." % name)  # Live 12.4.16's text, its period included
    device = INSERTABLE[name]()
    if index == -1:
        devices.append(device)
    else:
        devices.insert(index, device)
    return device


class FakeDrumPad(FakeObj):
    """A pad of a FakeDrumRack: its chains are the rack's chains that receive
    its note (in_note), as in Live."""

    def __init__(self, note, rack):
        super(FakeDrumPad, self).__init__(note=note, name="", mute=False, solo=False)
        object.__setattr__(self, "_rack", rack)

    @property
    def chains(self):
        return [c for c in self._rack.chains if getattr(c, "in_note", None) == self.note]

    @chains.setter
    def chains(self, chains):
        """What a sample dropped on the pad does: its chains replace the pad's."""
        for chain in chains:
            object.__setattr__(chain, "in_note", self.note)
        self._rack.chains = [c for c in self._rack.chains
                             if getattr(c, "in_note", None) != self.note] + list(chains)

    def delete_all_chains(self):
        self.chains = []


def chain_mixer(sends=0):
    """A rack chain's ChainMixerDevice (Live 12.4.16's parameter names)."""
    return FakeObj(volume=FakeParam("Chain Volume", 0.85, display=volume_display),
                   panning=FakeParam("Chain Panorama", 0.0, -1.0, 1.0, display=pan_display),
                   sends=[FakeParam("Chain Send %d" % i, 0.0, display=volume_display)
                          for i in range(sends)],
                   chain_activator=FakeParam("Speaker On", 1.0, items=["Off", "On"]))


class FakeDrumChain(FakeObj):
    """A chain from RackDevice.insert_chain (Live 12.3+): it receives every
    note (in_note -1) until in_note puts it on a pad."""

    def __init__(self):
        super(FakeDrumChain, self).__init__(name="", in_note=-1, out_note=60, choke_group=0,
                                            devices=[], mute=False, solo=False,
                                            mixer_device=chain_mixer())

    def insert_device(self, name, index=-1):
        return _insert_device(self.devices, name, index)

    def delete_device(self, index):
        del self.devices[index]


class FakeChain(FakeObj):
    """A chain of an Instrument or Audio Effect Rack; Chain.delete_device
    deleted the Saturator inside "BNYX Boot Kit" in Live 12.4.16."""

    def __init__(self, name, devices):
        super(FakeChain, self).__init__(name=name, devices=list(devices), mute=False, solo=False,
                                        mixer_device=chain_mixer())

    def insert_device(self, name, index=-1):
        return _insert_device(self.devices, name, index)

    def delete_device(self, index):
        del self.devices[index]


class FakeDrumRack(FakeDevice):
    def __init__(self):
        super(FakeDrumRack, self).__init__("Drum Rack", "DrumGroupDevice", chains=[],
                                           return_chains=[],
                                           view=FakeObj(selected_drum_pad=None))
        object.__setattr__(self, "drum_pads", [FakeDrumPad(n, self) for n in range(128)])
        object.__setattr__(self, "can_have_chains", True)

    def insert_chain(self, index=None):
        """Returns nothing, the way the fake assumes Live does."""
        chains = list(self.chains)
        chains.insert(len(chains) if index is None else int(index), FakeDrumChain())
        self.chains = chains


def _drum_rack():
    return FakeDrumRack()


class FakeRack(FakeDevice):
    """An Instrument (or Audio Effect) Rack: chains of devices, each with its
    mixer. Kits like Live 12.4's "BNYX Boot Kit" are one holding a Drum Rack
    and a Saturator in one chain."""

    def __init__(self, name, chains, class_name="InstrumentGroupDevice"):
        super(FakeRack, self).__init__(name, class_name, chains=[
            FakeChain(chain_name, devices) for chain_name, devices in chains])
        object.__setattr__(self, "can_have_chains", True)


def _log_display(low, high, unit_low, unit_high=None, split=1000.0, digits=2):
    """A logarithmic control (frequency, time) shown like Live: "90.0 Hz", "10.0 kHz"."""
    def show(value):
        amount = low * (high / low) ** value
        if unit_high and amount >= split:
            return "%.*f %s" % (digits, amount / split, unit_high)
        return "%.*f %s" % (digits if amount < 100 else 1, amount, unit_low)
    return show


def _hz(name, value=0.5):
    return FakeParam(name, value, display=_log_display(10.0, 22000.0, "Hz", "kHz"))


def _db(name, value=0.0, low=-36.0, high=36.0):
    return FakeParam(name, value, low, high, display=lambda v: "%.1f dB" % v)


def _percent(name, value=0.0, high=100.0):
    return FakeParam(name, value, 0.0, high, display=lambda v: "%.0f %%" % v)


def _ms(name, value=0.5, low=0.1, high=1000.0):
    return FakeParam(name, value, display=_log_display(low, high, "ms", "s"))


def _choice(name, items, index=0):
    return FakeParam(name, index, 0, len(items) - 1, items=list(items))


def _stepped(name, shown, index=0):
    """Glue Compressor's Attack, Release and Ratio (Live 12.4.16): not
    quantized, 0..n-1, each step shown from its value up ("A" is Auto)."""
    return FakeParam(name, float(index), 0.0, float(len(shown) - 1),
                     display=lambda v: shown[min(int(v), len(shown) - 1)])


def _switch(name, on=False):
    return _choice(name, ["Off", "On"], 1 if on else 0)


EQ_FILTER_TYPES = ["High Pass 48dB", "High Pass 12dB", "Low Shelf", "Bell", "Notch",
                   "High Shelf", "Low Pass 12dB", "Low Pass 48dB"]


def _eq_eight():
    params = []
    for band, kind, hz in ((1, 2, 0.05), (2, 3, 0.3), (3, 3, 0.45), (4, 5, 0.6),
                           (5, 3, 0.7), (6, 3, 0.8), (7, 3, 0.85), (8, 6, 0.95)):
        # bands 1-4 on, 5-8 off, as Live 12.4.16 inserts it (2026-11-26):
        # Low Shelf, Bell, Bell, High Shelf, Bell, Bell, Bell, Low Pass 12dB
        params += [_switch("%d Filter On A" % band, band <= 4),
                   _choice("%d Filter Type A" % band, EQ_FILTER_TYPES, kind),
                   _hz("%d Frequency A" % band, hz),
                   _db("%d Gain A" % band, 0.0, -15.0, 15.0),
                   FakeParam("%d Q A" % band, 0.71, 0.1, 18.0)]
    return FakeDevice("EQ Eight", "Eq8", params + [_db("Output", 0.0, -12.0, 12.0)])


def _instrument(name, class_name):
    extra = [_choice("Osc-A Wave", ["Sine", "Sine 4 Bit", "Sine 8 Bit", "Saw 3", "Saw 4", "Saw 6",
                                    "Saw 8", "Saw 16", "Saw D", "Square 3"]),
             _switch("Filter On", True)] if name == "Operator" else []
    return lambda: FakeDevice(name, class_name, extra + [
        _hz("Filter Freq", 0.8), FakeParam("Filter Res", 0.2), _db("Volume", -12.0, -70.0, 6.0)])


def _release_display(ms):
    return "%.1f ms" % ms if ms < 1000 else "%.2f s" % (ms / 1000.0)


class FakeSimpler(FakeDevice):
    """Simpler with the parameters the tools set by name (Live 12.4's names;
    "Vol < Vel" a raw 0..1, Live's default for a Simpler made with
    insert_device) and voices, a property of the device. "Fade In" and
    "Fade Out" start at 0.1 ms, as on a Simpler made with insert_device in
    Live 12.4.16 (2026-11-14); a pad the browser loads starts its Fade In at 0
    (FakeBrowser.load_item). Their ranges were not measured."""

    def __init__(self, name, sample=None, fade_in=0.1, voices=6):
        super(FakeSimpler, self).__init__(name, "OriginalSimpler", [
            _percent("S Start", 0.0),
            FakeParam("Transpose", 0.0, -48.0, 48.0, display=lambda v: "%d st" % round(v)),
            FakeParam("Detune", 0.0, -50.0, 50.0, display=lambda v: "%d ct" % round(v)),
            _db("Volume", -12.0, -36.0, 36.0),
            _choice("Trigger Mode", ["Trigger", "Gate"], 1),
            FakeParam("Vol < Vel", 0.35, 0.0, 1.0, display=lambda v: "%.0f %%" % (100 * v)),
            _switch("Snap", True), _switch("F On", True),
            FakeParam("Ve Release", 50.0, 1.0, 60000.0, display=_release_display),
            _choice("Glide Mode", ["Off", "Glide", "Portamento"]),
            FakeParam("Glide Time", 50.0, 0.0, 5000.0, display=_release_display),
            FakeParam("Fade In", fade_in, 0.0, 2000.0, display=_release_display),
            FakeParam("Fade Out", 0.1, 0.0, 2000.0, display=_release_display)],
            sample=sample, playback_mode=0, reversed=False, voices=voices)

    def reverse(self):
        """A toggle. As in Live 12.4.16, the sample becomes a reversed copy in
        the project's Samples/Processed/Reverse ("<name> R.<its extension>"), and
        reversing again gives the original file back."""
        self.reversed = not self.reversed
        sample = self.sample
        if sample is not None:
            if self.reversed:
                object.__setattr__(self, "_forward", sample.file_path)
                stem, ext = os.path.splitext(os.path.basename(sample.file_path))
                sample.file_path = "/project/Samples/Processed/Reverse/%s R%s" % (stem, ext)
            else:
                sample.file_path = vars(self).get("_forward", sample.file_path)

    def replace_sample(self, file_path):
        """Live 12.4: the audio file at an absolute path replaces the sample."""
        if not os.path.isabs(file_path) or not os.path.isfile(file_path):
            raise RuntimeError("Could not load %s" % file_path)
        self.sample = FakeSample(file_path)


def _named_after(track, instrument):
    """Live 12.4.16 (2026-11-23): a regular track still named as Live named it
    ("1-MIDI") takes the name of an instrument that lands on it ("1-Drift"),
    by insert_device or the browser, and of the next one that replaces it
    ("1-Operator"); effects (Reverb) leave it. (On a return track an effect
    renames it too, "A-Reverb | Utility"; the fake does not model that.)"""
    numbered = re.match(r"(\d+)-(MIDI|Audio)$", track.name)
    if not numbered and track.name == vars(track).get("_live_named"):
        numbered = re.match(r"(\d+)-", track.name)
    if numbered:
        track.name = "%s-%s" % (numbered.group(1), instrument)
        object.__setattr__(track, "_live_named", track.name)


def _songs_of(track):
    return [song for song in (ref() for ref in _SONGS) if song is not None
            and any(t is track for t in list(song.tracks) + list(song.return_tracks)
                    + [song.master_track])]


def _playhead_to(track, beat, moved=False):
    """Live 12.4.16 moves the stopped playhead to the start of a clip that
    duplicate_clip_to_arrangement, create_midi_clip or create_audio_clip
    makes in a track's Arrangement (deleting one leaves it). moved: not a
    set, for the pinned selection (FakeSongView)."""
    for song in _songs_of(track):
        if not vars(song).get("is_playing"):
            if moved:
                object.__setattr__(song, "_position", float(beat))
                object.__setattr__(song, "_moved", True)
                if song.running_clock:
                    object.__setattr__(song, "_clock", (time.monotonic(), float(beat)))
            else:
                song.current_song_time = float(beat)


def _select(track, new=False):
    """Live 12.4.16 selects a track a device was inserted on, a new MIDI or
    audio track (create_*_track, new), and one whose device was deleted
    while another device stays on it (that device is selected); Live does
    it after the command, the fake at once. A new track, selected so, can be
    pinned (FakeSongView)."""
    for song in _songs_of(track):
        song.view.selected_track = track
        if new:
            object.__setattr__(song.view, "_made", track)


class FakeSongView(FakeObj):
    """Song.view with the pinned selection measured in Live 12.4.16
    (2026-11-23). A clip copied onto a new track Live selected on creating
    it, then the playhead set, pin that track and the copy's span. From a
    playhead outside the span, a playhead set to inside it ([start, end])
    or a copy overlapping it selects the pinned track, seen from the next
    command on (here at end_undo_step); a copy that only moves the
    playhead, or a set from inside, does not. Inside is [start, end] for a
    playhead set there, [start, end) for one a copy moved there.
    selected_track set back to the track selected before is then ignored
    until another track is set; the pin stays. Deleting the pinned track,
    or a clip covering its whole span (the pinning copy, or one that
    replaced it), ends it. Where Live was not measured the fake is strict: a
    device insert's selection pins nothing (one Reverb trial did not), and the
    pin outlives new tracks (one made and deleted in Live ended it; which of
    the two is unknown)."""

    def __setattr__(self, name, value):
        if name == "selected_track":
            ignored = vars(self).get("_ignored")
            if ignored is not None and value is ignored:
                return
            object.__setattr__(self, "_ignored", None)
        FakeObj.__setattr__(self, name, value)

    def _entering(self, origin, moved, lands):
        """Queue the jump when the pinned span is entered from outside;
        moved: a copy put the playhead at origin (not a set)."""
        pinned = vars(self).get("_pinned")
        if pinned is None:
            return
        track, start, end = pinned
        inside = start <= origin < end if moved else start <= origin <= end
        if not inside and lands(start, end):
            object.__setattr__(self, "_jump", track)

    def arrangement_copy(self, track, copy, origin, moved):
        """duplicate_clip_to_arrangement made `copy` on `track`, the playhead
        at `origin` before it (the copy then moves it)."""
        start, end = copy.start_time, copy.end_time
        self._entering(origin, moved, lambda s, e: start < e and end > s)
        if track is vars(self).get("_made") and track is self.selected_track:
            object.__setattr__(self, "_pin_next", (track, start, end))

    def playhead_set(self, origin, moved, target):
        if vars(self).get("_pin_next") is not None:
            object.__setattr__(self, "_pinned", self._pin_next)
            object.__setattr__(self, "_pin_next", None)
            return
        self._entering(origin, moved, lambda s, e: s <= target <= e)

    def after_command(self):
        jump = vars(self).get("_jump")
        object.__setattr__(self, "_jump", None)
        if jump is not None and self.selected_track is not jump:
            previous = self.selected_track
            FakeObj.__setattr__(self, "selected_track", jump)
            object.__setattr__(self, "_ignored", previous)

    def track_deleted(self, track):
        pinned = vars(self).get("_pinned")
        if pinned is not None and pinned[0] is track:
            object.__setattr__(self, "_pinned", None)

    def clip_deleted(self, track, clip):
        """Track.delete_clip: a clip of the pinned track covering its whole
        span (the pinning copy, or one that replaced it) ends the pin."""
        pinned = vars(self).get("_pinned")
        if pinned is not None and pinned[0] is track and clip.start_time <= pinned[1] \
                and clip.end_time >= pinned[2]:
            object.__setattr__(self, "_pinned", None)


def _auto_arm(track, empty=None):
    """Live 12.4.16, a device landing on a MIDI track with none: the track is
    armed (arm, not implicit_arm), and with exclusive arm (Live's default)
    every other track loses its arm. A MIDI track holding only a MIDI effect
    (Arpeggiator) is not empty: an instrument onto it does not arm it.
    empty: whether the track had no device (default: has none now)."""
    if not track.has_midi_input or not (not track.devices if empty is None else empty):
        return
    track.arm = True
    for song in _songs_of(track):
        if song.exclusive_arm:
            for other in song.tracks:
                if other is not track and other.arm:
                    other.arm = False


_SONGS = []  # weak references to every FakeSong, for _auto_arm


# Live 12.4's instruments, as its browser lists them
INSTRUMENTS = frozenset(["Analog", "Collision", "Drift", "Drum Rack", "Drum Sampler", "Electric",
                         "External Instrument", "Impulse", "Instrument Rack", "Meld", "Operator",
                         "Sampler", "Simpler", "Tension", "Wavetable"])

# What Track.insert_device builds (Live 12.3+), by browser name
INSERTABLE = {
    "Drum Rack": _drum_rack,
    "Compressor": _compressor,
    "Utility": lambda: FakeDevice("Utility", "StereoGain", [
        FakeParam("Bass Mono", 0, 0, 1, items=["Off", "On"]),
        FakeParam("Gain", 0.0, -1.0, 1.0),
        FakeParam("Bass Freq", 0.3, display=_log_display(50.0, 500.0, "Hz")),
        _percent("Stereo Width", 100.0, 400.0), _switch("Mono")]),
    "EQ Eight": _eq_eight,
    "Saturator": lambda: FakeDevice("Saturator", "Saturator", [
        _db("Drive", 0.0, -36.0, 36.0),
        _choice("Type", ["Analog Clip", "Soft Sine", "Bass Shaper", "Medium Curve",
                         "Hard Curve", "Sinoid Fold", "Digital Clip", "Waveshaper"]),
        _db("Output", 0.0, -36.0, 0.0), _percent("Dry/Wet", 100.0)]),
    # Live 12.4.16: Blend shows a split, FB Note note names
    "Roar": lambda: FakeDevice("Roar", "Roar", [
        FakeParam("Blend", 0.5, display=lambda v: "%d / %d" % (round(100 * (1 - v)),
                                                                 round(100 * v))),
        FakeParam("FB Note", 60.0, 12.0, 84.0, display=note_display),
        _db("Output", 0.0, -36.0, 36.0)]),
    "Glue Compressor": lambda: FakeDevice("Glue Compressor", "GlueCompressor", [
        _db("Threshold", 0.0, -40.0, 0.0),
        _stepped("Ratio", ["2", "4", "10"], 1),
        _stepped("Attack", [".01", ".1", ".3", "1", "3", "10", "30"], 3),
        _stepped("Release", [".1", ".2", ".4", ".6", ".8", "1.2", "A"], 3),
        _db("Output", 0.0, -20.0, 20.0), _percent("Dry/Wet", 100.0)]),
    "Drum Buss": lambda: FakeDevice("Drum Buss", "DrumBuss", [
        _percent("Drive"), _percent("Crunch"), FakeParam("Transients", 0.0, -1.0, 1.0),
        _percent("Boom"), _db("Output", 0.0, -36.0, 12.0), _percent("Dry/Wet", 100.0)]),
    "Reverb": lambda: FakeDevice("Reverb", "Reverb", [
        FakeParam("Decay Time", 0.4, display=_log_display(0.2, 60.0, "s")),
        _ms("Predelay", 0.3, 0.5, 250.0), FakeParam("Size", 50.0, 0.22, 500.0),
        _percent("Dry/Wet", 50.0)]),
    "Delay": lambda: FakeDevice("Delay", "Delay", [
        _percent("Feedback", 40.0), _hz("Filter Freq", 0.6), _percent("Dry/Wet", 50.0)]),
    "Chorus-Ensemble": lambda: FakeDevice("Chorus-Ensemble", "Chorus2", [
        FakeParam("Rate", 0.3, display=_log_display(0.01, 20.0, "Hz")), _percent("Amount", 50.0),
        _percent("Dry/Wet", 50.0)]),
    "Auto Filter": lambda: FakeDevice("Auto Filter", "AutoFilter2", [
        _choice("Filter Type", ["Low-pass", "High-pass", "Band-pass", "Notch", "Morph", "DJ",
                                "Comb", "Resampling"]),
        _hz("Frequency", 0.9), _percent("Resonance", 20.0), _percent("LFO Amount"),
        _percent("Dry/Wet", 100.0)]),
    # Live 12.4.16's parameters, in its order (2026-11-23); Live stores the
    # levels as 0..1, the fake as dB
    "Limiter": lambda: FakeDevice("Limiter", "Limiter", [
        _db("Input Gain", 0.0, -24.0, 24.0), _db("Ceiling", 0.0, -24.0, 0.0),
        _ms("Release", 0.73, 1.0, 1000.0), _switch("Auto"), _percent("Link", 100.0),
        _percent("M/S Link"), _choice("Lookahead", ["1.5 ms", "3 ms", "6 ms"]),
        _choice("Routing", ["L/R", "M/S"]), _choice("Mode", ["Standard", "Soft Clip", "True Peak"]),
        _switch("Maximize On"), _db("Threshold", 0.0, -24.0, 0.0),
        _db("Output", 0.0, -24.0, 0.0)]),
    "Operator": _instrument("Operator", "Operator"),
    "Drift": _instrument("Drift", "Drift"),
    "Wavetable": _instrument("Wavetable", "InstrumentVector"),
    "Analog": _instrument("Analog", "UltraAnalog"),
    "Simpler": lambda: FakeSimpler("Simpler"),
}


def _slot_of(midi):
    slot = FakeClipSlot()
    object.__setattr__(slot, "_midi", bool(midi))  # Live refuses MIDI clips on audio tracks
    return slot


class FakeTrack(FakeObj):
    def __init__(self, name, midi=True, slots=4, devices=None, sends=2):
        mixer = FakeObj(volume=FakeParam("Track Volume", 0.85, display=volume_display),
                        panning=FakeParam("Track Panning", 0.0, -1.0, 1.0, display=pan_display),
                        sends=[FakeParam("Send %s" % chr(65 + i), 0.0, display=volume_display)
                               for i in range(sends)])
        routing = [FakeObj(display_name=n) for n in ("Ext. In", "Resampling", "Drums")]
        super(FakeTrack, self).__init__(
            name=name, has_midi_input=midi, has_audio_input=not midi, mute=False, solo=False,
            arm=False, implicit_arm=False, can_be_armed=True, is_foldable=False,
            is_grouped=False,
            color_index=1, color=0, mixer_device=mixer, devices=list(devices or []),
            clip_slots=[_slot_of(midi) for _ in range(slots)], arrangement_clips=[],
            current_monitoring_state=1, available_input_routing_types=routing,
            input_routing_type=routing[0], has_audio_output=True,
            output_meter_left=0.0, output_meter_right=0.0)

    def delete_device(self, index):
        del self.devices[index]
        if self.devices:
            _select(self)

    def delete_clip(self, clip):
        """Track.delete_clip: removes an Arrangement clip (Live 12.4.16)."""
        if not any(c is clip for c in self.arrangement_clips):
            raise RuntimeError("Clip not found on this track")
        for song in _songs_of(self):
            song.view.clip_deleted(self, clip)
        self.arrangement_clips[:] = [c for c in self.arrangement_clips if c is not clip]
        object.__setattr__(clip, "_gone", True)

    def duplicate_clip_to_arrangement(self, clip, time):
        """Like Live 12.4: the copy is the clip's length long (loop_end -
        loop_start, looped or not) and keeps notes and envelopes; clips under
        it are split, the parts keeping all their notes and moving their
        markers, and clips it covers go."""
        if clip.is_midi_clip != bool(self.has_midi_input):
            raise RuntimeError("Clip type does not match the track")
        start = float(time)
        end = start + (clip.end_time - clip.start_time if vars(clip).get("start_time") is not None
                       else clip.length)
        copy = _copy_clip(clip)
        object.__setattr__(copy, "is_arrangement_clip", True)
        object.__setattr__(copy, "start_time", start)
        object.__setattr__(copy, "end_time", end)
        kept = []
        for other in self.arrangement_clips:
            if other.end_time <= start or other.start_time >= end:
                kept.append(other)
                continue
            if other.start_time >= start and other.end_time <= end:  # covered: replaced
                object.__setattr__(other, "_gone", True)
                continue
            if other.end_time > end:  # a part goes on after the copy
                tail = other if other.start_time >= start else _copy_clip(other)
                object.__setattr__(tail, "start_marker", _clip_time_at(other, end))
                object.__setattr__(tail, "start_time", end)
                object.__setattr__(tail, "end_time", other.end_time)
                kept.append(tail)
            if other.start_time < start:  # a part plays before it
                object.__setattr__(other, "end_time", start)
                kept.append(other)
        kept.append(copy)
        self.arrangement_clips[:] = sorted(kept, key=lambda c: c.start_time)
        for song in _songs_of(self):
            song.view.arrangement_copy(self, copy, song.current_song_time,
                                       bool(vars(song).get("_moved")))
        _playhead_to(self, start, moved=True)
        return copy

    def create_midi_clip(self, start, length):
        clip = FakeClip(length=length)
        object.__setattr__(clip, "is_arrangement_clip", True)
        object.__setattr__(clip, "start_time", float(start))
        object.__setattr__(clip, "end_time", float(start) + length)
        self.arrangement_clips.append(clip)
        _playhead_to(self, start)
        return clip

    def create_audio_clip(self, path, position):
        clip = _new_audio_clip(path)
        object.__setattr__(clip, "is_arrangement_clip", True)
        object.__setattr__(clip, "start_time", float(position))
        object.__setattr__(clip, "end_time", float(position) + clip.loop_end)
        self.arrangement_clips.append(clip)
        _playhead_to(self, position)
        return clip

    def insert_device(self, name, index=-1):
        was_empty = not self.devices
        device = _insert_device(self.devices, name, index)  # an unknown name raises first
        _auto_arm(self, was_empty)  # Reverb, Drum Rack, Drift alike (Live 12.4.16)
        _select(self)
        if name in INSTRUMENTS:
            _named_after(self, device.name)
        return device


# ── Undo history, by the rules measured on Live 12.4 (see journal.py) ───────

# Bookkeeping of the fakes themselves, never part of an undo step
_NOT_SET_STATE = frozenset(["_live_named", "_unwarped_length", "_gone", "_midi", "exclusive_arm", "_listeners", "undo_log", "calls",
                            "_undo", "_redo", "_open_step",
                            "_position", "_clock", "_stopped_at", "_pass", "_pass_closing", "_pass_starting",
                            "running_clock", "_data", "_moved"])
_MISSING = object()


def _capture(root):
    """The state of every FakeObj reachable from root: {id: (obj, {attr: value})},
    lists and dicts copied so later changes to them show."""
    seen = {}
    stack = [root]
    while stack:
        obj = stack.pop()
        if isinstance(obj, (list, tuple)):
            stack.extend(obj)
            continue
        if not isinstance(obj, FakeObj) or id(obj) in seen:
            continue
        state = {}
        for key, value in vars(obj).items():
            if key in _NOT_SET_STATE:
                continue
            if isinstance(value, list):
                state[key] = list(value)
                stack.extend(value)
            elif isinstance(value, dict):
                state[key] = dict(value)
                stack.extend(value.values())
            else:
                state[key] = value
                stack.append(value)
        seen[id(obj)] = (obj, state)
    return seen


def _same(a, b):
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(x is y or x == y for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(a[k] is b[k] or a[k] == b[k] for k in a)
    return a is b or a == b


def _changes(before, after):
    """(obj, attr, old, new) for each attribute that changed and Live would undo."""
    found = []
    for key, (obj, old_state) in before.items():
        if key not in after:
            continue  # no longer in the set: the list that held it changed
        new_state = after[key][1]
        for attr in set(old_state) | set(new_state):
            if attr in NO_UNDO_PROPERTIES:
                continue
            old, new = old_state.get(attr, _MISSING), new_state.get(attr, _MISSING)
            if not _same(old, new):
                found.append((obj, attr, old, new))
    return found


def _apply(changes, use_old):
    """Undo (use_old) walks the changes back, redo forward: a step may change
    one value several times (a recorded automation pass)."""
    for obj, attr, old, new in (reversed(changes) if use_old else changes):
        value = old if use_old else new
        current = vars(obj).get(attr, _MISSING)
        vars(obj).pop("_gone", None)  # a clip undo or redo changes is there again
        if value is _MISSING:
            vars(obj).pop(attr, None)
        elif isinstance(value, list) and isinstance(current, list):
            for item in value:  # clips back in a track's lists live again (as in Live)
                if isinstance(item, FakeObj):
                    vars(item).pop("_gone", None)
            for item in current:  # and clips that leave them (a redone delete) are gone
                if isinstance(item, FakeClip) and not any(item is v for v in value):
                    object.__setattr__(item, "_gone", True)
            current[:] = value
        elif isinstance(value, dict) and isinstance(current, dict):
            current.clear()
            current.update(value)
        else:
            setattr(obj, attr, value)


# Live 12's Key & Scale names and their intervals (scale_intervals follows scale_name)
LIVE_SCALES = {
    "Major": [0, 2, 4, 5, 7, 9, 11], "Minor": [0, 2, 3, 5, 7, 8, 10],
    "Dorian": [0, 2, 3, 5, 7, 9, 10], "Mixolydian": [0, 2, 4, 5, 7, 9, 10],
    "Lydian": [0, 2, 4, 6, 7, 9, 11], "Phrygian": [0, 1, 3, 5, 7, 8, 10],
    "Locrian": [0, 1, 3, 5, 6, 8, 10], "Harmonic Minor": [0, 2, 3, 5, 7, 8, 11],
    "Melodic Minor": [0, 2, 3, 5, 7, 9, 11], "Major Pentatonic": [0, 2, 4, 7, 9],
    "Minor Pentatonic": [0, 3, 5, 7, 10], "Whole Tone": [0, 2, 4, 6, 8, 10],
    "Phrygian Dominant": [0, 1, 4, 5, 7, 8, 10], "Minor Blues": [0, 3, 5, 6, 7, 10],
}


class FakeSong(FakeObj):
    # The simulator's song keeps time: while it plays, current_song_time moves
    # at the tempo, and parameter changes made while the Arrangement records
    # become automation. As in Live 12.4, a recording pass leaves an undo step
    # for every command run while it records plus one: here record on leaves
    # none and record off holds the automation. Tests keep the playhead still
    # unless they ask.
    running_clock = False

    def __setattr__(self, name, value):
        FakeObj.__setattr__(self, name, value)
        if name == "scale_name" and value in LIVE_SCALES:
            FakeObj.__setattr__(self, "scale_intervals", list(LIVE_SCALES[value]))
        if name in ("record_mode", "is_playing"):
            self._update_recording()

    @property
    def current_song_time(self):
        clock = vars(self).get("_clock")
        if clock is not None and vars(self).get("is_playing"):
            started, position = clock
            return position + (time.monotonic() - started) * vars(self)["tempo"] / 60.0
        return vars(self).get("_position", 0.0)

    @current_song_time.setter
    def current_song_time(self, value):
        origin, moved = vars(self).get("_position", 0.0), bool(vars(self).get("_moved"))
        object.__setattr__(self, "_position", float(value))
        object.__setattr__(self, "_moved", False)
        if "view" in vars(self):
            self.view.playhead_set(origin, moved, float(value))
        if self.running_clock:
            object.__setattr__(self, "_clock", (time.monotonic(), float(value)))

    def _update_recording(self):
        recording = bool(vars(self).get("record_mode")) and bool(vars(self).get("is_playing"))
        if recording and _RECORDING[0] is not self:
            _RECORDING[0] = self
            object.__setattr__(self, "_pass", [])
            object.__setattr__(self, "_pass_starting", True)
        elif not recording and _RECORDING[0] is self:
            _RECORDING[0] = None
            object.__setattr__(self, "_pass_closing", True)

    def _record_automation(self, param, value):
        lanes = vars(self).setdefault("automation", {})
        lane = lanes.get(id(param))
        points = list(lane["points"]) if lane else []
        points.append((round(self.current_song_time, 4), float(value)))
        lanes[id(param)] = {"parameter": param, "points": points}

    def __init__(self, running_clock=False, exclusive_arm=True):
        object.__setattr__(self, "running_clock", running_clock)
        object.__setattr__(self, "exclusive_arm", exclusive_arm)  # Live's preference
        _SONGS[:] = [ref for ref in _SONGS if ref() is not None] + [weakref.ref(self)]
        tracks = [FakeTrack("Drums", devices=[FakeDevice(
                      "Kit", "DrumGroupDevice",
                      drum_pads=[FakeObj(note=36, name="Kick 808", mute=False, solo=False,
                                         chains=[FakeObj(name="c")]),
                                 FakeObj(note=38, name="Snare Tight", mute=False, solo=False,
                                         chains=[FakeObj(name="c")]),
                                 FakeObj(note=42, name="Closed Hat", mute=False, solo=False,
                                         chains=[FakeObj(name="c")]),
                                 FakeObj(note=43, name="", mute=False, solo=False, chains=[])])]),
                  FakeTrack("Bass", devices=[FakeDevice("Operator", "Operator", [
                      FakeParam("Filter Freq", 0.5, display=freq_display),
                      FakeParam("Filter Type", 0, 0, 2, items=["Lowpass", "Highpass", "Bandpass"]),
                  ])]),
                  FakeTrack("Vox", midi=False)]
        super(FakeSong, self).__init__(
            tempo=120.0, signature_numerator=4, signature_denominator=4, is_playing=False,
            current_song_time=0.0, start_time=0.0, song_length=64.0, loop=False, loop_start=0.0,
            loop_length=16.0, tracks=tracks,
            return_tracks=[FakeTrack("A-Reverb", sends=0), FakeTrack("B-Delay", sends=0)],
            master_track=FakeTrack("Master", sends=0),
            scenes=[FakeObj(name="Intro", tempo=120.0), FakeObj(name="Drop", tempo=120.0)],
            cue_points=[], swing_amount=0.0, metronome=False, record_mode=False,
            midi_recording_quantization=RecordingQuantization(0),
            clip_trigger_quantization=Quantization(4), groove_amount=1.0,
            session_record=False, arrangement_overdub=False, session_automation_record=False,
            root_note=0, scale_name="Major", scale_mode=False,
            scale_intervals=[0, 2, 4, 5, 7, 9, 11], punch_in=False, punch_out=False,
            back_to_arranger=False, automation={})
        object.__setattr__(self, "view", FakeSongView(selected_track=tracks[0], detail_clip=None))
        object.__setattr__(self, "undo_log", [])
        object.__setattr__(self, "_undo", [])
        object.__setattr__(self, "_redo", [])
        object.__setattr__(self, "_open_step", None)

    # Like Live 12.4: what changed between begin and end becomes one step for
    # parameter changes and one for the rest; nothing changed, no step
    def begin_undo_step(self):
        self.undo_log.append("begin")
        object.__setattr__(self, "_open_step", _capture(self))

    def end_undo_step(self):
        self.undo_log.append("end")
        self.view.after_command()  # what Live changes after the command
        before = self._open_step
        object.__setattr__(self, "_open_step", None)
        if before is None:
            return
        changes = _changes(before, _capture(self))
        if vars(self).get("_pass") is not None:  # while recording: a step per command
            recorded = [c for c in changes if c[0] is self and c[1] == "automation"]
            step = [c for c in changes if not (c[0] is self and c[1] == "automation")]
            self._pass.extend(recorded)
            if vars(self).get("_pass_starting"):  # record on: its step is the pass's
                object.__setattr__(self, "_pass_starting", False)
                return
            if vars(self).get("_pass_closing"):
                step += self._pass
                object.__setattr__(self, "_pass", None)
                object.__setattr__(self, "_pass_closing", False)
            self._undo.append(step)
            del self._redo[:]
            return
        steps = [[c for c in changes if c[1] not in PARAMETER_PROPERTIES],
                 [c for c in changes if c[1] in PARAMETER_PROPERTIES]]
        steps = [s for s in steps if s]
        if steps:
            self._undo.extend(steps)
            del self._redo[:]

    @property
    def can_undo(self):
        return bool(self._undo)

    @property
    def can_redo(self):
        return bool(self._redo)

    def _step(self, source, target, use_old):
        if source:
            changes = source.pop()
            _apply(changes, use_old)
            target.append(changes)
        if self._open_step is not None:  # the undo itself is no new step
            object.__setattr__(self, "_open_step", _capture(self))

    def undo(self):
        self.undo_log.append("undo")
        self._step(self._undo, self._redo, True)

    def redo(self):
        self.undo_log.append("redo")
        self._step(self._redo, self._undo, False)

    def delete_scene(self, index):
        del self.scenes[index]

    # Song.set_data/get_data: saved with the set, no undo step (Live 12.4)
    def set_data(self, key, value):
        vars(self).setdefault("_data", {})[key] = value

    def get_data(self, key, default):
        return vars(self).get("_data", {}).get(key, default)

    # The Arrangement's horizontal zoom, in zoom_view steps from the default
    # (positive: in). A model, not a measurement: Live 12.4 snapped cues at
    # beats 9, 21 and 33 to 8, 20 and 32 in 4/4 and to 12, 24 and 36 in 3/4 at
    # its default zoom, placed beat 9 of 3/4 exactly after 4 steps in and an
    # eighth of a beat after ~30. Here the grid starts at the fewest bars
    # (1, 2, 4, …) that span 4 beats, halves every two steps in down to
    # 1/64 beat, and the zoom stops at ZOOM_STEPS either way.
    ZOOM_STEPS = (-12, 40)

    def arranger_zoom(self, steps):
        low, high = self.ZOOM_STEPS
        level = vars(self).get("_arranger_zoom", 0) + steps
        object.__setattr__(self, "_arranger_zoom", max(low, min(high, level)))

    def arranger_grid(self):
        """The beats a new cue snaps to at the Arrangement's zoom."""
        bar = vars(self).get("signature_numerator", 4) * 4.0 / vars(self).get("signature_denominator", 4)
        grid = bar
        while grid < 4.0:
            grid *= 2
        return max(grid / 2 ** (vars(self).get("_arranger_zoom", 0) // 2), 1.0 / 64)

    def set_or_delete_cue(self):
        """Toggles a cue at the playhead, snapped to the Arrangement's grid
        (to the nearest line, a tie upwards)."""
        grid = self.arranger_grid()
        at = math.floor(self.current_song_time / grid + 0.5) * grid
        here = [c for c in self.cue_points if c.time == at]
        if here:
            self.cue_points.remove(here[0])
        else:
            self.cue_points.append(FakeObj(name="", time=at))

    def create_midi_track(self, index):
        track = FakeTrack("%d-MIDI" % (len(self.tracks) + 1))
        if index == -1:
            self.tracks.append(track)
        else:
            self.tracks.insert(index, track)
        _select(track, new=True)
        return track

    def create_audio_track(self, index):
        track = FakeTrack("%d-Audio" % (len(self.tracks) + 1), midi=False)
        if index == -1:
            self.tracks.append(track)
        else:
            self.tracks.insert(index, track)
        _select(track, new=True)
        return track

    def delete_track(self, index):
        self.view.track_deleted(self.tracks[index])
        del self.tracks[index]

    def create_scene(self, index):
        scene = FakeObj(name="", tempo=120.0)
        if index == -1:
            self.scenes.append(scene)
        else:
            self.scenes.insert(index, scene)

    # Where playback starts, as Live 12.4.2 does it: start_playing at
    # start_time, continue_playing where it last stopped (setting
    # current_song_time while stopped does not move that).
    def _play_from(self, position):
        object.__setattr__(self, "_position", float(position))
        if self.running_clock:
            object.__setattr__(self, "_clock", (time.monotonic(), float(position)))
        self.is_playing = True

    def start_playing(self):
        self._play_from(getattr(self, "start_time", 0.0))

    def continue_playing(self):
        self._play_from(vars(self).get("_stopped_at", 0.0))

    def stop_playing(self):
        if self.is_playing:
            object.__setattr__(self, "_stopped_at", self.current_song_time)
        if self.running_clock and self.is_playing:
            object.__setattr__(self, "_position", self.current_song_time)
            object.__setattr__(self, "_clock", None)
        self.is_playing = False

    def stop_all_clips(self, quantized=True):
        for track in self.tracks:
            for slot in track.clip_slots:
                if slot.clip is not None:
                    slot.clip.is_playing = False


class FakeAppView(FakeObj):
    """Application.view: which views were shown or focused, in order, and the
    Arrangement's zoom (on the song, whose cues snap to its grid)."""
    def __init__(self, song=None):
        super(FakeAppView, self).__init__(shown=[])
        object.__setattr__(self, "_song", song)

    def show_view(self, name):
        self.shown.append(name)

    def focus_view(self, name):
        self.shown.append(name)

    def is_view_visible(self, name):
        return name in self.shown

    def zoom_view(self, direction, view_name, modifier_pressed):
        # 3 zooms the Arranger in horizontally, 2 out (Live 12.4.16); 0 and 1
        # change track heights only
        if view_name == "Arranger" and direction in (2, 3) and self._song is not None:
            self._song.arranger_zoom(1 if direction == 3 else -1)


class FakeApplication(FakeObj):
    """Live's Application: its view, the browser and the version (12.4.16,
    where the latest measurements were made, unless given)."""

    def __init__(self, browser, version=(12, 4, 16)):
        view = FakeAppView(getattr(browser, "song", None))
        super(FakeApplication, self).__init__(view=view, browser=browser)
        object.__setattr__(self, "_version", tuple(version))

    def get_major_version(self):
        return self._version[0]

    def get_minor_version(self):
        return self._version[1]

    def get_bugfix_version(self):
        return self._version[2]


class FakeBrowserItem(FakeObj):
    def __init__(self, name, children=(), source="Core Library"):
        folder = bool(children) or "." not in name
        super(FakeBrowserItem, self).__init__(
            name=name, children=list(children), is_folder=folder, is_loadable=not folder,
            is_device=False, source=source, uri="query:" + name.replace(" ", "%20"))


def _first_drum_rack(devices):
    """The first Drum Rack in devices or in their racks' chains (breadth first)."""
    level = list(devices)
    while level:
        rack = next((d for d in level if d.class_name == "DrumGroupDevice"), None)
        if rack is not None:
            return rack
        level = [d for r in level if getattr(r, "class_name", "") != "DrumGroupDevice"
                 for c in (getattr(r, "chains", None) or []) for d in getattr(c, "devices", [])]
    return None


class FakeBrowser(FakeObj):
    """Live's browser: roots of items; load_item drops onto the selected track,
    or onto the selected pad of its Drum Rack."""

    def __init__(self, song):
        item = FakeBrowserItem
        drum_hits = item("Drum Hits", [
            item("Kick", [item("Kick 808 Long.wav"), item("Kick Acoustic.wav")]),
            item("Snare", [item("Snare Tight.wav")]),
            item("Hihat", [item("Hihat Closed 1.wav"), item("Hihat Open 1.wav")]),
            item("Cymbal", [item("Crash Reverse Swell.wav"), item("Crash 505.wav"),
                            item("Ride 1.wav")]),
            # Live 12.4.16 lists the reversed one first
            item("FX Hit", [item("Impact Layered Reversed.wav"), item("Impact Layered.wav")])])
        roots = dict(
            sounds=item("Sounds", [item("Pad", [item("Warm Pad.adg"), item("Glass Pad.adg")])]),
            drums=item("Drums", [drum_hits, item("808 Core Kit.adg")]),
            samples=item("Samples", [item("Kick 808 Long.wav"), item("Vocal Chop.wav")]))
        for name in ("instruments", "audio_effects", "midi_effects", "max_for_live", "plugins",
                     "clips", "packs", "user_library", "current_project"):
            roots[name] = item(name.title(), [])
        super(FakeBrowser, self).__init__(**roots)
        object.__setattr__(self, "song", song)
        object.__setattr__(self, "loaded", [])
        object.__setattr__(self, "previewing", None)

    def load_item(self, item):
        track = self.song.view.selected_track
        rack = _first_drum_rack(track.devices)
        pad = rack.view.selected_drum_pad if rack is not None else None
        stem = item.name.rsplit(".", 1)[0]
        if pad is not None:
            # as in Live: the pad's chain holds a Simpler with the sample, its
            # Fade In at 0, 3 voices, and plays it at C3 (Live 12.4.16, 2026-11-14)
            chain = FakeDrumChain()  # pad.chains lists it in rack.chains (FakeDrumPad)
            object.__setattr__(chain, "name", stem)
            chain.devices.append(FakeSimpler(stem, FakeSample("/samples/" + item.name, 0.5),
                                             fade_in=0.0, voices=3))
            pad.chains = [chain]
            pad.name = stem
            self.loaded.append((track.name, pad.note, item.name))
        else:
            preset = item.name.lower().endswith(".adg")
            held = [d for d in track.devices if getattr(d, "_from_preset", False)]
            if preset and held:
                # Live loads a rack preset over a rack in place: same device
                # (same id), new name (Live 12.4.16, 2026-11-26)
                object.__setattr__(held[0], "name", stem)
                self.loaded.append((track.name, None, item.name))
                _named_after(track, stem)
                return
            seconds = 2.76 if "crash" in item.name.lower() else 0.5
            _auto_arm(track)  # as insert_device does
            device = FakeSimpler(stem, FakeSample("/samples/" + item.name, seconds))
            object.__setattr__(device, "_from_preset", preset)
            track.devices.append(device)
            self.loaded.append((track.name, None, item.name))
            _named_after(track, stem)

    def preview_item(self, item):
        object.__setattr__(self, "previewing", item.name)

    def stop_preview(self):
        object.__setattr__(self, "previewing", None)
