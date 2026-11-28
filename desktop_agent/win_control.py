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


# --- one window (messages go to the program; the person's mouse and keyboard are not used) ---

WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x0200, 0x0201, 0x0202, 0x0203
WM_RBUTTONDOWN, WM_RBUTTONUP, WM_RBUTTONDBLCLK = 0x0204, 0x0205, 0x0206
WM_MBUTTONDOWN, WM_MBUTTONUP, WM_MBUTTONDBLCLK = 0x0207, 0x0208, 0x0209
WM_MOUSEWHEEL, WM_MOUSEHWHEEL = 0x020A, 0x020E
WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x0100, 0x0101, 0x0102
MK_LBUTTON, MK_RBUTTON, MK_MBUTTON = 0x0001, 0x0002, 0x0010
PW_RENDERFULLCONTENT = 2
_BUTTON_MSG = {
    "left": (WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK, MK_LBUTTON),
    "right": (WM_RBUTTONDOWN, WM_RBUTTONUP, WM_RBUTTONDBLCLK, MK_RBUTTON),
    "middle": (WM_MBUTTONDOWN, WM_MBUTTONUP, WM_MBUTTONDBLCLK, MK_MBUTTON),
}
_BUTTON_FLAG = {
    LEFT_DOWN: ("left", True, False), LEFT_UP: ("left", False, False),
    0x0008: ("right", True, False), 0x0010: ("right", False, False),
    0x0020: ("middle", True, False), 0x0040: ("middle", False, False),
}


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_int32), ("top", ctypes.c_int32), ("right", ctypes.c_int32), ("bottom", ctypes.c_int32)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32), ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]


class FensterNichtGefunden(Exception):
    """No usable window. `.namen` lists visible app names; a minimized hit has an empty list and a fixed message."""

    def __init__(self, namen) -> None:
        if isinstance(namen, str):
            self.namen: list[str] = []
            super().__init__(namen)
        else:
            self.namen = list(namen)
            super().__init__("Fenster nicht gefunden. Sichtbar: " + ", ".join(self.namen))


def pack_lparam(x: int, y: int) -> int:
    """Client or screen point → lParam (low word x, high word y), negatives kept as 16-bit."""
    return (int(x) & 0xFFFF) | ((int(y) & 0xFFFF) << 16)


def app_label(path: str) -> str:
    """File name without a trailing .exe, for matching and the visible-app list."""
    base = path.replace("\\", "/").rsplit("/", 1)[-1]
    return base[:-4] if base.lower().endswith(".exe") else base


def choose_window(windows: list[dict], fenster: str) -> dict:
    """Largest normal top-level window whose title or app name contains `fenster`. Keys: hwnd, title, app, x, y, w, h,
    iconic, visible. A minimized hit raises; no match lists visible app names."""
    needle = fenster.casefold().strip()
    visible = [w for w in windows if w["visible"] and not w["iconic"] and w["w"] > 0 and w["h"] > 0]
    namen = sorted({app_label(w["app"]) for w in visible if app_label(w["app"])}, key=str.casefold)
    if not needle:
        raise FensterNichtGefunden(namen)
    matches = [w for w in windows if w["visible"] and w["w"] > 0 and w["h"] > 0 and _win_name_hit(w, needle)]
    normal = [w for w in matches if not w["iconic"]]
    if normal:
        return max(normal, key=lambda w: (w["w"] * w["h"], int(w["hwnd"])))
    if matches:
        raise FensterNichtGefunden("Fenster ist minimiert")
    raise FensterNichtGefunden(namen)


def _win_name_hit(window: dict, needle: str) -> bool:
    app = window["app"].casefold()
    return needle in window["title"].casefold() or needle in app or needle in app_label(window["app"]).casefold()


def _argtypes(fn, args, rest) -> None:
    if hasattr(fn, "argtypes"):
        fn.argtypes = args
        fn.restype = rest


def _win_callback(*types):
    """stdcall on Windows. On other hosts the same prototype is cdecl, which is enough for tests."""
    return getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(*types)


class WinWindowScreen(WinScreen):
    """One window, possibly covered (PrintWindow). Input is PostMessage to that program; SendInput and
    SetForegroundWindow are never used, so the person's mouse and keyboard stay put."""

    def __init__(self, fenster: str, *args, **kwargs) -> None:
        self.fenster = fenster
        self.hwnd = 0
        self.origin_x = 0
        self.origin_y = 0
        self._at: tuple[float, float] | None = None
        self._target = 0
        self._client = (0, 0)
        super().__init__(*args, **kwargs)
        self._bind()
        self._locate()

    def screenshot(self) -> str:
        """JPEG of this window. Position and size are read again first. Texts are in window coordinates."""
        self._locate()
        raw = self._capture_bgra()
        from PIL import Image

        image = Image.frombuffer("RGB", (self.points_w, self.points_h), raw, "raw", "BGRX", 0, 1)
        if image.size != (self.width, self.height):
            image = image.resize((self.width, self.height), Image.Resampling.LANCZOS)
        self.last_texts = self._ocr.read(image, self.width, self.height)
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=JPEG_QUALITY)
        return base64.b64encode(buffer.getvalue()).decode()

    def run(self, action: dict, look: bool = True) -> str | None:
        self._locate()
        return super().run(action, look)

    def cursor_in_screenshot(self) -> list[int]:
        """Virtual pointer in JPEG pixels: last point we sent, otherwise the window centre."""
        if self._at is None:
            return [round(self.width / 2), round(self.height / 2)]
        x = (self._at[0] - self.origin_x) * self.width / self.points_w
        y = (self._at[1] - self.origin_y) * self.height / self.points_h
        return [round(x), round(y)]

    def _point(self, coordinate: list[int]) -> tuple[float, float]:
        x, y = coordinate
        return self.origin_x + x * self.points_w / self.width, self.origin_y + y * self.points_h / self.height

    def _cursor(self) -> tuple[int, int]:
        if self._at is not None:
            return int(self._at[0]), int(self._at[1])
        return int(self.origin_x + self.points_w / 2), int(self.origin_y + self.points_h / 2)

    def _move(self, x: float, y: float, held: int = 0) -> None:
        self._check_stop()
        self._target, cx, cy = self._child_at(x, y)
        self._client = (cx, cy)
        self._at = (float(x), float(y))
        self._post(self._target, WM_MOUSEMOVE, held, pack_lparam(cx, cy))

    def _click(self, name: str, coordinate, modifier_text: str | None) -> None:
        point = self._point(coordinate) if coordinate else self._cursor()
        self._move(*point)
        self.last_click = (point[0], point[1], time.time())
        mods = modifier_keys(modifier_text)
        self._send([("key", vk, vk == VK_LWIN, True) for vk in mods])
        which = CLICK_BUTTON.get(name, "left")
        count = CLICK_COUNT.get(name, 1)
        try:
            for n in range(1, count + 1):
                self._button(which, True, double=(n == 2))
                self._button(which, False)
                time.sleep(CLICK_GAP_SECONDS)
        finally:
            self._send([("key", vk, vk == VK_LWIN, False) for vk in reversed(mods)])

    def _drag(self, start, end) -> None:
        """Mouse-down picks the child window. The rest of the gesture stays on that HWND."""
        self._move(*start)
        hwnd = self._target or self.hwnd
        self._button("left", True)
        try:
            for i in range(1, DRAG_STEPS + 1):
                self._check_stop()
                x = start[0] + (end[0] - start[0]) * i / DRAG_STEPS
                y = start[1] + (end[1] - start[1]) * i / DRAG_STEPS
                self._move_pinned(hwnd, x, y, MK_LBUTTON)
                time.sleep(0.02)
        finally:
            self._target = hwnd
            self._button("left", False)

    def _move_pinned(self, hwnd, x: float, y: float, held: int) -> None:
        cx, cy = self._client_point(hwnd, x, y)
        self._target = hwnd
        self._client = (cx, cy)
        self._at = (float(x), float(y))
        self._post(hwnd, WM_MOUSEMOVE, held, pack_lparam(cx, cy))

    def _type(self, text: str) -> None:
        for char in text:
            self._check_stop()
            if char == "\n":
                self._key("Return")
                continue
            if char == "\t":
                self._key("Tab")
                continue
            data = char.encode("utf-16-le")
            for i in range(0, len(data), 2):
                self._post(self._target or self.hwnd, WM_CHAR, int.from_bytes(data[i:i + 2], "little"), 1)
            time.sleep(TYPE_GAP_SECONDS)

    def _send(self, events: list[Event]) -> None:
        """Key and button events as window messages. Never SendInput."""
        for event in events:
            kind = event[0]
            if kind == "key":
                _, vk, extended, down = event
                self._post_key(vk, down, extended)
            elif kind == "unicode":
                _, unit, down = event
                if down:
                    self._post(self._target or self.hwnd, WM_CHAR, unit, 1)
            elif kind == "button":
                which, down, _double = _BUTTON_FLAG[event[1]]
                self._button(which, down)
            elif kind == "wheel":
                _, delta, horizontal = event
                sx, sy = self._cursor()
                msg = WM_MOUSEHWHEEL if horizontal else WM_MOUSEWHEEL
                self._post(self._target or self.hwnd, msg, (int(delta) & 0xFFFF) << 16, pack_lparam(sx, sy))
            elif kind == "move":
                continue
            else:
                raise ValueError(f"unknown input event {event!r}")

    def _button(self, which: str, down: bool, double: bool = False) -> None:
        if not self._target:
            self._move(*self._cursor())
        down_msg, up_msg, dbl_msg, mk = _BUTTON_MSG[which]
        msg = dbl_msg if double else (down_msg if down else up_msg)
        self._post(self._target, msg, mk if (down or double) else 0, pack_lparam(*self._client))

    def _post_key(self, vk: int, down: bool, extended: bool) -> None:
        scan = 0
        mapper = getattr(self.user32, "MapVirtualKeyW", None)
        if mapper:
            scan = int(mapper(vk, 0)) & 0xFF
        lparam = 1 | (scan << 16)
        if extended:
            lparam |= 1 << 24
        if not down:
            lparam |= (1 << 30) | (1 << 31)
        self._post(self._target or self.hwnd, WM_KEYDOWN if down else WM_KEYUP, vk, lparam)

    def _post(self, hwnd, msg: int, wparam: int, lparam: int) -> None:
        self.user32.PostMessageW(hwnd, msg, wparam, lparam)

    def _client_point(self, hwnd, x: float, y: float) -> tuple[int, int]:
        client = _POINT(int(round(x)), int(round(y)))
        self.user32.ScreenToClient(hwnd, ctypes.byref(client))
        return int(client.x), int(client.y)

    def _child_at(self, screen_x: float, screen_y: float) -> tuple[int, int, int]:
        """Child hit-testing wants the parent's client coordinates, not screen coordinates."""
        px, py = self._client_point(self.hwnd, screen_x, screen_y)
        child = self.user32.RealChildWindowFromPoint(self.hwnd, _POINT(px, py)) if hasattr(self.user32, "RealChildWindowFromPoint") else 0
        if not child and hasattr(self.user32, "ChildWindowFromPointEx"):
            child = self.user32.ChildWindowFromPointEx(self.hwnd, _POINT(px, py), 0x0001 | 0x0004)
        if not child:
            child = self.hwnd
        cx, cy = self._client_point(child, screen_x, screen_y)
        return int(child), cx, cy

    def _locate(self) -> None:
        windows = self._read_top_windows()
        if self.hwnd:
            same = [w for w in windows if int(w["hwnd"]) == int(self.hwnd)]
            if same:
                win = same[0]
                if win["iconic"] or win["w"] <= 0 or win["h"] <= 0:
                    raise FensterNichtGefunden("Fenster ist minimiert")
                self._apply(win)
                return
        self._apply(choose_window(windows, self.fenster))

    def _apply(self, win: dict) -> None:
        self.hwnd = int(win["hwnd"])
        self.origin_x, self.origin_y = int(win["x"]), int(win["y"])
        self.points_w, self.points_h = max(int(win["w"]), 1), max(int(win["h"]), 1)
        self.width, self.height = self.points_w, self.points_h

    def _read_top_windows(self) -> list[dict]:
        found: list[dict] = []

        def callback(hwnd, _lparam) -> bool:
            if not self.user32.IsWindowVisible(hwnd):
                return True
            rect = _RECT()
            self.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            found.append({
                "hwnd": int(hwnd),
                "title": self._window_text(hwnd),
                "app": self._process_name(hwnd),
                "x": int(rect.left), "y": int(rect.top),
                "w": int(rect.right - rect.left), "h": int(rect.bottom - rect.top),
                "iconic": bool(self.user32.IsIconic(hwnd)),
                "visible": True,
            })
            return True

        enum = _win_callback(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(callback)
        self.user32.EnumWindows(enum, 0)
        return found

    def _window_text(self, hwnd) -> str:
        length = int(self.user32.GetWindowTextLengthW(hwnd) or 0)
        buf = ctypes.create_unicode_buffer(length + 1)
        self.user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value

    def _process_name(self, hwnd) -> str:
        pid = ctypes.c_uint32()
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        kernel = self._kernel32()
        handle = kernel.OpenProcess(0x1000, False, pid.value)
        if not handle:
            return ""
        try:
            size = ctypes.c_uint32(512)
            buf = ctypes.create_unicode_buffer(512)
            if not kernel.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return ""
            return buf.value
        finally:
            kernel.CloseHandle(handle)

    def _kernel32(self):
        if not hasattr(self, "_kernel"):
            self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            _argtypes(self._kernel.OpenProcess, [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32], ctypes.c_void_p)
            _argtypes(self._kernel.CloseHandle, [ctypes.c_void_p], ctypes.c_bool)
            _argtypes(
                self._kernel.QueryFullProcessImageNameW,
                [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)],
                ctypes.c_bool,
            )
        return self._kernel

    def _capture_bgra(self) -> bytes:
        """PrintWindow into a top-down BGRX buffer. Flag 2 paints the window even when it is covered."""
        user32, gdi = self.user32, self._gdi32()
        self._bind_gdi(gdi)
        w, h = self.points_w, self.points_h
        hwnd = self.hwnd
        hdc_win = user32.GetWindowDC(hwnd)
        mem = gdi.CreateCompatibleDC(hdc_win)
        bmp = gdi.CreateCompatibleBitmap(hdc_win, w, h)
        old = gdi.SelectObject(mem, bmp)
        try:
            if not user32.PrintWindow(hwnd, mem, PW_RENDERFULLCONTENT):
                raise RuntimeError("Fensterbild fehlgeschlagen.")
            info = _BITMAPINFO()
            info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            info.bmiHeader.biWidth = w
            info.bmiHeader.biHeight = h
            info.bmiHeader.biPlanes = 1
            info.bmiHeader.biBitCount = 32
            info.bmiHeader.biCompression = 0
            buf = ctypes.create_string_buffer(w * h * 4)
            if gdi.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(info), 0) == 0:
                raise RuntimeError("Fensterbild fehlgeschlagen.")
            row = w * 4
            rows = [buf.raw[i:i + row] for i in range(0, len(buf.raw), row)]
            return b"".join(reversed(rows))
        finally:
            if mem and old:
                gdi.SelectObject(mem, old)
            if bmp:
                gdi.DeleteObject(bmp)
            if mem:
                gdi.DeleteDC(mem)
            if hdc_win:
                user32.ReleaseDC(hwnd, hdc_win)

    def _bind(self) -> None:
        user32 = self.user32
        _argtypes(user32.PostMessageW, [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_size_t], ctypes.c_bool)
        _argtypes(user32.RealChildWindowFromPoint, [ctypes.c_void_p, _POINT], ctypes.c_void_p)
        _argtypes(user32.ChildWindowFromPointEx, [ctypes.c_void_p, _POINT, ctypes.c_uint], ctypes.c_void_p)
        _argtypes(user32.ScreenToClient, [ctypes.c_void_p, ctypes.POINTER(_POINT)], ctypes.c_bool)
        _argtypes(user32.EnumWindows, [_win_callback(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p), ctypes.c_void_p], ctypes.c_bool)
        _argtypes(user32.IsWindowVisible, [ctypes.c_void_p], ctypes.c_bool)
        _argtypes(user32.IsIconic, [ctypes.c_void_p], ctypes.c_bool)
        _argtypes(user32.GetWindowRect, [ctypes.c_void_p, ctypes.POINTER(_RECT)], ctypes.c_bool)
        _argtypes(user32.GetWindowTextLengthW, [ctypes.c_void_p], ctypes.c_int)
        _argtypes(user32.GetWindowTextW, [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int], ctypes.c_int)
        _argtypes(user32.GetWindowThreadProcessId, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)], ctypes.c_uint32)
        _argtypes(user32.GetWindowDC, [ctypes.c_void_p], ctypes.c_void_p)
        _argtypes(user32.ReleaseDC, [ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int)
        _argtypes(user32.PrintWindow, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint], ctypes.c_bool)

    def _gdi32(self):
        return ctypes.WinDLL("gdi32", use_last_error=True)

    def _bind_gdi(self, gdi) -> None:
        _argtypes(gdi.CreateCompatibleDC, [ctypes.c_void_p], ctypes.c_void_p)
        _argtypes(gdi.CreateCompatibleBitmap, [ctypes.c_void_p, ctypes.c_int, ctypes.c_int], ctypes.c_void_p)
        _argtypes(gdi.SelectObject, [ctypes.c_void_p, ctypes.c_void_p], ctypes.c_void_p)
        _argtypes(gdi.DeleteObject, [ctypes.c_void_p], ctypes.c_bool)
        _argtypes(gdi.DeleteDC, [ctypes.c_void_p], ctypes.c_bool)
        _argtypes(
            gdi.GetDIBits,
            [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(_BITMAPINFO), ctypes.c_uint],
            ctypes.c_int,
        )
