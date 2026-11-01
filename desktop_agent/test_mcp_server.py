"""Offline tests for the MCP bridge: JSON-RPC handling and status reporting, no server needed.

Run: .venv/bin/python -m unittest test_mcp_server.py
"""

import io
import json
import os
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

    def test_server_down_is_reported(self):
        bridge = FakeBridge()
        bridge._state = lambda since: (_ for _ in ()).throw(urllib.error.URLError("refused"))
        reply = mcp_server.handle(bridge, _call(1, "computer_auftrag", {"text": "x"}))["result"]
        self.assertTrue(reply["isError"])
        self.assertEqual(reply["content"][0]["text"], mcp_server.NOT_RUNNING)

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


def _call(msg_id, name, arguments):
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


if __name__ == "__main__":
    unittest.main()
