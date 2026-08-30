"""VISCA protocol driver for Sony FCB block cameras (FCB-EV / EW / ER / EH series).

Talks to the camera over a serial port. On a Twiga USB3 NeoHD (or similar FX3
interface board) the VISCA line is bridged to a USB CDC-ACM port, typically
/dev/ttyACM0, at 9600 8N1.
"""
from __future__ import annotations

import logging
import threading
import time

import serial

log = logging.getLogger(__name__)

BAUD_CANDIDATES = [9600, 38400, 19200, 115200, 4800, 57600]

ERROR_TEXT = {
    0x01: "message length error",
    0x02: "syntax error",
    0x03: "command buffer full",
    0x04: "command canceled",
    0x05: "no socket",
    0x41: "command not executable",
}

# Zoom position limits (VISCA units). Optical end-stop is 0x4000 on all FCB
# blocks; digital zoom extends the range up to 0x7AC0 (12x digital).
ZOOM_OPTICAL_MAX = 0x4000
ZOOM_DIGITAL_MAX = 0x7AC0
# Focus: 0x1000 == infinity, 0xC000 == closest.
FOCUS_MIN = 0x1000
FOCUS_MAX = 0xC000


class ViscaError(Exception):
    """The camera replied with a VISCA error packet."""


class ViscaTimeout(ViscaError):
    """The camera did not reply in time."""


def nibbles(value: int, count: int = 4) -> list[int]:
    """Split an int into `count` 4-bit nibbles, most significant first."""
    return [(value >> (4 * (count - 1 - i))) & 0x0F for i in range(count)]


def from_nibbles(data: bytes) -> int:
    """Rebuild an int from a sequence of low-nibble-carrying bytes."""
    value = 0
    for byte in data:
        value = (value << 4) | (byte & 0x0F)
    return value


class Visca:
    """Thread-safe VISCA transport plus the FCB command set."""

    def __init__(self, port: str, baud: int = 9600, address: int = 1, timeout: float = 0.8):
        self.port = port
        self.baud = baud
        self.address = address
        self.timeout = timeout
        self.header = 0x80 | address
        self.model = None          # filled in by identify()
        self._ser: serial.Serial | None = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ link

    @property
    def connected(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def open(self, autobaud: bool = True) -> None:
        """Open the port. With autobaud, probe common rates until one answers."""
        with self._lock:
            self.close()
            rates = [self.baud] + [b for b in BAUD_CANDIDATES if b != self.baud] if autobaud else [self.baud]
            last_exc = None
            for rate in rates:
                try:
                    self._ser = serial.Serial(
                        self.port, rate, bytesize=8, parity="N", stopbits=1,
                        timeout=self.timeout, write_timeout=self.timeout,
                        rtscts=False, dsrdtr=False, xonxoff=False,
                    )
                except serial.SerialException as exc:
                    last_exc = exc
                    continue
                time.sleep(0.15)
                self._ser.reset_input_buffer()
                try:
                    self.identify()
                    self.baud = rate
                    log.info("VISCA link up on %s @ %d baud (%s)", self.port, rate, self.model)
                    return
                except ViscaError as exc:
                    last_exc = exc
                    self._ser.close()
                    self._ser = None
            self._ser = None
            raise ViscaTimeout(f"no VISCA response on {self.port} (tried {rates}): {last_exc}")

    def close(self) -> None:
        with self._lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:
                    pass
                self._ser = None

    # --------------------------------------------------------------- packets

    def _read_packet(self) -> bytes:
        """Read one 0xFF-terminated VISCA packet."""
        buf = bytearray()
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            chunk = self._ser.read(1)
            if not chunk:
                continue
            buf += chunk
            if chunk[0] == 0xFF:
                return bytes(buf)
            if len(buf) > 32:          # runaway / desync guard
                break
        raise ViscaTimeout(f"no reply within {self.timeout}s (got {buf.hex(' ') or 'nothing'})")

    def transact(self, payload: bytes, want_data: bool = False, retries: int = 1) -> bytes:
        """Send one command/inquiry and pump replies until completion.

        Returns the inquiry payload (bytes between 'y0 50' and 0xFF), or b'' for
        a plain command.
        """
        with self._lock:
            if not self.connected:
                raise ViscaError("serial port is not open")
            packet = bytes([self.header]) + payload + b"\xff"
            for attempt in range(retries + 1):
                try:
                    self._ser.reset_input_buffer()
                    self._ser.write(packet)
                    self._ser.flush()
                    for _ in range(4):          # ACK, then completion
                        reply = self._read_packet()
                        kind = reply[1] & 0xF0
                        if kind == 0x40:        # ACK - command accepted
                            continue
                        if kind == 0x50:        # completion / inquiry reply
                            data = reply[2:-1]
                            if want_data and not data:
                                continue
                            return data
                        if kind == 0x60:        # error
                            code = reply[2] if len(reply) > 3 else 0
                            raise ViscaError(ERROR_TEXT.get(code, f"error 0x{code:02x}"))
                    raise ViscaTimeout("no completion packet")
                except ViscaTimeout:
                    if attempt >= retries:
                        raise
                    time.sleep(0.05)
            return b""

    def command(self, *payload: int) -> None:
        self.transact(bytes(payload))

    def inquire(self, *payload: int) -> bytes:
        return self.transact(bytes(payload), want_data=True)

    def raw(self, hexstr: str) -> str:
        """Send an arbitrary VISCA packet written as hex; return the reply hex."""
        data = bytes.fromhex(hexstr.replace(",", " ").replace("0x", ""))
        if data and data[0] & 0x80:      # caller supplied the header themselves
            payload = data[1:-1] if data[-1] == 0xFF else data[1:]
        else:
            payload = data[:-1] if data and data[-1] == 0xFF else data
        return self.transact(payload, want_data=True).hex(" ")

    # ---------------------------------------------------------------- system

    def identify(self) -> dict:
        """CAM_VersionInq -> vendor/model/rom ids."""
        data = self.inquire(0x09, 0x00, 0x02)
        if len(data) < 7:
            raise ViscaError(f"short version reply: {data.hex(' ')}")
        info = {
            "vendor": f"0x{data[0] << 8 | data[1]:04x}",
            "model": f"0x{data[2] << 8 | data[3]:04x}",
            "rom": f"0x{data[4] << 8 | data[5]:04x}",
            "sockets": data[6],
        }
        info["name"] = FCB_MODELS.get(info["model"], f"Sony block, model id {info['model']}")
        self.model = info["name"]
        return info

    def if_clear(self) -> None:
        self.command(0x01, 0x00, 0x01)

    def power(self, on: bool) -> None:
        self.command(0x01, 0x04, 0x00, 0x02 if on else 0x03)

    def power_state(self) -> bool:
        return self.inquire(0x09, 0x04, 0x00)[0] == 0x02

    # ------------------------------------------------------------------ zoom

    def zoom_stop(self) -> None:
        self.command(0x01, 0x04, 0x07, 0x00)

    def zoom_tele(self, speed: int | None = None) -> None:
        """Tele (in). speed 0-7, or None for the camera's standard speed."""
        code = 0x02 if speed is None else 0x20 | max(0, min(7, speed))
        self.command(0x01, 0x04, 0x07, code)

    def zoom_wide(self, speed: int | None = None) -> None:
        """Wide (out). speed 0-7, or None for the camera's standard speed."""
        code = 0x03 if speed is None else 0x30 | max(0, min(7, speed))
        self.command(0x01, 0x04, 0x07, code)

    def zoom_to(self, position: int) -> None:
        position = max(0, min(ZOOM_DIGITAL_MAX, int(position)))
        self.command(0x01, 0x04, 0x47, *nibbles(position))

    def zoom_position(self) -> int:
        return from_nibbles(self.inquire(0x09, 0x04, 0x47))

    def digital_zoom(self, on: bool) -> None:
        self.command(0x01, 0x04, 0x06, 0x02 if on else 0x03)

    def dzoom_mode_combined(self, combined: bool = True) -> None:
        self.command(0x01, 0x04, 0x36, 0x00 if combined else 0x01)

    # ----------------------------------------------------------------- focus

    def focus_stop(self) -> None:
        self.command(0x01, 0x04, 0x08, 0x00)

    def focus_far(self, speed: int | None = None) -> None:
        code = 0x02 if speed is None else 0x20 | max(0, min(7, speed))
        self.command(0x01, 0x04, 0x08, code)

    def focus_near(self, speed: int | None = None) -> None:
        code = 0x03 if speed is None else 0x30 | max(0, min(7, speed))
        self.command(0x01, 0x04, 0x08, code)

    def focus_to(self, position: int) -> None:
        position = max(FOCUS_MIN, min(FOCUS_MAX, int(position)))
        self.command(0x01, 0x04, 0x48, *nibbles(position))

    def focus_position(self) -> int:
        return from_nibbles(self.inquire(0x09, 0x04, 0x48))

    def focus_auto(self, auto: bool) -> None:
        self.command(0x01, 0x04, 0x38, 0x02 if auto else 0x03)

    def focus_is_auto(self) -> bool:
        return self.inquire(0x09, 0x04, 0x38)[0] == 0x02

    def focus_one_push(self) -> None:
        self.command(0x01, 0x04, 0x18, 0x01)

    def af_mode(self, mode: int) -> None:
        """0 normal, 1 interval, 2 zoom trigger."""
        self.command(0x01, 0x04, 0x57, mode & 0x0F)

    def focus_near_limit(self, position: int) -> None:
        self.command(0x01, 0x04, 0x28, *nibbles(int(position)))

    # ------------------------------------------------------------- exposure

    AE_MODES = {"auto": 0x00, "manual": 0x03, "shutter": 0x0A, "iris": 0x0B, "bright": 0x0D}

    def ae_mode(self, mode: str) -> None:
        self.command(0x01, 0x04, 0x39, self.AE_MODES[mode])

    def ae_mode_get(self) -> str:
        value = self.inquire(0x09, 0x04, 0x39)[0]
        return next((k for k, v in self.AE_MODES.items() if v == value), f"0x{value:02x}")

    def shutter(self, value: int) -> None:
        self.command(0x01, 0x04, 0x4A, 0x00, 0x00, *nibbles(int(value), 2))

    def shutter_get(self) -> int:
        return from_nibbles(self.inquire(0x09, 0x04, 0x4A))

    def iris(self, value: int) -> None:
        self.command(0x01, 0x04, 0x4B, 0x00, 0x00, *nibbles(int(value), 2))

    def iris_get(self) -> int:
        return from_nibbles(self.inquire(0x09, 0x04, 0x4B))

    def gain(self, value: int) -> None:
        self.command(0x01, 0x04, 0x4C, 0x00, 0x00, *nibbles(int(value), 2))

    def gain_get(self) -> int:
        return from_nibbles(self.inquire(0x09, 0x04, 0x4C))

    def gain_limit(self, value: int) -> None:
        self.command(0x01, 0x04, 0x2C, int(value) & 0x0F)

    def bright(self, value: int) -> None:
        self.command(0x01, 0x04, 0x4D, 0x00, 0x00, *nibbles(int(value), 2))

    def bright_get(self) -> int:
        return from_nibbles(self.inquire(0x09, 0x04, 0x4D))

    def exp_comp(self, on: bool) -> None:
        self.command(0x01, 0x04, 0x3E, 0x02 if on else 0x03)

    def exp_comp_level(self, value: int) -> None:
        self.command(0x01, 0x04, 0x4E, 0x00, 0x00, *nibbles(int(value), 2))

    def exp_comp_get(self) -> int:
        return from_nibbles(self.inquire(0x09, 0x04, 0x4E))

    def backlight(self, on: bool) -> None:
        self.command(0x01, 0x04, 0x33, 0x02 if on else 0x03)

    def backlight_get(self) -> bool:
        return self.inquire(0x09, 0x04, 0x33)[0] == 0x02

    def wide_dynamic(self, on: bool) -> None:
        self.command(0x01, 0x04, 0x3D, 0x02 if on else 0x03)

    def slow_shutter_auto(self, on: bool) -> None:
        self.command(0x01, 0x04, 0x5A, 0x02 if on else 0x03)

    # ----------------------------------------------------------- white bal.

    WB_MODES = {"auto": 0x00, "indoor": 0x01, "outdoor": 0x02, "onepush": 0x03,
                "atw": 0x04, "manual": 0x05, "sodium": 0x0A}

    def wb_mode(self, mode: str) -> None:
        self.command(0x01, 0x04, 0x35, self.WB_MODES[mode])

    def wb_mode_get(self) -> str:
        value = self.inquire(0x09, 0x04, 0x35)[0]
        return next((k for k, v in self.WB_MODES.items() if v == value), f"0x{value:02x}")

    def wb_one_push_trigger(self) -> None:
        self.command(0x01, 0x04, 0x10, 0x05)

    def r_gain(self, value: int) -> None:
        self.command(0x01, 0x04, 0x43, 0x00, 0x00, *nibbles(int(value), 2))

    def r_gain_get(self) -> int:
        return from_nibbles(self.inquire(0x09, 0x04, 0x43))

    def b_gain(self, value: int) -> None:
        self.command(0x01, 0x04, 0x44, 0x00, 0x00, *nibbles(int(value), 2))

    def b_gain_get(self) -> int:
        return from_nibbles(self.inquire(0x09, 0x04, 0x44))

    # ---------------------------------------------------------------- image

    def aperture(self, value: int) -> None:
        """Sharpness / detail enhancement, 0-15."""
        self.command(0x01, 0x04, 0x42, 0x00, 0x00, *nibbles(int(value), 2))

    def aperture_get(self) -> int:
        return from_nibbles(self.inquire(0x09, 0x04, 0x42))

    def noise_reduction(self, level: int) -> None:
        self.command(0x01, 0x04, 0x53, int(level) & 0x0F)

    def gamma(self, level: int) -> None:
        self.command(0x01, 0x04, 0x5B, int(level) & 0x0F)

    def chroma_suppress(self, level: int) -> None:
        self.command(0x01, 0x04, 0x5F, int(level) & 0x0F)

    def defog(self, on: bool) -> None:
        self.command(0x01, 0x04, 0x37, 0x02 if on else 0x03, 0x00)

    def picture_effect(self, effect: str) -> None:
        codes = {"off": 0x00, "negative": 0x02, "bw": 0x04}
        self.command(0x01, 0x04, 0x63, codes[effect])

    def mirror(self, on: bool) -> None:
        """Left/right reverse."""
        self.command(0x01, 0x04, 0x61, 0x02 if on else 0x03)

    def flip(self, on: bool) -> None:
        """Vertical picture flip."""
        self.command(0x01, 0x04, 0x66, 0x02 if on else 0x03)

    def freeze(self, on: bool) -> None:
        self.command(0x01, 0x04, 0x62, 0x02 if on else 0x03)

    def ir_cut(self, removed: bool) -> None:
        """ICR: True = filter removed (night/IR mode), False = day mode."""
        self.command(0x01, 0x04, 0x01, 0x02 if removed else 0x03)

    def ir_cut_get(self) -> bool:
        return self.inquire(0x09, 0x04, 0x01)[0] == 0x02

    def auto_ir_cut(self, on: bool) -> None:
        self.command(0x01, 0x04, 0x51, 0x02 if on else 0x03)

    def stabilizer(self, on: bool) -> None:
        self.command(0x01, 0x04, 0x34, 0x02 if on else 0x03)

    def high_sensitivity(self, on: bool) -> None:
        self.command(0x01, 0x04, 0x5E, 0x02 if on else 0x03)

    # -------------------------------------------------------------- presets

    def preset_set(self, slot: int) -> None:
        self.command(0x01, 0x04, 0x3F, 0x01, int(slot) & 0x7F)

    def preset_recall(self, slot: int) -> None:
        self.command(0x01, 0x04, 0x3F, 0x02, int(slot) & 0x7F)

    def preset_reset(self, slot: int) -> None:
        self.command(0x01, 0x04, 0x3F, 0x00, int(slot) & 0x7F)

    # ----------------------------------------------------------------- menu

    def menu(self, on: bool) -> None:
        """FCB blocks open the OSD via the reserved preset 0x5F."""
        if on:
            self.command(0x01, 0x04, 0x3F, 0x02, 0x5F)
        else:
            self.command(0x01, 0x06, 0x06, 0x03)

    def menu_nav(self, direction: str) -> None:
        """Drive the OSD cursor with pseudo pan/tilt packets."""
        codes = {"up": (0x03, 0x01), "down": (0x03, 0x02), "left": (0x01, 0x03),
                 "right": (0x02, 0x03), "enter": (0x03, 0x03)}
        if direction == "enter":
            self.command(0x01, 0x06, 0x06, 0x05)
            return
        pan, tilt = codes[direction]
        self.command(0x01, 0x06, 0x01, 0x0E, 0x0E, pan, tilt)
        self.command(0x01, 0x06, 0x01, 0x0E, 0x0E, 0x03, 0x03)

    # ------------------------------------------------------- bulk state read

    def snapshot_state(self) -> dict:
        """Best-effort read of everything the GUI shows. Never raises."""
        state: dict = {}
        readers = {
            "zoom": self.zoom_position,
            "focus": self.focus_position,
            "focus_auto": self.focus_is_auto,
            "ae_mode": self.ae_mode_get,
            "shutter": self.shutter_get,
            "iris": self.iris_get,
            "gain": self.gain_get,
            "bright": self.bright_get,
            "exp_comp": self.exp_comp_get,
            "backlight": self.backlight_get,
            "wb_mode": self.wb_mode_get,
            "r_gain": self.r_gain_get,
            "b_gain": self.b_gain_get,
            "aperture": self.aperture_get,
            "ir_cut": self.ir_cut_get,
            "power": self.power_state,
        }
        for key, reader in readers.items():
            try:
                state[key] = reader()
            except (ViscaError, serial.SerialException, IndexError):
                state[key] = None
        return state


# Model ids reported by CAM_VersionInq, for the block families a NeoHD carries.
FCB_MODELS = {
    "0x0402": "Sony FCB-EX/EH series",
    "0x0432": "Sony FCB-EV7100 (10x)",
    "0x0435": "Sony FCB-EV7300",
    "0x0436": "Sony FCB-EV7310",
    "0x0437": "Sony FCB-EV7500",
    "0x0438": "Sony FCB-EV7520 (30x)",
    "0x0440": "Sony FCB-EV9500L",
    "0x0441": "Sony FCB-EV9520L",
    "0x044a": "Sony FCB-EV7520A",
    "0x0455": "Sony FCB-ER8300 (4K)",
    "0x0456": "Sony FCB-ER8530 (4K)",
    "0x0500": "Sony FCB-EW9500H",
}
