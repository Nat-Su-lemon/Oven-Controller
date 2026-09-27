#!/usr/bin/env bash
# Builds dist/CureOven.app on macOS (dist/CureOven on Linux).
set -euo pipefail
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || bash setup_mac.sh
.venv/bin/python -m pip install -r requirements.txt -r requirements-build.txt
.venv/bin/pyinstaller --noconfirm --clean CureOven.spec

if [ "$(uname)" = "Darwin" ]; then
  # Zip with ditto so the .app keeps its structure and permissions when shared
  ditto -c -k --keepParent dist/CureOven.app dist/CureOven-macOS.zip
  echo
  echo "Built: $(pwd)/dist/CureOven.app  (and dist/CureOven-macOS.zip to share)"
else
  echo
  echo "Built: $(pwd)/dist/CureOven"
fi
