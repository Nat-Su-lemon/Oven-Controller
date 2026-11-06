#!/usr/bin/env bash
# Creates .venv and installs the GUI's dependencies (macOS and Linux).
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "python3 not found. Install it from https://www.python.org/downloads/"
  exit 1
fi
if ! "$PY" -c "import tkinter" >/dev/null 2>&1; then
  echo "This Python has no tkinter (the GUI toolkit)."
  echo "  python.org installer: already included, use that Python instead"
  echo "  Homebrew:             brew install python-tk"
  echo "  Ubuntu/Debian:        sudo apt install python3-tk python3-venv"
  exit 1
fi

[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
echo
echo "Setup done. Start the GUI with:  bash run_mac.sh"
