#!/usr/bin/env bash
set -euo pipefail
# List USB / V4L2 cameras and formats for /dev/video0 (or CAMERA_DEVICE).

if ! command -v v4l2-ctl >/dev/null 2>&1; then
  echo "v4l2-ctl not found. Install with: sudo apt install v4l-utils" >&2
  exit 1
fi

DEVICE="${CAMERA_DEVICE:-/dev/video0}"

echo "=== v4l2-ctl --list-devices ==="
v4l2-ctl --list-devices
echo
if [[ ! -e "$DEVICE" ]]; then
  echo "Device $DEVICE does not exist"
  exit 1
fi
echo "=== formats for $DEVICE ==="
v4l2-ctl --device="$DEVICE" --list-formats-ext
