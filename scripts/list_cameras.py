#!/usr/bin/env python3
"""List V4L2 cameras and the formats of CAMERA_DEVICE (default /dev/video0)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import load_config


def run(command: list[str]) -> int:
    print("+", " ".join(command))
    result = subprocess.run(command, check=False)
    return result.returncode


def main() -> int:
    if shutil.which("v4l2-ctl") is None:
        print("v4l2-ctl not found. Install with: sudo apt install v4l-utils", file=sys.stderr)
        return 1

    device = load_config(project_root=ROOT).camera_device
    print("=== v4l2-ctl --list-devices ===")
    run(["v4l2-ctl", "--list-devices"])
    print()
    if not Path(device).exists():
        print(f"Device {device} does not exist")
        return 1
    print(f"=== formats for {device} ===")
    return run(["v4l2-ctl", f"--device={device}", "--list-formats-ext"])


if __name__ == "__main__":
    sys.exit(main())
