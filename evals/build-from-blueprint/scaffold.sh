#!/usr/bin/env bash
# A finished reference job in the run's home (prepare.py), then a simulated
# empty Live set for this case (see ../start-simulator.sh)
set -euo pipefail
CASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$CASE/../.." && pwd)"
if [ -x "$ROOT/.venv/bin/python" ]; then
  "$ROOT/.venv/bin/python" "$CASE/prepare.py" "$HOME"
else
  uv run --directory "$ROOT" --frozen --no-dev python "$CASE/prepare.py" "$HOME"
fi
exec "$CASE/../start-simulator.sh" "$CASE"
