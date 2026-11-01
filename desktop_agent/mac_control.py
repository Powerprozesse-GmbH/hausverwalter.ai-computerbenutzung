"""Executes Claude's computer actions on this Mac (computer_20251124 or the computer toolset, both as {"action": ...} dicts).

Screenshots: `screencapture`, scaled so the long edge is MAX_EDGE px. Claude's
coordinates live in that screenshot space and are mapped back to screen points.
Mouse and typing: Quartz events. The pointer glides to its target so people can
follow it (GLIDE_SECONDS). Shortcuts and special keys: System Events, which
respects the keyboard layout (QWERTZ). The hosting app needs the macOS permissions
„Bildschirmaufnahme“ and „Bedienungshilfen“ (the Claude app's terminal has both).
"""

from __future__ import annotations

import base64
import subprocess
import tempfile
import time
from pathlib import Path

import Quartz

MAX_EDGE = 1280
SETTLE_SECONDS = 0.3
GLIDE_SECONDS = 0.2
GLIDE_STEPS = 16
JPEG_QUALITY = 70  # a third of the PNG size, text stays readable
MAX_TEXTS = 200

# AppleScript key codes for named keys (xdotool names as Claude sends them).
KEY_CODES = {
    "return": 36, "enter": 36, "kp_enter": 76, "tab": 48, "space": 49,
    "backspace": 51, "delete": 117, "escape": 53, "esc": 53,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "home": 115, "end": 119, "page_up": 116, "prior": 116, "page_down": 121, "next": 121,
    **{f"f{n}": code for n, code in zip(range(1, 13), (122, 120, 99, 118, 96, 97, 98, 100, 101, 109, 103, 111))},
}
MODIFIERS = {
    "cmd": "command down", "command": "command down", "super": "command down", "meta": "command down",
    "ctrl": "control down", "control": "control down",
    "alt": "option down", "option": "option down",
    "shift": "shift down",
}


class Interrupted(RuntimeError):
    """Stopp was pressed in the middle of an action (long typing, a glide)."""


class MacScreen:
    platform = "darwin"

    def __init__(self) -> None:
        bounds = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
        self.points_w, self.points_h = int(bounds.size.width), int(bounds.size.height)
        scale = MAX_EDGE / max(self.points_w, self.points_h)
        self.width, self.height = round(self.points_w * scale), round(self.points_h * scale)
        # Last click in screen points + time, for the overlay's click ring.
        self.last_click: tuple[float, float, float] | None = None
        # Texts on the last screenshot with their centre in screenshot pixels (on-device OCR).
        self.last_texts: list[tuple[str, int, int]] = []
        # Set by agent.Session: long typing and glides end mid-way when it is set.
        self.stop_event = None

    # --- screenshot -----------------------------------------------------------------

    def screenshot(self) -> str:
        """Base64 JPEG in the model's coordinate space; also reads the screen's texts into last_texts."""
        with tempfile.TemporaryDirectory() as tmp:
            raw, small = Path(tmp, "raw.png"), Path(tmp, "small.jpg")
            subprocess.run(["screencapture", "-x", "-m", "-C", str(raw)], check=True, capture_output=True)
            subprocess.run(
                ["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(JPEG_QUALITY),
                 "-z", str(self.height), str(self.width), str(raw), "--out", str(small)],
                check=True,
                capture_output=True,
            )
            self.last_texts = read_texts(raw, self.width, self.height)
            return base64.b64encode(small.read_bytes()).decode()

    # --- actions --------------------------------------------------------------------

    def run(self, action: dict, look: bool = True) -> str | None:
        """Runs one action. Returns a base64 JPEG for actions that need a fresh look (look=False: never)."""
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
            down = name == "left_mouse_down"
            self._post_mouse(Quartz.kCGEventLeftMouseDown if down else Quartz.kCGEventLeftMouseUp, self._cursor())
        elif name == "scroll":
            self._scroll(action)
        elif name == "type":
            self._type(action["text"])
        elif name in ("key", "hold_key"):
            for _ in range(int(action.get("repeat", 1) or 1)):
                self._key(action["text"])
        elif name == "cursor_position":
            return None
        else:
            raise ValueError(f"Aktion nicht unterstützt: {name}")
        time.sleep(SETTLE_SECONDS)
        return self.screenshot() if look else None

    def cursor_in_screenshot(self) -> list[int]:
        x, y = self._cursor()
        return [round(x * self.width / self.points_w), round(y * self.height / self.points_h)]

    # --- helpers --------------------------------------------------------------------

    def _check_stop(self) -> None:
        stop = getattr(self, "stop_event", None)
        if stop is not None and stop.is_set():
            raise Interrupted("Vom Nutzer gestoppt.")

    def _point(self, coordinate: list[int]) -> tuple[float, float]:
        x, y = coordinate
        return x * self.points_w / self.width, y * self.points_h / self.height

    def _cursor(self) -> tuple[float, float]:
        loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        return loc.x, loc.y

    def _post_mouse(self, kind: int, point, button=Quartz.kCGMouseButtonLeft, clicks: int = 1, flags: int = 0):
        event = Quartz.CGEventCreateMouseEvent(None, kind, point, button)
        Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, clicks)
        if flags:
            Quartz.CGEventSetFlags(event, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    def _move(self, x: float, y: float) -> None:
        """Glide from the current position (ease in/out) instead of jumping."""
        x0, y0 = self._cursor()
        for i in range(1, GLIDE_STEPS + 1):
            self._check_stop()
            t = i / GLIDE_STEPS
            t = t * t * (3 - 2 * t)
            self._post_mouse(Quartz.kCGEventMouseMoved, (x0 + (x - x0) * t, y0 + (y - y0) * t))
            time.sleep(GLIDE_SECONDS / GLIDE_STEPS)

    def _click(self, name: str, coordinate, modifier_text: str | None) -> None:
        point = self._point(coordinate) if coordinate else self._cursor()
        self._move(*point)
        self.last_click = (point[0], point[1], time.time())
        flags = self._modifier_flags(modifier_text)
        if name == "right_click":
            down, up, button = Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp, Quartz.kCGMouseButtonRight
        elif name == "middle_click":
            down, up, button = Quartz.kCGEventOtherMouseDown, Quartz.kCGEventOtherMouseUp, Quartz.kCGMouseButtonCenter
        else:
            down, up, button = Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp, Quartz.kCGMouseButtonLeft
        count = {"double_click": 2, "triple_click": 3}.get(name, 1)
        for n in range(1, count + 1):
            self._post_mouse(down, point, button, n, flags)
            self._post_mouse(up, point, button, n, flags)
            time.sleep(0.05)

    def _drag(self, start, end) -> None:
        self._move(*start)
        self._post_mouse(Quartz.kCGEventLeftMouseDown, start)
        for i in range(1, 11):
            p = (start[0] + (end[0] - start[0]) * i / 10, start[1] + (end[1] - start[1]) * i / 10)
            self._post_mouse(Quartz.kCGEventLeftMouseDragged, p)
            time.sleep(0.02)
        self._post_mouse(Quartz.kCGEventLeftMouseUp, end)

    def _scroll(self, action: dict) -> None:
        if action.get("coordinate"):
            self._move(*self._point(action["coordinate"]))
        amount = int(action.get("scroll_amount", 3))
        dy = {"up": amount, "down": -amount}.get(action["scroll_direction"], 0)
        dx = {"left": amount, "right": -amount}.get(action["scroll_direction"], 0)
        event = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 2, dy, dx)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    def _type(self, text: str) -> None:
        for char in text:
            self._check_stop()
            if char == "\n":
                self._key("Return")
                continue
            for down in (True, False):
                event = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
                Quartz.CGEventKeyboardSetUnicodeString(event, len(char.encode("utf-16-le")) // 2, char)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
            time.sleep(0.01)

    def _key(self, combo: str) -> None:
        for part in combo.split():  # xdotool allows several combos separated by spaces
            *mods, key = part.split("+")
            using = [MODIFIERS[m.lower()] for m in mods if m.lower() in MODIFIERS]
            using_clause = f" using {{{', '.join(using)}}}" if using else ""
            code = KEY_CODES.get(key.lower())
            stroke = f"key code {code}" if code is not None else f'keystroke "{key.lower() if using else key}"'
            subprocess.run(
                ["osascript", "-e", f'tell application "System Events" to {stroke}{using_clause}'],
                check=True,
                capture_output=True,
            )

    @staticmethod
    def _modifier_flags(text: str | None) -> int:
        flags = 0
        for mod in (text or "").lower().split("+"):
            flags |= {
                "cmd": Quartz.kCGEventFlagMaskCommand, "super": Quartz.kCGEventFlagMaskCommand,
                "ctrl": Quartz.kCGEventFlagMaskControl, "alt": Quartz.kCGEventFlagMaskAlternate,
                "shift": Quartz.kCGEventFlagMaskShift,
            }.get(mod.strip(), 0)
        return flags


def read_texts(path: Path, width: int, height: int) -> list[tuple[str, int, int]]:
    """On-device OCR (Apple Vision, ~0.2 s, nothing leaves the Mac): (text, x, y) per text line,
    centre in screenshot pixels, top to bottom. Works on pixels, so also inside remote-desktop windows."""
    try:
        import Vision
        from Foundation import NSURL
    except ImportError:  # pyobjc-framework-Vision missing: screenshots still work
        return []
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setRecognitionLanguages_(["de-DE", "en-US"])
    request.setUsesLanguageCorrection_(False)
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(str(path)), None)
    handler.performRequests_error_([request], None)
    texts = []
    for observation in request.results() or []:
        candidates = observation.topCandidates_(1)
        if not candidates or not candidates[0].string().strip():
            continue
        box = observation.boundingBox()  # normalised, origin bottom-left
        x = round((box.origin.x + box.size.width / 2) * width)
        y = round((1 - box.origin.y - box.size.height / 2) * height)
        texts.append((candidates[0].string().strip(), x, y))
    return sorted(texts, key=lambda t: (t[2], t[1]))[:MAX_TEXTS]
