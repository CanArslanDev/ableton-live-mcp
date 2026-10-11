import assert from "node:assert/strict";
import { test } from "node:test";

import {
  describeArrangement, describeClipSlots, describeObject, findTrack, type Lookup, type SdkClasses,
} from "../src/describe.ts";
import { classes, handles, song } from "./fake-sdk.ts";

const sdk = classes as unknown as SdkClasses;
const as = (lookup: unknown) => lookup as Lookup;

test("an arrangement selection names its tracks once, in beats", () => {
  const s = song();
  const { list, lookup } = handles(s.drums, s.vox, s.lane);
  const shared = describeArrangement(as(lookup), sdk, {
    time_selection_start: 16, time_selection_end: 32, selected_lanes: list,
  });
  assert.deepEqual(shared, { kind: "arrangement", start_beat: 16, end_beat: 32,
                             tracks: ["Drums", "Vox"], take_lanes: ["Take Lane 1"] });
});

test("clip slots name their track, row and clip", () => {
  const s = song();
  const { list, lookup } = handles(s.drums.clipSlots[2], s.vox.clipSlots[0]);
  assert.deepEqual(describeClipSlots(as(lookup), sdk, { selected_clip_slots: list }), {
    kind: "clip_slots",
    slots: [{ track: "Drums", slot: 2, clip: "Beat" }, { track: "Vox", slot: 0, clip: null }],
  });
});

test("a clip, a slot with a clip, and a track", () => {
  const s = song();
  const { list, lookup } = handles(s.beat, s.take, s.drums.clipSlots[2], s.vox, s.vox.clipSlots[1]);
  assert.deepEqual(describeObject(as(lookup), sdk, list[0]), {
    kind: "clip", clip: { track: "Drums", name: "Beat", kind: "midi", looping: true, slot: 2, notes: 1 },
  });
  // bigint positions arrive as numbers
  assert.deepEqual(describeObject(as(lookup), sdk, list[1]), {
    kind: "clip", clip: { track: "Vox", name: "Take 1", kind: "audio", looping: false,
                          start_beat: 16, end_beat: 48, file: "/samples/take.wav" },
  });
  assert.equal((describeObject(as(lookup), sdk, list[2]) as { kind: string }).kind, "clip");
  assert.deepEqual(describeObject(as(lookup), sdk, list[3]), { kind: "track", track: "Vox" });
  assert.deepEqual(describeObject(as(lookup), sdk, list[4]), { kind: "track", track: "Vox" });
});

test("tracks by index or name", () => {
  const s = song();
  const tracks = s.tracks as unknown as Parameters<typeof findTrack>[0];
  assert.equal(findTrack(tracks, 1).name, "Vox");
  assert.equal(findTrack(tracks, "0").name, "Drums");
  assert.equal(findTrack(tracks, "vox").name, "Vox");
  assert.throws(() => findTrack(tracks, 5), /out of range/);
  assert.throws(() => findTrack(tracks, "Bass"), /No track named "Bass"/);
});
