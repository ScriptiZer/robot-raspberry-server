#!/usr/bin/env python3
"""Raspberry Pi robot server entry point."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from config import load_config
from control_server import ControlServer
from logging_setup import setup_logging
from motor_controller import MotorController
from video_streamer import VideoStreamer
from watchdog import MotorWatchdog

LOGGER = logging.getLogger("app")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Raspberry Pi robot control/video server")
    parser.add_argument("--config", type=Path, default=None, help="Path to config.yaml")
    parser.add_argument(
        "--simulation",
        action="store_true",
        help="Do not touch GPIO; log intended motor actions",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(__file__).resolve().parent
    config = load_config(project_root=root, yaml_path=args.config)
    if args.simulation:
        config.simulation_mode = True

    setup_logging(config.log_level, config.log_file or None)
    LOGGER.info("Starting robot server (simulation=%s)", config.simulation_mode)

    motors: MotorController | None = None
    watchdog: MotorWatchdog | None = None
    video: VideoStreamer | None = None
    server: ControlServer | None = None
    shutting_down = threading.Event()

    def shutdown(signum: int | None = None, _frame: object = None) -> None:
        if shutting_down.is_set():
            return
        shutting_down.set()
        if signum is not None:
            LOGGER.info("Received signal %s; shutting down", signum)
        else:
            LOGGER.info("Shutting down")
        if server is not None:
            server.shutdown()
        if watchdog is not None:
            watchdog.shutdown()
        if video is not None:
            video.shutdown()
        if motors is not None:
            motors.close()

    try:
        motors = MotorController(config)
    except Exception:
        LOGGER.exception("Cannot initialize motors")
        return 1

    watchdog = MotorWatchdog(motors, timeout_s=config.watchdog_timeout)
    video = VideoStreamer(config)
    server = ControlServer(config, motors, watchdog, video)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        watchdog.start()
        video.start()
        server.serve_forever()
    except Exception:
        LOGGER.exception("Fatal server error")
        return 1
    finally:
        shutdown()
        LOGGER.info("Robot server stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
