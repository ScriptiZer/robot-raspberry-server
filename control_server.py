"""Single-client TCP control server (newline-delimited JSON)."""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Optional

from config import AppConfig
from motor_controller import MotorController
from protocol import (
    ControlCommand,
    ProtocolError,
    encode_message,
    iter_json_lines,
)
from video_streamer import VideoStreamer
from watchdog import MotorWatchdog

LOGGER = logging.getLogger("control")

RECV_SIZE = 4096


class ControlServer:
    def __init__(
        self,
        config: AppConfig,
        motors: MotorController,
        watchdog: MotorWatchdog,
        video: VideoStreamer,
    ) -> None:
        self._config = config
        self._motors = motors
        self._watchdog = watchdog
        self._video = video
        self._stop = threading.Event()
        self._client_lock = threading.Lock()
        self._active_addr: Optional[str] = None
        self._sock: socket.socket | None = None

    def serve_forever(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self._config.control_host, self._config.control_port))
        sock.listen(5)
        sock.settimeout(1.0)
        self._sock = sock
        LOGGER.info(
            "Control server listening on %s:%s",
            self._config.control_host,
            self._config.control_port,
        )
        if not self._config.control_token:
            LOGGER.warning(
                "CONTROL_TOKEN is empty; authentication is disabled "
                "(acceptable only on a trusted private LAN)"
            )
        try:
            while not self._stop.is_set():
                try:
                    conn, addr = sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._stop.is_set():
                        break
                    raise
                threading.Thread(
                    target=self._handle_client,
                    args=(conn, addr),
                    name=f"client-{addr[0]}:{addr[1]}",
                    daemon=True,
                ).start()
        finally:
            self._close_socket()

    def shutdown(self) -> None:
        self._stop.set()
        self._close_socket()

    def _handle_client(self, conn: socket.socket, addr: tuple[str, int]) -> None:
        peer = f"{addr[0]}:{addr[1]}"
        with self._client_lock:
            if self._active_addr is not None:
                LOGGER.warning("Rejecting extra client %s (busy)", peer)
                _send(conn, {"type": "busy", "message": "another client is already connected"})
                conn.close()
                return
            self._active_addr = peer

        LOGGER.info("Client connected: %s", peer)
        authenticated = not bool(self._config.control_token)
        buffer = bytearray()
        try:
            conn.settimeout(1.0)
            if self._config.control_token:
                authenticated = self._authenticate(conn, buffer, peer)
                if not authenticated:
                    return
            self._video.set_target_ip(addr[0])
            self._command_loop(conn, buffer, peer, authenticated)
        except Exception:
            LOGGER.exception("Client handler error (%s)", peer)
        finally:
            try:
                conn.close()
            except OSError:
                pass
            self._motors.stop(reason="client-disconnect")
            self._watchdog.reset_idle()
            with self._client_lock:
                if self._active_addr == peer:
                    self._active_addr = None
            LOGGER.info("Client disconnected: %s", peer)

    def _authenticate(self, conn: socket.socket, buffer: bytearray, peer: str) -> bool:
        deadline = time.monotonic() + self._config.auth_timeout
        while time.monotonic() < deadline and not self._stop.is_set():
            try:
                conn.settimeout(min(1.0, max(0.05, deadline - time.monotonic())))
                chunk = conn.recv(RECV_SIZE)
            except socket.timeout:
                continue
            if not chunk:
                LOGGER.warning("Client %s closed during auth", peer)
                return False
            buffer.extend(chunk)
            try:
                for message in iter_json_lines(buffer):
                    if message.get("type") != "auth":
                        LOGGER.warning("Client %s sent %s before auth", peer, message.get("type"))
                        _send(conn, {"type": "auth_error", "message": "authentication required"})
                        return False
                    token = str(message.get("token", ""))
                    if token != self._config.control_token:
                        LOGGER.warning("Client %s failed authentication", peer)
                        _send(conn, {"type": "auth_error", "message": "invalid token"})
                        return False
                    LOGGER.info("Client %s authenticated", peer)
                    _send(conn, {"type": "auth_ok"})
                    return True
            except ProtocolError as exc:
                LOGGER.warning("Auth parse error from %s: %s", peer, exc)
                _send(conn, {"type": "auth_error", "message": "malformed auth"})
                return False
        LOGGER.warning("Client %s authentication timed out", peer)
        _send(conn, {"type": "auth_error", "message": "authentication timeout"})
        return False

    def _command_loop(
        self,
        conn: socket.socket,
        buffer: bytearray,
        peer: str,
        authenticated: bool,
    ) -> None:
        if not authenticated:
            return
        while not self._stop.is_set():
            try:
                chunk = conn.recv(RECV_SIZE)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buffer.extend(chunk)
            try:
                messages = list(iter_json_lines(buffer))
            except ProtocolError as exc:
                LOGGER.warning("Malformed command from %s: %s", peer, exc)
                _send(conn, {"type": "error", "message": str(exc)})
                continue
            for message in messages:
                self._dispatch(message, peer)

    def _dispatch(self, message: dict, peer: str) -> None:
        msg_type = message.get("type")
        if msg_type == "heartbeat":
            LOGGER.debug("Heartbeat from %s", peer)
            return
        if msg_type == "auth":
            LOGGER.debug("Ignoring extra auth from %s", peer)
            return
        if msg_type not in {"control", "stop"}:
            LOGGER.warning("Unknown message type from %s: %s", peer, msg_type)
            return
        try:
            command = ControlCommand.from_message(message)
        except ProtocolError as exc:
            LOGGER.warning("Invalid control from %s: %s", peer, exc)
            return
        self._watchdog.ping()
        try:
            label = self._motors.apply_command(command)
        except Exception:
            LOGGER.exception("Motor command failed from %s; forcing STOP", peer)
            self._motors.stop(reason="exception")
            return
        LOGGER.debug("Command from %s: %s", peer, label)

    def _close_socket(self) -> None:
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _send(conn: socket.socket, payload: dict) -> None:
    try:
        conn.sendall(encode_message(payload))
    except OSError:
        pass
