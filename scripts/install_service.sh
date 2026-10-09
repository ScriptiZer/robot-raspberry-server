#!/usr/bin/env bash
set -euo pipefail
# Install and enable robot-server.service for the current user and checkout.

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(id -un)"
GROUP_NAME="$(id -gn)"
SERVICE_SRC="$ROOT/systemd/robot-server.service"
TMP="$(mktemp)"

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "Expected virtualenv at $ROOT/.venv — create it first." >&2
  exit 1
fi

sed \
  -e "s|^User=.*|User=${USER_NAME}|" \
  -e "s|^Group=.*|Group=${GROUP_NAME}|" \
  -e "s|/home/pi/robot-raspberry-server|${ROOT}|g" \
  "$SERVICE_SRC" > "$TMP"

echo "Installing systemd unit as user ${USER_NAME}"
echo "WorkingDirectory=${ROOT}"
sudo cp "$TMP" /etc/systemd/system/robot-server.service
rm -f "$TMP"
sudo systemctl daemon-reload
sudo systemctl enable robot-server.service
sudo systemctl restart robot-server.service
sudo systemctl --no-pager --full status robot-server.service
