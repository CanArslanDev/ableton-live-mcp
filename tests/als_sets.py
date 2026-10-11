"""Small Live Set files (.als) for tests, in the layout Live 12 writes.

Only the elements MCP_Server.als reads are here; real sets carry much more
(see the module docstring there for what was checked against Live's sets).
"""

import gzip
from pathlib import Path


def mixer(volume=1.0, pan=0.0, on=True, solo=False, sends=()):
    holders = "".join(f'<TrackSendHolder Id="{i}"><Send><Manual Value="{level}" /></Send>'
                      f'</TrackSendHolder>' for i, level in enumerate(sends))
    return (f'<Mixer><Volume><Manual Value="{volume}" /><AutomationTarget Id="1" /></Volume>'
            f'<Pan><Manual Value="{pan}" /></Pan><Speaker><Manual Value="{str(on).lower()}" /></Speaker>'
            f'<SoloSink Value="{str(solo).lower()}" /><Sends>{holders}</Sends></Mixer>')


def note_events(*events):
    """events: (pitch, time, duration, velocity[, extra attributes])"""
    by_pitch: dict[int, list[str]] = {}
    for pitch, time, duration, velocity, *extra in events:
        attributes = f'Time="{time}" Duration="{duration}" Velocity="{velocity}" OffVelocity="64"'
        if extra:
            attributes += " " + extra[0]
        by_pitch.setdefault(pitch, []).append(f"<MidiNoteEvent {attributes} />")
    tracks = "".join(f'<KeyTrack Id="{i}"><Notes>{"".join(evs)}</Notes><MidiKey Value="{pitch}" />'
                     f"</KeyTrack>" for i, (pitch, evs) in enumerate(sorted(by_pitch.items())))
    return f"<Notes><KeyTracks>{tracks}</KeyTracks><PerNoteEventStore><EventLists /></PerNoteEventStore></Notes>"


def midi_clip(name, start, end, loop_start, loop_end, loop_on, events, time=0):
    return (f'<MidiClip Id="0" Time="{time}"><CurrentStart Value="{start}" /><CurrentEnd Value="{end}" />'
            f'<Loop><LoopStart Value="{loop_start}" /><LoopEnd Value="{loop_end}" />'
            f'<StartRelative Value="0" /><LoopOn Value="{str(loop_on).lower()}" /></Loop>'
            f'<Name Value="{name}" /><Color Value="3" /><Disabled Value="false" />'
            f"<Envelopes><Envelopes /></Envelopes>{note_events(*events)}</MidiClip>")


def sample_ref(path="", relative="", relative_type=0, pack=""):
    return (f'<SampleRef><FileRef><RelativePathType Value="{relative_type}" />'
            f'<RelativePath Value="{relative}" /><Path Value="{path}" /><Type Value="2" />'
            f'<LivePackName Value="{pack}" /></FileRef></SampleRef>')


def audio_clip(name, start, end, ref, time=0):
    return (f'<AudioClip Id="0" Time="{time}"><CurrentStart Value="{start}" /><CurrentEnd Value="{end}" />'
            f'<Loop><LoopStart Value="0" /><LoopEnd Value="{end - start}" /><LoopOn Value="false" /></Loop>'
            f'<Name Value="{name}" /><Disabled Value="false" />{ref}<IsWarped Value="true" /></AudioClip>')


def slots(*clips):
    return "".join(f'<ClipSlot Id="{i}"><ClipSlot><Value>{clip or ""}</Value></ClipSlot></ClipSlot>'
                   for i, clip in enumerate(clips))


def track(tag, track_id, name, devices="", session="", arrangement="", group=-1, color=1,
          frozen=False, **mixer_args):
    sequencer = "ClipTimeable" if tag == "MidiTrack" else "Sample"
    return (f'<{tag} Id="{track_id}"><Name><EffectiveName Value="{name}" /><UserName Value="{name}" />'
            f'</Name><Color Value="{color}" /><TrackGroupId Value="{group}" />'
            f'<Freeze Value="{str(frozen).lower()}" /><DeviceChain>'
            f'<AudioOutputRouting><Target Value="AudioOut/Main" /><UpperDisplayString Value="Main" />'
            f'</AudioOutputRouting>{mixer(**mixer_args)}'
            f'<MainSequencer><ClipSlotList>{session}</ClipSlotList><{sequencer}><ArrangerAutomation>'
            f'<Events>{arrangement}</Events></ArrangerAutomation></{sequencer}></MainSequencer>'
            f'<FreezeSequencer><ClipSlotList /></FreezeSequencer>'
            f'<DeviceChain><Devices>{devices}</Devices></DeviceChain></DeviceChain></{tag}>')


KEYS_DEVICES = (
    '<Eq8 Id="1"><UserName Value="" /><On><Manual Value="false" /></On></Eq8>'
    '<InstrumentGroupDevice Id="2"><UserName Value="Pad Stack" /><On><Manual Value="true" /></On>'
    '<Branches><InstrumentBranch Id="0"><Name><EffectiveName Value="Layer" /></Name><DeviceChain>'
    '<MidiToAudioDeviceChain><Devices><InstrumentVector Id="3"><UserName Value="" /></InstrumentVector>'
    '</Devices></MidiToAudioDeviceChain></DeviceChain></InstrumentBranch></Branches>'
    '</InstrumentGroupDevice>'
    '<PluginDevice Id="4"><UserName Value="" /><PluginDesc><Vst3PluginInfo Id="0">'
    '<Name Value="Serum" /><DeviceType Value="1" /></Vst3PluginInfo></PluginDesc></PluginDevice>'
    '<MxDeviceAudioEffect Id="5"><UserName Value="" /><PatchSlot><Value><MxPatchRef Id="6"><FileRef>'
    '<RelativePathType Value="7" /><RelativePath Value="Devices/Audio Effects/LFO.amxd" />'
    '<Path Value="" /></FileRef></MxPatchRef></Value></PatchSlot>'
    '<ParameterList><MxDFloatParameter Id="1" /></ParameterList></MxDeviceAudioEffect>'
)

DRUM_RACK = (
    '<DrumGroupDevice Id="7"><UserName Value="" /><Branches>'
    '<DrumBranch Id="0"><Name><EffectiveName Value="Kick" /></Name><DeviceChain><MidiToAudioDeviceChain>'
    '<Devices><OriginalSimpler Id="8"><UserName Value="" /></OriginalSimpler></Devices>'
    '</MidiToAudioDeviceChain></DeviceChain><BranchInfo><ReceivingNote Value="92" />'
    '<SendingNote Value="60" /></BranchInfo></DrumBranch></Branches></DrumGroupDevice>'
)


def song(tempo=124, signature=303, root=2, scale=2, main_tag="MainTrack", tracks=None,
         creator="Ableton Live 12.3.2", locators=((7, "Drop"),), loop=(7, 14)):
    if tracks is None:
        tracks = default_tracks()
    locator_xml = "".join(f'<Locator Id="{i}"><Time Value="{t}" /><Name Value="{n}" /></Locator>'
                          for i, (t, n) in enumerate(locators))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<Ableton MajorVersion="5" MinorVersion="12.0_12300" Creator="{creator}"><LiveSet>'
        f"<Tracks>{tracks}</Tracks>"
        f'<{main_tag}><DeviceChain><Mixer><Tempo><Manual Value="{tempo}" /><AutomationTarget Id="9" />'
        f'</Tempo><TimeSignature><Manual Value="{signature}" /></TimeSignature>'
        f'<Volume><Manual Value="1" /></Volume></Mixer><DeviceChain><Devices>'
        f'<Limiter Id="10"><UserName Value="" /></Limiter></Devices></DeviceChain></DeviceChain></{main_tag}>'
        '<Scenes><Scene Id="0"><Name Value="Verse" /><Tempo Value="128" />'
        '<IsTempoEnabled Value="true" /><TimeSignatureId Value="201" />'
        '<IsTimeSignatureEnabled Value="false" /></Scene>'
        '<Scene Id="1"><Name Value="" /><Tempo Value="120" /><IsTempoEnabled Value="false" />'
        '</Scene></Scenes>'
        f'<Transport><LoopOn Value="{"true" if loop else "false"}" />'
        f'<LoopStart Value="{loop[0] if loop else 0}" /><LoopLength Value="{loop[1] if loop else 16}" />'
        '</Transport>'
        f'<ScaleInformation><Root Value="{root}" /><Name Value="{scale}" /></ScaleInformation>'
        '<InKey Value="true" /><PreferFlatRootNote Value="false" />'
        f"<Locators><Locators>{locator_xml}</Locators></Locators>"
        "</LiveSet></Ableton>")


def default_tracks(sample_path="", missing_path="/nonexistent/missing.wav"):
    chords = midi_clip("Chords", 0, 8, 0, 8, True,
                       [(60, 0, 1, 100), (60, 4, 2, 90, 'Probability="0.5"'), (64, 9, 1, 80)])
    arranged = midi_clip("Verse", 16, 24, 4, 12, False,
                         [(62, 2, 1, 70), (62, 4, 1, 100), (65, 11.5, 2, 110)], time=16)
    take = audio_clip("take", 0, 16, sample_ref(path="/elsewhere/take.wav",
                                                relative="Samples/Recorded/take.wav",
                                                relative_type=1), time=0)
    lost = audio_clip("lost", 0, 4, sample_ref(path=missing_path))
    return "".join([
        track("GroupTrack", 10, "Band"),
        track("MidiTrack", 11, "Keys", KEYS_DEVICES, slots(None, chords), arranged, group=10,
              volume=0.5011872336, pan=-0.4, sends=(0.2511886432,)),
        track("MidiTrack", 14, "Drums", DRUM_RACK),
        track("AudioTrack", 12, "Vox", "", slots(lost), take, on=False, frozen=True),
        track("ReturnTrack", 13, "A-Reverb", '<Reverb Id="11"><UserName Value="" /></Reverb>'),
    ])


def write_set(path: Path, xml: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(xml.encode("utf-8")))
    return path


def project(tmp_path: Path, name="Song", **song_args) -> Path:
    """A project folder with the set and the one sample it finds."""
    folder = tmp_path / f"{name} Project"
    sample = folder / "Samples" / "Recorded" / "take.wav"
    sample.parent.mkdir(parents=True, exist_ok=True)
    sample.write_bytes(b"RIFF")
    return write_set(folder / f"{name}.als", song(**song_args))
