// The built bundle in a context like Live's Extension Host: a `vm` context
// with `require` for node: builtins, no `global`, no URL/TextEncoder, and an
// SDK whose integers are bigint. activate() must register its menus and
// commands, write the session file and answer over its socket.
import assert from "node:assert/strict";
import { statSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import * as net from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, test } from "node:test";
import vm from "node:vm";

import { build } from "esbuild";

// @ts-expect-error: plain JavaScript module
import { buildOptions } from "../build-options.mjs";
import { classes, handles, song } from "./fake-sdk.ts";

const nodeRequire = createRequire(import.meta.url);
const servers: net.Server[] = [];
after(() => servers.forEach((server) => server.close()));

async function freePort(): Promise<number> {
  const probe = net.createServer();
  await new Promise<void>((resolve) => probe.listen(0, "127.0.0.1", resolve));
  const port = (probe.address() as net.AddressInfo).port;
  await new Promise((resolve) => probe.close(resolve));
  return port;
}

function fakeHost(storage: string) {
  const s = song();
  const { list, lookup } = handles(s.drums, s.vox, s.beat);
  const registered = { commands: new Map<string, (...args: unknown[]) => void>(),
                       menus: [] as string[][], dialogs: [] as string[] };
  const sdk = {
    ...classes,
    initialize(_activation: unknown, version: string) {
      assert.equal(version, "1.0.0");
      return {
        application: { song: { tracks: s.tracks, tempo: 124n, rootNote: 9n, scaleName: "Minor" } },
        commands: {
          registerCommand: (id: string, cb: (...args: unknown[]) => void) => registered.commands.set(id, cb),
          executeCommand: (id: string, ...args: unknown[]) => registered.commands.get(id)?.(...args),
        },
        ui: {
          registerContextMenuAction: async (scope: string, title: string, command: string) => {
            registered.menus.push([scope, title, command]);
            return async () => {};
          },
          showModalDialog: async (url: string) => {
            registered.dialogs.push(url);
            return JSON.stringify({ confirmed: true });
          },
          withinProgressDialog: async (_text: string, _options: unknown,
                                       body: (update: unknown, signal: AbortSignal) => Promise<unknown>) =>
            body(async () => {}, new AbortController().signal),
        },
        resources: { renderPreFxAudio: async (track: { name: string }, start: number, end: number) =>
          `/tmp/${track.name}-${start}-${end}.wav` },
        environment: { storageDirectory: storage, tempDirectory: storage, language: "EN" },
        getObjectFromHandle: lookup.getObjectFromHandle,
        withinTransaction: <T>(fn: () => T) => fn(),
      };
    },
  };
  return { sdk, registered, selection: list };
}

async function activateInVm(code: string, sdk: object) {
  const sandbox: Record<string, unknown> = {
    module: { exports: {} }, process, console, Buffer, setTimeout, clearTimeout, setInterval,
    clearInterval, setImmediate,
    require(id: string) {
      if (id === "@ableton-extensions/sdk") return sdk;
      if (!id.startsWith("node:")) throw new Error(`the host has no module ${id}`);
      if (id === "node:net") {
        return { ...net, createServer: (...args: Parameters<typeof net.createServer>) => {
          const server = net.createServer(...args);
          servers.push(server);
          return server;
        } };
      }
      return nodeRequire(id);
    },
  };
  sandbox.exports = (sandbox.module as { exports: object }).exports;
  vm.createContext(sandbox);
  assert.equal(vm.runInContext("typeof global", sandbox), "undefined");
  assert.equal(vm.runInContext("typeof URL", sandbox), "undefined");
  vm.runInContext(code, sandbox, { filename: "extension.js" });
  const exported = (sandbox.module as { exports: { activate: (a: unknown) => void } }).exports;
  exported.activate({ hostApiVersion: "1.0.0" });
}

async function request(port: number, lines: object[]): Promise<Record<string, unknown>[]> {
  const socket = net.connect(port, "127.0.0.1");
  socket.setEncoding("utf8");
  let received = "";
  socket.on("data", (chunk: string) => { received += chunk; });
  socket.write(lines.map((line) => JSON.stringify(line) + "\n").join(""));
  while (received.split("\n").length <= lines.length) await new Promise((r) => setTimeout(r, 10));
  socket.destroy();
  return received.trim().split("\n").map((line) => JSON.parse(line));
}

async function waitForServer(port: number) {
  for (let i = 0; i < 200; i++) {
    const ok = await new Promise<boolean>((resolve) => {
      const probe = net.connect(port, "127.0.0.1", () => { probe.destroy(); resolve(true); });
      probe.on("error", () => resolve(false));
    });
    if (ok) return;
    await new Promise((r) => setTimeout(r, 10));
  }
  throw new Error("the bridge did not start");
}

test("the bundle loads and serves in an Extension Host-like context", async () => {
  const dir = mkdtempSync(join(tmpdir(), "companion-"));
  const outfile = join(dir, "extension.js");
  await build({ ...buildOptions({ outfile, externalSdk: true }), logLevel: "silent" });
  const port = await freePort();
  const home = join(dir, "home");
  process.env.HOME = home;
  process.env.ABLETON_MCP_COMPANION_PORT = String(port);
  const host = fakeHost(join(dir, "storage"));
  await activateInVm(readFileSync(outfile, "utf8"), host.sdk);
  await waitForServer(port);

  assert.deepEqual([...host.registered.commands.keys()].sort(),
                   ["abletonmcp.shareArrangement", "abletonmcp.shareClipSlots", "abletonmcp.shareObject"]);
  assert.equal(host.registered.menus.length, 7);
  assert.ok(host.registered.menus.some(([scope]) => scope === "MidiTrack.ArrangementSelection"));

  const sessionFile = join(home, ".ableton-mcp", "companion-session.json");
  const session = JSON.parse(readFileSync(sessionFile, "utf8"));
  assert.equal(session.port, port);
  assert.equal(statSync(sessionFile).mode & 0o777, 0o600);

  host.registered.commands.get("abletonmcp.shareArrangement")!({
    time_selection_start: 8n, time_selection_end: 24n, selected_lanes: host.selection.slice(0, 2) });
  const [hello, selection, songInfo, confirm, render, notAudio] = await request(port, [
    { id: 1, type: "hello", params: { token: session.token } },
    { id: 2, type: "get_selection" },
    { id: 3, type: "song" },
    { id: 4, type: "confirm", params: { message: "Replace the drums?" } },
    { id: 5, type: "render_audio", params: { track: "Vox", start_beat: 0, end_beat: 8 } },
    { id: 6, type: "render_audio", params: { track: 0, end_beat: 8 } },
  ]);
  assert.equal(hello.status, "success");
  const shared = selection.result as Record<string, unknown>;
  assert.equal(shared.kind, "arrangement");
  assert.deepEqual([shared.start_beat, shared.end_beat, shared.tracks], [8, 24, ["Drums", "Vox"]]);
  assert.deepEqual(songInfo.result, { tempo: 124, root_note: 9, scale_name: "Minor", tracks: [
    { index: 0, name: "Drums", type: "midi" }, { index: 1, name: "Vox", type: "audio" }] });
  assert.deepEqual(confirm.result, { confirmed: true });
  assert.ok(decodeURIComponent(host.registered.dialogs[0]).includes("Replace the drums?"));
  assert.deepEqual(render.result, { file: "/tmp/Vox-0-8.wav", track: "Vox", start_beat: 0, end_beat: 8 });
  assert.match(String(notAudio.message), /Drums is not an audio track/);
});

test("without a writable home folder the session file goes to the storage folder", async () => {
  const dir = mkdtempSync(join(tmpdir(), "companion-"));
  const outfile = join(dir, "extension.js");
  await build({ ...buildOptions({ outfile, externalSdk: true }), logLevel: "silent" });
  const port = await freePort();
  const blocked = join(dir, "not-a-folder");
  writeFileSync(blocked, "");
  process.env.HOME = blocked;
  process.env.ABLETON_MCP_COMPANION_PORT = String(port);
  const host = fakeHost(join(dir, "storage"));
  await activateInVm(readFileSync(outfile, "utf8"), host.sdk);
  await waitForServer(port);
  const session = JSON.parse(readFileSync(join(dir, "storage", "companion-session.json"), "utf8"));
  assert.equal(session.port, port);
});
