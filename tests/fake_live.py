"""A small in-memory imitation of Live's Object Model for tests.

Covers what the bridge and the extended tools touch: song, tracks, mixer,
devices, parameters, clip slots, clips with the Live 11 note API, scenes,
views and property listeners. Objects carry ``_live_ptr`` like real LOM
objects, so the bridge treats them as Live objects.
"""

import math
import sys
import types


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
    live.Song = types.SimpleNamespace(RecordingQuantization=RecordingQuantization)
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
    def __init__(self):
        super(FakeEnvelope, self).__init__(steps=[])

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
        env = FakeEnvelope()
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
    ], available_input_routing_types=routing, input_routing_type=routing[-1])


def _drum_rack():
    pads = [FakeObj(note=n, name="", mute=False, solo=False, chains=[]) for n in range(128)]
    rack = FakeDevice("Drum Rack", "DrumGroupDevice", drum_pads=pads, chains=[],
                      view=FakeObj(selected_drum_pad=None))
    object.__setattr__(rack, "can_have_chains", True)
    return rack


# What Track.insert_device builds (Live 12.3+), by browser name
INSERTABLE = {
    "Drum Rack": _drum_rack,
    "Compressor": _compressor,
    "Utility": lambda: FakeDevice("Utility", "StereoGain", [
        FakeParam("Bass Mono", 0, 0, 1, items=["Off", "On"]),
        FakeParam("Gain", 0.0, -1.0, 1.0)]),
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

    def insert_device(self, name, index=-1):
        if name not in INSERTABLE:
            raise RuntimeError("Unknown device name: %s" % name)
        device = INSERTABLE[name]()
        if index == -1:
            self.devices.append(device)
        else:
            self.devices.insert(index, device)
        return device


class FakeSong(FakeObj):
    def __init__(self):
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
            current_song_time=0.0, song_length=64.0, loop=False, loop_start=0.0,
            loop_length=16.0, tracks=tracks,
            return_tracks=[FakeTrack("A-Reverb", sends=0), FakeTrack("B-Delay", sends=0)],
            master_track=FakeTrack("Master", sends=0),
            scenes=[FakeObj(name="Intro", tempo=120.0), FakeObj(name="Drop", tempo=120.0)],
            cue_points=[], swing_amount=0.0, metronome=False, record_mode=False,
            midi_recording_quantization=RecordingQuantization(0),
            root_note=0, scale_name="Major", scale_intervals=[0, 2, 4, 5, 7, 9, 11])
        object.__setattr__(self, "view", FakeObj(selected_track=tracks[0], detail_clip=None))
        object.__setattr__(self, "undo_log", [])

    def begin_undo_step(self):
        self.undo_log.append("begin")

    def end_undo_step(self):
        self.undo_log.append("end")

    def create_midi_track(self, index):
        track = FakeTrack("%d-MIDI" % (len(self.tracks) + 1))
        if index == -1:
            self.tracks.append(track)
        else:
            self.tracks.insert(index, track)

    def delete_track(self, index):
        del self.tracks[index]

    def create_scene(self, index):
        scene = FakeObj(name="", tempo=120.0)
        if index == -1:
            self.scenes.append(scene)
        else:
            self.scenes.insert(index, scene)

    def stop_playing(self):
        self.is_playing = False


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
            item("Hihat", [item("Hihat Closed 1.wav"), item("Hihat Open 1.wav")])])
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
            pad.chains = [FakeObj(name=stem)]
            pad.name = stem
            self.loaded.append((track.name, pad.note, item.name))
        else:
            track.devices.append(FakeDevice(stem, "OriginalSimpler"))
            self.loaded.append((track.name, None, item.name))

    def preview_item(self, item):
        object.__setattr__(self, "previewing", item.name)

    def stop_preview(self):
        object.__setattr__(self, "previewing", None)
