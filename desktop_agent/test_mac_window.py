"""Window mode on the Mac, without screen-recording or accessibility permission.

Run: PYTHONPATH=.. ../.venv/bin/python -m unittest test_mac_window.py
"""

import ctypes
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

import Quartz

import mac_control as m


def win(owner, title, number, pid, x, y, w, h, onscreen=True, layer=0):
    return {"owner": owner, "title": title, "number": number, "pid": pid, "x": x, "y": y, "w": w, "h": h, "onscreen": onscreen, "layer": layer}


WINDOWS = [
    win("Finder", "Schreibtisch", 1, 10, 0, 0, 1440, 900),
    win("TextEdit", "klein", 2, 20, 10, 10, 200, 100),
    win("TextEdit", "Entwurf", 3, 20, 100, 40, 400, 300),
    win("Safari", "Start", 4, 30, 0, 0, 800, 600, onscreen=False),
    win("Dock", "", 5, 40, 0, 800, 1440, 80, layer=20),
]


class ChooseWindowTest(unittest.TestCase):
    def test_largest_match_ignores_case_and_title(self):
        found = m.choose_window(WINDOWS, "textedit")
        self.assertEqual(found["number"], 3)
        self.assertEqual(m.choose_window(WINDOWS, "ENTWURF")["number"], 3)

    def test_covered_window_still_counts_when_on_screen(self):
        covered = win("Notizen", "hinten", 8, 50, 5, 5, 50, 50, onscreen=True)
        self.assertEqual(m.choose_window([covered], "notizen")["number"], 8)

    def test_minimized(self):
        with self.assertRaises(m.FensterNichtGefunden) as caught:
            m.choose_window(WINDOWS, "safari")
        self.assertEqual(str(caught.exception), "Fenster ist minimiert")
        self.assertEqual(caught.exception.namen, [])

    def test_missing_lists_visible_apps(self):
        with self.assertRaises(m.FensterNichtGefunden) as caught:
            m.choose_window(WINDOWS, "gibt es nicht")
        self.assertEqual(caught.exception.namen, ["Finder", "TextEdit"])

    def test_visible_window_wins_over_a_minimized_one(self):
        rows = [win("Mail", "alt", 1, 1, 0, 0, 900, 700, onscreen=False), win("Mail", "neu", 2, 1, 0, 0, 100, 80)]
        self.assertEqual(m.choose_window(rows, "mail")["number"], 2)


class ListWindowsTest(unittest.TestCase):
    def test_option_all_includes_bounds(self):
        raw = [{
            "kCGWindowOwnerName": "TextEdit", "kCGWindowName": "A", "kCGWindowNumber": 9,
            "kCGWindowOwnerPID": 4, "kCGWindowLayer": 0, "kCGWindowIsOnscreen": 1,
            "kCGWindowBounds": {"X": 3, "Y": 4, "Width": 5, "Height": 6},
        }]
        with mock.patch.object(Quartz, "CGWindowListCopyWindowInfo", return_value=raw) as listed:
            windows = m.list_windows()
        listed.assert_called_once_with(Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID)
        self.assertEqual(windows, [win("TextEdit", "A", 9, 4, 3, 4, 5, 6)])


def _screen():
    screen = m.MacWindowScreen.__new__(m.MacWindowScreen)
    screen.fenster = "TextEdit"
    screen.pid = 20
    screen.window_id = 3
    screen.origin_x, screen.origin_y = 100.0, 40.0
    screen.points_w, screen.points_h = 400, 300
    screen.width, screen.height = 400, 300
    screen.last_click, screen.last_texts, screen.stop_event = None, [], None
    screen._at = None
    return screen


class InputTest(unittest.TestCase):
    def setUp(self):
        self.screen = _screen()
        self.posted = []
        self.screen._locate = lambda: None

        def post_to_pid(pid, event):
            loc = Quartz.CGEventGetLocation(event)
            self.posted.append((pid, Quartz.CGEventGetType(event), (loc.x, loc.y),
                                Quartz.CGEventGetIntegerValueField(event, Quartz.kCGMouseEventClickState)))

        self.patches = [
            mock.patch.object(Quartz, "CGEventPostToPid", post_to_pid),
            mock.patch.object(Quartz, "CGEventPost", side_effect=AssertionError("CGEventPost")),
            mock.patch.object(Quartz, "CGWarpMouseCursorPosition", side_effect=AssertionError("warp")),
            mock.patch.object(Quartz, "CGEventCreate", side_effect=AssertionError("hardware cursor")),
            mock.patch.object(subprocess, "run", side_effect=AssertionError("osascript")),
            mock.patch.object(m.time, "sleep"),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_click_posts_to_pid_in_window_coordinates(self):
        self.assertEqual(self.screen.cursor_in_screenshot(), [200, 150])  # centre until the first move
        self.screen.run({"action": "left_click", "coordinate": [50, 60]}, look=False)
        self.assertEqual(self.screen.cursor_in_screenshot(), [50, 60])
        moved = [row for row in self.posted if row[1] == Quartz.kCGEventMouseMoved]
        self.assertEqual(len(moved), 1)  # no glide
        self.assertEqual(moved[0][0], 20)
        self.assertEqual(moved[0][2], (150.0, 100.0))  # origin (100, 40) + (50, 60)
        downs = [row for row in self.posted if row[1] == Quartz.kCGEventLeftMouseDown]
        self.assertEqual([row[3] for row in downs], [1])

    def test_double_and_right_and_drag(self):
        self.screen.run({"action": "double_click", "coordinate": [1, 1]}, look=False)
        downs = [row[3] for row in self.posted if row[1] == Quartz.kCGEventLeftMouseDown]
        self.assertEqual(downs, [1, 2])
        self.posted.clear()
        self.screen.run({"action": "right_click", "coordinate": [1, 1]}, look=False)
        self.assertTrue(any(row[1] == Quartz.kCGEventRightMouseDown for row in self.posted))
        self.posted.clear()
        self.screen.run({"action": "left_click_drag", "start_coordinate": [0, 0], "coordinate": [40, 30]}, look=False)
        kinds = [row[1] for row in self.posted]
        self.assertIn(Quartz.kCGEventLeftMouseDragged, kinds)
        self.assertEqual(kinds[0], Quartz.kCGEventMouseMoved)

    def test_scroll_and_keys_and_text_stay_on_the_pid(self):
        created = []
        real = Quartz.CGEventCreateScrollWheelEvent

        def wrap(*args):
            created.append(args)
            return real(*args)

        with mock.patch.object(Quartz, "CGEventCreateScrollWheelEvent", wrap):
            self.screen.run({"action": "scroll", "coordinate": [10, 10], "scroll_direction": "down", "scroll_amount": 3}, look=False)
        self.assertEqual(created[-1][3:], (-3, 0))
        self.assertTrue(all(row[0] == 20 for row in self.posted))
        wheels = [row for row in self.posted if row[1] == Quartz.kCGEventScrollWheel]
        self.assertEqual(wheels[-1][2], (110.0, 50.0))  # origin (100, 40) + (10, 10), not the hardware cursor
        self.posted.clear()
        self.screen.run({"action": "key", "text": "cmd+c"}, look=False)
        self.assertGreaterEqual(len(self.posted), 2)
        typed = []
        real_set = Quartz.CGEventKeyboardSetUnicodeString

        def record(event, count, text):
            typed.append(text)
            return real_set(event, count, text)

        with mock.patch.object(Quartz, "CGEventKeyboardSetUnicodeString", record):
            self.screen._type("Ok")
        self.assertEqual(typed, ["O", "O", "k", "k"])

    def test_shortcut_uses_layout_keycode_not_us_table(self):
        previous = m._LAYOUT_CACHE
        m._LAYOUT_CACHE = {"z": 16, "y": 6}  # QWERTZ: Z is not the US keycode 6
        self.addCleanup(setattr, m, "_LAYOUT_CACHE", previous)
        codes = []
        real = Quartz.CGEventCreateKeyboardEvent

        def wrap(source, code, down):
            codes.append(code)
            return real(source, code, down)

        with mock.patch.object(Quartz, "CGEventCreateKeyboardEvent", wrap):
            self.screen._key("cmd+z")
        self.assertEqual(codes[0], 16)
        self.assertNotEqual(codes[0], m.ANSI_KEYCODES["z"])

    def test_layout_table_is_built_with_uckeytranslate(self):
        seen = []

        class Fn:
            def __init__(self, impl):
                self.impl = impl
                self.argtypes = None
                self.restype = None

            def __call__(self, *args):
                return self.impl(*args)

        def uc_key_translate(*args):
            seen.append("UCKeyTranslate")
            code, chars, length = args[1], args[-1], args[-2]
            mapping = {6: ord("y"), 16: ord("z"), 8: ord("c")}
            if code in mapping:
                chars[0] = mapping[code]
                length._obj.value = 1
            return 0

        impls = {
            "TISCopyCurrentKeyboardLayoutInputSource": lambda *a: 2,
            "TISGetInputSourceProperty": lambda *a: seen.append(a[1]) or 3,
            "CFDataGetBytePtr": lambda *a: 4,
            "CFRelease": lambda *a: None,
            "LMGetKbdType": lambda *a: 0,
            "UCKeyTranslate": uc_key_translate,
            "CFStringCreateWithCString": lambda *a: seen.append(a[1]) or 1,
        }

        class Lib:
            def __getattr__(self, name):
                return Fn(impls.get(name, lambda *a: 0))

        with mock.patch.object(ctypes, "CDLL", lambda path: Lib()):
            table = m._read_layout_keycodes()
        self.assertEqual(table["z"], 16)
        self.assertEqual(table["y"], 6)
        self.assertIn("UCKeyTranslate", seen)
        self.assertIn(b"TISPropertyUnicodeKeyLayoutData", seen)


class ScreenshotTest(unittest.TestCase):
    def test_rereads_bounds_and_captures_that_window(self):
        screen = _screen()
        calls = []

        def locate():
            calls.append("locate")
            screen.origin_x, screen.origin_y = 180.0, 40.0
            screen.points_w, screen.points_h = 410, 300
            screen.width, screen.height = 410, 300
            screen.window_id = 3

        def run(cmd, check, capture_output):
            calls.append(cmd)
            if cmd[0] == "screencapture":
                path = cmd[-1]
            else:
                path = cmd[cmd.index("--out") + 1]
            with open(path, "wb") as handle:
                handle.write(b"png" if cmd[0] == "screencapture" else b"jpeg")
            return SimpleNamespace(returncode=0)

        screen._locate = locate
        with mock.patch.object(subprocess, "run", run), mock.patch.object(m, "read_texts", return_value=[("Zeile", 4, 5)]):
            data = screen.screenshot()
        self.assertEqual(calls[0], "locate")
        self.assertEqual(calls[1][:5], ["screencapture", "-x", "-o", "-l", "3"])
        self.assertEqual(calls[2][calls[2].index("-z") + 1:calls[2].index("-z") + 3], ["300", "410"])
        self.assertEqual(screen.last_texts, [("Zeile", 4, 5)])
        self.assertTrue(data)


class InitTest(unittest.TestCase):
    def test_size_is_the_window_not_the_display(self):
        bounds = SimpleNamespace(size=SimpleNamespace(width=2000, height=1000))
        raw = [{
            "kCGWindowOwnerName": "TextEdit", "kCGWindowName": "Notiz", "kCGWindowNumber": 15,
            "kCGWindowOwnerPID": 77, "kCGWindowLayer": 0, "kCGWindowIsOnscreen": True,
            "kCGWindowBounds": {"X": 100, "Y": 40, "Width": 400, "Height": 300},
        }]
        with mock.patch.object(Quartz, "CGMainDisplayID", return_value=1), \
             mock.patch.object(Quartz, "CGDisplayBounds", return_value=bounds), \
             mock.patch.object(Quartz, "CGWindowListCopyWindowInfo", return_value=raw):
            screen = m.MacWindowScreen("notiz")
        self.assertEqual((screen.pid, screen.window_id), (77, 15))
        self.assertEqual((screen.origin_x, screen.origin_y), (100.0, 40.0))
        self.assertEqual((screen.width, screen.height), (400, 300))
        self.assertEqual(screen._point([10, 20]), (110.0, 60.0))

    def test_same_window_is_reread(self):
        first = [win("TextEdit", "A", 15, 77, 100, 40, 400, 300)]
        moved = [win("TextEdit", "A", 15, 77, 180, 50, 410, 320)]
        lists = iter([first, moved])
        screen = _screen()
        screen.window_id = 15
        with mock.patch.object(m, "list_windows", side_effect=lambda: next(lists)):
            screen._locate()
            self.assertEqual((screen.origin_x, screen.width), (100.0, 400))
            screen._locate()
        self.assertEqual((screen.origin_x, screen.origin_y, screen.width, screen.height), (180.0, 50.0, 410, 320))


if __name__ == "__main__":
    unittest.main()
