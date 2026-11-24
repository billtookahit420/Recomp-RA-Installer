#!/bin/sh
# Recomp RA Installer launcher: bootstraps a local venv with PySide6
# on first run (checks the import, not just the venv, so a half-made
# venv heals itself), then starts the GUI.
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
fi
if ! .venv/bin/python -c "import PySide6" 2>/dev/null; then
    .venv/bin/pip install --quiet PySide6
fi
exec .venv/bin/python app.py "$@"
