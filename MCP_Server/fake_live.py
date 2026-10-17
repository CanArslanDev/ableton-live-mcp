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
import sys
import time
import types

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
            name=name, length=length, loop_start=0.0, loop_end=length, start_marker=0.0,
            end_marker=length, looping=True, is_midi_clip=midi, is_audio_clip=not midi,
            is_playing=False, is_recording=False, warp_mode=WarpMode(0), muted=False,
            color_index=0, file_path=None)
        object.__setattr__(self, "_notes", [])
        object.__setattr__(self, "_next_id", 1)
        object.__setattr__(self, "envelopes", {})
        object.__setattr__(self, "calls", [])

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
        self.length = self.length * 2
        self.loop_end = self.length


def _copy_clip(clip):
    copy = FakeClip(name=clip.name, length=clip.length, midi=clip.is_midi_clip)
    for key in ("loop_start", "loop_end", "start_marker", "end_marker", "looping",
                "color_index", "file_path"):
        object.__setattr__(copy, key, getattr(clip, key))
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
        self.clip = FakeClip(length=length)
        self.has_clip = True

    def delete_clip(self):
        self.clip = None
        self.has_clip = False

    def duplicate_clip_to(self, target):
        target.clip = FakeClip(name=self.clip.name, length=self.clip.length)
        target.has_clip = True

    def create_audio_clip(self, path):
        clip = FakeClip(name=path.rsplit("/", 1)[-1].rsplit(".", 1)[0], length=8.0, midi=False)
        object.__setattr__(clip, "file_path", path)
        object.__setattr__(clip, "warping", True)
        self.clip = clip
        self.has_clip = True
        return clip


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


def _drum_rack():
    pads = [FakeObj(note=n, name="", mute=False, solo=False, chains=[]) for n in range(128)]
    rack = FakeDevice("Drum Rack", "DrumGroupDevice", drum_pads=pads, chains=[],
                      view=FakeObj(selected_drum_pad=None))
    object.__setattr__(rack, "can_have_chains", True)
    return rack


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


def _switch(name, on=False):
    return _choice(name, ["Off", "On"], 1 if on else 0)


EQ_FILTER_TYPES = ["High Pass 48dB", "High Pass 12dB", "Low Shelf", "Bell", "Notch",
                   "High Shelf", "Low Pass 12dB", "Low Pass 48dB"]


def _eq_eight():
    params = []
    for band, kind, hz in ((1, 0, 0.05), (2, 2, 0.3), (3, 3, 0.45), (4, 3, 0.6),
                           (5, 3, 0.7), (6, 3, 0.8), (7, 5, 0.85), (8, 7, 0.95)):
        params += [_switch("%d Filter On A" % band, band not in (1, 8)),
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


class FakeSimpler(FakeDevice):
    def __init__(self, name, sample=None):
        super(FakeSimpler, self).__init__(name, "OriginalSimpler", [
            _percent("S Start", 0.0), FakeParam("Transpose", 0.0, -48.0, 48.0),
            _db("Volume", -12.0, -36.0, 36.0),
            _choice("Trigger Mode", ["Trigger", "Gate"], 1)],
            sample=sample, playback_mode=0, reversed=False)

    def reverse(self):
        self.reversed = not self.reversed


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
    "Glue Compressor": lambda: FakeDevice("Glue Compressor", "GlueCompressor", [
        _db("Threshold", 0.0, -40.0, 0.0),
        _choice("Ratio", ["2", "4", "10"]),
        _choice("Attack", [".01", ".1", ".3", "1", "3", "10", "30"], 3),
        _choice("Release", [".1", ".2", ".4", ".6", ".8", "1.2", "A"], 6),
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
    "Limiter": lambda: FakeDevice("Limiter", "Limiter", [
        _db("Gain", 0.0, -24.0, 24.0), _db("Ceiling", 0.0, -24.0, 0.0),
        _choice("Mode", ["Standard", "Soft Clip", "True Peak"])]),
    "Operator": _instrument("Operator", "Operator"),
    "Drift": _instrument("Drift", "Drift"),
    "Wavetable": _instrument("Wavetable", "InstrumentVector"),
    "Analog": _instrument("Analog", "UltraAnalog"),
    "Simpler": lambda: FakeSimpler("Simpler"),
}


class FakeTrack(FakeObj):
    def __init__(self, name, midi=True, slots=4, devices=None, sends=2):
        mixer = FakeObj(volume=FakeParam("Track Volume", 0.85, display=volume_display),
                        panning=FakeParam("Track Panning", 0.0, -1.0, 1.0, display=pan_display),
                        sends=[FakeParam("Send %s" % chr(65 + i), 0.0, display=volume_display)
                               for i in range(sends)])
        routing = [FakeObj(display_name=n) for n in ("Ext. In", "Resampling", "Drums")]
        super(FakeTrack, self).__init__(
            name=name, has_midi_input=midi, has_audio_input=not midi, mute=False, solo=False,
            arm=False, can_be_armed=True, is_foldable=False, is_grouped=False,
            color_index=1, color=0, mixer_device=mixer, devices=list(devices or []),
            clip_slots=[FakeClipSlot() for _ in range(slots)], arrangement_clips=[],
            current_monitoring_state=1, available_input_routing_types=routing,
            input_routing_type=routing[0], has_audio_output=True,
            output_meter_left=0.0, output_meter_right=0.0)

    def delete_device(self, index):
        del self.devices[index]

    def duplicate_clip_to_arrangement(self, clip, time):
        """Like Live 12.4: the copy keeps notes and envelopes; clips under it
        are split, the parts keeping all their notes and moving their markers."""
        start, end = float(time), float(time) + clip.length
        copy = _copy_clip(clip)
        object.__setattr__(copy, "start_time", start)
        object.__setattr__(copy, "end_time", end)
        kept = []
        for other in self.arrangement_clips:
            if other.end_time <= start or other.start_time >= end:
                kept.append(other)
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
        return copy

    def create_midi_clip(self, start, length):
        clip = FakeClip(length=length)
        object.__setattr__(clip, "start_time", float(start))
        object.__setattr__(clip, "end_time", float(start) + length)
        self.arrangement_clips.append(clip)
        return clip

    def create_audio_clip(self, path, position):
        clip = FakeClip(name=path.rsplit("/", 1)[-1].rsplit(".", 1)[0], length=8.0, midi=False)
        for key, value in (("file_path", path), ("warping", True), ("start_time", float(position)),
                           ("end_time", float(position) + 8.0)):
            object.__setattr__(clip, key, value)
        self.arrangement_clips.append(clip)
        return clip

    def insert_device(self, name, index=-1):
        if name not in INSERTABLE:
            raise RuntimeError("Unknown device name: %s" % name)
        device = INSERTABLE[name]()
        if index == -1:
            self.devices.append(device)
        else:
            self.devices.insert(index, device)
        return device


# ── Undo history, by the rules measured on Live 12.4 (see journal.py) ───────

# Bookkeeping of the fakes themselves, never part of an undo step
_NOT_SET_STATE = frozenset(["_listeners", "undo_log", "calls", "_undo", "_redo", "_open_step",
                            "_position", "_clock", "_stopped_at", "_pass", "_pass_closing", "_pass_starting",
                            "running_clock", "_data"])
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
        if value is _MISSING:
            vars(obj).pop(attr, None)
        elif isinstance(value, list) and isinstance(current, list):
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
        object.__setattr__(self, "_position", float(value))
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

    def __init__(self, running_clock=False):
        object.__setattr__(self, "running_clock", running_clock)
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
        object.__setattr__(self, "view", FakeObj(selected_track=tracks[0], detail_clip=None))
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

    def set_or_delete_cue(self):
        here = [c for c in self.cue_points if c.time == self.current_song_time]
        if here:
            self.cue_points.remove(here[0])
        else:
            self.cue_points.append(FakeObj(name="", time=self.current_song_time))

    def create_midi_track(self, index):
        track = FakeTrack("%d-MIDI" % (len(self.tracks) + 1))
        if index == -1:
            self.tracks.append(track)
        else:
            self.tracks.insert(index, track)
        return track

    def create_audio_track(self, index):
        track = FakeTrack("%d-Audio" % (len(self.tracks) + 1), midi=False)
        if index == -1:
            self.tracks.append(track)
        else:
            self.tracks.insert(index, track)
        return track

    def delete_track(self, index):
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


class FakeBrowserItem(FakeObj):
    def __init__(self, name, children=(), source="Core Library"):
        folder = bool(children) or "." not in name
        super(FakeBrowserItem, self).__init__(
            name=name, children=list(children), is_folder=folder, is_loadable=not folder,
            is_device=False, source=source, uri="query:" + name.replace(" ", "%20"))


class FakeBrowser(FakeObj):
    """Live's browser: roots of items; load_item drops onto the selected track,
    or onto the selected pad of its Drum Rack."""

    def __init__(self, song):
        item = FakeBrowserItem
        drum_hits = item("Drum Hits", [
            item("Kick", [item("Kick 808 Long.wav"), item("Kick Acoustic.wav")]),
            item("Snare", [item("Snare Tight.wav")]),
            item("Hihat", [item("Hihat Closed 1.wav"), item("Hihat Open 1.wav")]),
            item("Cymbal", [item("Crash 505.wav"), item("Ride 1.wav")])])
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
        rack = next((d for d in track.devices if d.class_name == "DrumGroupDevice"), None)
        pad = rack.view.selected_drum_pad if rack is not None else None
        stem = item.name.rsplit(".", 1)[0]
        if pad is not None:
            # as in Live: the pad's chain holds a Simpler with the sample
            pad.chains = [FakeObj(name=stem, devices=[FakeSimpler(stem, FakeSample("/samples/" + item.name, 0.5))])]
            pad.name = stem
            self.loaded.append((track.name, pad.note, item.name))
        else:
            seconds = 2.76 if "crash" in item.name.lower() else 0.5
            track.devices.append(FakeSimpler(stem, FakeSample("/samples/" + item.name, seconds)))
            self.loaded.append((track.name, None, item.name))

    def preview_item(self, item):
        object.__setattr__(self, "previewing", item.name)

    def stop_preview(self):
        object.__setattr__(self, "previewing", None)
