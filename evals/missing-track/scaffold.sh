#!/usr/bin/env bash
# A simulated Live set for this case (see ../start-simulator.sh)
exec "$(dirname "${BASH_SOURCE[0]}")/../start-simulator.sh" "$(dirname "${BASH_SOURCE[0]}")"
