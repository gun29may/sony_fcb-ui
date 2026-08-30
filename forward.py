"""Forward the live camera stream onto the local network with ffmpeg.

Frames are taken from the running VideoSource and piped into ffmpeg as MJPEG,
so the V4L2 device stays open exactly once - the GUI and the network feed both
come from the same capture.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Output targets we are willing to hand to ffmpeg. Restricted on purpose: the
# GUI may be reachable from the whole LAN, and this spawns a process.
ALLOWED_SCHEMES = {"udp", "rtp", "rtsp", "srt"}

CODECS = {
    "mpeg4": ["-c:v", "mpeg4", "-pix_fmt", "yuv420p"],   # small, VLC-friendly
    "mjpeg": ["-c:v", "mjpeg", "-huffman", "optimal"],   # bigger, no re-encode loss
}


class ForwardError(Exception):
    pass


def container_for(url: str) -> str:
    scheme = urlparse(url).scheme
    if scheme == "rtp":
        return "rtp"
    if scheme == "rtsp":
        return "rtsp"
    return "mpegts"          # udp:// and srt://


def validate(url: str) -> str:
    scheme = urlparse(url).scheme
    if scheme not in ALLOWED_SCHEMES:
        raise ForwardError(
            f"target must be one of {', '.join(sorted(ALLOWED_SCHEMES))}:// "
            f"(got {scheme or 'no scheme'})")
    if not urlparse(url).hostname:
        raise ForwardError("target has no host, e.g. udp://239.255.0.1:1234")
    return url


class Forwarder:
    """Pipes JPEG frames from a VideoSource into ffmpeg, out onto the network."""

    def __init__(self, source, url: str, fps: int = 25, quality: int = 80,
                 scale: float = 1.0, codec: str = "mpeg4", bitrate: str = "4M"):
        if shutil.which("ffmpeg") is None:
            raise ForwardError("ffmpeg is not installed")
        if codec not in CODECS:
            raise ForwardError(f"codec must be one of {', '.join(CODECS)}")
        self.source = source
        self.url = validate(url)
        self.fps = max(1, min(60, int(fps)))
        self.quality = max(10, min(95, int(quality)))
        self.scale = max(0.1, min(1.0, float(scale)))
        self.codec = codec
        self.bitrate = bitrate

        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._frames = 0
        self._started_at = 0.0
        self._error: str | None = None
        self._log_tail: list[str] = []

    # ---------------------------------------------------------------- ffmpeg

    def _command(self) -> list[str]:
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-f", "image2pipe", "-vcodec", "mjpeg",
            "-framerate", str(self.fps), "-i", "-",
            "-an",
            *CODECS[self.codec],
            "-b:v", self.bitrate,
            "-flush_packets", "1",
            "-f", container_for(self.url), self.url,
        ]

    def start(self) -> None:
        if self._proc is not None:
            raise ForwardError("already forwarding")
        if self.source is None or not self.source.streaming:
            raise ForwardError("no live video to forward")
        command = self._command()
        log.info("forwarding: %s", " ".join(command))
        self._proc = subprocess.Popen(command, stdin=subprocess.PIPE,
                                      stdout=subprocess.DEVNULL,
                                      stderr=subprocess.PIPE)
        self._stop.clear()
        self._frames = 0
        self._error = None
        self._log_tail = []
        self._started_at = time.monotonic()
        self._thread = threading.Thread(target=self._feed, name="forward", daemon=True)
        self._thread.start()
        self._stderr_thread = threading.Thread(target=self._drain_stderr,
                                               name="forward-log", daemon=True)
        self._stderr_thread.start()
        # Surface an immediate failure (bad address, unreachable host) rather
        # than reporting a feed that is not actually running.
        time.sleep(0.4)
        if self._proc.poll() is not None:
            error = self._error or "\n".join(self._log_tail[-3:]) or "ffmpeg exited immediately"
            self.stop()
            raise ForwardError(error)

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            self._log_tail = (self._log_tail + [line])[-12:]
            log.warning("ffmpeg: %s", line)
            if self._error is None and ("Error" in line or "Invalid" in line
                                        or "Connection refused" in line):
                self._error = line

    def _feed(self) -> None:
        seq = -1
        interval = 1.0 / self.fps
        next_frame = time.monotonic()
        try:
            while not self._stop.is_set():
                source = self.source
                if source is None or not source.streaming:
                    if self._stop.wait(0.5):
                        break
                    continue
                seq, jpeg = source.jpeg(quality=self.quality, scale=self.scale, last_seq=seq)
                if jpeg is None:
                    continue
                proc = self._proc
                if proc is None or proc.stdin is None:
                    break
                proc.stdin.write(jpeg)
                self._frames += 1
                # Pace to the target rate; drop rather than queue if we fall behind.
                next_frame += interval
                delay = next_frame - time.monotonic()
                if delay > 0:
                    self._stop.wait(delay)
                else:
                    next_frame = time.monotonic()
        except (BrokenPipeError, ValueError, OSError) as exc:
            if not self._stop.is_set():
                self._error = f"stream to ffmpeg broke: {exc}"
                log.warning("%s", self._error)

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                if proc.stdin:
                    proc.stdin.close()
                proc.terminate()
                proc.wait(timeout=3.0)
            except (subprocess.TimeoutExpired, OSError):
                proc.kill()
        self._thread = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def status(self) -> dict:
        return {
            "url": self.url,
            "running": self.running,
            "codec": self.codec,
            "fps": self.fps,
            "scale": self.scale,
            "bitrate": self.bitrate,
            "frames": self._frames,
            "uptime": round(time.monotonic() - self._started_at, 1) if self._started_at else 0,
            "error": self._error,
            "log": self._log_tail[-3:],
        }

    def player_hint(self) -> str:
        """How to open this feed from another machine."""
        parsed = urlparse(self.url)
        host = parsed.hostname or ""
        if host.startswith(("239.", "224.")):
            return f"vlc udp://@{host}:{parsed.port or 1234}"
        if parsed.scheme == "rtp":
            return f"vlc {self.url}"
        return f"vlc {self.url}"
