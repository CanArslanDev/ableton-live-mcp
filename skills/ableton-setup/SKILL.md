---
name: ableton-setup
description: Setting up and troubleshooting the connection between Claude and Ableton Live (the ableton MCP server and the AbletonMCP Remote Script). Use when the ableton tools fail to connect, time out, report an outdated or missing Remote Script, ask for authentication, or when the user is installing this for the first time.
---

# Setup and troubleshooting

The MCP server talks to Live through a MIDI Remote Script ("AbletonMCP")
that runs inside Live and listens on 127.0.0.1:9877. Live must be running with
that script selected as a Control Surface.

## First-time setup

1. `install_remote_script()` copies the script into Live's User Library
   (Remote Scripts/AbletonMCP). It works for Live 11 and 12, the Trial too.
2. The user restarts Live, because Live loads Remote Scripts only at launch.
3. In Live: Settings → Link, Tempo & MIDI → Control Surface → AbletonMCP
   (Input/Output: None).
4. `doctor()` should now report the connection as ok.

## Diagnose

`doctor()` checks the server and Remote Script versions, every installed copy
of the script, the connection and its latency, dialogs open in Live, CPU load,
and AbletonMCP errors in Live's log. Each failed check comes with its fix; do
what it says rather than guessing.

| Symptom | Cause and fix |
|---|---|
| Could not connect to Ableton Live | Live is not running, or AbletonMCP is not chosen as a Control Surface, or something else holds port 9877. |
| "older than this server" / outdated script | `install_remote_script()`, then restart Live. If the version does not change, an old `AbletonMCP` folder inside the Live application (…/Contents/App-Resources/MIDI Remote Scripts/) shadows the User Library copy; `doctor()` finds it. Live updates have restored one there. |
| Authentication required | The server is older than the Remote Script, which wants the session token since 2.0. Update the server. |
| Invalid session token | Live restarted and wrote a new token (~/.ableton-mcp/live-session.json); the next call reads it again. Across users or containers, set `ABLETON_MCP_TOKEN`. |
| Tools hang while a dialog is open | Live waits for the user to close it; `doctor()` names the dialog. |
| Claude Desktop: no response within 2 minutes (macOS) | A pending "uv wants to access files in your Documents folder" prompt; allow it and restart the client. |

Live's own log is `~/Library/Preferences/Ableton/Live <version>/Log.txt` on
macOS (lines tagged `(AbletonMCP)`).

## Live Trial

Everything works in the Trial, Max for Live included, except that saving and
exporting are disabled once the trial has expired. Analysis tools record by
themselves and do not need export.

## Tool set

`ABLETON_MCP_PROFILE=full` adds the original upstream tools that newer tools
replace (for prompts written against them). The default `core` profile has one
tool per job.
