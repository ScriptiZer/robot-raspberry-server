"""Low-latency H.264 MPEG-TS video stream over UDP.

Runs in its own thread with an FFmpeg subprocess so a camera/encoder
failure cannot block motor control. The process is restarted automatically.

Encoders are tried in order. Being *listed* by FFmpeg (for example
``h264_v4l2m2m`` on a laptop) does not mean the hardware exists; a failed
pipeline rotates to the next encoder, typically ``libx264``.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from collections import deque

from config import AppConfig

LOGGER = logging.getLogger("video")

ENCODER_PREFERENCE = ("h264_v4l2m2m", "h264_omx", "libx264")


class VideoStreamer:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._target_ip = config.laptop_ip.strip()
        self._stop = threading.Event()
        self._changed = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._encoders: list[str] = []
        self._encoder_index = 0
        self._prefer_mjpeg = True
        self._ffmpeg_log: deque[str] = deque(maxlen=20)

    @property
    def _encoder(self) -> str:
        if not self._encoders:
            return "libx264"
        return self._encoders[self._encoder_index % len(self._encoders)]

    def start(self) -> None:
        if not self._config.video_enabled:
            LOGGER.info("Video streaming disabled (VIDEO_ENABLED=false)")
            return
        if shutil.which("ffmpeg") is None:
            LOGGER.error("ffmpeg not found on PATH; video streaming disabled")
            return
        self._encoders = _encoder_candidates()
        self._encoder_index = 0
        LOGGER.info("Video encoder candidates (first working wins): %s", ", ".join(self._encoders))
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="video-streamer", daemon=True)
        self._thread.start()

    def set_target_ip(self, ip: str) -> None:
        if not self._config.video_enabled or not self._config.video_follow_client:
            return
        ip = _normalize_ip(ip)
        with self._lock:
            if ip == self._target_ip:
                return
            self._target_ip = ip
        LOGGER.info("Video destination set to %s:%s", ip, self._config.video_port)
        self._changed.set()
        self._kill_process()

    def shutdown(self) -> None:
        self._stop.set()
        self._changed.set()
        self._kill_process()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                target = self._target_ip
            if not target:
                LOGGER.info("Video waiting for laptop IP (set LAPTOP_IP or connect a client)")
                self._changed.wait(timeout=1.0)
                self._changed.clear()
                continue
            encoder = self._encoder
            kind = "software CPU" if encoder == "libx264" else "hardware (if the device exists)"
            LOGGER.info("Starting video with encoder %s (%s)", encoder, kind)
            started = time.monotonic()
            try:
                self._spawn(target, encoder, prefer_mjpeg=self._prefer_mjpeg)
                assert self._proc is not None
                returncode = self._proc.wait()
                if self._stop.is_set():
                    break
                elapsed = time.monotonic() - started
                self._log_ffmpeg_failure(returncode)
                if elapsed < 4.0:
                    self._prefer_mjpeg = not self._prefer_mjpeg
                    if not self._prefer_mjpeg:
                        self._advance_encoder()
                    LOGGER.info(
                        "Retrying with encoder=%s mjpeg_input=%s",
                        self._encoder,
                        self._prefer_mjpeg,
                    )
            except Exception:
                LOGGER.exception("Video streamer error")
                self._advance_encoder()
            self._kill_process()
            if self._stop.wait(self._config.video_restart_delay):
                break

    def _advance_encoder(self) -> None:
        if len(self._encoders) <= 1:
            return
        previous = self._encoder
        self._encoder_index = (self._encoder_index + 1) % len(self._encoders)
        LOGGER.warning(
            "Encoder %s failed; switching to %s (common on laptops that list h264_v4l2m2m without Pi hardware)",
            previous,
            self._encoder,
        )

    def _spawn(self, target_ip: str, encoder: str, prefer_mjpeg: bool) -> None:
        cmd = self._build_command(target_ip, encoder, prefer_mjpeg=prefer_mjpeg)
        LOGGER.info("Starting video pipeline: %s", " ".join(cmd))
        self._ffmpeg_log.clear()
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        stderr = self._proc.stderr
        if stderr is not None:
            threading.Thread(
                target=self._drain_stderr,
                args=(stderr,),
                name="ffmpeg-stderr",
                daemon=True,
            ).start()

    def _build_command(self, target_ip: str, encoder: str, prefer_mjpeg: bool) -> list[str]:
        cfg = self._config
        input_args: list[str] = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-f",
            "v4l2",
        ]
        if prefer_mjpeg:
            input_args += ["-input_format", "mjpeg"]
        input_args += [
            "-video_size",
            f"{cfg.video_width}x{cfg.video_height}",
            "-framerate",
            str(cfg.video_fps),
            "-i",
            cfg.camera_device,
        ]
        encode_args = _encoder_args(encoder, cfg.video_bitrate, cfg.video_fps)
        output_args = [
            "-an",
            "-vf",
            f"scale={cfg.video_width}:{cfg.video_height}",
            "-r",
            str(cfg.video_fps),
            *encode_args,
            "-flush_packets",
            "1",
            "-muxdelay",
            "0",
            "-muxpreload",
            "0",
            "-f",
            "mpegts",
            f"udp://{target_ip}:{cfg.video_port}?pkt_size=1316",
        ]
        return input_args + output_args

    def _drain_stderr(self, stream: object) -> None:
        try:
            for raw in iter(stream.readline, b""):  # type: ignore[attr-defined]
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                self._ffmpeg_log.append(line)
                LOGGER.warning("ffmpeg: %s", line)
        except Exception:
            return

    def _log_ffmpeg_failure(self, returncode: int | None) -> None:
        recent = list(self._ffmpeg_log)
        if recent:
            LOGGER.warning("FFmpeg exited with code %s", returncode)
        else:
            LOGGER.warning("FFmpeg exited with code %s (no stderr)", returncode)

    def _kill_process(self) -> None:
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1.0)


def _encoder_args(encoder: str, bitrate: str, fps: int) -> list[str]:
    gop = max(1, fps // 2)
    if encoder == "libx264":
        return [
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-profile:v",
            "baseline",
            "-pix_fmt",
            "yuv420p",
            "-g",
            str(gop),
            "-bf",
            "0",
            "-b:v",
            bitrate,
        ]
    return [
        "-c:v",
        encoder,
        "-b:v",
        bitrate,
        "-pix_fmt",
        "yuv420p",
        "-g",
        str(gop),
        "-bf",
        "0",
    ]


def _encoder_candidates() -> list[str]:
    available = _ffmpeg_encoders()
    chosen = [name for name in ENCODER_PREFERENCE if name in available]
    if "libx264" not in chosen:
        chosen.append("libx264")
    if not chosen:
        chosen = ["libx264"]
    for name in chosen:
        kind = "hardware-listed" if name != "libx264" else "software CPU"
        LOGGER.info("FFmpeg reports encoder %s (%s)", name, kind)
    return chosen


def _ffmpeg_encoders() -> set[str]:
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return set()
    found: set[str] = set()
    for line in (result.stdout or "").splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[1] in ENCODER_PREFERENCE:
            found.add(parts[1])
    return found


def _normalize_ip(ip: str) -> str:
    ip = ip.strip()
    if ip.startswith("::ffff:"):
        return ip[7:]
    return ip
