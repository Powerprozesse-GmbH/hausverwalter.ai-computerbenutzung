"""Offline tests for the MCP bridge: JSON-RPC handling and status reporting, no server needed.

Run: .venv/bin/python -m unittest test_mcp_server.py
"""

import io
import json
import os
import signal
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import local_auth
import mcp_server


class FakeBridge(mcp_server.Bridge):
    """server.py stand-in: a log, a running flag and the posted requests."""

    def __init__(self):
        super().__init__("http://test")
        self.lines, self.running, self.posts = ["Du: alter Auftrag", "KI: Fertig."], False, []

    def _state(self, since):
        return {
            "log": self.lines[since:] if since is not None else self.lines,
            "total": len(self.lines),
            "running": self.running,
            "paused": False,
            "cost_usd": 0.12,
            "last_reply": "Ich öffne PowerHaus.",
        }

    def _post(self, path, body):
        self.posts.append((path, body))
        if path == "/send":
            self.lines.append(f"Du: {body['text']}")
            self.lines.append("KI: Ich öffne PowerHaus.")


class McpServerTest(unittest.TestCase):
    def test_initialize_and_tool_list(self):
        bridge = FakeBridge()
        init = mcp_server.handle(bridge, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-03-26")
        self.assertIsNone(mcp_server.handle(bridge, {"jsonrpc": "2.0", "method": "notifications/initialized"}))
        tools = mcp_server.handle(bridge, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertEqual(
            [t["name"] for t in tools],
            ["computer_auftrag", "computer_nachricht", "computer_status", "computer_pause", "computer_weiter", "computer_stopp"],
        )

    def test_task_then_status_reports_only_new_lines(self):
        bridge = FakeBridge()
        mcp_server.handle(bridge, _call(1, "computer_auftrag", {"text": "Buche die Rechnung in PowerHaus"}))
        text = mcp_server.handle(bridge, _call(2, "computer_status", {"warte_sekunden": 0}))["result"]["content"][0]["text"]
        self.assertIn("Du: Buche die Rechnung in PowerHaus", text)
        self.assertNotIn("alter Auftrag", text)
        self.assertIn("fertig oder wartet auf Antwort", text)
        again = mcp_server.handle(bridge, _call(3, "computer_status", {"warte_sekunden": 0}))["result"]["content"][0]["text"]
        self.assertIn("(nichts Neues)", again)

    def test_controls_post_to_server(self):
        bridge = FakeBridge()
        for i, name in enumerate(["computer_pause", "computer_weiter", "computer_stopp"]):
            mcp_server.handle(bridge, _call(i, name, {}))
        self.assertEqual([p[0] for p in bridge.posts], ["/pause", "/resume", "/stop"])

    def test_server_down_not_installed_says_install_yourself(self):
        bridge = _down_bridge(launched=False)
        with mock.patch.object(mcp_server.mac_rights, "read_status", return_value=None):
            reply = mcp_server.handle(bridge, _call(1, "computer_auftrag", {"text": "x"}))["result"]
        self.assertEqual(reply["content"][0]["text"], mcp_server.NOT_INSTALLED)

    def test_no_key_tells_the_agent_what_to_say_and_starts_nothing(self):
        bridge = _down_bridge(launched=True)
        bridge._key_saved = lambda: False
        with mock.patch.object(mcp_server.mac_rights, "read_status", return_value=None):
            text = mcp_server.handle(bridge, _call(1, "computer_status", {"warte_sekunden": 0}))["result"]["content"][0]["text"]
        self.assertIn("api_key.py", text)
        self.assertIn("fenster", text)
        self.assertEqual(bridge.launches, 0)

    def test_server_down_is_started_and_call_repeated(self):
        bridge = _down_bridge(launched=True)
        with mock.patch.object(mcp_server.mac_rights, "read_status", return_value=None):
            mcp_server.handle(bridge, _call(1, "computer_stopp", {}))
        self.assertEqual(bridge.launches, 1)
        self.assertEqual(bridge.posts, [("/stop", {})])

    def test_waiting_for_permission_gives_one_sentence_and_no_second_start(self):
        bridge = _down_bridge(launched=True)
        with mock.patch.object(mcp_server.mac_rights, "read_status", return_value=("Bitte den Schalter einschalten.", "x-apple.test:link")):
            text = mcp_server.handle(bridge, _call(1, "computer_status", {"warte_sekunden": 0}))["result"]["content"][0]["text"]
        self.assertIn("Bitte den Schalter einschalten.", text)
        self.assertIn('open "x-apple.test:link"', text)
        self.assertEqual(bridge.launches, 0)

    def test_problem_line_is_reported_as_reason(self):
        bridge = FakeBridge()
        bridge.lines.append("Problem: KI-Guthaben aufgebraucht.")
        text = mcp_server.handle(bridge, _call(1, "computer_status", {"warte_sekunden": 0}))["result"]["content"][0]["text"]
        self.assertIn("Auftrag abgebrochen. Problem: KI-Guthaben aufgebraucht.", text)

    def test_fenster_is_forwarded(self):
        bridge = FakeBridge()
        mcp_server.handle(bridge, _call(1, "computer_auftrag", {"text": "tippe", "fenster": "TextEdit"}))
        self.assertEqual(bridge.posts[0], ("/send", {"text": "tippe", "fenster": "TextEdit"}))

    def test_privacy_status_hides_names_iban_and_amounts(self):
        bridge = FakeBridge()
        bridge.privacy = True
        bridge.lines = ["KI: Erika Mustermann DE89370400440532013000 1250,00", "Aktion: type „DE89370400440532013000“"]

        def state(since):
            return {
                "datenschutz": True, "total": 2, "running": False, "paused": False,
                "log": bridge.lines, "last_reply": "Erika Mustermann DE89370400440532013000 1250,00",
                "zustand": "fertig", "schritte": 2, "kosten": 0.2, "fehlerart": "keine",
                "dateien": {"anzahl": 1, "endungen": [".pdf"], "groesse": 4, "namen": "Erika Mustermann.pdf"},
                "meldung": "2 Dateien", "cost_usd": 0.2,
            }

        bridge._state = state
        text = mcp_server.handle(bridge, _call(1, "computer_status", {"warte_sekunden": 0}))["result"]["content"][0]["text"]
        for secret in ("Erika", "Mustermann", "DE89", "1250", "type"):
            self.assertNotIn(secret, text)
        self.assertEqual(set(json.loads(text)), {"zustand", "schritte", "kosten", "fehlerart", "dateien", "meldung"})
        self.assertEqual(json.loads(text)["dateien"], {"anzahl": 1, "endungen": [".pdf"], "groesse": 4})

    def test_env_forces_privacy_on_the_reply(self):
        with mock.patch.dict(os.environ, {"HV_DATENSCHUTZ": "1"}):
            bridge = FakeBridge()
            text = mcp_server.handle(bridge, _call(1, "computer_auftrag", {"text": "Erika Mustermann 1250,00"}))["result"]["content"][0]["text"]
        self.assertNotIn("Erika", text)
        self.assertNotIn("Weitergegeben", text)
        self.assertNotIn("PowerHaus", text)
        self.assertEqual(json.loads(text)["fehlerart"], "keine")

    def test_mac_launch_stops_leftover_applet_before_open(self):
        calls = []
        killed = []
        seen_ps = {"n": 0}

        def run(cmd, check=False, capture_output=False, timeout=None, text=False):
            calls.append(cmd)
            stdout = ""
            if cmd[0] == "ps":
                seen_ps["n"] += 1
                if seen_ps["n"] == 1:
                    stdout = "24137 /Applications/hausverwalter.ai Computerbenutzung.app/Contents/MacOS/applet\n"
            return mock.Mock(returncode=0, stdout=stdout)

        launcher = mock.Mock()
        launcher.exists.return_value = True
        with mock.patch.object(sys, "platform", "darwin"), \
             mock.patch.object(mcp_server, "MAC_LAUNCHER", launcher), \
             mock.patch.object(mcp_server.subprocess, "run", run), \
             mock.patch.object(mcp_server.os, "kill", lambda pid, sig: killed.append((pid, sig))), \
             mock.patch.object(mcp_server.time, "sleep"):
            self.assertTrue(mcp_server.Bridge()._launch())
        self.assertEqual(killed, [(24137, signal.SIGTERM)])
        self.assertEqual(calls[-1][0], "open")
        self.assertNotIn("osascript", [cmd[0] for cmd in calls])

    def test_mac_launch_does_not_stop_when_the_applet_is_gone(self):
        calls = []
        killed = []

        def run(cmd, check=False, capture_output=False, timeout=None, text=False):
            calls.append(cmd[0])
            return mock.Mock(returncode=0, stdout="")

        launcher = mock.Mock()
        launcher.exists.return_value = True
        with mock.patch.object(sys, "platform", "darwin"), \
             mock.patch.object(mcp_server, "MAC_LAUNCHER", launcher), \
             mock.patch.object(mcp_server.subprocess, "run", run), \
             mock.patch.object(mcp_server.os, "kill", lambda pid, sig: killed.append((pid, sig))):
            self.assertTrue(mcp_server.Bridge()._launch())
        self.assertEqual(calls, ["ps", "open"])
        self.assertEqual(killed, [])

    def test_linux_starts_without_mac_rights(self):
        bridge = _down_bridge(True)
        with mock.patch.object(sys, "platform", "linux"), mock.patch.object(mcp_server.mac_rights, "read_status", side_effect=AssertionError("mac")):
            mcp_server.handle(bridge, _call(1, "computer_stopp", {}))
        self.assertEqual(bridge.launches, 1)
        self.assertEqual(bridge.posts, [("/stop", {})])

    def test_linux_missing_key_opens_no_window(self):
        bridge = _down_bridge(True)
        bridge._key_saved = lambda: False
        with mock.patch.object(sys, "platform", "linux"):
            text = mcp_server.handle(bridge, _call(1, "computer_status", {"warte_sekunden": 0}))["result"]["content"][0]["text"]
        self.assertEqual(text, mcp_server.KEY_FILE_MISSING)
        self.assertNotIn("fenster", text.lower())
        self.assertEqual(bridge.launches, 0)

    def test_unknown_method(self):
        reply = mcp_server.handle(FakeBridge(), {"jsonrpc": "2.0", "id": 9, "method": "resources/list"})
        self.assertEqual(reply["error"]["code"], -32601)


class TokenTest(unittest.TestCase):
    """The bridge sends the server's per-start token, read from the user-only file on every request."""

    def setUp(self):
        self.path = Path(tempfile.mkdtemp(), "server-token")
        patcher = mock.patch.dict(os.environ, {"HV_TOKEN_FILE": str(self.path)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_token_header_on_every_request(self):
        token = local_auth.new_token(self.path)
        seen = []

        def urlopen(request, timeout):
            seen.append((request.get_method(), request.full_url, request.get_header(local_auth.HEADER.capitalize())))
            body = {"log": [], "total": 0, "running": False, "paused": False, "cost_usd": 0, "last_reply": ""}
            return io.BytesIO(json.dumps(body).encode())

        with mock.patch.object(mcp_server.urllib.request, "urlopen", urlopen):
            bridge = mcp_server.Bridge("http://test")
            mcp_server.handle(bridge, _call(1, "computer_auftrag", {"text": "x"}))
            mcp_server.handle(bridge, _call(2, "computer_status", {"warte_sekunden": 0}))
        self.assertEqual([s[0] for s in seen], ["GET", "POST", "GET"])
        self.assertEqual({s[2] for s in seen}, {token})

    def test_refused_token_is_explained(self):
        error = urllib.error.HTTPError("http://test/send", 401, "Unauthorized", {}, None)
        self.addCleanup(error.close)
        with mock.patch.object(mcp_server.urllib.request, "urlopen", side_effect=error):
            reply = mcp_server.handle(mcp_server.Bridge("http://test"), _call(1, "computer_stopp", {}))["result"]
        self.assertTrue(reply["isError"])
        self.assertEqual(reply["content"][0]["text"], mcp_server.REFUSED)


def _down_bridge(launched):
    """Server down until _launch was called."""
    bridge = FakeBridge()
    bridge.launches, real_state, real_post = 0, bridge._state, bridge._post

    def launch():
        bridge.launches += 1
        return launched

    def guarded(real):
        def inner(*args):
            if not bridge.launches:
                raise urllib.error.URLError("refused")
            return real(*args)
        return inner

    bridge._launch, bridge._state, bridge._post = launch, guarded(real_state), guarded(real_post)
    bridge._key_saved = lambda: True
    return bridge


def _call(msg_id, name, arguments):
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


if __name__ == "__main__":
    unittest.main()
