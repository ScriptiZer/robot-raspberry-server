"""Four-motor differential-drive controller.

GPIO pins are *logic control signals only*. Never power motors from the
Raspberry Pi. Use a motor driver (L298N, TB6612, etc.) and an external
battery/PSU, with a shared logic ground between the Pi and the driver.
"""

from __future__ import annotations

import logging
import threading
from typing import Protocol

from config import AppConfig, MotorPins
from protocol import ControlCommand

LOGGER = logging.getLogger("motors")


class MotorChannel(Protocol):
    def set_speed(self, value: float) -> None:
        """Set signed speed in [-1.0, 1.0]. 0 is stop."""

    def close(self) -> None:
        ...


def configure_gpiozero_pin_factory() -> None:
    try:
        from gpiozero import Device
        from gpiozero.pins.lgpio import LGPIOFactory

        Device.pin_factory = LGPIOFactory()
        LOGGER.info("Using gpiozero pin factory: lgpio")
    except Exception as exc:
        LOGGER.info("lgpio pin factory not available (%s); using gpiozero default", exc)


class SimulatedMotor:
    def __init__(self, name: str) -> None:
        self.name = name
        self._value = 0.0

    def set_speed(self, value: float) -> None:
        self._value = max(-1.0, min(1.0, value))

    def close(self) -> None:
        self._value = 0.0


class GpioMotor:
    def __init__(self, name: str, pins: MotorPins) -> None:
        from gpiozero import Motor

        self.name = name
        self._motor = Motor(
            forward=pins.in1,
            backward=pins.in2,
            enable=pins.pwm,
            pwm=True,
        )

    def set_speed(self, value: float) -> None:
        value = max(-1.0, min(1.0, value))
        if abs(value) < 0.01:
            self._motor.stop()
        elif value > 0:
            self._motor.forward(value)
        else:
            self._motor.backward(-value)

    def close(self) -> None:
        try:
            self._motor.stop()
        finally:
            self._motor.close()


class MotorController:
    """Thread-safe differential drive for FL / FR / RL / RR motors."""

    def __init__(self, config: AppConfig) -> None:
        self._lock = threading.Lock()
        self._steer_slow = max(0.0, min(1.0, config.steer_slow_factor))
        self._in_place = max(0.0, min(1.0, config.in_place_turn_ratio))
        self._simulation = config.simulation_mode
        self._stopped = True
        self._last_label = "STOP"
        self._motors: dict[str, MotorChannel] = {}

        order = ("front_left", "front_right", "rear_left", "rear_right")
        try:
            if config.simulation_mode:
                LOGGER.warning("SIMULATION_MODE enabled: GPIO will not be accessed")
                self._motors = {name: SimulatedMotor(name) for name in order}
            else:
                configure_gpiozero_pin_factory()
                self._motors = {
                    name: GpioMotor(name, config.pin(name)) for name in order
                }
                LOGGER.info(
                    "GPIO motors initialized (BCM pins, example defaults unless overridden): %s",
                    {name: config.pin(name) for name in order},
                )
        except Exception:
            LOGGER.exception("Motor initialization failed; forcing software stop state")
            self._safe_close()
            raise

        self.stop(reason="startup")

    def apply_command(self, command: ControlCommand) -> str:
        resolved = command.resolved()
        name = resolved.motion_name()
        speed = resolved.speed
        if name == "STOP":
            self.stop()
            return name
        dispatch = {
            "FORWARD": self.forward,
            "BACKWARD": self.backward,
            "LEFT": self.left,
            "RIGHT": self.right,
            "FORWARD_LEFT": self.forward_left,
            "FORWARD_RIGHT": self.forward_right,
            "BACKWARD_LEFT": self.backward_left,
            "BACKWARD_RIGHT": self.backward_right,
        }
        dispatch[name](speed)
        return name

    def forward(self, speed: int) -> None:
        s = self._unit(speed)
        self._drive(s, s, s, s, f"FORWARD speed={speed}")

    def backward(self, speed: int) -> None:
        s = self._unit(speed)
        self._drive(-s, -s, -s, -s, f"BACKWARD speed={speed}")

    def left(self, speed: int) -> None:
        s = self._unit(speed)
        t = s * self._in_place
        self._drive(-t, s, -t, s, f"TURN LEFT speed={speed}")

    def right(self, speed: int) -> None:
        s = self._unit(speed)
        t = s * self._in_place
        self._drive(s, -t, s, -t, f"TURN RIGHT speed={speed}")

    def forward_left(self, speed: int) -> None:
        s = self._unit(speed)
        slow = s * self._steer_slow
        self._drive(slow, s, slow, s, f"FORWARD LEFT speed={speed}")

    def forward_right(self, speed: int) -> None:
        s = self._unit(speed)
        slow = s * self._steer_slow
        self._drive(s, slow, s, slow, f"FORWARD RIGHT speed={speed}")

    def backward_left(self, speed: int) -> None:
        s = self._unit(speed)
        slow = s * self._steer_slow
        self._drive(-slow, -s, -slow, -s, f"BACKWARD LEFT speed={speed}")

    def backward_right(self, speed: int) -> None:
        s = self._unit(speed)
        slow = s * self._steer_slow
        self._drive(-s, -slow, -s, -slow, f"BACKWARD RIGHT speed={speed}")

    def stop(self, reason: str = "") -> None:
        with self._lock:
            already = self._stopped
            self._set_raw(0.0, 0.0, 0.0, 0.0)
            self._stopped = True
            label = "STOP"
            if reason:
                label = f"STOP ({reason})"
            if not already or (reason and reason != self._last_label):
                self._log_action(label)
            self._last_label = reason or "STOP"

    def close(self) -> None:
        self.stop(reason="shutdown")
        self._safe_close()

    def _drive(self, fl: float, fr: float, rl: float, rr: float, label: str) -> None:
        with self._lock:
            self._set_raw(fl, fr, rl, rr)
            self._stopped = False
            if label != self._last_label:
                self._log_action(label)
            self._last_label = label

    def _set_raw(self, fl: float, fr: float, rl: float, rr: float) -> None:
        mapping = {
            "front_left": fl,
            "front_right": fr,
            "rear_left": rl,
            "rear_right": rr,
        }
        for name, value in mapping.items():
            motor = self._motors.get(name)
            if motor is not None:
                motor.set_speed(value)

    def _log_action(self, label: str) -> None:
        if self._simulation:
            LOGGER.info("SIM MOTOR: %s", label)
        else:
            LOGGER.info("MOTOR: %s", label)

    def _safe_close(self) -> None:
        for motor in list(self._motors.values()):
            try:
                motor.close()
            except Exception:
                LOGGER.exception("Error closing motor channel")
        self._motors.clear()

    @staticmethod
    def _unit(speed: int) -> float:
        return max(0.0, min(100, int(speed))) / 100.0
