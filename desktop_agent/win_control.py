"""Executes the model's computer actions on Windows: same {"action": ...} dicts and interface as mac_control.MacScreen.

Screenshots: mss (primary monitor), scaled so the long edge is MAX_EDGE px, JPEG in memory
(nothing on disk), with the pointer drawn in. The model's coordinates live in that screenshot
space and are mapped back to physical pixels (the process is per-monitor DPI aware).
Mouse and keyboard: SendInput, so remote-desktop and Citrix windows get real input events.
Text is typed as Unicode (KEYEVENTF_UNICODE, layout independent), named keys and modifiers
as virtual keys with scan codes; cmd/super as a modifier → ctrl, "win" = Windows key. The
pointer glides to its target like on the Mac. OCR: Windows.Media.Ocr (built in, on-device,
German, else the user's profile languages) via the winrt packages; [] when not available.

The pure helpers (key_events, type_events, to_absolute, build_inputs, parse_ocr_lines) need
no Windows and are unit-tested on macOS. Windows-only modules (ctypes.WinDLL, mss, PIL, winrt)
are loaded inside WinScreen only.
"""

from __future__ import annotations

import base64
import ctypes
import io
import time
from typing import Any, Callable

MAX_EDGE = 1280
SETTLE_SECONDS = 0.3
GLIDE_SECONDS = 0.2
GLIDE_STEPS = 16
DRAG_STEPS = 10
CLICK_GAP_SECONDS = 0.05
TYPE_GAP_SECONDS = 0.005
JPEG_QUALITY = 70  # same as the Mac: a third of the PNG size, text stays readable
MAX_TEXTS = 200
OCR_LANGUAGE = "de-DE"
OCR_MAX_DIMENSION = 2600  # Windows.Media.Ocr refuses larger images (OcrEngine.MaxImageDimension)
WHEEL_DELTA = 120
MAX_HOLD_SECONDS = 10

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0001, 0x0002, 0x0004
MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE = 0x0001, 0x8000
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
LEFT_DOWN, LEFT_UP = 0x0002, 0x0004
BUTTONS = {"left": (LEFT_DOWN, LEFT_UP), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}
CLICK_BUTTON = {"right_click": "right", "middle_click": "middle"}
CLICK_COUNT = {"double_click": 2, "triple_click": 3}

VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN = 0x10, 0x11, 0x12, 0x5B
VK_RETURN, VK_TAB = 0x0D, 0x09
# Modifier names as the model sends them (xdotool style). cmd/super/meta → ctrl: shortcuts trained on the Mac work here.
MODIFIERS = {
    "ctrl": VK_CONTROL, "control": VK_CONTROL, "control_l": VK_CONTROL, "control_r": VK_CONTROL, "strg": VK_CONTROL,
    "cmd": VK_CONTROL, "command": VK_CONTROL, "super": VK_CONTROL, "super_l": VK_CONTROL, "super_r": VK_CONTROL, "meta": VK_CONTROL,
    "shift": VK_SHIFT, "shift_l": VK_SHIFT, "shift_r": VK_SHIFT,
    "alt": VK_MENU, "alt_l": VK_MENU, "alt_r": VK_MENU, "option": VK_MENU,
    "win": VK_LWIN, "windows": VK_LWIN,
}
WINDOWS_KEY_ALONE = {"super", "super_l", "super_r", "win", "windows"}  # pressed alone: Start menu
KEYS = {
    "return": VK_RETURN, "enter": VK_RETURN, "kp_enter": VK_RETURN, "tab": VK_TAB, "space": 0x20,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "escape": 0x1B, "esc": 0x1B,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "home": 0x24, "end": 0x23, "page_up": 0x21, "prior": 0x21, "pageup": 0x21, "page_down": 0x22, "next": 0x22, "pagedown": 0x22,
    "caps_lock": 0x14, "num_lock": 0x90, "scroll_lock": 0x91, "print": 0x2C, "pause": 0x13, "menu": 0x5D, "apps": 0x5D,
    "kp_multiply": 0x6A, "kp_add": 0x6B, "kp_subtract": 0x6D, "kp_decimal": 0x6E, "kp_divide": 0x6F,
    **{f"kp_{n}": 0x60 + n for n in range(10)},
    **{f"f{n}": 0x6F + n for n in range(1, 25)},
}
# Keys outside the main block need the extended flag, else e.g. arrows arrive as numpad keys.
EXTENDED = {0x2E, 0x2D, 0x25, 0x26, 0x27, 0x28, 0x24, 0x23, 0x21, 0x22, 0x5B, 0x5D, 0x6F, 0x90, 0x2C}
EXTENDED_NAMES = {"kp_enter"}
# xdotool names of symbol keys → the character.
CHARS = {
    "plus": "+", "minus": "-", "period": ".", "comma": ",", "slash": "/", "backslash": "\\", "equal": "=",
    "semicolon": ";", "colon": ":", "apostrophe": "'", "quotedbl": '"', "grave": "`", "asciitilde": "~",
    "asciicircum": "^", "bracketleft": "[", "bracketright": "]", "braceleft": "{", "braceright": "}",
    "parenleft": "(", "parenright": ")", "less": "<", "greater": ">", "bar": "|", "asterisk": "*",
    "underscore": "_", "at": "@", "numbersign": "#", "exclam": "!", "question": "?", "dollar": "$",
    "percent": "%", "ampersand": "&", "section": "§", "degree": "°",
    "adiaeresis": "ä", "odiaeresis": "ö", "udiaeresis": "ü", "ssharp": "ß",
}

Event = tuple  # ("key", vk, extended, down) | ("unicode", utf16_unit, down) | ("move", nx, ny) | ("button", flags) | ("wheel", delta, horizontal)


# --- pure helpers (no Windows needed) ------------------------------------------------------------


def scaled_size(width: int, height: int, max_edge: int = MAX_EDGE) -> tuple[int, int]:
    """Screenshot size for the model: long edge max_edge, aspect ratio kept."""
    scale = max_edge / max(width, height)
    return round(width * scale), round(height * scale)


def to_absolute(x: float, y: float, width: int, height: int) -> tuple[int, int]:
    """Physical pixel on the primary monitor → SendInput absolute coordinates (0..65535), aimed at the pixel centre."""
    return min(int((x + 0.5) * 65536 / width), 65535), min(int((y + 0.5) * 65536 / height), 65535)


def modifier_keys(text: str | None) -> list[int]:
    """"ctrl+shift" (or None) → virtual keys, duplicates removed (cmd+ctrl is one ctrl)."""
    vks: list[int] = []
    for name in (text or "").lower().replace(" ", "+").split("+"):
        if not name:
            continue
        if name not in MODIFIERS:
            raise ValueError(f"Unbekannte Taste: {name}")
        if MODIFIERS[name] not in vks:
            vks.append(MODIFIERS[name])
    return vks


def key_events(combo: str, char_key: Callable[[str], tuple[int, bool] | None] | None = None) -> list[Event]:
    """xdotool combo(s) like "ctrl+shift+Tab" or "cmd+c Return" → input events in order: modifiers down,
    key down and up, modifiers up. A character without modifiers is typed as Unicode; with modifiers it
    needs a key: letters and digits directly, others via char_key (VkKeyScanW: vk and whether shift is needed)."""
    events: list[Event] = []
    for part in combo.split():
        if part.endswith("++"):
            mods, key = [m for m in part[:-2].split("+") if m], "+"
        elif part == "+":
            mods, key = [], "+"
        else:
            *mods, key = part.split("+")
        name = key.lower()
        vks = modifier_keys("+".join(mods))
        char = CHARS.get(name) or (key if len(key) == 1 else None)
        if not mods and name in WINDOWS_KEY_ALONE:
            vk, extended = VK_LWIN, True
        elif name in MODIFIERS:
            vk, extended = MODIFIERS[name], MODIFIERS[name] == VK_LWIN
        elif name in KEYS:
            vk, extended = KEYS[name], KEYS[name] in EXTENDED or name in EXTENDED_NAMES
        elif char and not vks:
            events += type_events(char)
            continue
        elif char and char.isascii() and char.isalnum():
            vk, extended = ord(char.upper()), False
        elif char and char_key and (found := char_key(char)):
            vk, shift = found
            extended = False
            if shift and VK_SHIFT not in vks:
                vks.append(VK_SHIFT)
        else:
            raise ValueError(f"Unbekannte Taste: {key}")
        events += [("key", m, m == VK_LWIN, True) for m in vks]
        events += [("key", vk, extended, True), ("key", vk, extended, False)]
        events += [("key", m, m == VK_LWIN, False) for m in reversed(vks)]
    return events


def type_events(text: str) -> list[Event]:
    """Text → Unicode key events (UTF-16 units, so emoji become surrogate pairs). Newline = Return, tab = Tab."""
    events: list[Event] = []
    for char in text:
        if char == "\r":
            continue
        if char in "\n\t":
            vk = VK_RETURN if char == "\n" else VK_TAB
            events += [("key", vk, False, True), ("key", vk, False, False)]
            continue
        data = char.encode("utf-16-le")
        for i in range(0, len(data), 2):
            unit = int.from_bytes(data[i:i + 2], "little")
            events += [("unicode", unit, True), ("unicode", unit, False)]
    return events


def parse_ocr_lines(lines: Any, scale_x: float, scale_y: float, limit: int = MAX_TEXTS) -> list[tuple[str, int, int]]:
    """Windows.Media.Ocr lines (text + words with bounding_rect) → (text, x, y) line centres in
    screenshot pixels, top to bottom, at most `limit`."""
    texts = []
    for line in lines or []:
        text = (line.text or "").strip()
        rects = [word.bounding_rect for word in line.words or []]
        if not text or not rects:
            continue
        x0, y0 = min(r.x for r in rects), min(r.y for r in rects)
        x1, y1 = max(r.x + r.width for r in rects), max(r.y + r.height for r in rects)
        texts.append((text, round((x0 + x1) / 2 * scale_x), round((y0 + y1) / 2 * scale_y)))
    return sorted(texts, key=lambda t: (t[2], t[1]))[:limit]


# --- SendInput structures (fixed-size fields: same layout as 64-bit Windows on any 64-bit host) ----


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_int32), ("dy", ctypes.c_int32), ("mouseData", ctypes.c_int32),
                ("dwFlags", ctypes.c_uint32), ("time", ctypes.c_uint32), ("dwExtraInfo", ctypes.c_size_t)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_uint16), ("wScan", ctypes.c_uint16), ("dwFlags", ctypes.c_uint32),
                ("time", ctypes.c_uint32), ("dwExtraInfo", ctypes.c_size_t)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_uint32), ("wParamL", ctypes.c_uint16), ("wParamH", ctypes.c_uint16)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_uint32), ("u", _INPUTUNION)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


def build_inputs(events: list[Event], scan: Callable[[int], int] = lambda vk: 0) -> ctypes.Array:
    """Events → INPUT array for SendInput. scan(vk) gives the hardware scan code (MapVirtualKeyW);
    remote-desktop clients forward scan codes, so named keys carry both."""
    inputs = (_INPUT * len(events))()
    for item, event in zip(inputs, events):
        kind = event[0]
        if kind == "key":
            _, vk, extended, down = event
            item.type = INPUT_KEYBOARD
            item.ki = _KEYBDINPUT(vk, scan(vk), (KEYEVENTF_EXTENDEDKEY if extended else 0) | (0 if down else KEYEVENTF_KEYUP), 0, 0)
        elif kind == "unicode":
            _, unit, down = event
            item.type = INPUT_KEYBOARD
            item.ki = _KEYBDINPUT(0, unit, KEYEVENTF_UNICODE | (0 if down else KEYEVENTF_KEYUP), 0, 0)
        elif kind == "move":
            _, nx, ny = event
            item.type = INPUT_MOUSE
            item.mi = _MOUSEINPUT(nx, ny, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, 0, 0)
        elif kind == "button":
            item.type = INPUT_MOUSE
            item.mi = _MOUSEINPUT(0, 0, 0, event[1], 0, 0)
        elif kind == "wheel":
            _, delta, horizontal = event
            item.type = INPUT_MOUSE
            item.mi = _MOUSEINPUT(0, 0, delta, MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL, 0, 0)
        else:
            raise ValueError(f"unknown input event {event!r}")
    return inputs


def dpi_aware() -> None:
    """Physical pixels everywhere (screenshots, SendInput, GetSystemMetrics). Must run before any window exists."""
    user32 = ctypes.WinDLL("user32")
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):  # per monitor v2, Windows 10 1703+
            return
    except AttributeError:
        pass
    try:
        if ctypes.WinDLL("shcore").SetProcessDpiAwareness(2) == 0:  # per monitor, Windows 8.1+
            return
    except (AttributeError, OSError):
        pass
    user32.SetProcessDPIAware()


# --- the screen -----------------------------------------------------------------------------------


class Interrupted(RuntimeError):
    """Stopp was pressed in the middle of an action (long typing, a glide)."""


class WinScreen:
    platform = "win32"

    def __init__(self) -> None:
        dpi_aware()
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int]
        self.user32.SendInput.restype = ctypes.c_uint
        self.user32.VkKeyScanW.argtypes = [ctypes.c_wchar]
        self.user32.VkKeyScanW.restype = ctypes.c_short
        self.user32.MapVirtualKeyW.argtypes = [ctypes.c_uint, ctypes.c_uint]
        self.user32.MapVirtualKeyW.restype = ctypes.c_uint
        # Primary monitor in physical pixels; its top-left is (0, 0) of the virtual screen.
        self.phys_w, self.phys_h = self.user32.GetSystemMetrics(0), self.user32.GetSystemMetrics(1)
        self.width, self.height = scaled_size(self.phys_w, self.phys_h)
        # Last click in physical pixels + time, for the overlay's click ring.
        self.last_click: tuple[float, float, float] | None = None
        # Texts on the last screenshot with their centre in screenshot pixels (on-device OCR).
        self.last_texts: list[tuple[str, int, int]] = []
        self.stop_event = None  # set by agent.Session
        self._ocr = _OcrReader()

    # --- screenshot -----------------------------------------------------------------------

    def screenshot(self) -> str:
        """Base64 JPEG in the model's coordinate space; also reads the screen's texts into last_texts."""
        import mss
        from PIL import Image

        with (getattr(mss, "mss", None) or mss.MSS)() as capture:  # tolerates both factory names
            raw = capture.grab({"left": 0, "top": 0, "width": self.phys_w, "height": self.phys_h})
        full = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        self.last_texts = self._ocr.read(full, self.width, self.height)
        _draw_pointer(full, *self._cursor())
        small = full.resize((self.width, self.height), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        small.save(buffer, "JPEG", quality=JPEG_QUALITY)
        return base64.b64encode(buffer.getvalue()).decode()

    # --- actions --------------------------------------------------------------------------

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
            self._send([("button", LEFT_DOWN if name == "left_mouse_down" else LEFT_UP)])
        elif name == "scroll":
            self._scroll(action)
        elif name == "type":
            self._type(action["text"])
        elif name == "key":
            for _ in range(int(action.get("repeat", 1) or 1)):
                self._send(key_events(action["text"], self._char_key))
        elif name == "hold_key":
            self._hold(action["text"], float(action.get("duration", 1)))
        elif name == "cursor_position":
            return None
        else:
            raise ValueError(f"Aktion nicht unterstützt: {name}")
        time.sleep(SETTLE_SECONDS)
        return self.screenshot() if look else None

    def cursor_in_screenshot(self) -> list[int]:
        x, y = self._cursor()
        return [round(x * self.width / self.phys_w), round(y * self.height / self.phys_h)]

    # --- helpers --------------------------------------------------------------------------

    def _check_stop(self) -> None:
        stop = getattr(self, "stop_event", None)
        if stop is not None and stop.is_set():
            raise Interrupted("Vom Nutzer gestoppt.")

    def _point(self, coordinate: list[int]) -> tuple[float, float]:
        x, y = coordinate
        return x * self.phys_w / self.width, y * self.phys_h / self.height

    def _cursor(self) -> tuple[int, int]:
        point = _POINT()
        self.user32.GetCursorPos(ctypes.byref(point))
        return point.x, point.y

    def _send(self, events: list[Event]) -> None:
        if not events:
            return
        inputs = build_inputs(events, lambda vk: self.user32.MapVirtualKeyW(vk, 0))
        sent = self.user32.SendInput(len(inputs), inputs, ctypes.sizeof(_INPUT))
        if sent != len(inputs):
            # UIPI: windows of programs running as administrator do not take input from normal programs
            raise OSError("Eingabe wurde blockiert (z. B. von einem Fenster mit Administratorrechten).")

    def _char_key(self, char: str) -> tuple[int, bool] | None:
        """Key for a character in the current keyboard layout (QWERTZ aware), None if it needs AltGr or is missing."""
        if len(char) != 1 or ord(char) > 0xFFFF:
            return None
        result = self.user32.VkKeyScanW(char)
        if result == -1 or (result >> 8) & 0x06:  # 2 = ctrl, 4 = alt (AltGr characters)
            return None
        return result & 0xFF, bool((result >> 8) & 0x01)

    def _move(self, x: float, y: float) -> None:
        """Glide from the current position (ease in/out) instead of jumping."""
        x0, y0 = self._cursor()
        for i in range(1, GLIDE_STEPS + 1):
            self._check_stop()
            t = i / GLIDE_STEPS
            t = t * t * (3 - 2 * t)
            self._send([("move", *to_absolute(x0 + (x - x0) * t, y0 + (y - y0) * t, self.phys_w, self.phys_h))])
            time.sleep(GLIDE_SECONDS / GLIDE_STEPS)

    def _click(self, name: str, coordinate, modifier_text: str | None) -> None:
        point = self._point(coordinate) if coordinate else self._cursor()
        self._move(*point)
        self.last_click = (point[0], point[1], time.time())
        mods = modifier_keys(modifier_text)
        down, up = BUTTONS[CLICK_BUTTON.get(name, "left")]
        self._send([("key", vk, vk == VK_LWIN, True) for vk in mods])
        try:
            for _ in range(CLICK_COUNT.get(name, 1)):
                self._send([("button", down), ("button", up)])
                time.sleep(CLICK_GAP_SECONDS)
        finally:
            self._send([("key", vk, vk == VK_LWIN, False) for vk in reversed(mods)])

    def _drag(self, start, end) -> None:
        self._move(*start)
        self._send([("button", LEFT_DOWN)])
        try:
            for i in range(1, DRAG_STEPS + 1):
                x, y = start[0] + (end[0] - start[0]) * i / DRAG_STEPS, start[1] + (end[1] - start[1]) * i / DRAG_STEPS
                self._send([("move", *to_absolute(x, y, self.phys_w, self.phys_h))])
                time.sleep(0.02)
        finally:
            self._send([("button", LEFT_UP)])

    def _scroll(self, action: dict) -> None:
        if action.get("coordinate"):
            self._move(*self._point(action["coordinate"]))
        amount = int(action.get("scroll_amount", 3))
        direction = action["scroll_direction"]
        delta = WHEEL_DELTA * amount * (1 if direction in ("up", "right") else -1)  # wheel: + = up; horizontal: + = right
        mods = modifier_keys(action.get("text"))
        self._send([
            *[("key", vk, vk == VK_LWIN, True) for vk in mods],
            ("wheel", delta, direction in ("left", "right")),
            *[("key", vk, vk == VK_LWIN, False) for vk in reversed(mods)],
        ])

    def _type(self, text: str) -> None:
        for char in text:
            self._check_stop()
            self._send(type_events(char))
            time.sleep(TYPE_GAP_SECONDS)

    def _hold(self, combo: str, seconds: float) -> None:
        events = key_events(combo, self._char_key)
        downs = [e for e in events if e[-1] is True]
        ups = [e for e in events if e[-1] is False]
        self._send(downs)
        try:
            time.sleep(min(seconds, MAX_HOLD_SECONDS))
        finally:
            self._send(ups)


class _OcrReader:
    """Windows.Media.Ocr engine, created on first use. read() returns [] when OCR is not available."""

    def __init__(self) -> None:
        self.engine: Any = None
        self.unavailable = False

    def read(self, image: Any, width: int, height: int) -> list[tuple[str, int, int]]:
        if self.unavailable:
            return []
        try:
            if self.engine is None:
                self.engine = _ocr_engine()
                if self.engine is None:
                    self.unavailable = True  # winrt packages or every OCR language missing
                    return []
            return _recognize(self.engine, image, width, height)
        except ImportError:
            self.unavailable = True
            return []
        except Exception:  # one bad frame: screenshot still goes out, just without texts
            return []


def _ocr_engine() -> Any:
    try:
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine
    except ImportError:
        return None
    language = Language(OCR_LANGUAGE)
    if OcrEngine.is_language_supported(language):
        return OcrEngine.try_create_from_language(language)
    return OcrEngine.try_create_from_user_profile_languages()  # None when no OCR language is installed


def _recognize(engine: Any, image: Any, width: int, height: int) -> list[tuple[str, int, int]]:
    import asyncio

    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.storage.streams import DataWriter

    scale = min(1.0, OCR_MAX_DIMENSION / max(image.size))
    if scale < 1:
        image = image.resize((int(image.width * scale), int(image.height * scale)))
    pixels = image.convert("RGBA").tobytes("raw", "BGRA")
    writer = DataWriter()
    try:
        writer.write_bytes(pixels)
    except TypeError:  # older winrt projections take only a sequence of ints
        writer.write_bytes(list(pixels))
    bitmap = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.BGRA8, image.width, image.height)

    async def recognize() -> Any:
        return await engine.recognize_async(bitmap)

    result = asyncio.run(recognize())
    return parse_ocr_lines(result.lines, width / image.width, height / image.height)


def _draw_pointer(image: Any, x: int, y: int) -> None:
    """mss leaves the pointer out; the model needs to see where it is (the Mac capture shows it with -C)."""
    from PIL import ImageDraw

    arrow = [(0, 0), (0, 17), (4, 13), (7, 20), (10, 19), (7, 12), (12, 12)]
    ImageDraw.Draw(image).polygon([(x + dx, y + dy) for dx, dy in arrow], fill="white", outline="black")
