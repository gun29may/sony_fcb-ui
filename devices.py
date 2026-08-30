"""Discovery of the FCB video node and VISCA serial port, plus V4L2 format
enumeration done with raw ioctls so v4l2-ctl is not required.
"""
from __future__ import annotations

import fcntl
import glob
import os
import struct

# USB ids of interface boards known to carry a Sony FCB block.
KNOWN_BOARDS = {
    ("04b4", "00f9"): "Twiga USB3 NeoHD",
    ("04b4", "00f8"): "Twiga USB3 Neo",
    ("1bcf", "2c99"): "Generic UVC capture",
}
# Substrings that identify an FCB carrier by its V4L2 device name.
CAMERA_NAME_HINTS = ("neohd", "neo", "twiga", "fcb", "visca", "uvc camera", "capture", "hd usb")
# Devices that are never the FCB (laptop webcams, virtual loopbacks).
CAMERA_NAME_EXCLUDE = ("integrated camera", "iriun", "v4l2 loopback", "obs virtual", "dummy")


def _ioc(direction: int, typ: str, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord(typ) << 8) | nr


_IOWR = 3
# struct v4l2_fmtdesc / frmsizeenum / frmivalenum
_FMTDESC = "=III32sI4I"
_FRMSIZE = "=III6I2I"
_FRMIVAL = "=IIIII6I2I"
VIDIOC_ENUM_FMT = _ioc(_IOWR, "V", 2, struct.calcsize(_FMTDESC))
VIDIOC_ENUM_FRAMESIZES = _ioc(_IOWR, "V", 74, struct.calcsize(_FRMSIZE))
VIDIOC_ENUM_FRAMEINTERVALS = _ioc(_IOWR, "V", 75, struct.calcsize(_FRMIVAL))
V4L2_BUF_TYPE_VIDEO_CAPTURE = 1
V4L2_FRMSIZE_TYPE_DISCRETE = 1
V4L2_FRMIVAL_TYPE_DISCRETE = 1


def fourcc_str(value: int) -> str:
    return "".join(chr((value >> (8 * i)) & 0xFF) for i in range(4)).strip()


def _sysfs_name(node: str) -> str:
    path = f"/sys/class/video4linux/{os.path.basename(node)}/name"
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _read(path: str) -> str:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _usb_info_for(sysfs_device: str) -> dict | None:
    """Walk up the sysfs tree to the owning USB device; read ids and link speed."""
    path = sysfs_device
    for _ in range(8):
        if os.path.exists(os.path.join(path, "idVendor")):
            speed = _read(os.path.join(path, "speed"))
            return {
                "vid": _read(os.path.join(path, "idVendor")),
                "pid": _read(os.path.join(path, "idProduct")),
                "speed": float(speed) if speed else 0.0,
                "version": _read(os.path.join(path, "version")),
            }
        parent = os.path.dirname(path)
        if parent == path or parent == "/sys":
            return None
        path = parent
    return None


def _usb_ids_for(sysfs_device: str) -> tuple[str, str] | None:
    info = _usb_info_for(sysfs_device)
    return (info["vid"], info["pid"]) if info else None


def speed_label(mbps: float) -> str:
    if mbps >= 10000:
        return "USB 3.1 SuperSpeed+ (10 Gb/s)"
    if mbps >= 5000:
        return "USB 3.0 SuperSpeed (5 Gb/s)"
    if mbps >= 480:
        return "USB 2.0 high speed (480 Mb/s)"
    return f"{mbps:.0f} Mb/s"


def list_video_devices() -> list[dict]:
    """Every /dev/video* node that can actually capture, newest first."""
    out = []
    for node in sorted(glob.glob("/dev/video*"), key=lambda p: int(p[10:] or 0)):
        name = _sysfs_name(node)
        link = f"/sys/class/video4linux/{os.path.basename(node)}/device"
        info = _usb_info_for(os.path.realpath(link)) if os.path.exists(link) else None
        usb = (info["vid"], info["pid"]) if info else None
        formats = enum_formats(node)
        if not formats:                       # metadata-only node (e.g. video2)
            continue
        low = name.lower()
        score = 0
        if usb and (usb[0], usb[1]) in KNOWN_BOARDS:
            score += 100
        if any(h in low for h in CAMERA_NAME_HINTS):
            score += 10
        if any(x in low for x in CAMERA_NAME_EXCLUDE):
            score -= 50
        out.append({
            "path": node,
            "name": name or node,
            "usb": f"{usb[0]}:{usb[1]}" if usb else None,
            "board": KNOWN_BOARDS.get(usb) if usb else None,
            "speed": info["speed"] if info else 0.0,
            "speed_label": speed_label(info["speed"]) if info else "unknown",
            "formats": formats,
            "score": score,
        })
    return sorted(out, key=lambda d: -d["score"])


def enum_formats(node: str) -> list[dict]:
    """Enumerate pixel formats, frame sizes and frame rates via V4L2 ioctls."""
    try:
        fd = os.open(node, os.O_RDWR | os.O_NONBLOCK)
    except OSError:
        return []
    formats = []
    try:
        for index in range(16):
            buf = bytearray(struct.pack(_FMTDESC, index, V4L2_BUF_TYPE_VIDEO_CAPTURE,
                                        0, b"", 0, 0, 0, 0, 0))
            try:
                fcntl.ioctl(fd, VIDIOC_ENUM_FMT, buf)
            except OSError:
                break
            _, _, flags, desc, pixfmt = struct.unpack(_FMTDESC, buf)[:5]
            entry = {
                "fourcc": fourcc_str(pixfmt),
                "description": desc.split(b"\x00")[0].decode("ascii", "replace"),
                "compressed": bool(flags & 0x0001),
                "sizes": _enum_sizes(fd, pixfmt),
            }
            formats.append(entry)
    finally:
        os.close(fd)
    return formats


def _enum_sizes(fd: int, pixfmt: int) -> list[dict]:
    sizes = []
    for index in range(48):
        buf = bytearray(struct.pack(_FRMSIZE, index, pixfmt, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        try:
            fcntl.ioctl(fd, VIDIOC_ENUM_FRAMESIZES, buf)
        except OSError:
            break
        fields = struct.unpack(_FRMSIZE, buf)
        kind = fields[2]
        if kind == V4L2_FRMSIZE_TYPE_DISCRETE:
            width, height = fields[3], fields[4]
        else:                                      # stepwise / continuous: take max
            width, height = fields[4], fields[7]
        sizes.append({"width": width, "height": height,
                      "fps": _enum_intervals(fd, pixfmt, width, height)})
        if kind != V4L2_FRMSIZE_TYPE_DISCRETE:
            break
    return sizes


def _enum_intervals(fd: int, pixfmt: int, width: int, height: int) -> list[float]:
    rates = []
    for index in range(24):
        buf = bytearray(struct.pack(_FRMIVAL, index, pixfmt, width, height,
                                    0, 0, 0, 0, 0, 0, 0, 0, 0))
        try:
            fcntl.ioctl(fd, VIDIOC_ENUM_FRAMEINTERVALS, buf)
        except OSError:
            break
        fields = struct.unpack(_FRMIVAL, buf)
        if fields[4] != V4L2_FRMIVAL_TYPE_DISCRETE:
            break
        numerator, denominator = fields[5], fields[6]
        if numerator:
            rates.append(round(denominator / numerator, 3))
    return sorted(set(rates), reverse=True)


def list_serial_ports() -> list[dict]:
    """Candidate VISCA ports, best guess first."""
    out = []
    for node in sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*")):
        link = f"/sys/class/tty/{os.path.basename(node)}/device"
        info = _usb_info_for(os.path.realpath(link)) if os.path.exists(link) else None
        usb = (info["vid"], info["pid"]) if info else None
        score = 100 if usb and usb in KNOWN_BOARDS else 0
        out.append({
            "path": node,
            "usb": f"{usb[0]}:{usb[1]}" if usb else None,
            "board": KNOWN_BOARDS.get(usb) if usb else None,
            "speed_label": speed_label(info["speed"]) if info else "unknown",
            "writable": os.access(node, os.R_OK | os.W_OK),
            "score": score,
        })
    return sorted(out, key=lambda d: -d["score"])


def best_mode(video_path: str) -> dict:
    """Pick a sensible default capture mode from what the device advertises.

    Prefers MJPG (far cheaper over USB), then the largest frame size and the
    highest sane frame rate offered for it.
    """
    formats = enum_formats(video_path)
    if not formats:
        return {"fourcc": "MJPG", "width": 1920, "height": 1080, "fps": 30}
    chosen = next((f for f in formats if f["fourcc"] == "MJPG"), formats[0])
    sizes = [s for s in chosen["sizes"] if s["width"] and s["height"]]
    if not sizes:
        return {"fourcc": chosen["fourcc"], "width": 1920, "height": 1080, "fps": 30}
    size = max(sizes, key=lambda s: s["width"] * s["height"])
    rates = [r for r in size["fps"] if 1 <= r <= 60] or [30]
    return {"fourcc": chosen["fourcc"], "width": size["width"],
            "height": size["height"], "fps": int(max(rates))}


def diagnose(videos: list[dict], serials: list[dict]) -> list[str]:
    """Human-readable warnings about how the interface board is attached."""
    notes = []
    board = next((d for d in videos if d["board"]), None)
    if board and 0 < board["speed"] < 5000:
        uncompressed = all(not f["compressed"] for f in board["formats"])
        notes.append(
            f"{board['board']} is linked at {board['speed_label']}. It is a "
            f"SuperSpeed device: on a USB 2.0 link it only offers "
            f"{'uncompressed ' if uncompressed else ''}1080p, which needs more "
            f"bandwidth than USB 2.0 provides, so video will not start. Move it "
            f"to a USB 3 port (blue, or USB-C) with a USB 3 cable - control over "
            f"VISCA still works either way.")
    if not videos:
        notes.append("No capture device found. Check the USB cable and power.")
    if not serials:
        notes.append("No VISCA serial port found - camera control will be unavailable.")
    for port in serials:
        if not port["writable"]:
            notes.append(f"{port['path']} is not writable by this user. Add "
                         f"yourself to the 'dialout' group, or install the "
                         f"bundled udev rule.")
    return notes


def autodetect() -> dict:
    """Pick the most likely (video node, serial port) pair."""
    videos = list_video_devices()
    serials = list_serial_ports()
    return {
        "video": videos[0]["path"] if videos and videos[0]["score"] >= 0 else None,
        "serial": serials[0]["path"] if serials else None,
        "videos": videos,
        "serials": serials,
        "warnings": diagnose(videos, serials),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(autodetect(), indent=2))
