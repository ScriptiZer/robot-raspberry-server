"""Newline-delimited JSON protocol for robot motor control.

Wire format
-----------
Each message is one UTF-8 JSON object followed by a single newline (``\\n``).
TCP may deliver partial or multiple messages per ``recv()``; callers must
buffer and split on newlines.

Client -> server
----------------
Authenticate (required when the server has CONTROL_TOKEN set)::

    {"type": "auth", "token": "change-me"}

Movement state (sent about every 100 ms, even if unchanged)::

    {"type": "control", "forward": true, "backward": false,
     "left": false, "right": false, "speed": 70}

Immediate stop (Space / emergency button)::

    {"type": "stop"}

TCP keep-alive (does *not* refresh the motor watchdog)::

    {"type": "heartbeat"}

Server -> client
----------------
    {"type": "auth_ok"}
    {"type": "auth_error", "message": "invalid token"}
    {"type": "error", "message": "..."}
    {"type": "busy", "message": "another client is already connected"}

Semantics
---------
* ``speed`` is an integer 0-100.
* Opposite flags cancel: forward+backward => no longitudinal motion;
  left+right => no turn.
* Combinations produce differential steering (W+A = forward-left, etc.).
* Only ``control`` and ``stop`` count as valid motor packets for the 500 ms
  dead-man watchdog. ``heartbeat`` and ``auth`` do not.
* ``stop`` always takes priority over movement.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Iterator

LOGGER = logging.getLogger("protocol")

VALID_CLIENT_TYPES = frozenset({"auth", "control", "stop", "heartbeat"})


class ProtocolError(ValueError):
    """Raised when a complete line is not a usable protocol message."""


def encode_message(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, separators=(",", ":"), ensure_ascii=True) + "\n").encode(
        "utf-8"
    )


def iter_json_lines(buffer: bytearray) -> Iterator[dict[str, Any]]:
    """Yield complete JSON objects from *buffer*, removing consumed bytes."""
    while True:
        newline = buffer.find(b"\n")
        if newline < 0:
            return
        raw = bytes(buffer[:newline])
        del buffer[: newline + 1]
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            LOGGER.warning("Ignoring malformed JSON (%s): %s", exc, line[:160])
            continue
        if not isinstance(obj, dict):
            LOGGER.warning("Ignoring non-object JSON: %s", line[:160])
            continue
        msg_type = obj.get("type")
        if not isinstance(msg_type, str):
            LOGGER.warning("Ignoring message without string 'type': %s", line[:160])
            continue
        yield obj


@dataclass(frozen=True)
class ControlCommand:
    forward: bool
    backward: bool
    left: bool
    right: bool
    speed: int

    @classmethod
    def stop(cls) -> ControlCommand:
        return cls(False, False, False, False, 0)

    @classmethod
    def from_message(cls, message: dict[str, Any]) -> ControlCommand:
        msg_type = message.get("type")
        if msg_type == "stop":
            return cls.stop()
        if msg_type != "control":
            raise ProtocolError(f"not a control message: {msg_type!r}")

        def flag(name: str) -> bool:
            value = message.get(name, False)
            return bool(value)

        speed_raw = message.get("speed", 0)
        try:
            speed = int(speed_raw)
        except (TypeError, ValueError) as exc:
            raise ProtocolError("speed must be an integer") from exc
        speed = max(0, min(100, speed))
        return cls(
            forward=flag("forward"),
            backward=flag("backward"),
            left=flag("left"),
            right=flag("right"),
            speed=speed,
        )

    def resolved(self) -> ControlCommand:
        """Cancel conflicting flags. STOP-equivalent if speed is 0."""
        forward = self.forward
        backward = self.backward
        left = self.left
        right = self.right
        if forward and backward:
            forward = False
            backward = False
        if left and right:
            left = False
            right = False
        speed = 0 if not (forward or backward or left or right) else self.speed
        return ControlCommand(forward, backward, left, right, speed)

    def motion_name(self) -> str:
        cmd = self.resolved()
        if cmd.speed <= 0 or not (cmd.forward or cmd.backward or cmd.left or cmd.right):
            return "STOP"
        if cmd.forward and cmd.left:
            return "FORWARD_LEFT"
        if cmd.forward and cmd.right:
            return "FORWARD_RIGHT"
        if cmd.backward and cmd.left:
            return "BACKWARD_LEFT"
        if cmd.backward and cmd.right:
            return "BACKWARD_RIGHT"
        if cmd.forward:
            return "FORWARD"
        if cmd.backward:
            return "BACKWARD"
        if cmd.left:
            return "LEFT"
        if cmd.right:
            return "RIGHT"
        return "STOP"
