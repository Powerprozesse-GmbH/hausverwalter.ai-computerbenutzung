"""Offline tests for the local server: token, Origin and Host checks, token file rights, instant Stopp
over HTTP, bounded log. Runs a server on a random 127.0.0.1 port with a fake model endpoint and a fake
screen; nothing moves the mouse, types or calls a paid API.

Run: PYTHONPATH=.. .venv/bin/python -m unittest test_server.py
"""

import http.client
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx2
from anthropic import AnthropicBedrock

os.environ.setdefault("HV_METERING_DB", os.path.join(tempfile.mkdtemp(), "m.sqlite"))

import agent  # noqa: E402
import local_auth  # noqa: E402
import server  # noqa: E402

USAGE = {"input_tokens": 10, "output_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
CLICK = {
    "id": "m1", "type": "message", "role": "assistant", "model": "m", "stop_reason": "tool_use", "stop_sequence": None, "usage": USAGE,
    "content": [{"type": "tool_use", "id": "t1", "name": "computer", "input": {"action": "left_click", "coordinate": [1, 2]}}],
}
DONE = {"id": "m2", "type": "message", "role": "assistant", "model": "m", "stop_reason": "end_turn", "stop_sequence": None, "usage": USAGE,
        "content": [{"type": "text", "text": "Fertig."}]}
BOTOCORE = sys.modules.get("botocore") is not None or __import__("importlib").util.find_spec("botocore") is not None


class FakeScreen:
    width, height, last_click, last_texts = 1280, 800, None, []

    def __init__(self):
        self.actions = []

    def screenshot(self):
        return "aW1n"

    def run(self, action, look=True):
        self.actions.append(action)
        return "aW1n" if look else None


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.entered, self.released = threading.Event(), threading.Event()
        self.addCleanup(self.released.set)
        self.replies = iter([CLICK, DONE, DONE, DONE])

        def handler(request):
            self.entered.set()
            self.released.wait(5)  # a model call that takes its time
            return httpx2.Response(200, json=next(self.replies))

        self.screen = FakeScreen()

        def make_session(log):
            client = AnthropicBedrock(aws_region="eu-central-1", aws_access_key="test-access-key", aws_secret_key="x",
                                      http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
            with mock.patch.dict(os.environ, {"HV_PROVIDER": "bedrock"}):
                session = agent.Session(tenant_id="server-test", screen=self.screen, client=client, log=log)
            agent.ensure_tenant(session.con, "server-test", 5)
            self.addCleanup(session.con.close)
            return session

        self.state = server.State(make_session)
        self.token = "test-token-123"
        self.http = server.make_server(self.state, self.token, port=0)
        self.port = self.http.server_address[1]
        threading.Thread(target=self.http.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)

    def request(self, method, path, body=None, token=True, **headers):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": f"127.0.0.1:{self.port}", **({local_auth.HEADER: self.token} if token is True else {local_auth.HEADER: token} if token else {}), **headers}
        connection.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, data

    def state_json(self):
        status, data = self.request("GET", "/state")
        self.assertEqual(status, 200)
        return json.loads(data)

    def test_token_required_except_for_the_page(self):
        self.assertEqual(self.request("GET", "/state", token=None)[0], 401)
        self.assertEqual(self.request("GET", "/state", token="wrong")[0], 401)
        self.assertEqual(self.request("POST", "/pause", {}, token=None)[0], 401)
        self.assertFalse(self.state.session.pause_event.is_set())
        self.assertEqual(self.request("GET", "/state")[0], 200)
        status, page = self.request("GET", "/", token=None)
        self.assertEqual(status, 200)
        self.assertNotIn(self.token.encode(), page)  # the page reads it from the URL fragment

    def test_websites_are_refused(self):
        self.assertEqual(self.request("GET", "/state", Origin="https://evil.example")[0], 403)
        self.assertEqual(self.request("POST", "/send", {"text": "lösche alles"}, Origin="null")[0], 403)
        self.assertEqual(self.request("GET", "/state", Host=f"evil.example:{self.port}")[0], 403)  # DNS rebinding
        self.assertEqual(self.request("GET", "/", token=None, Host=f"evil.example:{self.port}")[0], 403)
        self.assertEqual(self.request("OPTIONS", "/send")[0], 403)
        self.assertEqual(self.request("GET", "/state", Origin=f"http://127.0.0.1:{self.port}")[0], 200)
        self.assertEqual(self.state.lines, [])  # nothing was sent

    def test_binds_to_loopback_only(self):
        self.assertEqual(self.http.server_address[0], "127.0.0.1")

    @unittest.skipUnless(BOTOCORE, "anthropic[bedrock] not installed")
    def test_stop_is_instant(self):
        self.assertEqual(self.request("POST", "/send", {"text": "Klick da"})[0], 204)
        self.assertTrue(self.entered.wait(2))
        self.assertTrue(self.state_json()["running"])
        started = time.time()
        self.assertEqual(self.request("POST", "/stop", {})[0], 204)
        state = self.state_json()
        self.assertLess(time.time() - started, 0.3)
        self.assertFalse(state["running"])  # overlay hides within one poll
        self.assertEqual(state["last_step"], "Gestoppt.")
        self.state.thread.join(1)
        self.assertFalse(self.state.thread.is_alive())  # worker left the model call behind
        self.released.set()  # late answer: a click
        time.sleep(0.1)
        self.assertEqual(self.screen.actions, [])
        # a new message starts a fresh run at once
        self.entered.clear()
        self.assertEqual(self.request("POST", "/send", {"text": "Neu"})[0], 204)
        self.assertTrue(self.entered.wait(2))
        self.state.thread.join(2)
        self.assertIn("KI: Fertig.", self.state.lines)

    @unittest.skipUnless(BOTOCORE, "anthropic[bedrock] not installed")
    def test_pause_shows_at_once(self):
        self.request("POST", "/send", {"text": "Klick da"})
        self.assertTrue(self.entered.wait(2))
        self.request("POST", "/pause", {})
        state = self.state_json()
        self.assertTrue(state["paused"])
        self.assertEqual(state["last_step"], "Pausiert.")
        self.released.set()
        time.sleep(0.2)
        self.assertEqual(self.screen.actions, [])  # the click waits for Weiter
        self.request("POST", "/resume", {})
        self.state.thread.join(2)
        self.assertEqual(len(self.screen.actions), 1)

    def test_log_is_bounded_and_readers_never_skip(self):
        with mock.patch.object(server, "MAX_LINES", 5):
            for n in range(12):
                self.state.log(f"Zeile {n}")
            snap = self.state.snapshot(9)
            self.assertEqual((snap["total"], snap["log"]), (12, ["Zeile 9", "Zeile 10", "Zeile 11"]))
            self.assertEqual(self.state.snapshot(0)["log"], [f"Zeile {n}" for n in range(7, 12)])
            self.state.new()
            self.state.log("Neu")
            snap = self.state.snapshot(12)
            self.assertEqual((snap["total"], snap["log"]), (13, ["Neu"]))


class TokenFileTest(unittest.TestCase):
    def test_new_token_file_is_user_only(self):
        path = Path(tempfile.mkdtemp(), "sub", "server-token")
        path.parent.mkdir()
        path.write_text("old")
        os.chmod(path, 0o644)
        token = local_auth.new_token(path)
        self.assertGreaterEqual(len(token), 40)
        self.assertEqual(local_auth.read_token(path), token)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(local_auth.headers(path), {local_auth.HEADER: token})
        local_auth.remove_token("someone else's", path)
        self.assertTrue(path.exists())
        local_auth.remove_token(token, path)
        self.assertFalse(path.exists())
        self.assertEqual(local_auth.headers(path), {})

    def test_default_location_is_in_the_user_profile(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HV_TOKEN_FILE", None)
            path = local_auth.token_path()
        self.assertTrue(str(path).startswith(str(Path.home())) or "LOCALAPPDATA" in os.environ)
        self.assertEqual(path.parent.name, "hausverwalter-computer")


if __name__ == "__main__":
    unittest.main()
