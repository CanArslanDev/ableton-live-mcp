// A stand-in for "@ableton-extensions/sdk": the classes describe.ts checks
// with instanceof, objects behind handles, and a context that records what
// the extension registers. Integers come back as bigint, as from Live.

export class DataModelObject {
  parent: DataModelObject | null = null;
}

export class Track extends DataModelObject {
  name: string;
  clipSlots: ClipSlot[] = [];
  arrangementClips: Clip[] = [];
  constructor(name: string) {
    super();
    this.name = name;
  }
}
export class AudioTrack extends Track {}
export class MidiTrack extends Track {}

export class TakeLane extends DataModelObject {
  name: string;
  constructor(name: string, track: Track) {
    super();
    this.name = name;
    this.parent = track;
  }
}

export class ClipSlot extends DataModelObject {
  clip: Clip | null = null;
}

export class Clip extends DataModelObject {
  name: string;
  startTime: bigint | number;
  endTime: bigint | number;
  looping = true;
  constructor(name: string, start: bigint | number, end: bigint | number) {
    super();
    this.name = name;
    this.startTime = start;
    this.endTime = end;
  }
}
export class AudioClip extends Clip {
  filePath = "/samples/take.wav";
}
export class MidiClip extends Clip {
  notes = [{ pitch: 60, startTime: 0, duration: 1 }];
}

export const classes = { DataModelObject, Track, AudioTrack, MidiTrack, TakeLane, ClipSlot, Clip,
                         AudioClip, MidiClip };

/** A song with tracks "Drums" (MIDI) and "Vox" (audio), a clip in each. */
export function song() {
  const drums = new MidiTrack("Drums");
  const vox = new AudioTrack("Vox");
  for (const track of [drums, vox]) {
    for (let i = 0; i < 4; i++) {
      const slot = new ClipSlot();
      slot.parent = track;
      track.clipSlots.push(slot);
    }
  }
  const beat = new MidiClip("Beat", 0n, 4n);
  beat.parent = drums.clipSlots[2];
  drums.clipSlots[2].clip = beat;
  const take = new AudioClip("Take 1", 16n, 48n);
  take.parent = vox;
  take.looping = false;
  vox.arrangementClips.push(take);
  const lane = new TakeLane("Take Lane 1", vox);
  return { drums, vox, beat, take, lane, tracks: [drums, vox] };
}

/** Handles 1, 2, … for the given objects, and a lookup like ExtensionContext's. */
export function handles(...objects: DataModelObject[]) {
  const byId = new Map<bigint, DataModelObject>();
  const list = objects.map((object, index) => {
    byId.set(BigInt(index + 1), object);
    return { id: BigInt(index + 1) };
  });
  const lookup = {
    getObjectFromHandle(handle: { id: bigint }, _type: unknown) {
      const object = byId.get(handle.id);
      if (!object) throw new Error("stale handle");
      return object;
    },
  };
  return { list, lookup };
}
