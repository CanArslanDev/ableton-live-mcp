// What the user shares from Live's context menus, as plain JSON for the MCP
// server: track names, times in beats, clip positions. The SDK classes are
// passed in, so this file has no runtime import of the SDK (tests use fakes).
import type {
  ArrangementSelection, AudioClip, AudioTrack, Clip, ClipSlot, ClipSlotSelection,
  DataModelObject, ExtensionContext, Handle, MidiClip, MidiTrack, TakeLane, Track,
} from "@ableton-extensions/sdk";

type Class<T> = abstract new (...args: never) => T;
type V = "1.0.0";

export interface SdkClasses {
  DataModelObject: Class<DataModelObject<V>>;
  Track: Class<Track<V>>;
  AudioTrack: Class<AudioTrack<V>>;
  MidiTrack: Class<MidiTrack<V>>;
  TakeLane: Class<TakeLane<V>>;
  ClipSlot: Class<ClipSlot<V>>;
  Clip: Class<Clip<V>>;
  AudioClip: Class<AudioClip<V>>;
  MidiClip: Class<MidiClip<V>>;
}

export type Lookup = Pick<ExtensionContext<V>, "getObjectFromHandle">;

export interface SharedClip {
  track: string | null;
  name: string;
  kind: "audio" | "midi";
  slot?: number;
  start_beat?: number;
  end_beat?: number;
  looping: boolean;
  file?: string;
  notes?: number;
}

export type Shared =
  | { kind: "arrangement"; start_beat: number; end_beat: number; tracks: string[]; take_lanes?: string[] }
  | { kind: "clip_slots"; slots: { track: string | null; slot: number; clip: string | null }[] }
  | { kind: "clip"; clip: SharedClip }
  | { kind: "track"; track: string };

// The host returns integer getters as bigint, whatever the types say
export const num = (value: unknown): number => Number(value);

function resolve(lookup: Lookup, sdk: SdkClasses, handle: Handle): DataModelObject<V> {
  return lookup.getObjectFromHandle(handle, sdk.DataModelObject);
}

export function trackOf(object: DataModelObject<V> | null, sdk: SdkClasses): Track<V> | null {
  let current: DataModelObject<V> | null = object;
  while (current !== null) {
    if (current instanceof sdk.Track) return current;
    current = current.parent;
  }
  return null;
}

function unique(values: string[]): string[] {
  return [...new Set(values)];
}

export function describeArrangement(lookup: Lookup, sdk: SdkClasses,
                                    selection: ArrangementSelection): Shared {
  const lanes = selection.selected_lanes.map((handle) => resolve(lookup, sdk, handle));
  const takeLanes = lanes.filter((lane): lane is TakeLane<V> => lane instanceof sdk.TakeLane);
  const tracks = lanes.map((lane) => trackOf(lane, sdk)).filter((t): t is Track<V> => t !== null);
  const shared: Shared = {
    kind: "arrangement",
    start_beat: num(selection.time_selection_start),
    end_beat: num(selection.time_selection_end),
    tracks: unique(tracks.map((t) => t.name)),
  };
  if (takeLanes.length) shared.take_lanes = takeLanes.map((lane) => lane.name);
  return shared;
}

export function describeClip(clip: Clip<V>, sdk: SdkClasses): SharedClip {
  const track = trackOf(clip, sdk);
  const described: SharedClip = {
    track: track ? track.name : null,
    name: clip.name,
    kind: clip instanceof sdk.MidiClip ? "midi" : "audio",
    looping: Boolean(clip.looping),
  };
  const parent = clip.parent;
  if (parent instanceof sdk.ClipSlot && track) {
    described.slot = track.clipSlots.indexOf(parent);
  } else {
    described.start_beat = num(clip.startTime);
    described.end_beat = num(clip.endTime);
  }
  if (clip instanceof sdk.AudioClip) described.file = clip.filePath;
  if (clip instanceof sdk.MidiClip) described.notes = clip.notes.length;
  return described;
}

export function describeClipSlots(lookup: Lookup, sdk: SdkClasses,
                                  selection: ClipSlotSelection): Shared {
  return {
    kind: "clip_slots",
    slots: selection.selected_clip_slots.map((handle) => {
      const slot = resolve(lookup, sdk, handle);
      const track = trackOf(slot, sdk);
      const clip = slot instanceof sdk.ClipSlot ? slot.clip : null;
      return { track: track ? track.name : null,
               slot: track && slot instanceof sdk.ClipSlot ? track.clipSlots.indexOf(slot) : -1,
               clip: clip ? clip.name : null };
    }),
  };
}

export function describeObject(lookup: Lookup, sdk: SdkClasses, handle: Handle): Shared {
  const object = resolve(lookup, sdk, handle);
  if (object instanceof sdk.Clip) return { kind: "clip", clip: describeClip(object, sdk) };
  if (object instanceof sdk.ClipSlot && object.clip) {
    return { kind: "clip", clip: describeClip(object.clip, sdk) };
  }
  const track = trackOf(object, sdk);
  if (track) return { kind: "track", track: track.name };
  throw new Error("Nothing to share here");
}

export function findTrack(tracks: Track<V>[], wanted: unknown): Track<V> {
  if (typeof wanted === "number" || (typeof wanted === "string" && /^\d+$/.test(wanted))) {
    const index = Number(wanted);
    const track = tracks[index];
    if (!track) throw new Error(`Track index ${index} out of range (${tracks.length} tracks)`);
    return track;
  }
  const name = String(wanted ?? "").toLowerCase();
  const track = tracks.find((t) => t.name.toLowerCase() === name);
  if (!track) throw new Error(`No track named ${JSON.stringify(wanted)}`);
  return track;
}
