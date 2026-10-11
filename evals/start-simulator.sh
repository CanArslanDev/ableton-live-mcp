#!/usr/bin/env bash
# Starts a simulated Live set for one eval run (MCP_Server/simulator.py), called by
# each case's scaffold.sh with the case's folder. The run's HOME is a fresh temporary
# folder, so the session file written there is the one the plugin's server reads (it
# takes the port from it); the set's description (live-state.json) and the case's
# verdicts (checks.json) are kept in the workspace for the graders. The simulator
# stops a minute after the server disconnects.
set -euo pipefail
CASE="$(cd "$1" && pwd)"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -x "$ROOT/.venv/bin/python" ]; then
  PYTHON=("$ROOT/.venv/bin/python")
else
  PYTHON=(uv run --directory "$ROOT" --frozen --no-dev python)
fi
ARGS=(--detach --port 0 --session-file "$HOME/.ableton-mcp/live-session.json"
      --state-file "$PWD/live-state.json" --check "$CASE/check.py"
      --checks-file "$PWD/checks.json" --idle-exit 60 --lifetime 1800
      --log-file "$PWD/simulator.log")
if [ -f "$CASE/setup.json" ]; then
  ARGS+=(--setup "$CASE/setup.json")
fi
cd "$ROOT"
exec "${PYTHON[@]}" -m MCP_Server.simulator "${ARGS[@]}"
