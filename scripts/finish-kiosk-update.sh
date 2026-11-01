#!/usr/bin/env bash
# One-shot recovery / manual update for a Vinyl Streamer kiosk.
# Use this when Settings → Update Now fails with externally-managed-environment:
# the running app still has the old updater, which rolls back the git pull.
set -euo pipefail

# Prefer the live installs we've seen in the wild.
for d in \
  "$HOME/vinyl-airplay" \
  /home/listen/vinyl-airplay \
  /opt/vinyl-streamer \
  "$(cd "$(dirname "$0")/.." && pwd)"
do
  if [[ -d "$d/.git" ]]; then
    cd "$d"
    break
  fi
done

echo "Updating in $PWD ..."
git fetch --prune origin
# Merge when the Pi has a local commit and GitHub main has moved.
# A plain pull dies on Git 2.27+ with "divergent branches".
git -c user.name='Vinyl Streamer' -c user.email='listen@localhost' \
  pull --no-rebase --no-edit origin main

# Point systemd at the venv PATH so future Update Now clicks use venv pip.
UNIT=/etc/systemd/system/vinyl-airplay.service
if [[ -f "$UNIT" ]] && [[ -x "$PWD/venv/bin/python" ]]; then
  if ! grep -q 'Environment=PATH=.*venv/bin' "$UNIT" 2>/dev/null; then
    echo "Patching $UNIT to prefer venv/bin on PATH..."
    # Insert Environment= after WorkingDirectory= when missing.
    if grep -q '^WorkingDirectory=' "$UNIT"; then
      sudo sed -i "/^WorkingDirectory=/a Environment=PATH=$PWD/venv/bin:/usr/local/bin:/usr/bin:/bin" "$UNIT"
    else
      sudo sed -i "/^\[Service\]/a Environment=PATH=$PWD/venv/bin:/usr/local/bin:/usr/bin:/bin" "$UNIT"
    fi
    sudo systemctl daemon-reload
  fi
fi

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
  echo "No venv pip found; skipping dependency install (code update still applies)."
fi

echo "Restarting vinyl-airplay and the WiFi portal..."
sudo systemctl restart vinyl-wifi-setup || true
sudo systemctl restart vinyl-airplay
echo "Done. Hard-refresh the kiosk browser if the UI looks stale."
