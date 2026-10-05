#!/bin/bash
# Open the Study Mentor window (starts the server first if it is not running).
URL=http://127.0.0.1:8765
if ! curl -fs -m 1 "$URL/api/state" >/dev/null 2>&1; then
  systemctl --user start study-mentor.service 2>/dev/null || (setsid "$(dirname "$0")/run.sh" >/dev/null 2>&1 &)
  for _ in $(seq 1 40); do curl -fs -m 1 "$URL/api/state" >/dev/null 2>&1 && break; sleep 0.5; done
fi
exec flatpak run --socket=x11 com.google.Chrome --ozone-platform=x11 \
  --user-data-dir="$HOME/.var/app/com.google.Chrome/config/study-mentor-profile" \
  --app="$URL" --window-size=1440,860 --no-first-run --no-default-browser-check
