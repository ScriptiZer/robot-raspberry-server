#!/usr/bin/env python3
"""Safely pulse one configured motor channel without starting the server."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import L298N_PIN_NAMES, MOTOR_NAMES, load_config
from motor_controller import GpioMotor, configure_gpiozero_pin_factory


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pulse a single configured motor channel without starting the server."
    )
    parser.add_argument("--motor", required=True, choices=(*MOTOR_NAMES, "all"))
    parser.add_argument("--direction", required=True, choices=("forward", "backward"))
    parser.add_argument("--speed", type=float, default=0.15, help="0.01 to 0.25 (default: 0.15)")
    parser.add_argument("--duration", type=float, default=0.5, help="0.1 to 1 second (default: 0.5)")
    parser.add_argument("--config", type=Path, default=None, help="Path to config.yaml")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 0.01 <= args.speed <= 0.25:
        raise SystemExit("--speed must be between 0.01 and 0.25")
    if not 0.1 <= args.duration <= 1.0:
        raise SystemExit("--duration must be between 0.1 and 1 second")

    config = load_config(project_root=ROOT, yaml_path=args.config)
    names = MOTOR_NAMES if args.motor == "all" else (args.motor,)
    sign = 1 if args.direction == "forward" else -1

    print("LIVE GPIO TEST: selected motor(s) will move briefly.")
    print("Lift the wheels, keep clear, and verify BCM wiring before continuing.")
    print("Remove the L298N ENA jumper (GPIO19) and ENB jumper (GPIO26).")
    for name in names:
        pins = config.pin(name)
        in_a, in_b, enable = L298N_PIN_NAMES[name]
        print(
            f"  {name}: BCM {in_a}={pins.in1} {in_b}={pins.in2} {enable}/PWM={pins.pwm}"
        )
    if input('Type MOVE to continue: ').strip() != "MOVE":
        print("Cancelled; no motor was moved.")
        return 1

    try:
        configure_gpiozero_pin_factory()
        for name in names:
            motor: GpioMotor | None = None
            try:
                motor = GpioMotor(name, config.pin(name))
                motor.set_speed(sign * args.speed)
                print(f"{name}: {args.direction} at {args.speed:.2f} for {args.duration:.1f}s")
                time.sleep(args.duration)
            finally:
                if motor is not None:
                    motor.close()
        print("Motor test complete; STOP sent.")
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted; motor channel closed with STOP.")
        return 130
    except Exception as exc:
        print(f"Motor test failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())