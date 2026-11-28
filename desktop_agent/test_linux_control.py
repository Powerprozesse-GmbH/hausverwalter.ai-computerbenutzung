"""Linux screen without a real X server: xdotool argv and tesseract TSV.

Run: PYTHONPATH=.. ../.venv/bin/python -m unittest test_linux_control.py
"""

import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import linux_control
import server


TSV = """level	page_num	block_num	par_num	line_num	word_num	left	top	width	height	conf	text
5	1	1	1	1	1	10	20	20	10	90	Hallo
5	1	1	1	1	2	40	20	20	10	90	Welt
5	1	1	1	2	1	10	80	30	10	90	Unten
4	1	1	1	1	0	10	20	50	10	90	Hallo Welt
"""


class TesseractTest(unittest.TestCase):
    def test_tsv_lines_become_centres(self):
        texts = linux_control.parse_tesseract_tsv(TSV, 1, 1)
        self.assertEqual(texts, [("Hallo Welt", 35, 25), ("Unten", 25, 85)])

    def test_command_is_deu_eng_tsv(self):
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            self.assertIsNot(kwargs.get("shell"), True)
            return SimpleNamespace(returncode=0, stdout=TSV, stderr="")

        image = mock.Mock(width=100, height=50)
        image.save = lambda path: None
        with mock.patch("linux_control.subprocess.run", run):
            texts = linux_control.read_texts(image, 100, 50)
        self.assertEqual(calls[0][:3], ["tesseract", calls[0][1], "stdout"])
        self.assertEqual(calls[0][-3:], ["-l", "deu+eng", "tsv"])
        self.assertEqual(texts[0][0], "Hallo Welt")


class XdotoolTest(unittest.TestCase):
    def test_click_type_key_and_scroll(self):
        calls = []

        def run(args, **kwargs):
            self.assertIsInstance(args, list)
            self.assertEqual(args[0], "xdotool")
            self.assertIsNot(kwargs.get("shell"), True)
            calls.append(list(args))
            stdout = "X=0\nY=0\nSCREEN=0\nWINDOW=1\n" if args[1] == "getmouselocation" else ""
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        with mock.patch("linux_control._display_size", return_value=(1280, 800)), \
                mock.patch("linux_control.subprocess.run", run), \
                mock.patch("linux_control.time.sleep"):
            screen = linux_control.LinuxScreen()
            screen.run({"action": "left_click", "coordinate": [0, 0]}, look=False)
            screen.run({"action": "type", "text": "ab"}, look=False)
            screen.run({"action": "key", "text": "ctrl+c"}, look=False)
            screen.run({"action": "scroll", "scroll_direction": "down", "scroll_amount": 2}, look=False)
        self.assertIn(["xdotool", "click", "1"], calls)
        self.assertIn(["xdotool", "mousemove", "0", "0"], calls)
        self.assertIn(["xdotool", "type", "--delay", "0", "--", "a"], calls)
        self.assertIn(["xdotool", "type", "--delay", "0", "--", "b"], calls)
        self.assertIn(["xdotool", "key", "--", "ctrl+c"], calls)
        self.assertIn(["xdotool", "click", "--repeat", "2", "5"], calls)

    def test_unknown_key_is_refused_and_known_keys_use_double_dash(self):
        calls = []

        def run(args, **kwargs):
            self.assertIsInstance(args, list)
            self.assertIsNot(kwargs.get("shell"), True)
            calls.append(list(args))
            stdout = "X=0\nY=0\nSCREEN=0\nWINDOW=1\n" if len(args) > 1 and args[1] == "getmouselocation" else ""
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        with mock.patch("linux_control._display_size", return_value=(1280, 800)), \
                mock.patch("linux_control.subprocess.run", run), \
                mock.patch("linux_control.time.sleep"):
            screen = linux_control.LinuxScreen()
            screen.run({"action": "key", "text": "ä"}, look=False)
            screen.run({"action": "left_click", "coordinate": [0, 0], "text": "ctrl"}, look=False)
            with self.assertRaises(RuntimeError) as caught:
                screen.run({"action": "key", "text": "--clearmodifiers"}, look=False)
        self.assertIn(["xdotool", "key", "--", "ä"], calls)
        self.assertIn(["xdotool", "keydown", "--", "ctrl"], calls)
        self.assertIn(["xdotool", "keyup", "--", "ctrl"], calls)
        self.assertNotIn("--clearmodifiers", str(caught.exception))
        self.assertFalse(any("--clearmodifiers" in arg for args in calls for arg in args))


class SessionPickTest(unittest.TestCase):
    def test_make_session_uses_linux_without_quartz(self):
        created = []

        class FakeSession:
            def __init__(self, tenant_id, screen, log, **kwargs):
                created.append(screen)
                self.con = None

            def set_privacy(self):
                pass

        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.dict(sys.modules, {"mac_control": None, "Quartz": None}), \
                mock.patch("linux_control._display_size", return_value=(1280, 800)), \
                mock.patch("agent.Session", FakeSession):
            server.make_session(lambda line: None)
        self.assertEqual(created[0].platform, "linux")
        self.assertIsInstance(created[0], linux_control.LinuxScreen)


if __name__ == "__main__":
    unittest.main()
