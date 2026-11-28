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
import ctypes
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
            try:
                subprocess.run(["screencapture", "-x", "-m", "-C", str(raw)], check=True, capture_output=True)
            except subprocess.CalledProcessError as exc:
                raise RuntimeError("Keine Bildschirm-Freigabe. Assistent neu über das Startsymbol „hausverwalter.ai Computerbenutzung“ starten, er fragt dann selbst nach.") from exc
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


# US ANSI key codes. Fallback only: on a German keyboard the key labelled Z is not code 6.
ANSI_KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26, "8": 28, "0": 29,
    "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45, "m": 46,
}
_LAYOUT_CACHE: dict[str, int] | None = None


def layout_keycodes() -> dict[str, int]:
    """Character → virtual keycode for the active keyboard, built once. Empty if Carbon cannot be read."""
    global _LAYOUT_CACHE
    if _LAYOUT_CACHE is None:
        try:
            _LAYOUT_CACHE = _read_layout_keycodes()
        except Exception:
            _LAYOUT_CACHE = {}
    return _LAYOUT_CACHE


def keycode_for(char: str) -> int | None:
    """Keycode of one character. The active layout wins, otherwise the US table."""
    name = char.lower()
    table = layout_keycodes()
    if name in table:
        return table[name]
    return ANSI_KEYCODES.get(name)


def _read_layout_keycodes() -> dict[str, int]:
    """Ask UCKeyTranslate which character each keycode produces, with no modifiers."""
    carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    cf.CFDataGetBytePtr.restype = ctypes.c_void_p
    cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
    cf.CFDataGetLength.restype = ctypes.c_long
    cf.CFDataGetLength.argtypes = [ctypes.c_void_p]
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    carbon.TISCopyCurrentKeyboardLayoutInputSource.restype = ctypes.c_void_p
    carbon.TISGetInputSourceProperty.restype = ctypes.c_void_p
    carbon.TISGetInputSourceProperty.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    carbon.LMGetKbdType.restype = ctypes.c_uint32
    carbon.UCKeyTranslate.restype = ctypes.c_uint32
    carbon.UCKeyTranslate.argtypes = [
        ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint16, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint16),
    ]
    source = carbon.TISCopyCurrentKeyboardLayoutInputSource()
    if not source:
        return {}
    try:
        try:
            prop = ctypes.c_void_p.in_dll(carbon, "kTISPropertyUnicodeKeyLayoutData")
        except (ValueError, TypeError, AttributeError):
            prop = cf.CFStringCreateWithCString(None, b"TISPropertyUnicodeKeyLayoutData", 0x08000100)
        data = carbon.TISGetInputSourceProperty(source, prop)
        ptr = cf.CFDataGetBytePtr(data) if data else None
        if not ptr:
            return {}
        translate = _carbon_translate(carbon.UCKeyTranslate, ptr, carbon.LMGetKbdType())
    finally:
        cf.CFRelease(source)
    return _table_from_translate(translate)


def _carbon_translate(uc_key_translate, layout_ptr, keyboard_type):
    """translate(keycode) → the character that key shows, or ''."""

    def translate(code: int) -> str:
        dead = ctypes.c_uint32(0)
        length = ctypes.c_uint32(0)
        chars = (ctypes.c_uint16 * 4)()
        # key action 3 = display, options 1 = no dead keys
        status = uc_key_translate(ctypes.c_void_p(layout_ptr), code, 3, 0, keyboard_type, 1, ctypes.byref(dead), 4, ctypes.byref(length), chars)
        if status != 0 or not length.value:
            return ""
        return "".join(chr(chars[i]) for i in range(length.value))

    return translate


def _table_from_translate(translate) -> dict[str, int]:
    table: dict[str, int] = {}
    if translate is None:
        return table
    for code in range(128):
        text = translate(code)
        if text and len(text) == 1 and text.lower() not in table:
            table[text.lower()] = code
    return table


class FensterNichtGefunden(Exception):
    """No usable window. `.namen` lists visible app names; a minimized hit has an empty list and a fixed message."""

    def __init__(self, namen) -> None:
        if isinstance(namen, str):
            self.namen: list[str] = []
            super().__init__(namen)
        else:
            self.namen = list(namen)
            super().__init__("Fenster nicht gefunden. Sichtbar: " + ", ".join(self.namen))


def choose_window(windows: list[dict], fenster: str) -> dict:
    """Largest normal (on-screen, layer 0) window whose app or title contains `fenster`. Dict keys: owner, title,
    number, pid, x, y, w, h, onscreen, layer. Minimized matches raise; no match lists visible app names."""
    needle = fenster.casefold().strip()
    visible = [w for w in windows if _normal_window(w)]
    namen = sorted({w["owner"] for w in visible if w["owner"]}, key=str.casefold)
    if not needle:
        raise FensterNichtGefunden(namen)
    matches = [w for w in windows if w["layer"] == 0 and w["w"] > 0 and w["h"] > 0 and _name_hit(w["owner"], w["title"], needle)]
    normal = [w for w in matches if w["onscreen"]]
    if normal:
        return max(normal, key=lambda w: (w["w"] * w["h"], w["number"]))
    if matches:
        raise FensterNichtGefunden("Fenster ist minimiert")
    raise FensterNichtGefunden(namen)


def _normal_window(window: dict) -> bool:
    return window["layer"] == 0 and window["onscreen"] and window["w"] > 0 and window["h"] > 0


def _name_hit(owner: str, title: str, needle: str) -> bool:
    return needle in owner.casefold() or needle in title.casefold()


def _cg_bounds(bounds) -> tuple[float, float, float, float]:
    if isinstance(bounds, dict) or hasattr(bounds, "keys"):
        try:
            return float(bounds["X"]), float(bounds["Y"]), float(bounds["Width"]), float(bounds["Height"])
        except Exception:
            pass
    origin, size = getattr(bounds, "origin", None), getattr(bounds, "size", None)
    if origin is not None and size is not None:
        return float(origin.x), float(origin.y), float(size.width), float(size.height)
    return 0.0, 0.0, 0.0, 0.0


def _cg_value(info, key, default=None):
    try:
        return info[key]
    except Exception:
        return default


def list_windows() -> list[dict]:
    """Every window, including covered and minimized ones (kCGWindowListOptionAll)."""
    infos = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID) or []
    windows = []
    for info in infos:
        x, y, w, h = _cg_bounds(_cg_value(info, "kCGWindowBounds"))
        layer = _cg_value(info, "kCGWindowLayer", 0)
        windows.append({
            "owner": str(_cg_value(info, "kCGWindowOwnerName") or ""),
            "title": str(_cg_value(info, "kCGWindowName") or ""),
            "number": int(_cg_value(info, "kCGWindowNumber") or 0),
            "pid": int(_cg_value(info, "kCGWindowOwnerPID") or 0),
            "x": x, "y": y, "w": w, "h": h,
            "onscreen": bool(_cg_value(info, "kCGWindowIsOnscreen")),
            "layer": int(layer or 0),
        })
    return windows


class MacWindowScreen(MacScreen):
    """One window, possibly covered. Screenshots and input go to that window; the person's mouse and keyboard stay put
    and the window is never ordered front. Model coordinates are relative to the window."""

    def __init__(self, fenster: str, *args, **kwargs) -> None:
        self.fenster = fenster
        self.pid = 0
        self.window_id = 0
        self.origin_x = 0.0
        self.origin_y = 0.0
        self._at: tuple[float, float] | None = None  # last point we sent, in screen points
        super().__init__(*args, **kwargs)
        self._locate()

    def screenshot(self) -> str:
        """JPEG of this window only. Position and size are read again first; retina pixels are scaled to the window's point size."""
        self._locate()
        with tempfile.TemporaryDirectory() as tmp:
            raw, small = Path(tmp, "raw.png"), Path(tmp, "small.jpg")
            try:
                subprocess.run(
                    ["screencapture", "-x", "-o", "-l", str(self.window_id), str(raw)],
                    check=True, capture_output=True,
                )
            except subprocess.CalledProcessError as exc:
                raise RuntimeError("Keine Bildschirm-Freigabe. Assistent neu über das Startsymbol „hausverwalter.ai Computerbenutzung“ starten, er fragt dann selbst nach.") from exc
            subprocess.run(
                ["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(JPEG_QUALITY),
                 "-z", str(self.height), str(self.width), str(raw), "--out", str(small)],
                check=True, capture_output=True,
            )
            self.last_texts = read_texts(raw, self.width, self.height)
            return base64.b64encode(small.read_bytes()).decode()

    def run(self, action: dict, look: bool = True) -> str | None:
        self._locate()
        return super().run(action, look)

    def cursor_in_screenshot(self) -> list[int]:
        """Virtual pointer in JPEG pixels: last point we sent, otherwise the window centre. Not the hardware cursor."""
        if self._at is None:
            return [round(self.width / 2), round(self.height / 2)]
        x = (self._at[0] - self.origin_x) * self.width / self.points_w
        y = (self._at[1] - self.origin_y) * self.height / self.points_h
        return [round(x), round(y)]

    def _point(self, coordinate: list[int]) -> tuple[float, float]:
        x, y = super()._point(coordinate)
        return self.origin_x + x, self.origin_y + y

    def _cursor(self) -> tuple[float, float]:
        """Where we last sent the pointer, not the hardware cursor."""
        if self._at is not None:
            return self._at
        return (self.origin_x + self.points_w / 2, self.origin_y + self.points_h / 2)

    def _post_mouse(self, kind: int, point, button=Quartz.kCGMouseButtonLeft, clicks: int = 1, flags: int = 0):
        event = Quartz.CGEventCreateMouseEvent(None, kind, point, button)
        Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, clicks)
        if flags:
            Quartz.CGEventSetFlags(event, flags)
        self._at = (float(point[0]), float(point[1]))
        Quartz.CGEventPostToPid(self.pid, event)

    def _move(self, x: float, y: float) -> None:
        """One move event at the target. No glide and no cursor warp, so the person's mouse does not move."""
        self._check_stop()
        self._post_mouse(Quartz.kCGEventMouseMoved, (x, y))

    def _scroll(self, action: dict) -> None:
        if action.get("coordinate"):
            self._move(*self._point(action["coordinate"]))
        amount = int(action.get("scroll_amount", 3))
        dy = {"up": amount, "down": -amount}.get(action["scroll_direction"], 0)
        dx = {"left": amount, "right": -amount}.get(action["scroll_direction"], 0)
        event = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 2, dy, dx)
        x, y = self._cursor()
        Quartz.CGEventSetLocation(event, (x, y))  # the wheel event otherwise carries the hardware cursor
        Quartz.CGEventPostToPid(self.pid, event)

    def _type(self, text: str) -> None:
        for char in text:
            self._check_stop()
            if char == "\n":
                self._key("Return")
                continue
            for down in (True, False):
                event = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
                Quartz.CGEventKeyboardSetUnicodeString(event, len(char.encode("utf-16-le")) // 2, char)
                Quartz.CGEventPostToPid(self.pid, event)
            time.sleep(0.01)

    def _key(self, combo: str) -> None:
        for part in combo.split():
            *mods, key = part.split("+")
            flags = self._modifier_flags("+".join(mods))
            name = key.lower()
            code = KEY_CODES.get(name)
            text = None
            if code is None:
                code = keycode_for(name)
            if code is None:
                code, text = 0, key
            for down in (True, False):
                event = Quartz.CGEventCreateKeyboardEvent(None, code, down)
                if text is not None:
                    Quartz.CGEventKeyboardSetUnicodeString(event, len(text.encode("utf-16-le")) // 2, text)
                if flags:
                    Quartz.CGEventSetFlags(event, flags)
                Quartz.CGEventPostToPid(self.pid, event)

    def _locate(self) -> None:
        windows = list_windows()
        if self.window_id:
            same = [w for w in windows if w["number"] == self.window_id and w["layer"] == 0]
            if same:
                win = same[0]
                if not win["onscreen"] or win["w"] <= 0 or win["h"] <= 0:
                    raise FensterNichtGefunden("Fenster ist minimiert")
                self._apply(win)
                return
        self._apply(choose_window(windows, self.fenster))

    def _apply(self, win: dict) -> None:
        self.window_id = int(win["number"])
        self.pid = int(win["pid"])
        self.origin_x, self.origin_y = float(win["x"]), float(win["y"])
        self.points_w, self.points_h = max(int(round(win["w"])), 1), max(int(round(win["h"])), 1)
        self.width, self.height = self.points_w, self.points_h
