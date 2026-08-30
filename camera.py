"""Video capture worker: one grabber thread per device, latest-frame fanout to
any number of MJPEG clients.

The thread owns the cv2.VideoCapture end to end, so an unplugged or stalled
camera is reopened automatically without restarting the server.
"""
from __future__ import annotations

import logging
import threading
import time

import cv2

log = logging.getLogger(__name__)

STALL_TIMEOUT = 5.0        # seconds without a frame before we reopen
RETRY_INTERVAL = 2.0       # pause between reopen attempts


class CaptureError(Exception):
    pass


class VideoSource:
    """Owns a cv2.VideoCapture and republishes frames to waiting streamers."""

    def __init__(self, device: str, width: int = 1920, height: int = 1080,
                 fps: int = 30, fourcc: str = "MJPG"):
        self.device = device
        self.width = width
        self.height = height
        self.fps = fps
        self.fourcc = fourcc

        self._cap: cv2.VideoCapture | None = None
        self._frame = None
        self._seq = 0
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()      # first frame, or first hard failure
        self._error: str | None = None
        self._measured_fps = 0.0
        self._frames_seen = 0
        self._last_frame_at = 0.0
        self._reopens = 0
        # Encoded-frame cache so N viewers cost one JPEG encode, not N.
        # Keyed by (quality, scale); each entry holds a single frame.
        self._jpeg_cache: dict[tuple, tuple] = {}
        self._jpeg_lock = threading.Lock()

    # ------------------------------------------------------------ lifecycle

    def start(self, wait: float = 12.0) -> bool:
        """Start grabbing. Returns True once a first frame has arrived.

        Returning False is not fatal: the worker keeps retrying in the
        background, so a camera plugged in later still comes up on its own.
        """
        self.stop()
        self._stop.clear()
        self._ready.clear()
        self._error = None
        self._thread = threading.Thread(target=self._loop, name="capture", daemon=True)
        self._thread.start()
        self._ready.wait(wait)
        return self.streaming

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            # A stalled V4L2 read can sit in select() for ~10s before returning.
            thread.join(timeout=13.0)
        self._thread = None
        with self._condition:
            self._frame = None
            self._condition.notify_all()

    # ---------------------------------------------------------------- device

    def _open(self) -> None:
        cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
        if not cap.isOpened():
            raise CaptureError(f"cannot open {self.device} - is the camera plugged in?")
        if self.fourcc:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*self.fourcc))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)          # keep latency down
        ok, frame = cap.read()
        if not ok or frame is None:
            cap.release()
            raise CaptureError(
                f"{self.device} opened but delivered no frames - the USB link is "
                f"probably too slow for {self.width}x{self.height} {self.fourcc}")
        self.width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or self.width
        self.height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or self.height
        self._cap = cap
        self._publish(frame)
        log.info("capture open: %s %dx%d @%s", self.device, self.width, self.height, self.fourcc)

    def _publish(self, frame) -> None:
        self._frames_seen += 1
        self._last_frame_at = time.monotonic()
        with self._condition:
            self._frame = frame
            self._seq += 1
            self._condition.notify_all()
        self._ready.set()

    # ----------------------------------------------------------------- loop

    def _loop(self) -> None:
        window_start, window_count = time.monotonic(), 0
        try:
            while not self._stop.is_set():
                if self._cap is None:
                    try:
                        self._open()
                        self._error = None
                    except CaptureError as exc:
                        self._error = str(exc)
                        self._ready.set()            # unblock start()
                        log.warning("%s", exc)
                        self._stop.wait(RETRY_INTERVAL)
                        continue

                ok, frame = self._cap.read()
                if not ok or frame is None:
                    if time.monotonic() - self._last_frame_at > STALL_TIMEOUT:
                        self._error = f"no frames for {STALL_TIMEOUT:.0f}s - reopening {self.device}"
                        log.warning("%s", self._error)
                        self._cap.release()
                        self._cap = None
                        self._reopens += 1
                        self._stop.wait(RETRY_INTERVAL)
                    else:
                        self._stop.wait(0.02)
                    continue

                self._error = None
                self._publish(frame)

                window_count += 1
                now = time.monotonic()
                if now - window_start >= 1.0:
                    self._measured_fps = round(window_count / (now - window_start), 1)
                    window_start, window_count = now, 0
        finally:
            if self._cap is not None:
                self._cap.release()
                self._cap = None
            self._measured_fps = 0.0
            with self._jpeg_lock:
                self._jpeg_cache.clear()

    # ---------------------------------------------------------------- reads

    def latest(self, last_seq: int = -1, timeout: float = 2.0):
        """Block until a frame newer than last_seq arrives. Returns (seq, frame)."""
        with self._condition:
            if self._seq == last_seq:
                self._condition.wait(timeout)
            if self._frame is None:
                return -1, None
            return self._seq, self._frame

    def jpeg(self, quality: int = 80, scale: float = 1.0, last_seq: int = -1):
        seq, frame = self.latest(last_seq)
        if frame is None:
            return -1, None

        key = (int(quality), round(float(scale), 3))
        with self._jpeg_lock:
            cached = self._jpeg_cache.get(key)
            if cached is not None and cached[0] == seq:
                return seq, cached[1]          # another viewer already encoded it

        if scale and scale != 1.0:
            frame = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        if not ok:
            return -1, None
        data = buf.tobytes()

        with self._jpeg_lock:
            if len(self._jpeg_cache) > 8:      # only ever a handful of settings
                self._jpeg_cache.clear()
            self._jpeg_cache[key] = (seq, data)
        return seq, data

    @property
    def running(self) -> bool:
        """The worker thread is alive (it may still be retrying the open)."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def streaming(self) -> bool:
        """Frames actually arrived recently."""
        return self._frame is not None and (time.monotonic() - self._last_frame_at) < STALL_TIMEOUT

    def status(self) -> dict:
        return {
            "device": self.device,
            "running": self.streaming,
            "worker_alive": self.running,
            "width": self.width,
            "height": self.height,
            "requested_fps": self.fps,
            "measured_fps": self._measured_fps if self.streaming else 0.0,
            "fourcc": self.fourcc,
            "frames": self._frames_seen,
            "reopens": self._reopens,
            "error": self._error,
        }
