#!/usr/bin/env bash
# Starts the GUI. Extra arguments are passed through, e.g.  bash run_mac.sh --sim
set -euo pipefail
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || bash setup_mac.sh
exec .venv/bin/python app/cure_gui.py "$@"
