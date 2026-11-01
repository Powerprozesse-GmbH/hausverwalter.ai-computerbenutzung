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


if __name__ == "__main__":
    unittest.main()
