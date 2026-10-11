"""Load configuration from defaults, optional YAML, then environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def _load_env_file(path: Path) -> None:
    """Load KEY=VALUE pairs. python-dotenv is used when installed."""
    try:
        from dotenv import load_dotenv

        load_dotenv(path)
        return
    except ImportError:
        pass
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


# BCM GPIO numbers (not physical header pins). gpiozero + lgpio use BCM.
# One L298N dual H-bridge: rear_left on IN1/IN2/ENA, rear_right on IN3/IN4/ENB.
# Physical header pins are listed only so the BCM numbers are not confused with them.
#
# rear_left  -> L298N IN1=GPIO24 (phys 18), IN2=GPIO25 (phys 22), ENA=GPIO19 (phys 35)
# rear_right -> L298N IN3=GPIO5  (phys 29), IN4=GPIO6  (phys 31), ENB=GPIO26 (phys 37)
# Pi GND phys 39 -> L298N GND. Do not connect the L298N 5V terminal to the Pi.
DEFAULT_MOTOR_PINS: dict[str, dict[str, int]] = {
    "rear_left": {"in1": 24, "in2": 25, "pwm": 19},
    "rear_right": {"in1": 5, "in2": 6, "pwm": 26},
}

# Code field names stay in1/in2/pwm. On the right channel those pins are L298N IN3/IN4/ENB.
L298N_PIN_NAMES: dict[str, tuple[str, str, str]] = {
    "rear_left": ("IN1", "IN2", "ENA"),
    "rear_right": ("IN3", "IN4", "ENB"),
}

MOTOR_NAMES: tuple[str, ...] = tuple(DEFAULT_MOTOR_PINS)


@dataclass(frozen=True)
class MotorPins:
    in1: int
    in2: int
    pwm: int


@dataclass
class AppConfig:
    control_host: str = "0.0.0.0"
    control_port: int = 5000
    video_port: int = 5001
    control_token: str = ""
    laptop_ip: str = ""
    video_follow_client: bool = True
    video_enabled: bool = True
    watchdog_timeout: float = 0.5
    camera_device: str = "/dev/video0"
    video_width: int = 1280
    video_height: int = 720
    video_fps: int = 30
    video_bitrate: str = "2000k"
    video_restart_delay: float = 2.0
    simulation_mode: bool = False
    log_level: str = "INFO"
    log_file: str = ""
    auth_timeout: float = 5.0
    # Left/right side speed multiplier for W+A / W+D style curves (0-1).
    steer_slow_factor: float = 0.45
    # Reverse-side speed multiplier for in-place A/D turns (0-1).
    in_place_turn_ratio: float = 1.0
    motor_pins: dict[str, MotorPins] = field(default_factory=dict)

    def pin(self, name: str) -> MotorPins:
        return self.motor_pins[name]


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    return int(value)


def _as_float(value: Any, default: float) -> float:
    if value is None or value == "":
        return default
    return float(value)


def _pins_from_mapping(raw: dict[str, Any] | None) -> dict[str, MotorPins]:
    source = raw or DEFAULT_MOTOR_PINS
    result: dict[str, MotorPins] = {}
    for name, default in DEFAULT_MOTOR_PINS.items():
        block = source.get(name, default) or default
        result[name] = MotorPins(
            in1=int(block.get("in1", default["in1"])),
            in2=int(block.get("in2", default["in2"])),
            pwm=int(block.get("pwm", default["pwm"])),
        )
    return result


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML root in {path} must be a mapping")
    return data


def load_config(
    project_root: Path | None = None,
    yaml_path: Path | None = None,
) -> AppConfig:
    root = project_root or Path(__file__).resolve().parent
    _load_env_file(root / ".env")

    yaml_file = yaml_path or Path(os.environ.get("ROBOT_CONFIG", root / "config.yaml"))
    data = _load_yaml(yaml_file)

    env = os.environ
    cfg = AppConfig()
    cfg.control_host = env.get("CONTROL_HOST", data.get("control_host", cfg.control_host))
    cfg.control_port = _as_int(env.get("CONTROL_PORT", data.get("control_port")), cfg.control_port)
    cfg.video_port = _as_int(env.get("VIDEO_PORT", data.get("video_port")), cfg.video_port)
    cfg.control_token = str(env.get("CONTROL_TOKEN", data.get("control_token", cfg.control_token)))
    cfg.laptop_ip = str(env.get("LAPTOP_IP", data.get("laptop_ip", cfg.laptop_ip)))
    cfg.video_follow_client = _as_bool(
        env.get("VIDEO_FOLLOW_CLIENT", data.get("video_follow_client")),
        cfg.video_follow_client,
    )
    cfg.video_enabled = _as_bool(env.get("VIDEO_ENABLED", data.get("video_enabled")), cfg.video_enabled)
    cfg.watchdog_timeout = _as_float(
        env.get("WATCHDOG_TIMEOUT", data.get("watchdog_timeout")),
        cfg.watchdog_timeout,
    )
    cfg.camera_device = str(env.get("CAMERA_DEVICE", data.get("camera_device", cfg.camera_device)))
    cfg.video_width = _as_int(env.get("VIDEO_WIDTH", data.get("video_width")), cfg.video_width)
    cfg.video_height = _as_int(env.get("VIDEO_HEIGHT", data.get("video_height")), cfg.video_height)
    cfg.video_fps = _as_int(env.get("VIDEO_FPS", data.get("video_fps")), cfg.video_fps)
    cfg.video_bitrate = str(env.get("VIDEO_BITRATE", data.get("video_bitrate", cfg.video_bitrate)))
    cfg.video_restart_delay = _as_float(
        env.get("VIDEO_RESTART_DELAY", data.get("video_restart_delay")),
        cfg.video_restart_delay,
    )
    cfg.simulation_mode = _as_bool(
        env.get("SIMULATION_MODE", data.get("simulation_mode")),
        cfg.simulation_mode,
    )
    cfg.log_level = str(env.get("LOG_LEVEL", data.get("log_level", cfg.log_level)))
    cfg.log_file = str(env.get("LOG_FILE", data.get("log_file", cfg.log_file)))
    cfg.auth_timeout = _as_float(env.get("AUTH_TIMEOUT", data.get("auth_timeout")), cfg.auth_timeout)
    cfg.steer_slow_factor = _as_float(
        env.get("STEER_SLOW_FACTOR", data.get("steer_slow_factor")),
        cfg.steer_slow_factor,
    )
    cfg.in_place_turn_ratio = _as_float(
        env.get("IN_PLACE_TURN_RATIO", data.get("in_place_turn_ratio")),
        cfg.in_place_turn_ratio,
    )
    yaml_motors = data.get("motors") if isinstance(data.get("motors"), dict) else None
    cfg.motor_pins = _pins_from_mapping(yaml_motors)

    # Optional per-pin overrides: MOTOR_REAR_LEFT_IN1=24 etc.
    for name in DEFAULT_MOTOR_PINS:
        prefix = f"MOTOR_{name.upper()}_"
        current = cfg.motor_pins[name]
        cfg.motor_pins[name] = MotorPins(
            in1=_as_int(env.get(prefix + "IN1"), current.in1),
            in2=_as_int(env.get(prefix + "IN2"), current.in2),
            pwm=_as_int(env.get(prefix + "PWM"), current.pwm),
        )
    return cfg
