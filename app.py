#!/usr/bin/env python3
"""Web GUI for a Sony FCB block camera on a USB interface board.

Video comes from the board's UVC node; every camera function (zoom, focus,
exposure, white balance, image controls, presets, OSD) is driven over VISCA on
the board's USB serial port.

    python3 app.py --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
import threading
import time

import cv2
from flask import Flask, Response, g, jsonify, request, send_from_directory

import devices
import forward
import network
from camera import VideoSource
from forward import ForwardError, Forwarder
from visca import Visca, ViscaError

log = logging.getLogger("fcb")

HERE = os.path.dirname(os.path.abspath(__file__))
CAPTURE_DIR = os.path.join(HERE, "captures")

# A continuous zoom/focus move is stopped automatically if the browser goes
# away without sending its stop command.
MOTION_TIMEOUT = 2.5

# Give up on an MJPEG response that has had no frame for this long, so idle
# requests release their worker thread instead of spinning forever.
STREAM_IDLE_TIMEOUT = 20.0


class Controller:
    """Holds the video source, the VISCA link and the cached camera state."""

    def __init__(self):
        self.video: VideoSource | None = None
        self.visca: Visca | None = None
        self.camera_info: dict = {}
        self.state: dict = {}
        self.state_error: str | None = None
        self.diagnostics: list[str] = []
        self.forwarder: Forwarder | None = None
        self.last_error: str | None = None
        self._lock = threading.Lock()
        self._poller: threading.Thread | None = None
        self._stop_poll = threading.Event()
        self._motion_deadline = 0.0
        self._motion_axis: str | None = None
        self._recorder: cv2.VideoWriter | None = None
        self._record_path: str | None = None
        self._record_thread: threading.Thread | None = None
        self._stop_record = threading.Event()

    # ----------------------------------------------------------- connection

    def connect(self, opts: dict) -> dict:
        with self._lock:
            self.disconnect_locked()
            found = devices.autodetect()
            self.diagnostics = found["warnings"]
            result = {"video": None, "visca": None, "warnings": list(self.diagnostics)}

            video_path = opts.get("video")
            if video_path:
                source = VideoSource(
                    video_path,
                    width=int(opts.get("width", 1920)),
                    height=int(opts.get("height", 1080)),
                    fps=int(opts.get("fps", 30)),
                    fourcc=opts.get("fourcc", "MJPG"),
                )
                self.video = source
                if not source.start():
                    # Not fatal: the worker keeps retrying, so the stream comes
                    # up by itself once the link or the camera is happy.
                    result["warnings"].append(
                        "video: " + (source.status()["error"] or "no frames yet"))
                result["video"] = source.status()

            serial_path = opts.get("serial")
            if serial_path:
                link = Visca(serial_path, baud=int(opts.get("baud", 9600)),
                             address=int(opts.get("address", 1)))
                try:
                    link.open(autobaud=bool(opts.get("autobaud", True)))
                    self.visca = link
                    self.camera_info = link.identify()
                    self.camera_info["port"] = serial_path
                    self.camera_info["baud"] = link.baud
                    result["visca"] = self.camera_info
                    self._start_poller()
                except (ViscaError, OSError) as exc:
                    result["warnings"].append(f"visca: {exc}")

            if not self.video and not self.visca:
                raise RuntimeError("; ".join(result["warnings"]) or "nothing to connect")
            return result

    def disconnect(self) -> None:
        with self._lock:
            self.disconnect_locked()

    def disconnect_locked(self) -> None:
        self.stop_forward()
        self.stop_recording()
        self._stop_poll.set()
        if self._poller and self._poller.is_alive():
            self._poller.join(timeout=2.0)
        self._poller = None
        if self.video:
            self.video.stop()
            self.video = None
        if self.visca:
            try:
                self.visca.zoom_stop()
                self.visca.focus_stop()
            except Exception:
                pass
            self.visca.close()
            self.visca = None
        self.camera_info = {}
        self.state = {}

    # -------------------------------------------------------- state polling

    def _start_poller(self) -> None:
        # Each poller gets its own stop flag and its own link reference, so a
        # reconnect can never revive the previous poller onto the new port.
        stop = threading.Event()
        self._stop_poll = stop
        link = self.visca
        self._poller = threading.Thread(target=self._poll_loop, args=(stop, link),
                                        name="visca-poll", daemon=True)
        self._poller.start()

    def _poll_loop(self, stop: threading.Event, link: Visca) -> None:
        while not stop.is_set():
            if link is None or not link.connected or link is not self.visca:
                break
            # Stop a move whose browser never sent the stop command.
            if self._motion_axis and time.monotonic() > self._motion_deadline:
                axis, self._motion_axis = self._motion_axis, None
                try:
                    link.zoom_stop() if axis == "zoom" else link.focus_stop()
                except ViscaError:
                    pass
            try:
                self.state = link.snapshot_state()
                self.state_error = None
            except Exception as exc:
                self.state_error = str(exc)
            stop.wait(1.0)

    def note_motion(self, axis: str | None) -> None:
        self._motion_axis = axis
        self._motion_deadline = time.monotonic() + MOTION_TIMEOUT

    # ------------------------------------------------------------ recording

    def start_recording(self) -> str:
        if self.video is None or not self.video.running:
            raise RuntimeError("no video stream to record")
        if self._recorder is not None:
            raise RuntimeError("already recording")
        os.makedirs(CAPTURE_DIR, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(CAPTURE_DIR, f"fcb-{stamp}.avi")
        fps = self.video.status()["measured_fps"] or self.video.fps or 30
        writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"MJPG"), float(fps),
                                 (self.video.width, self.video.height))
        if not writer.isOpened():
            raise RuntimeError("could not open the video writer")
        self._recorder = writer
        self._record_path = path
        self._stop_record.clear()
        self._record_thread = threading.Thread(target=self._record_loop, name="record", daemon=True)
        self._record_thread.start()
        return path

    def _record_loop(self) -> None:
        seq = -1
        while not self._stop_record.is_set() and self.video is not None:
            seq, frame = self.video.latest(seq, timeout=1.0)
            if frame is not None and self._recorder is not None:
                self._recorder.write(frame)

    def stop_recording(self) -> str | None:
        if self._recorder is None:
            return None
        self._stop_record.set()
        if self._record_thread and self._record_thread.is_alive():
            self._record_thread.join(timeout=3.0)
        self._recorder.release()
        self._recorder = None
        path, self._record_path = self._record_path, None
        return path

    # ------------------------------------------------------------ forwarding

    def start_forward(self, opts: dict) -> dict:
        self.stop_forward()
        sender = Forwarder(
            self.video,
            url=opts.get("url", ""),
            fps=int(opts.get("fps", 25)),
            quality=int(opts.get("quality", 80)),
            scale=float(opts.get("scale", 1.0)),
            codec=opts.get("codec", "mpeg4"),
            bitrate=opts.get("bitrate", "4M"),
        )
        sender.start()
        self.forwarder = sender
        return sender.status()

    def stop_forward(self) -> dict | None:
        sender, self.forwarder = self.forwarder, None
        if sender is None:
            return None
        status = sender.status()
        sender.stop()
        return status

    def status(self) -> dict:
        return {
            "video": self.video.status() if self.video else None,
            "forward": self.forwarder.status() if self.forwarder else None,
            "visca": {**self.camera_info, "connected": bool(self.visca and self.visca.connected)}
                     if self.camera_info else {"connected": False},
            "state": self.state,
            "state_error": self.state_error,
            "diagnostics": self.diagnostics,
            "recording": self._record_path,
        }


ctl = Controller()
app = Flask(__name__, static_folder=None)

# Set by --token. When present every request must carry it, so putting the GUI
# on the LAN does not hand camera control to everyone on the network.
AUTH_TOKEN: str | None = None
BIND_HOST = "127.0.0.1"
BIND_PORT = 8080


@app.before_request
def _require_token():
    if not AUTH_TOKEN:
        return None
    supplied = (request.args.get("token")
                or request.headers.get("X-Auth-Token")
                or request.cookies.get("fcb_token"))
    if supplied == AUTH_TOKEN:
        # Remember it so the browser does not need ?token= on every asset.
        g.set_token_cookie = request.args.get("token") == AUTH_TOKEN
        return None
    return Response("unauthorized - append ?token=... to the URL\n", 401,
                    {"Content-Type": "text/plain"})


@app.after_request
def _persist_token(response):
    if getattr(g, "set_token_cookie", False):
        response.set_cookie("fcb_token", AUTH_TOKEN, httponly=True, samesite="Lax")
    return response


# ---------------------------------------------------------------- VISCA API

def _int(payload, key, default=0):
    try:
        return int(payload.get(key, default))
    except (TypeError, ValueError):
        return default


def _bool(payload, key, default=False):
    value = payload.get(key, default)
    if isinstance(value, str):
        return value.lower() in ("1", "true", "on", "yes")
    return bool(value)


ACTIONS = {
    # zoom
    "zoom_tele":      lambda c, p: (c.note_motion("zoom"), c.visca.zoom_tele(_int(p, "speed", 4)))[1],
    "zoom_wide":      lambda c, p: (c.note_motion("zoom"), c.visca.zoom_wide(_int(p, "speed", 4)))[1],
    "zoom_stop":      lambda c, p: (c.note_motion(None), c.visca.zoom_stop())[1],
    "zoom_to":        lambda c, p: c.visca.zoom_to(_int(p, "value")),
    "digital_zoom":   lambda c, p: c.visca.digital_zoom(_bool(p, "on")),
    # focus
    "focus_far":      lambda c, p: (c.note_motion("focus"), c.visca.focus_far(_int(p, "speed", 4)))[1],
    "focus_near":     lambda c, p: (c.note_motion("focus"), c.visca.focus_near(_int(p, "speed", 4)))[1],
    "focus_stop":     lambda c, p: (c.note_motion(None), c.visca.focus_stop())[1],
    "focus_to":       lambda c, p: c.visca.focus_to(_int(p, "value")),
    "focus_auto":     lambda c, p: c.visca.focus_auto(_bool(p, "on")),
    "focus_one_push": lambda c, p: c.visca.focus_one_push(),
    "af_mode":        lambda c, p: c.visca.af_mode(_int(p, "value")),
    # exposure
    "ae_mode":        lambda c, p: c.visca.ae_mode(p.get("value", "auto")),
    "shutter":        lambda c, p: c.visca.shutter(_int(p, "value")),
    "iris":           lambda c, p: c.visca.iris(_int(p, "value")),
    "gain":           lambda c, p: c.visca.gain(_int(p, "value")),
    "gain_limit":     lambda c, p: c.visca.gain_limit(_int(p, "value")),
    "bright":         lambda c, p: c.visca.bright(_int(p, "value")),
    "exp_comp":       lambda c, p: c.visca.exp_comp(_bool(p, "on")),
    "exp_comp_level": lambda c, p: c.visca.exp_comp_level(_int(p, "value")),
    "backlight":      lambda c, p: c.visca.backlight(_bool(p, "on")),
    "wide_dynamic":   lambda c, p: c.visca.wide_dynamic(_bool(p, "on")),
    "slow_shutter":   lambda c, p: c.visca.slow_shutter_auto(_bool(p, "on")),
    # white balance
    "wb_mode":        lambda c, p: c.visca.wb_mode(p.get("value", "auto")),
    "wb_trigger":     lambda c, p: c.visca.wb_one_push_trigger(),
    "r_gain":         lambda c, p: c.visca.r_gain(_int(p, "value")),
    "b_gain":         lambda c, p: c.visca.b_gain(_int(p, "value")),
    # image
    "aperture":       lambda c, p: c.visca.aperture(_int(p, "value")),
    "noise_reduction": lambda c, p: c.visca.noise_reduction(_int(p, "value")),
    "gamma":          lambda c, p: c.visca.gamma(_int(p, "value")),
    "chroma_suppress": lambda c, p: c.visca.chroma_suppress(_int(p, "value")),
    "defog":          lambda c, p: c.visca.defog(_bool(p, "on")),
    "picture_effect": lambda c, p: c.visca.picture_effect(p.get("value", "off")),
    "mirror":         lambda c, p: c.visca.mirror(_bool(p, "on")),
    "flip":           lambda c, p: c.visca.flip(_bool(p, "on")),
    "freeze":         lambda c, p: c.visca.freeze(_bool(p, "on")),
    "ir_cut":         lambda c, p: c.visca.ir_cut(_bool(p, "on")),
    "auto_ir_cut":    lambda c, p: c.visca.auto_ir_cut(_bool(p, "on")),
    "stabilizer":     lambda c, p: c.visca.stabilizer(_bool(p, "on")),
    "high_sensitivity": lambda c, p: c.visca.high_sensitivity(_bool(p, "on")),
    # presets / system
    "preset_set":     lambda c, p: c.visca.preset_set(_int(p, "slot")),
    "preset_recall":  lambda c, p: c.visca.preset_recall(_int(p, "slot")),
    "preset_reset":   lambda c, p: c.visca.preset_reset(_int(p, "slot")),
    "menu":           lambda c, p: c.visca.menu(_bool(p, "on")),
    "menu_nav":       lambda c, p: c.visca.menu_nav(p.get("value", "enter")),
    "power":          lambda c, p: c.visca.power(_bool(p, "on")),
    "if_clear":       lambda c, p: c.visca.if_clear(),
    "raw":            lambda c, p: c.visca.raw(p.get("value", "")),
}


@app.post("/api/visca/<action>")
def api_visca(action: str):
    if action not in ACTIONS:
        return jsonify(ok=False, error=f"unknown action '{action}'"), 404
    if ctl.visca is None or not ctl.visca.connected:
        return jsonify(ok=False, error="VISCA not connected"), 409
    payload = request.get_json(silent=True) or request.args.to_dict()
    try:
        result = ACTIONS[action](ctl, payload)
    except ViscaError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    except (KeyError, ValueError) as exc:
        return jsonify(ok=False, error=f"bad argument: {exc}"), 400
    except Exception as exc:                          # serial dropped, etc.
        log.exception("visca action failed")
        return jsonify(ok=False, error=str(exc)), 500
    return jsonify(ok=True, result=result)


# ------------------------------------------------------------- device setup

@app.get("/api/devices")
def api_devices():
    return jsonify(devices.autodetect())


@app.post("/api/connect")
def api_connect():
    payload = request.get_json(silent=True) or {}
    if not payload.get("video") and not payload.get("serial"):
        found = devices.autodetect()
        payload.setdefault("video", found["video"])
        payload.setdefault("serial", found["serial"])
    if payload.get("video") and not payload.get("fourcc"):
        payload.update(devices.best_mode(payload["video"]))
    try:
        return jsonify(ok=True, **ctl.connect(payload))
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 400


@app.post("/api/disconnect")
def api_disconnect():
    ctl.disconnect()
    return jsonify(ok=True)


@app.get("/api/status")
def api_status():
    return jsonify(ctl.status())


# ------------------------------------------------------------------- video

@app.get("/stream.mjpg")
def stream():
    quality = max(10, min(95, int(request.args.get("quality", 80))))
    scale = max(0.1, min(1.0, float(request.args.get("scale", 1.0))))

    def generate():
        seq = -1
        idle_since = time.monotonic()
        while True:
            # End the response after a spell with no frames, so a dead or
            # never-started stream cannot pin a worker thread indefinitely.
            if time.monotonic() - idle_since > STREAM_IDLE_TIMEOUT:
                return
            source = ctl.video
            if source is None or not source.streaming:
                time.sleep(0.3)
                continue
            seq, jpeg = source.jpeg(quality=quality, scale=scale, last_seq=seq)
            if jpeg is None:
                time.sleep(0.05)
                continue
            idle_since = time.monotonic()
            yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                   + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")

    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame",
                    headers={"Cache-Control": "no-store, no-cache", "Pragma": "no-cache"})


@app.get("/snapshot.jpg")
def snapshot():
    if ctl.video is None:
        return "no video", 409
    _, jpeg = ctl.video.jpeg(quality=95)
    if jpeg is None:
        return "no frame", 503
    return Response(jpeg, mimetype="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.post("/api/capture")
def api_capture():
    if ctl.video is None:
        return jsonify(ok=False, error="no video"), 409
    _, jpeg = ctl.video.jpeg(quality=95)
    if jpeg is None:
        return jsonify(ok=False, error="no frame"), 503
    os.makedirs(CAPTURE_DIR, exist_ok=True)
    name = dt.datetime.now().strftime("fcb-%Y%m%d-%H%M%S.jpg")
    path = os.path.join(CAPTURE_DIR, name)
    with open(path, "wb") as fh:
        fh.write(jpeg)
    return jsonify(ok=True, path=path)


@app.get("/api/network")
def api_network():
    return jsonify(network.summary(BIND_PORT, BIND_HOST, AUTH_TOKEN))


@app.post("/api/forward/<verb>")
def api_forward(verb: str):
    try:
        if verb == "start":
            payload = request.get_json(silent=True) or {}
            return jsonify(ok=True, forward=ctl.start_forward(payload))
        if verb == "stop":
            return jsonify(ok=True, forward=ctl.stop_forward())
    except ForwardError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    except Exception as exc:
        log.exception("forward failed")
        return jsonify(ok=False, error=str(exc)), 500
    return jsonify(ok=False, error="use start or stop"), 404


@app.post("/api/record/<verb>")
def api_record(verb: str):
    try:
        if verb == "start":
            return jsonify(ok=True, path=ctl.start_recording())
        if verb == "stop":
            return jsonify(ok=True, path=ctl.stop_recording())
    except RuntimeError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    return jsonify(ok=False, error="use start or stop"), 404


# -------------------------------------------------------------------- pages

@app.get("/")
def index():
    return send_from_directory(os.path.join(HERE, "static"), "index.html")


@app.get("/static/<path:filename>")
def static_files(filename: str):
    return send_from_directory(os.path.join(HERE, "static"), filename)


def main():
    parser = argparse.ArgumentParser(description="Sony FCB camera web GUI")
    parser.add_argument("--host", default="127.0.0.1",
                        help="address to bind (default: localhost only)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--lan", action="store_true",
                        help="serve to the whole local network (binds 0.0.0.0)")
    parser.add_argument("--token", help="require ?token=... from every client; "
                                        "strongly recommended with --lan")
    parser.add_argument("--forward", metavar="URL",
                        help="also push the stream to the network on start, "
                             "e.g. udp://239.255.0.1:1234")
    parser.add_argument("--forward-fps", type=int, default=25)
    parser.add_argument("--forward-codec", default="mpeg4", choices=sorted(forward.CODECS))
    parser.add_argument("--forward-bitrate", default="4M")
    parser.add_argument("--video", help="force a video node, e.g. /dev/video3")
    parser.add_argument("--serial", help="force a VISCA port, e.g. /dev/ttyACM0")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--fourcc", default="MJPG")
    parser.add_argument("--baud", type=int, default=9600)
    parser.add_argument("--no-autoconnect", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    global AUTH_TOKEN, BIND_HOST, BIND_PORT
    host = "0.0.0.0" if args.lan else args.host
    AUTH_TOKEN = args.token
    BIND_HOST, BIND_PORT = host, args.port

    if not args.no_autoconnect:
        found = devices.autodetect()
        video = args.video or found["video"]
        opts = {
            "video": video,
            "serial": args.serial or found["serial"],
            "width": args.width, "height": args.height, "fps": args.fps,
            "fourcc": args.fourcc, "baud": args.baud,
        }
        # Unless the user pinned a mode, use whatever the device advertises.
        if video and not any(a in " ".join(sys.argv) for a in
                             ("--fourcc", "--width", "--height", "--fps")):
            opts.update(devices.best_mode(video))
            log.info("auto mode: %(width)dx%(height)d %(fourcc)s @%(fps)d fps", opts)
        if opts["video"] or opts["serial"]:
            try:
                result = ctl.connect(opts)
                for warning in result["warnings"]:
                    log.warning(warning)
            except Exception as exc:
                log.warning("autoconnect failed: %s", exc)
        else:
            log.warning("no camera found yet - plug it in and press Connect in the GUI")

    if args.forward:
        try:
            status = ctl.start_forward({
                "url": args.forward, "fps": args.forward_fps,
                "codec": args.forward_codec, "bitrate": args.forward_bitrate,
            })
            log.info("forwarding to %s", status["url"])
        except ForwardError as exc:
            log.warning("forward not started: %s", exc)

    _print_access_banner(host, args.port)
    app.run(host=host, port=args.port, threaded=True, debug=False, use_reloader=False)


def _print_access_banner(host: str, port: int) -> None:
    """Tell the user every address this server can be reached on."""
    info = network.summary(port, host, AUTH_TOKEN)
    log.info("GUI ready on %s", info["local"]["gui"])
    if not info["shared"]:
        log.info("This machine only. Use --lan to share it on the local network.")
        return
    for entry in info["urls"]:
        log.info("  LAN (%s): %s", entry["interface"], entry["gui"])
        log.info("       stream: %s", entry["stream"])
    if not AUTH_TOKEN:
        log.warning("Serving to the whole local network with no token - anyone "
                    "who can reach this port can drive the camera. Add "
                    "--token SECRET to require one.")


if __name__ == "__main__":
    main()
