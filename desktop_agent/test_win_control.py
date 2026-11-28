"""Offline tests for the Windows screen, runnable on macOS: key and text mapping, coordinates, SendInput
structures, OCR result parsing with fakes, action dispatch with SendInput replaced. Nothing is sent.

Run: PYTHONPATH=.. .venv/bin/python -m unittest test_win_control.py
"""

import ctypes
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import win_control as w

WINFUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)

CTRL, SHIFT, ALT, WIN = w.VK_CONTROL, w.VK_SHIFT, w.VK_MENU, w.VK_LWIN


def press(vk, extended=False):
    return [("key", vk, extended, True), ("key", vk, extended, False)]


def held(mods, inner):
    return [*[("key", m, m == WIN, True) for m in mods], *inner, *[("key", m, m == WIN, False) for m in reversed(mods)]]


class KeyMappingTest(unittest.TestCase):
    def test_mac_shortcuts_become_ctrl(self):
        self.assertEqual(w.key_events("cmd+c"), held([CTRL], press(ord("C"))))
        self.assertEqual(w.key_events("super+v"), held([CTRL], press(ord("V"))))
        self.assertEqual(w.key_events("cmd+ctrl+z"), held([CTRL], press(ord("Z"))))  # one ctrl

    def test_named_keys_and_several_combos(self):
        self.assertEqual(w.key_events("Return ctrl+shift+Tab"), [*press(w.VK_RETURN), *held([CTRL, SHIFT], press(w.VK_TAB))])
        self.assertEqual(w.key_events("alt+F4"), held([ALT], press(0x73)))
        self.assertEqual(w.key_events("Page_Down"), press(0x22, True))  # extended, else numpad 3
        self.assertEqual(w.key_events("Left"), press(0x25, True))
        self.assertEqual(w.key_events("KP_Enter"), press(w.VK_RETURN, True))
        self.assertEqual(w.key_events("BackSpace"), press(0x08))

    def test_windows_key(self):
        self.assertEqual(w.key_events("super"), press(WIN, True))  # alone: Start menu
        self.assertEqual(w.key_events("win"), press(WIN, True))
        self.assertEqual(w.key_events("win+r"), held([WIN], press(ord("R"))))

    def test_characters(self):
        self.assertEqual(w.key_events("a"), [("unicode", ord("a"), True), ("unicode", ord("a"), False)])  # typed, any layout
        self.assertEqual(w.key_events("plus"), [("unicode", ord("+"), True), ("unicode", ord("+"), False)])
        layout = {"+": (0xBB, False), "?": (0xDB, True)}.get  # VkKeyScanW on a German layout
        self.assertEqual(w.key_events("ctrl+plus", layout), held([CTRL], press(0xBB)))
        self.assertEqual(w.key_events("ctrl++", layout), held([CTRL], press(0xBB)))
        self.assertEqual(w.key_events("ctrl+question", layout), held([CTRL, SHIFT], press(0xDB)))
        self.assertEqual(w.key_events("shift+1"), held([SHIFT], press(ord("1"))))

    def test_unknown_keys_are_reported(self):
        for combo in ("Hyper_L", "ctrl+schnurz", "foo+a", "ctrl+€"):
            with self.assertRaises(ValueError, msg=combo):
                w.key_events(combo)

    def test_text_as_unicode(self):
        events = w.type_events("Mü\n\t😀\r")
        units = [e[1] for e in events if e[0] == "unicode" and e[2]]
        self.assertEqual(units, [ord("M"), ord("ü"), 0xD83D, 0xDE00])  # emoji as surrogate pair
        keys = [e[1] for e in events if e[0] == "key" and e[3]]
        self.assertEqual(keys, [w.VK_RETURN, w.VK_TAB])
        self.assertEqual(len(events), 12)  # \r skipped

    def test_modifier_text_for_clicks(self):
        self.assertEqual(w.modifier_keys("shift"), [SHIFT])
        self.assertEqual(w.modifier_keys("ctrl+shift"), [CTRL, SHIFT])
        self.assertEqual(w.modifier_keys(None), [])
        with self.assertRaises(ValueError):
            w.modifier_keys("hyper")


class CoordinateTest(unittest.TestCase):
    def test_screenshot_size(self):
        self.assertEqual(w.scaled_size(1920, 1080), (1280, 720))
        self.assertEqual(w.scaled_size(2560, 1600), (1280, 800))
        self.assertEqual(w.scaled_size(1080, 1920), (720, 1280))

    def test_absolute_coordinates_hit_the_pixel(self):
        width, height = 1920, 1080
        for x, y in ((0, 0), (960, 540), (1919, 1079), (1, 1), (1234, 567)):
            nx, ny = w.to_absolute(x, y, width, height)
            self.assertTrue(0 <= nx <= 65535 and 0 <= ny <= 65535)
            self.assertEqual((nx * width // 65536, ny * height // 65536), (x, y))  # how Windows maps them back

    def test_screenshot_to_physical_pixels_and_back(self):
        screen = _screen(2560, 1440)
        self.assertEqual(screen._point([640, 360]), (1280.0, 720.0))
        with mock.patch.object(screen, "_cursor", return_value=(2560, 1440)):
            self.assertEqual(screen.cursor_in_screenshot(), [1280, 720])


class SendInputTest(unittest.TestCase):
    @unittest.skipUnless(ctypes.sizeof(ctypes.c_void_p) == 8, "64-bit layout")
    def test_input_size_matches_64_bit_windows(self):
        self.assertEqual(ctypes.sizeof(w._INPUT), 40)

    def test_events_become_inputs(self):
        events = [("key", CTRL, False, True), ("key", 0x25, True, False), ("unicode", 0xFC, True), ("move", 100, 200), ("button", w.LEFT_DOWN), ("wheel", -360, False), ("wheel", 120, True)]
        inputs = w.build_inputs(events, scan=lambda vk: vk + 1000)
        self.assertEqual([i.type for i in inputs], [w.INPUT_KEYBOARD] * 3 + [w.INPUT_MOUSE] * 4)
        self.assertEqual((inputs[0].ki.wVk, inputs[0].ki.wScan, inputs[0].ki.dwFlags), (CTRL, CTRL + 1000, 0))
        self.assertEqual(inputs[1].ki.dwFlags, w.KEYEVENTF_EXTENDEDKEY | w.KEYEVENTF_KEYUP)
        self.assertEqual((inputs[2].ki.wVk, inputs[2].ki.wScan, inputs[2].ki.dwFlags), (0, 0xFC, w.KEYEVENTF_UNICODE))
        self.assertEqual((inputs[3].mi.dx, inputs[3].mi.dy, inputs[3].mi.dwFlags), (100, 200, w.MOUSEEVENTF_MOVE | w.MOUSEEVENTF_ABSOLUTE))
        self.assertEqual(inputs[4].mi.dwFlags, w.LEFT_DOWN)
        self.assertEqual((inputs[5].mi.mouseData, inputs[5].mi.dwFlags), (-360, w.MOUSEEVENTF_WHEEL))
        self.assertEqual((inputs[6].mi.mouseData, inputs[6].mi.dwFlags), (120, w.MOUSEEVENTF_HWHEEL))


class OcrParsingTest(unittest.TestCase):
    def test_lines_to_centres_top_to_bottom(self):
        def line(text, *rects):
            return SimpleNamespace(text=text, words=[SimpleNamespace(bounding_rect=SimpleNamespace(x=x, y=y, width=ww, height=hh)) for x, y, ww, hh in rects])

        lines = [
            line("Speichern", (1000, 800, 200, 40)),
            line("Datei Bearbeiten", (20, 10, 60, 30), (100, 12, 120, 30)),
            line("   ", (0, 0, 5, 5)),
            line("leer"),
        ]
        texts = w.parse_ocr_lines(lines, 0.5, 0.5)
        self.assertEqual(texts, [("Datei Bearbeiten", 60, 13), ("Speichern", 550, 410)])
        self.assertEqual(len(w.parse_ocr_lines([line(f"z{n}", (0, n, 1, 1)) for n in range(300)], 1, 1)), w.MAX_TEXTS)

    def test_no_winrt_means_no_texts(self):
        reader = w._OcrReader()
        with mock.patch.dict(sys.modules, {"winrt": None, "winrt.windows.media.ocr": None, "winrt.windows.globalization": None}):
            self.assertEqual(reader.read(object(), 1280, 720), [])
        self.assertTrue(reader.unavailable)

    def test_windows_modules_not_loaded_on_import(self):
        for name in ("mss", "winrt", "PIL"):
            self.assertNotIn(name, sys.modules)


class DispatchTest(unittest.TestCase):
    """WinScreen.run with SendInput replaced: which events go out for each action."""

    def setUp(self):
        self.screen = _screen(1920, 1080)
        self.sent = []
        patches = [
            mock.patch.object(self.screen, "_send", side_effect=self.sent.extend),
            mock.patch.object(self.screen, "_cursor", return_value=(0, 0)),
            mock.patch.object(self.screen, "screenshot", return_value="aW1n"),
            mock.patch.object(w.time, "sleep"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_click_glides_then_clicks_with_modifier(self):
        self.screen.run({"action": "left_click", "coordinate": [640, 360], "text": "shift"}, look=False)
        moves = [e for e in self.sent if e[0] == "move"]
        self.assertEqual(len(moves), w.GLIDE_STEPS)
        self.assertEqual(moves[-1], ("move", *w.to_absolute(960, 540, 1920, 1080)))
        rest = [e for e in self.sent if e[0] != "move"]
        self.assertEqual(rest, [("key", SHIFT, False, True), ("button", w.LEFT_DOWN), ("button", w.LEFT_UP), ("key", SHIFT, False, False)])
        self.assertEqual(self.screen.last_click[:2], (960.0, 540.0))

    def test_double_right_and_drag(self):
        self.screen.run({"action": "double_click", "coordinate": [1, 1]}, look=False)
        self.assertEqual([e for e in self.sent if e[0] == "button"], [("button", w.LEFT_DOWN), ("button", w.LEFT_UP)] * 2)
        self.sent.clear()
        self.screen.run({"action": "right_click", "coordinate": [1, 1]}, look=False)
        self.assertEqual([e for e in self.sent if e[0] == "button"], [("button", 0x08), ("button", 0x10)])
        self.sent.clear()
        self.screen.run({"action": "left_click_drag", "start_coordinate": [0, 0], "coordinate": [100, 100]}, look=False)
        buttons = [i for i, e in enumerate(self.sent) if e[0] == "button"]
        self.assertEqual([self.sent[i][1] for i in buttons], [w.LEFT_DOWN, w.LEFT_UP])
        self.assertEqual(buttons[1] - buttons[0] - 1, w.DRAG_STEPS)  # moves between down and up

    def test_scroll_and_keys(self):
        self.screen.run({"action": "scroll", "coordinate": [10, 10], "scroll_direction": "down", "scroll_amount": 3}, look=False)
        self.assertEqual(self.sent[-1], ("wheel", -360, False))
        self.screen.run({"action": "scroll", "scroll_direction": "right", "scroll_amount": 1}, look=False)
        self.assertEqual(self.sent[-1], ("wheel", 120, True))
        self.sent.clear()
        self.screen.run({"action": "key", "text": "cmd+s", "repeat": 2}, look=False)
        self.assertEqual(self.sent, held([CTRL], press(ord("S"))) * 2)

    def test_hold_key_and_screenshot_after_action(self):
        self.assertEqual(self.screen.run({"action": "hold_key", "text": "shift", "duration": 1}), "aW1n")
        self.assertEqual(self.sent, press(SHIFT))

    def test_typing_ends_mid_way_on_stop(self):
        self.screen.stop_event = threading.Event()
        sent = self.sent

        def send(events):
            sent.extend(events)
            if len(sent) >= 4:
                self.screen.stop_event.set()

        self.screen._send.side_effect = send
        with self.assertRaises(w.Interrupted):
            self.screen.run({"action": "type", "text": "Hallo Welt"}, look=False)
        self.assertEqual(len(sent), 4)  # two characters

    def test_unknown_action(self):
        with self.assertRaises(ValueError):
            self.screen.run({"action": "zoom"}, look=False)


def _screen(phys_w, phys_h):
    """WinScreen without __init__ (that needs Windows)."""
    screen = w.WinScreen.__new__(w.WinScreen)
    screen.phys_w, screen.phys_h = phys_w, phys_h
    screen.width, screen.height = w.scaled_size(phys_w, phys_h)
    screen.last_click, screen.last_texts, screen.stop_event = None, [], None
    screen.user32 = SimpleNamespace(VkKeyScanW=lambda char: -1)
    return screen


def _winrow(hwnd, title, app, x, y, width, height, iconic=False):
    return {"hwnd": hwnd, "title": title, "app": app, "x": x, "y": y, "w": width, "h": height, "iconic": iconic, "visible": True}


WIN_ROWS = [
    _winrow(1, "klein", r"C:\App\TextEdit.exe", 0, 0, 100, 80),
    _winrow(2, "Entwurf", r"C:\App\TextEdit.exe", 100, 40, 400, 300),
    _winrow(3, "Start", r"C:\App\Safari.exe", 0, 0, 800, 600, iconic=True),
    _winrow(4, "Dock", r"C:\App\Dock.exe", 0, 800, 1440, 40),
]


class ChooseWinWindowTest(unittest.TestCase):
    def test_largest_match_ignores_case(self):
        self.assertEqual(w.choose_window(WIN_ROWS, "textedit")["hwnd"], 2)
        self.assertEqual(w.choose_window(WIN_ROWS, "ENTWURF")["hwnd"], 2)

    def test_minimized(self):
        with self.assertRaises(w.FensterNichtGefunden) as caught:
            w.choose_window(WIN_ROWS, "safari")
        self.assertEqual(str(caught.exception), "Fenster ist minimiert")

    def test_missing_lists_visible_apps(self):
        with self.assertRaises(w.FensterNichtGefunden) as caught:
            w.choose_window(WIN_ROWS, "gibt es nicht")
        self.assertEqual(caught.exception.namen, ["Dock", "TextEdit"])

    def test_pack_and_point(self):
        self.assertEqual(w.pack_lparam(10, 25), (10 & 0xFFFF) | (25 << 16))
        self.assertEqual(w.pack_lparam(-1, 2) & 0xFFFF, 0xFFFF)


class WinWindowInputTest(unittest.TestCase):
    def setUp(self):
        self.posts = []
        self.screen = w.WinWindowScreen.__new__(w.WinWindowScreen)
        self.screen.fenster = "TextEdit"
        self.screen.hwnd = 2
        self.screen.origin_x, self.screen.origin_y = 100, 40
        self.screen.points_w, self.screen.points_h = 400, 300
        self.screen.width, self.screen.height = 400, 300
        self.screen.last_click, self.screen.last_texts, self.screen.stop_event = None, [], None
        self.screen._at = None
        self.screen._target = 0
        self.screen._client = (0, 0)
        self.screen._locate = lambda: None
        self.screen._child_at = lambda x, y: (55, int(round(x - 100)), int(round(y - 40)))
        names = []

        def call(name):
            def fn(*args):
                names.append(name)
                if name == "PostMessageW":
                    self.posts.append(args)
                    return True
                if name in ("SendInput", "SetForegroundWindow"):
                    raise AssertionError(name)
                if name == "MapVirtualKeyW":
                    return 0
                raise AttributeError(name)
            return fn

        self.screen.user32 = SimpleNamespace(
            PostMessageW=call("PostMessageW"), SendInput=call("SendInput"),
            SetForegroundWindow=call("SetForegroundWindow"), MapVirtualKeyW=call("MapVirtualKeyW"),
            VkKeyScanW=lambda char: -1,
        )
        self.names = names
        patch = mock.patch.object(w.time, "sleep")
        patch.start()
        self.addCleanup(patch.stop)

    def test_click_is_postmessage_not_sendinput(self):
        self.assertEqual(self.screen.cursor_in_screenshot(), [200, 150])
        self.screen.run({"action": "double_click", "coordinate": [50, 60]}, look=False)
        self.assertEqual(self.screen.cursor_in_screenshot(), [50, 60])
        self.assertNotIn("SendInput", self.names)
        self.assertNotIn("SetForegroundWindow", self.names)
        msgs = [row[1] for row in self.posts]
        self.assertEqual(msgs.count(w.WM_MOUSEMOVE), 1)
        self.assertIn(w.WM_LBUTTONDBLCLK, msgs)
        move = self.posts[0]
        self.assertEqual(move[0], 55)
        self.assertEqual(move[3], w.pack_lparam(50, 60))  # screen (150, 100) minus origin (100, 40)

    def test_right_click_wheel_key_and_char(self):
        self.screen.run({"action": "right_click", "coordinate": [1, 1]}, look=False)
        self.assertIn(w.WM_RBUTTONDOWN, [row[1] for row in self.posts])
        self.posts.clear()
        self.screen.run({"action": "scroll", "scroll_direction": "down", "scroll_amount": 3}, look=False)
        wheels = [row for row in self.posts if row[1] == w.WM_MOUSEWHEEL]
        self.assertEqual(len(wheels), 1)
        self.assertEqual(wheels[0][2], ((-360) & 0xFFFF) << 16)
        self.posts.clear()
        self.screen.run({"action": "key", "text": "ctrl+s"}, look=False)
        keys = [row for row in self.posts if row[1] in (w.WM_KEYDOWN, w.WM_KEYUP)]
        self.assertEqual(keys[0][2], CTRL)
        self.assertEqual(keys[1][2], ord("S"))
        self.posts.clear()
        self.screen._type("A")
        chars = [row for row in self.posts if row[1] == w.WM_CHAR]
        self.assertEqual([row[2] for row in chars], [ord("A")])

    def test_drag_keeps_the_mousedown_window(self):
        def child_at(x, y):
            return (55 if x < 200 else 99, 1, 2)

        @WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.POINTER(w._POINT))
        def to_client(hwnd, pt):
            pt.contents.x -= 100
            pt.contents.y -= 40
            return True

        self.screen._child_at = child_at
        self.screen.user32.ScreenToClient = to_client
        self.screen.run({"action": "left_click_drag", "start_coordinate": [0, 0], "coordinate": [300, 0]}, look=False)
        mouse = [row for row in self.posts if row[1] in (w.WM_MOUSEMOVE, w.WM_LBUTTONDOWN, w.WM_LBUTTONUP)]
        self.assertGreaterEqual(len(mouse), 3)
        self.assertTrue(all(row[0] == 55 for row in mouse))
        moves = [row for row in mouse if row[1] == w.WM_MOUSEMOVE]
        self.assertEqual(moves[-1][3], w.pack_lparam(300, 0))

    def test_child_window_and_no_foreground(self):
        seen = {}

        @WINFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, w._POINT)
        def real_child(hwnd, pt):
            seen["pt"] = (pt.x, pt.y)
            return 55

        @WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.POINTER(w._POINT))
        def to_client(hwnd, pt):
            seen.setdefault("order", []).append(("screen", int(hwnd), pt.contents.x, pt.contents.y))
            if int(hwnd) == 2:  # parent: screen (150, 100) → client (50, 60)
                pt.contents.x -= 100
                pt.contents.y -= 40
            else:  # child sits 10 px further in
                pt.contents.x -= 110
                pt.contents.y -= 50
            return True

        @WINFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, w._POINT, ctypes.c_uint)
        def child_ex(hwnd, pt, flags):
            seen["fallback"] = (pt.x, pt.y, flags)
            return 77

        def forbidden(*_args):
            raise AssertionError("foreground")

        self.screen.user32.RealChildWindowFromPoint = real_child
        self.screen.user32.ScreenToClient = to_client
        self.screen.user32.ChildWindowFromPointEx = child_ex
        self.screen.user32.SetForegroundWindow = forbidden
        del self.screen._child_at
        child, x, y = w.WinWindowScreen._child_at(self.screen, 150, 100)
        self.assertEqual(seen["order"][0], ("screen", 2, 150, 100))
        self.assertEqual(seen["pt"], (50, 60))  # parent client point, not the screen point
        self.assertEqual(seen["order"][1], ("screen", 55, 150, 100))
        self.assertEqual((child, x, y), (55, 40, 50))
        self.screen.user32.RealChildWindowFromPoint = lambda *args: 0
        seen.clear()
        child, x, y = w.WinWindowScreen._child_at(self.screen, 150, 100)
        self.assertEqual(seen["fallback"][:2], (50, 60))
        self.assertEqual(child, 77)

    def test_enum_marks_minimized_and_reads_title(self):
        @WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.POINTER(w._RECT))
        def get_rect(hwnd, rect):
            rect.contents.left, rect.contents.top = 10, 20
            rect.contents.right, rect.contents.bottom = 110, 70
            return True

        def enum(cb, _lp):
            cb(7, 0)
            return True

        user32 = SimpleNamespace(
            EnumWindows=enum,
            IsWindowVisible=lambda hwnd: True,
            IsIconic=lambda hwnd: True,
            GetWindowRect=get_rect,
            GetWindowTextLengthW=lambda hwnd: 4,
            GetWindowTextW=lambda hwnd, buf, n: setattr(buf, "value", "Mail") or 4,
            GetWindowThreadProcessId=lambda hwnd, pid: None,
        )
        self.screen.user32 = user32
        self.screen._process_name = lambda hwnd: r"C:\App\Mail.exe"
        rows = self.screen._read_top_windows()
        self.assertEqual(rows[0]["title"], "Mail")
        self.assertTrue(rows[0]["iconic"])
        self.assertEqual((rows[0]["x"], rows[0]["y"], rows[0]["w"], rows[0]["h"]), (10, 20, 100, 50))
        with self.assertRaises(w.FensterNichtGefunden) as caught:
            w.choose_window(rows, "mail")
        self.assertEqual(str(caught.exception), "Fenster ist minimiert")

    def test_print_window_flag_without_foreground(self):
        flags = []

        class Gdi:
            def CreateCompatibleDC(self, hdc):
                return 1

            def CreateCompatibleBitmap(self, hdc, width, height):
                return 2

            def SelectObject(self, dc, obj):
                return 3

            def GetDIBits(self, hdc, bmp, start, lines, buf, info, usage):
                ctypes.memset(buf, 7, 2 * 2 * 4)
                return lines

            def DeleteObject(self, obj):
                return True

            def DeleteDC(self, dc):
                return True

        def print_window(hwnd, hdc, flag):
            flags.append((hwnd, flag))
            return True

        def forbidden(*_args):
            raise AssertionError("foreground or sendinput")

        self.screen.points_w = self.screen.points_h = 2
        self.screen.hwnd = 7
        self.screen.user32.GetWindowDC = lambda hwnd: 9
        self.screen.user32.ReleaseDC = lambda hwnd, hdc: 1
        self.screen.user32.PrintWindow = print_window
        self.screen.user32.SetForegroundWindow = forbidden
        self.screen.user32.SendInput = forbidden
        self.screen._gdi32 = Gdi
        raw = self.screen._capture_bgra()
        self.assertEqual(flags, [(7, w.PW_RENDERFULLCONTENT)])
        self.assertEqual(len(raw), 16)


if __name__ == "__main__":
    unittest.main()
