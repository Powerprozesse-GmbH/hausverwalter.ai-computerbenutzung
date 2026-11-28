"""Computer actions on Linux (X11 via DISPLAY). Same interface as mac_control.MacScreen.

Screenshot: mss, scaled so the long edge is MAX_EDGE px. Mouse and keys: xdotool
as a subprocess, never a shell. Texts: tesseract CLI (`-l deu+eng tsv`), centres
in screenshot pixels, top to bottom, same shape as Mac and Windows.
"""

from __future__ import annotations

import base64
import io
import subprocess
import tempfile
import time
from pathlib import Path

MAX_EDGE = 1280
SETTLE_SECONDS = 0.3
GLIDE_SECONDS = 0.2
GLIDE_STEPS = 16
JPEG_QUALITY = 70
MAX_TEXTS = 200
TYPE_DELAY_MS = "0"

KEY_MAP = {
    "return": "Return", "enter": "Return", "kp_enter": "KP_Enter", "tab": "Tab", "space": "space",
    "backspace": "BackSpace", "delete": "Delete", "escape": "Escape", "esc": "Escape",
    "left": "Left", "right": "Right", "down": "Down", "up": "Up",
    "home": "Home", "end": "End", "page_up": "Prior", "prior": "Prior", "page_down": "Next", "next": "Next",
    "cmd": "super", "command": "super", "super": "super", "meta": "super", "win": "super",
    "ctrl": "ctrl", "control": "ctrl", "alt": "alt", "option": "alt", "shift": "shift",
    **{f"f{n}": f"F{n}" for n in range(1, 13)},
}
BUTTONS = {"left": "1", "middle": "2", "right": "3", "up": "4", "down": "5", "left_scroll": "6", "right_scroll": "7"}
CLICK_BUTTON = {"right_click": "right", "middle_click": "middle"}
CLICK_COUNT = {"double_click": "2", "triple_click": "3"}


class Interrupted(RuntimeError):
    """Stopp was pressed in the middle of an action (long typing, a glide)."""


def scaled_size(width: int, height: int, max_edge: int = MAX_EDGE) -> tuple[int, int]:
    scale = max_edge / max(width, height)
    return round(width * scale), round(height * scale)


def parse_tesseract_tsv(tsv: str, scale_x: float, scale_y: float, limit: int = MAX_TEXTS) -> list[tuple[str, int, int]]:
    """Word rows (level 5) joined into lines: (text, centre x, centre y) in screenshot pixels."""
    rows = tsv.splitlines()
    if len(rows) < 2:
        return []
    header = rows[0].split("\t")
    groups: dict[tuple[str, str, str], list[tuple[str, int, int, int, int]]] = {}
    order: list[tuple[str, str, str]] = []
    for row in rows[1:]:
        cols = row.split("\t")
        if len(cols) < len(header):
            cols += [""] * (len(header) - len(cols))
        data = dict(zip(header, cols))
        if data.get("level") != "5":
            continue
        text = data.get("text", "").strip()
        if not text:
            continue
        key = (data.get("block_num", ""), data.get("par_num", ""), data.get("line_num", ""))
        left, top = int(float(data.get("left") or 0)), int(float(data.get("top") or 0))
        width, height = int(float(data.get("width") or 0)), int(float(data.get("height") or 0))
        if key not in groups:
            order.append(key)
            groups[key] = []
        groups[key].append((text, left, top, left + width, top + height))
    texts = []
    for key in order:
        words = groups[key]
        left, top = min(item[1] for item in words), min(item[2] for item in words)
        right, bottom = max(item[3] for item in words), max(item[4] for item in words)
        texts.append((
            " ".join(item[0] for item in words),
            round((left + right) / 2 * scale_x),
            round((top + bottom) / 2 * scale_y),
        ))
    texts.sort(key=lambda item: (item[2], item[1]))
    return texts[:limit]


def _display_size() -> tuple[int, int]:
    import mss

    with mss.mss() as capture:
        monitors = capture.monitors
        mon = monitors[1] if len(monitors) > 1 else monitors[0]
        return int(mon["width"]), int(mon["height"])


def _grab():
    """Primary monitor as a Pillow image, or the whole virtual screen if there is no primary."""
    import mss
    from PIL import Image

    with mss.mss() as capture:
        monitors = capture.monitors
        mon = monitors[1] if len(monitors) > 1 else monitors[0]
        raw = capture.grab(mon)
    return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")


def read_texts(image, shot_w: int, shot_h: int) -> list[tuple[str, int, int]]:
    """tesseract -l deu+eng tsv. Missing tesseract: screenshots still work, texts stay empty."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "screen.png"
        image.save(path)
        try:
            result = subprocess.run(
                ["tesseract", str(path), "stdout", "-l", "deu+eng", "tsv"],
                check=True, capture_output=True, text=True,
            )
        except (OSError, subprocess.CalledProcessError):
            return []
    return parse_tesseract_tsv(result.stdout, shot_w / image.width, shot_h / image.height)


class LinuxScreen:
    platform = "linux"

    def __init__(self) -> None:
        self.phys_w, self.phys_h = _display_size()
        self.width, self.height = scaled_size(self.phys_w, self.phys_h)
        self.last_click: tuple[float, float, float] | None = None
        self.last_texts: list[tuple[str, int, int]] = []
        self.stop_event = None

    def screenshot(self) -> str:
        """Base64 JPEG in the model's coordinate space; also reads texts into last_texts."""
        image = _grab()
        self.last_texts = read_texts(image, self.width, self.height)
        small = image.resize((self.width, self.height))
        buffer = io.BytesIO()
        small.save(buffer, "JPEG", quality=JPEG_QUALITY)
        return base64.b64encode(buffer.getvalue()).decode()

    def run(self, action: dict, look: bool = True) -> str | None:
        """Runs one action. Returns a base64 JPEG when look is true (look=False: never)."""
        name = action["action"]
        if name == "screenshot":
            return self.screenshot() if look else None
        if name == "wait":
            time.sleep(min(float(action.get("duration", 1)), 10))
        elif name == "mouse_move":
            self._move(*self._point(action["coordinate"]))
        elif name in ("left_click", "right_click", "middle_click", "double_click", "triple_click"):
            self._click(name, action.get("coordinate"), action.get("text"))
        elif name == "left_click_drag":
            self._drag(self._point(action["start_coordinate"]), self._point(action["coordinate"]))
        elif name in ("left_mouse_down", "left_mouse_up"):
            self._xdo("mousedown" if name == "left_mouse_down" else "mouseup", BUTTONS["left"])
        elif name == "scroll":
            self._scroll(action)
        elif name == "type":
            self._type(action["text"])
        elif name == "key":
            for _ in range(int(action.get("repeat", 1) or 1)):
                self._key(action["text"])
        elif name == "hold_key":
            self._hold(action["text"], float(action.get("duration", 0) or 0))
        elif name == "cursor_position":
            return None
        else:
            raise ValueError(f"Aktion nicht unterstützt: {name}")
        time.sleep(SETTLE_SECONDS)
        return self.screenshot() if look else None

    def cursor_in_screenshot(self) -> list[int]:
        x, y = self._cursor()
        return [round(x * self.width / self.phys_w), round(y * self.height / self.phys_h)]

    def _check_stop(self) -> None:
        stop = getattr(self, "stop_event", None)
        if stop is not None and stop.is_set():
            raise Interrupted("Vom Nutzer gestoppt.")

    def _point(self, coordinate: list[int]) -> tuple[float, float]:
        x, y = coordinate
        return x * self.phys_w / self.width, y * self.phys_h / self.height

    def _cursor(self) -> tuple[float, float]:
        vals = {}
        for line in self._xdo("getmouselocation", "--shell").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                vals[key] = value
        return float(vals.get("X", "0")), float(vals.get("Y", "0"))

    def _xdo(self, *args: str) -> str:
        """One xdotool call. The error text never contains the arguments (they can be a secret)."""
        try:
            result = subprocess.run(["xdotool", *args], check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError):
            raise RuntimeError("xdotool konnte die Eingabe nicht ausführen.") from None
        return result.stdout

    def _move(self, x: float, y: float) -> None:
        x0, y0 = self._cursor()
        for i in range(1, GLIDE_STEPS + 1):
            self._check_stop()
            t = i / GLIDE_STEPS
            t = t * t * (3 - 2 * t)
            self._xdo("mousemove", str(round(x0 + (x - x0) * t)), str(round(y0 + (y - y0) * t)))
            time.sleep(GLIDE_SECONDS / GLIDE_STEPS)

    def _click(self, name: str, coordinate, modifier_text: str | None) -> None:
        point = self._point(coordinate) if coordinate else self._cursor()
        self._move(*point)
        self.last_click = (point[0], point[1], time.time())
        button = BUTTONS[CLICK_BUTTON.get(name, "left")]
        repeat = CLICK_COUNT.get(name, "1")
        mods = [self._key_name(part) for part in (modifier_text or "").split("+") if part.strip()]
        for mod in mods:
            self._xdo("keydown", "--", mod)
        try:
            if repeat == "1":
                self._xdo("click", button)
            else:
                self._xdo("click", "--repeat", repeat, button)
        finally:
            for mod in reversed(mods):
                self._xdo("keyup", "--", mod)

    def _drag(self, start, end) -> None:
        self._move(*start)
        self._xdo("mousedown", BUTTONS["left"])
        try:
            for i in range(1, 11):
                x = start[0] + (end[0] - start[0]) * i / 10
                y = start[1] + (end[1] - start[1]) * i / 10
                self._xdo("mousemove", str(round(x)), str(round(y)))
                time.sleep(0.02)
        finally:
            self._xdo("mouseup", BUTTONS["left"])

    def _scroll(self, action: dict) -> None:
        if action.get("coordinate"):
            self._move(*self._point(action["coordinate"]))
        amount = max(1, int(action.get("scroll_amount", 3)))
        button = {"up": "4", "down": "5", "left": "6", "right": "7"}[action["scroll_direction"]]
        self._xdo("click", "--repeat", str(amount), button)

    def _type(self, text: str) -> None:
        for char in text:
            self._check_stop()
            if char == "\n":
                self._xdo("key", "--", self._key_name("return"))
                continue
            self._xdo("type", "--delay", TYPE_DELAY_MS, "--", char)
            time.sleep(0.01)

    def _key(self, combo: str) -> None:
        for part in combo.split():
            self._xdo("key", "--", self._combo(part))

    def _hold(self, combo: str, seconds: float) -> None:
        if seconds <= 0:
            self._key(combo)
            return
        names = [self._key_name(part) for part in combo.split("+")]
        for name in names:
            self._xdo("keydown", "--", name)
        try:
            time.sleep(min(seconds, 10))
        finally:
            for name in reversed(names):
                self._xdo("keyup", "--", name)

    def _combo(self, part: str) -> str:
        return "+".join(self._key_name(piece) for piece in part.split("+"))

    @staticmethod
    def _key_name(key: str) -> str:
        """KEY_MAP value or one character. Anything else is refused, so a leading '-' cannot become an option."""
        raw = key.strip()
        mapped = KEY_MAP.get(raw.lower())
        if mapped is not None:
            return mapped
        if len(raw) == 1:
            return raw
        raise RuntimeError("Taste nicht erlaubt.")
