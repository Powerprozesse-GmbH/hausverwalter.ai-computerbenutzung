"""Offline tests for the local server: token, Origin and Host checks, token file rights, instant Stopp
over HTTP, bounded log. Runs a server on a random 127.0.0.1 port with a fake model endpoint and a fake
screen; nothing moves the mouse, types or calls a paid API.

Run: PYTHONPATH=.. .venv/bin/python -m unittest test_server.py
"""

import contextlib
import http.client
import io
import json
import os
import queue
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import ModuleType
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


def _idle_session():
    session = mock.Mock()
    session.stop_event = threading.Event()
    session.pause_event = threading.Event()
    session.inbox = queue.Queue()
    session.send = lambda text: None
    session.set_privacy = lambda: None
    session.datenschutz_aktiv = lambda: False
    session.screen = FakeScreen()
    session.cost_micro_usd = session.tokens_in = session.tokens_out = session.error_count = 0
    session.acting = False
    session.provider = "bedrock"
    return session


class FensterTest(unittest.TestCase):
    def test_title_is_passed_to_the_window_screen(self):
        seen = {}

        class MacWindowScreen:
            def __init__(self, fenster, *args, **kwargs):
                seen["fenster"] = fenster
                self.platform = "darwin"
                self.width, self.height = 10, 10

        module = ModuleType("mac_control")
        module.MacWindowScreen = MacWindowScreen
        with mock.patch.object(sys, "platform", "darwin"), mock.patch.dict(sys.modules, {"mac_control": module}):
            screen = server.screen_for("TextEdit")
        self.assertEqual(seen["fenster"], "TextEdit")
        self.assertIsInstance(screen, MacWindowScreen)

    def test_missing_class_is_a_clear_error(self):
        module = ModuleType("mac_control")
        with mock.patch.object(sys, "platform", "darwin"), mock.patch.dict(sys.modules, {"mac_control": module}):
            with self.assertRaises(RuntimeError) as caught:
                server.screen_for("TextEdit")
        self.assertEqual(str(caught.exception), "Fenster-Modus nicht verfügbar")

    def test_send_forwards_the_title(self):
        calls = []

        def factory(log, fenster=None):
            calls.append(fenster)
            return _idle_session()

        state = server.State(factory)
        state.send("hallo", fenster="Rechner")
        state.thread.join(2)
        self.assertEqual(calls, [None, "Rechner"])

    def test_privacy_window_error_hides_the_title(self):
        def factory(log, fenster=None):
            if fenster:
                raise RuntimeError("Fenster Erika Mustermann")
            return _idle_session()

        state = server.State(factory)
        token = "test-token-123"
        httpd = server.make_server(state, token, port=0)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        body = json.dumps({"text": "öffne", "fenster": "Akte", "datenschutz": True}).encode()
        log = io.StringIO()
        with contextlib.redirect_stderr(log):
            connection.request("POST", "/send", body=body, headers={"Host": f"127.0.0.1:{port}", local_auth.HEADER: token, "content-type": "application/json"})
            response = connection.getresponse()
            data = response.read()
        connection.close()
        self.assertEqual(response.status, 409)
        self.assertEqual(data, b"sonstiges")
        self.assertIn("Auftrag abgelehnt: RuntimeError", log.getvalue())
        self.assertNotIn("Erika", log.getvalue())
        self.assertNotIn(b"Erika", data)


class PrivacySnapshotTest(unittest.TestCase):
    def test_status_drops_names_iban_and_amounts(self):
        session = _idle_session()
        session.datenschutz_aktiv = lambda: True
        session.notice = "2 Dateien"
        session.steps = 3
        session.fehlerart = "keine"
        session.privacy = True

        def factory(log):
            return session

        state = server.State(factory)
        state.log("KI: Erika Mustermann DE89370400440532013000 1250,00")
        state.log("Aktion: type „DE89370400440532013000“")
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "Erika Mustermann.pdf").write_bytes(b"abc")
            with mock.patch.dict(os.environ, {"HV_EXPORT_DIR": tmp}):
                snap = state.snapshot(0)
        blob = json.dumps(snap)
        for secret in ("Erika", "Mustermann", "DE89", "1250"):
            self.assertNotIn(secret, blob)
        self.assertEqual(snap["meldung"], "2 Dateien")
        self.assertEqual(snap["log"], [])
        self.assertEqual(snap["dateien"], {"anzahl": 1, "endungen": [".pdf"], "groesse": 3})


if __name__ == "__main__":
    unittest.main()
