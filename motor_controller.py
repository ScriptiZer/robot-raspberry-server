"""Two-channel L298N differential-drive controller for rear_left and rear_right.

GPIO pins are logic signals only. Motor current comes from the L298N supply,
not from the Raspberry Pi. Share Pi GND with the L298N GND, and do not connect
the L298N 5V terminal to the Pi.
"""

from __future__ import annotations

import logging
import threading
from typing import Protocol

from config import L298N_PIN_NAMES, MOTOR_NAMES, AppConfig, MotorPins
from protocol import ControlCommand

LOGGER = logging.getLogger("motors")

# L298N enable inputs are slow. 1 kHz is fast enough for speed control
# without asking the driver to switch harder than it can.
_PWM_FREQUENCY_HZ = 1000
_SPEED_EPSILON = 0.01


class MotorChannel(Protocol):
    def set_speed(self, value: float) -> None:
        """Set signed speed in [-1.0, 1.0]. 0 is stop."""

    def close(self) -> None:
        ...


def configure_gpiozero_pin_factory() -> None:
    """Select the Pi 5 pin factory. gpiozero pin numbers are BCM, not header pins."""
    from gpiozero import Device
    from gpiozero.pins.lgpio import LGPIOFactory

    Device.pin_factory = LGPIOFactory()
    LOGGER.info("GPIO numbering mode: BCM via lgpio (not physical header pins)")


def l298n_outputs(speed: float) -> tuple[bool, bool, float]:
    """Map a signed speed to (in_a high, in_b high, enable duty).

    Stop is both direction pins low and duty 0. Forward and reverse each
    assert only one direction pin. Both pins are never left high together.
    """
    speed = max(-1.0, min(1.0, float(speed)))
    if speed > _SPEED_EPSILON:
        return True, False, speed
    if speed < -_SPEED_EPSILON:
        return False, True, -speed
    return False, False, 0.0


class SimulatedMotor:
    def __init__(self, name: str) -> None:
        self.name = name
        self._value = 0.0

    def set_speed(self, value: float) -> None:
        self._value = max(-1.0, min(1.0, value))

    def close(self) -> None:
        self._value = 0.0


class GpioMotor:
    """One L298N half of the dual H-bridge.

    ``pins.in1`` / ``pins.in2`` are the direction pair (IN1/IN2 or IN3/IN4).
    ``pins.pwm`` is the enable input (ENA or ENB) and is always PWM.
    """

    def __init__(self, name: str, pins: MotorPins) -> None:
        from gpiozero import DigitalOutputDevice, PWMOutputDevice

        self.name = name
        # Start disabled: both inputs low, enable duty 0.
        self._in_a = DigitalOutputDevice(pins.in1, initial_value=False)
        self._in_b = DigitalOutputDevice(pins.in2, initial_value=False)
        self._enable = PWMOutputDevice(
            pins.pwm,
            frequency=_PWM_FREQUENCY_HZ,
            initial_value=0.0,
        )
        in_a_name, in_b_name, enable_name = L298N_PIN_NAMES[name]
        LOGGER.info(
            "%s L298N BCM %s=%s %s=%s %s/PWM=%s at %s Hz",
            name,
            in_a_name,
            pins.in1,
            in_b_name,
            pins.in2,
            enable_name,
            pins.pwm,
            _PWM_FREQUENCY_HZ,
        )

    def set_speed(self, value: float) -> None:
        in_a, in_b, duty = l298n_outputs(value)
        self._write(in_a, in_b, duty)

    def close(self) -> None:
        try:
            self._write(False, False, 0.0)
        finally:
            self._enable.close()
            self._in_a.close()
            self._in_b.close()

    def _write(self, in_a: bool, in_b: bool, duty: float) -> None:
        # Disable the bridge before touching direction pins so a stale input
        # cannot keep the motor running, and so both inputs are never high.
        self._enable.value = 0.0
        self._in_a.off()
        self._in_b.off()
        if in_a:
            self._in_a.on()
        elif in_b:
            self._in_b.on()
        if duty > 0.0:
            self._enable.value = duty


class MotorController:
    """Thread-safe differential drive for the rear_left and rear_right channels."""

    def __init__(self, config: AppConfig) -> None:
        self._lock = threading.Lock()
        self._steer_slow = max(0.0, min(1.0, config.steer_slow_factor))
        self._in_place = max(0.0, min(1.0, config.in_place_turn_ratio))
        self._simulation = config.simulation_mode
        self._stopped = True
        self._last_label = "STOP"
        self._motors: dict[str, MotorChannel] = {}

        try:
            if config.simulation_mode:
                LOGGER.warning("SIMULATION_MODE enabled: GPIO will not be accessed")
                self._motors = {name: SimulatedMotor(name) for name in MOTOR_NAMES}
            else:
                configure_gpiozero_pin_factory()
                self._motors = {
                    name: GpioMotor(name, config.pin(name)) for name in MOTOR_NAMES
                }
                LOGGER.info(
                    "L298N motors initialized (BCM): %s",
                    {
                        name: (
                            f"{L298N_PIN_NAMES[name][0]}={config.pin(name).in1} "
                            f"{L298N_PIN_NAMES[name][1]}={config.pin(name).in2} "
                            f"{L298N_PIN_NAMES[name][2]}={config.pin(name).pwm}"
                        )
                        for name in MOTOR_NAMES
                    },
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
        self._drive(s, s, f"FORWARD speed={speed}")

    def backward(self, speed: int) -> None:
        s = self._unit(speed)
        self._drive(-s, -s, f"BACKWARD speed={speed}")

    def left(self, speed: int) -> None:
        s = self._unit(speed)
        t = s * self._in_place
        self._drive(-t, s, f"TURN LEFT speed={speed}")

    def right(self, speed: int) -> None:
        s = self._unit(speed)
        t = s * self._in_place
        self._drive(s, -t, f"TURN RIGHT speed={speed}")

    def forward_left(self, speed: int) -> None:
        s = self._unit(speed)
        slow = s * self._steer_slow
        self._drive(slow, s, f"FORWARD LEFT speed={speed}")

    def forward_right(self, speed: int) -> None:
        s = self._unit(speed)
        slow = s * self._steer_slow
        self._drive(s, slow, f"FORWARD RIGHT speed={speed}")

    def backward_left(self, speed: int) -> None:
        s = self._unit(speed)
        slow = s * self._steer_slow
        self._drive(-slow, -s, f"BACKWARD LEFT speed={speed}")

    def backward_right(self, speed: int) -> None:
        s = self._unit(speed)
        slow = s * self._steer_slow
        self._drive(-s, -slow, f"BACKWARD RIGHT speed={speed}")

    def stop(self, reason: str = "") -> None:
        with self._lock:
            already = self._stopped
            self._set_raw(0.0, 0.0)
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

    def _drive(self, left: float, right: float, label: str) -> None:
        with self._lock:
            self._set_raw(left, right)
            self._stopped = False
            if label != self._last_label:
                self._log_action(label)
            self._last_label = label

    def _set_raw(self, left: float, right: float) -> None:
        mapping = {
            "rear_left": left,
            "rear_right": right,
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
