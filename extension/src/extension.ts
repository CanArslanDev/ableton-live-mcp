// AbletonMCP Companion: an Extensions SDK add-on that gives the ableton-live-mcp
// server what its Remote Script cannot reach.
//
// - "Share … with AbletonMCP" in the context menus of the Arrangement time
//   selection, clips, clip slots and tracks: the MCP server then knows which
//   part of the song the user means (the Live Object Model has no time
//   selection).
// - confirm: a question in a dialog inside Live, where the musician looks.
// - render_audio: an audio track's clips rendered to a file before its
//   devices, faster than playing it in real time.
//
// The host runs this bundle in a stripped `vm` context (no `global`, few web
// globals, `require` for node: builtins) and returns integers as bigint; see
// build.mjs and describe.ts.
import {
  AudioClip, AudioTrack, Clip, ClipSlot, DataModelObject, MidiClip, MidiTrack, TakeLane, Track,
  initialize,
  type ActivationContext, type ArrangementSelection, type ClipSlotSelection, type ExtensionContext,
  type Handle,
} from "@ableton-extensions/sdk";

import { messageOf, startBridge } from "./bridge.ts";
import {
  describeArrangement, describeClipSlots, describeObject, findTrack, num, type SdkClasses, type Shared,
} from "./describe.ts";
import { confirmPage, confirmed } from "./dialogs.ts";
import { defaultFolders, newToken, writeSession } from "./session.ts";

export const VERSION = "0.1.0";
export const PORT = Number(process.env.ABLETON_MCP_COMPANION_PORT) || 9878;

const log = (message: string): void => console.log(`[AbletonMCP Companion] ${message}`);

export function activate(activation: ActivationContext): void {
  const context: ExtensionContext<"1.0.0"> = initialize(activation, "1.0.0");
  const sdk: SdkClasses = { DataModelObject, Track, AudioTrack, MidiTrack, TakeLane, ClipSlot, Clip,
                            AudioClip, MidiClip };
  let shared: (Shared & { shared_at: string }) | null = null;

  const share = (describe: () => Shared): void => {
    try {
      shared = { ...describe(), shared_at: new Date().toISOString() };
    } catch (error) {
      log(`could not share: ${messageOf(error)}`);
    }
  };
  context.commands.registerCommand("abletonmcp.shareArrangement", (selection) =>
    share(() => describeArrangement(context, sdk, selection as ArrangementSelection)));
  context.commands.registerCommand("abletonmcp.shareClipSlots", (selection) =>
    share(() => describeClipSlots(context, sdk, selection as ClipSlotSelection)));
  context.commands.registerCommand("abletonmcp.shareObject", (handle) =>
    share(() => describeObject(context, sdk, handle as Handle)));

  const menus: [Parameters<typeof context.ui.registerContextMenuAction>[0], string, string][] = [
    ["AudioTrack.ArrangementSelection", "Share selection with AbletonMCP", "abletonmcp.shareArrangement"],
    ["MidiTrack.ArrangementSelection", "Share selection with AbletonMCP", "abletonmcp.shareArrangement"],
    ["ClipSlotSelection", "Share clips with AbletonMCP", "abletonmcp.shareClipSlots"],
    ["AudioClip", "Share clip with AbletonMCP", "abletonmcp.shareObject"],
    ["MidiClip", "Share clip with AbletonMCP", "abletonmcp.shareObject"],
    ["AudioTrack", "Share track with AbletonMCP", "abletonmcp.shareObject"],
    ["MidiTrack", "Share track with AbletonMCP", "abletonmcp.shareObject"],
  ];
  for (const [scope, title, command] of menus) {
    context.ui.registerContextMenuAction(scope, title, command)
      .catch((error: unknown) => log(`menu ${scope}: ${messageOf(error)}`));
  }

  const token = newToken();
  const where = writeSession({ port: PORT, token, companion: VERSION, pid: process.pid },
                             defaultFolders(context.environment.storageDirectory), log);
  log(where ? `session file: ${where}` : "no folder could hold the session file");

  startBridge({
    port: PORT, token, version: VERSION, log,
    handlers: {
      song: () => {
        const song = context.application.song;
        return {
          tempo: num(song.tempo), root_note: num(song.rootNote), scale_name: song.scaleName,
          tracks: song.tracks.map((track, index) => ({
            index, name: track.name,
            type: track instanceof AudioTrack ? "audio" : track instanceof MidiTrack ? "midi" : "other",
          })),
        };
      },
      get_selection: () => shared,
      confirm: async (params) => ({
        confirmed: confirmed(await context.ui.showModalDialog(confirmPage(params), 460, 210)
          .catch(() => null)),
      }),
      render_audio: async (params) => {
        const track = findTrack(context.application.song.tracks, params.track);
        if (!(track instanceof AudioTrack)) {
          throw new Error(`${track.name} is not an audio track; only audio tracks render before their devices`);
        }
        const start = Number(params.start_beat ?? 0);
        const end = Number(params.end_beat);
        if (!(end > start)) throw new Error("end_beat must be after start_beat");
        const file = await context.ui.withinProgressDialog(
          `AbletonMCP: rendering ${track.name}`, {},
          async () => context.resources.renderPreFxAudio(track, start, end));
        return { file, track: track.name, start_beat: start, end_beat: end };
      },
    },
  });
}
