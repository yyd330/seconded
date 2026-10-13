#!/usr/bin/env bash
# Setup script for Seconded
# Creates a Python 3.12 venv and installs deps. Models download on first run
# (HuggingFace cache) — see README for choosing ASR/summary models.
set -euo pipefail

cd "$(dirname "$0")"

VENV="${VENV:-.venv}"
if [ ! -d "$VENV" ]; then
    echo "Creating Python 3.12 virtual environment at $VENV..."
    python3.12 -m venv "$VENV"
fi

echo "Installing dependencies..."
"$VENV/bin/pip" install -r requirements.txt

echo ""
echo "Setup complete."
echo "Quick test:  ./$VENV/bin/python server.py --file samples/sample_meeting_en.wav"
echo "Run server:  ./$VENV/bin/python server.py --https-port 8766"
echo "Model choice: --asr-backend/--asr-model/--summary-url/--summary-model"
echo "              (see README 'Choosing models')"
