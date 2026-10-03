#!/usr/bin/env bash
# One-shot recovery / manual update for a Vinyl Streamer kiosk.
# Uses the project venv pip (never system pip3) so Bookworm PEP 668 is happy.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "Fetching origin/main..."
git fetch --prune origin
git pull --ff-only origin main

PIP=""
if [[ -x venv/bin/pip ]]; then
  PIP=venv/bin/pip
elif [[ -x .venv/bin/pip ]]; then
  PIP=.venv/bin/pip
fi

if [[ -n "$PIP" ]]; then
  echo "Installing requirements with $PIP..."
  "$PIP" install -r requirements.txt
else
  echo "No venv pip found; using current python -m pip..."
  python3 -m pip install -r requirements.txt
fi

echo "Restarting vinyl-airplay..."
systemctl restart vinyl-airplay
echo "Done."
