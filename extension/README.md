# AbletonMCP Companion (Extensions SDK add-on)

An optional add-on for **Live 12.4.5+ Suite** (Ableton's Extensions SDK,
public beta) that gives the `ableton-live-mcp` server three things its Remote
Script cannot reach:

| In Live | For the MCP server |
|---|---|
| **Share selection / clips / clip / track with AbletonMCP** in the right-click menus of the Arrangement time selection, clip slots, clips and tracks | `get_live_selection`: which part of the song the user means (the Live Object Model has no time selection) |
| A dialog with your question and OK/Cancel | `confirm_in_live`: a yes/no from the musician, in Live |
| Rendering an audio track's clips to a file before its devices | `render_track_audio`: audio without playing the set in real time |

**Status: not yet run inside Live.** The Trial this was written on cannot load
extensions (they need Suite and the beta). It type-checks against the SDK's
own declarations (`@ableton-extensions/sdk` 1.0.0-beta), and its tests run the
built bundle in a `vm` context set up like the Extension Host's. Please report
what happens in Live.

## Build and install

1. Join Ableton's beta program and download the Extensions SDK (Centercode).
2. In this folder:
   ```bash
   npm ci
   npm install --no-save <path>/ableton-extensions-sdk-1.0.0-beta.N.tgz
   npm run build            # → dist/extension.js
   ```
3. In Live: **Settings › Extensions**, add this folder (the one with
   `manifest.json`), and enable it.
4. Ask the assistant to run `doctor`: the `companion` line should say it
   answers.

## How it talks to the server

It listens on `127.0.0.1:9878` (`ABLETON_MCP_COMPANION_PORT` changes it) with
the Remote Script's framing: one JSON object per line each way, replies echo
the request's `id`, and the first request is `hello` with a per-launch token.
The token and port go to `~/.ableton-mcp/companion-session.json` (mode 600);
where Live only lets extensions write in their storage folder, the file goes
there and the server searches Ableton's folders for it. If the server still
cannot find it, set `ABLETON_MCP_COMPANION_TOKEN` (the token is in Live's log,
`[AbletonMCP Companion] session file: …`).

Commands: `hello`, `song`, `get_selection`, `confirm`, `render_audio`.

## The Extension Host, as other projects found it in Live

These shaped the code (see `build-options.mjs` and the tests); they come from
projects that ran extensions in Live 12.4.5–12.4.15 betas, not from this one:

- The bundle runs in a `vm` context: `globalThis` but no `global`, few web
  globals (no `URL`, `TextEncoder`, `crypto`), `require` for `node:` modules.
  So the build is one CommonJS file with `global` defined as `globalThis`.
- Integer getters (colors, pitches, positions) arrive as `bigint` even where
  the types say `number`: values are converted with `Number()`.
- Errors from `node:` modules come from another realm (`instanceof Error` is
  false); messages are read by shape.
- File access may be limited to the extension's storage and temp folders.

## Development

```bash
npm run typecheck       # against types/ableton-sdk.d.ts (no SDK needed)
npm run typecheck:sdk   # against the installed SDK
npm test                # unit tests + the bundle in a simulated host
npm run build:check     # bundles with the SDK left out
```
