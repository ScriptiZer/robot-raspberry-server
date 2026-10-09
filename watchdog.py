"""Dead-man timer: stop motors if no valid control packet arrives in time.

This is independent of TCP disconnect detection. A frozen socket, dropped
Wi-Fi, or a crashed laptop still trips the timeout.
"""

from __future__ import annotations

import logging
import threading
import time

from motor_controller import MotorController

LOGGER = logging.getLogger("watchdog")


class MotorWatchdog:
    def __init__(self, motors: MotorController, timeout_s: float = 0.5, poll_s: float = 0.05) -> None:
        self._motors = motors
        self._timeout = max(0.1, float(timeout_s))
        self._poll = max(0.02, float(poll_s))
        self._lock = threading.Lock()
        self._last_valid = time.monotonic()
        self._armed = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._trip_logged = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="motor-watchdog", daemon=True)
        self._thread.start()
        LOGGER.info("Watchdog started (timeout=%.3fs)", self._timeout)

    def ping(self) -> None:
        """Call for every valid ``control`` or ``stop`` packet."""
        with self._lock:
            self._last_valid = time.monotonic()
            self._armed = True
            self._trip_logged = False

    def reset_idle(self) -> None:
        """Client gone: motors already stopped; do not keep re-tripping."""
        with self._lock:
            self._armed = False
            self._trip_logged = False
            self._last_valid = time.monotonic()

    def shutdown(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1.0)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self._poll):
            try:
                now = time.monotonic()
                with self._lock:
                    armed = self._armed
                    elapsed = now - self._last_valid
                    already_logged = self._trip_logged
                if armed and elapsed >= self._timeout:
                    if not already_logged:
                        LOGGER.warning(
                            "Watchdog stop: no valid control packet for %.3fs",
                            elapsed,
                        )
                        with self._lock:
                            self._trip_logged = True
                    self._motors.stop(reason="watchdog")
            except Exception:
                LOGGER.exception("Watchdog loop error; forcing motor stop")
                try:
                    self._motors.stop(reason="watchdog-error")
                except Exception:
                    LOGGER.exception("Failed to stop motors from watchdog handler")
