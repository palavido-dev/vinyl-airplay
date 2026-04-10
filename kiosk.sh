#!/bin/bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export WLR_NO_HARDWARE_CURSORS=1
export XCURSOR_THEME=invisible
export XCURSOR_SIZE=1
export XCURSOR_PATH="$HOME/.icons"

# Clear Chromium cache so stale pages don't stick after reboot
rm -rf "$HOME/.config/chromium/Default/Cache" \
       "$HOME/.config/chromium/Default/Code Cache" \
       "$HOME/.config/chromium/Default/Service Worker" 2>/dev/null

# Wait for the vinyl-airplay service to be responding before launching the browser
echo "[kiosk] Waiting for vinyl-airplay..."
for i in $(seq 1 60); do
  if curl -s -o /dev/null -w '' http://localhost:8080/api/status 2>/dev/null; then
    echo "[kiosk] Service is up after ${i}s"
    break
  fi
  sleep 1
done

# Force the touchscreen output to its native 1024x600. The panel returns no EDID
# so wlroots would otherwise default to the first VESA mode (1024x768) and stretch
# the UI vertically. wlr-randr runs inside cage to change the mode before chromium.
cage -d -- bash -c 'wlr-randr --output HDMI-A-2 --mode 1024x600 2>/dev/null; exec chromium \
  --kiosk \
  --noerrdialogs \
  --disable-infobars \
  --no-first-run \
  --disable-translate \
  --disable-features=OverscrollHistoryNavigation \
  --disable-pinch \
  --autoplay-policy=no-user-gesture-required \
  --window-size=1024,600 \
  --touch-events=enabled \
  http://localhost:8080'
