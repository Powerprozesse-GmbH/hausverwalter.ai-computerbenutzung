"""Offline tests: agent loop against fake hausverwalter.ai, Bedrock and Vertex endpoints, Stopp/Pause,
history compaction, key mapping without key presses. Nothing moves the mouse, types or calls a paid API.

Run: PYTHONPATH=.. .venv/bin/python -m unittest test_agent.py
"""

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from types import ModuleType, SimpleNamespace
from unittest import mock

import httpx2
from anthropic import AnthropicBedrock, AnthropicVertex

os.environ["HV_METERING_DB"] = os.path.join(tempfile.mkdtemp(), "m.sqlite")
# Customers default to hausverwalter (HausverwalterLoopTest); the older tests mean Bedrock.
os.environ["HV_PROVIDER"] = "bedrock"

import agent  # noqa: E402
import bedrock_computer_use as metering  # noqa: E402
import mac_control  # noqa: E402


def _installed(module):
    try:
        return importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:
        return False


# The internal providers need extras (pip install ".[intern]"); customers' installs skip these tests.
needs_bedrock = unittest.skipUnless(_installed("botocore"), "anthropic[bedrock] not installed")
needs_vertex = unittest.skipUnless(_installed("google.auth"), "anthropic[vertex] not installed")

USAGE = {"input_tokens": 1000, "output_tokens": 100, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
CLICK = {
    "id": "msg_1", "type": "message", "role": "assistant", "model": "m", "stop_reason": "tool_use",
    "stop_sequence": None, "usage": USAGE,
    "content": [
        {"type": "text", "text": "Ich öffne die Notizen."},
        {"type": "tool_use", "id": "toolu_1", "name": "computer", "input": {"action": "left_click", "coordinate": [100, 50]}},
    ],
}
# Vertex toolset turn: two member calls, the action is the block's name
BATCH = {
    "id": "msg_v1", "type": "message", "role": "assistant", "model": "m", "stop_reason": "tool_use",
    "stop_sequence": None, "usage": USAGE,
    "content": [
        {"type": "text", "text": "Ich öffne die Notizen."},
        {"type": "tool_use", "id": "toolu_1", "name": "left_click", "toolset_name": "computer", "input": {"coordinate": [100, 50]}},
        {"type": "tool_use", "id": "toolu_2", "name": "type", "toolset_name": "computer", "input": {"text": "Hallo"}},
    ],
}
DONE = {
    "id": "msg_2", "type": "message", "role": "assistant", "model": "m", "stop_reason": "end_turn",
    "stop_sequence": None, "usage": USAGE, "content": [{"type": "text", "text": "Fertig."}],
}


class FakeScreen:
    width, height = 1280, 800

    def __init__(self, fail_on=()):
        self.actions = []
        self.fail_on = fail_on

    def screenshot(self):
        return "aW1n"

    def run(self, action, look=True):
        self.actions.append(action)
        if action["action"] in self.fail_on:
            raise RuntimeError("Klick ging daneben")
        return "aW1n" if look else None

    def cursor_in_screenshot(self):
        return [1, 2]


@needs_bedrock
class AgentLoopTest(unittest.TestCase):
    def setUp(self):
        self.bodies = []
        replies = iter([CLICK, DONE])

        def handler(request):
            self.bodies.append(json.loads(request.content))
            return httpx2.Response(200, json=next(replies))

        client = AnthropicBedrock(
            aws_region="eu-central-1", aws_access_key="test-access-key", aws_secret_key="x",
            http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        )
        self.screen = FakeScreen()
        self.lines = []
        self.tenant = self._testMethodName
        self.session = agent.Session(tenant_id=self.tenant, screen=self.screen, client=client, log=self.lines.append)
        agent.ensure_tenant(self.session.con, self.tenant, 5)

    def tearDown(self):
        self.session.con.close()

    def test_click_then_done(self):
        self.session.send("Öffne die Notizen-App")
        self.assertEqual(self.screen.actions, [{"action": "left_click", "coordinate": [100, 50]}])
        first, second = self.bodies
        self.assertEqual(first["anthropic_beta"], ["computer-use-2025-11-24"])
        self.assertEqual(first["tools"][0]["display_width_px"], 1280)
        self.assertIn("cmd statt ctrl", first["system"])
        # second call replays history append-only: user, assistant, tool_result
        self.assertEqual([m["role"] for m in second["messages"]], ["user", "assistant", "user"])
        result = second["messages"][2]["content"][0]
        self.assertEqual(result["tool_use_id"], "toolu_1")
        self.assertEqual(result["content"][0]["type"], "image")
        self.assertIn("KI: Fertig.", self.lines)
        self.assertEqual(self.session.cost_micro_usd, 2 * round((1000 * 2 + 100 * 10) * 1.1))

    def test_budget_gate_blocks_call(self):
        self.session.con.execute("UPDATE tenants SET budget_micro_usd = 0 WHERE tenant_id = ?", (self.tenant,))
        self.session.con.commit()
        self.session.send("Irgendwas")
        self.assertEqual(self.bodies, [])
        self.assertIn("Budget aufgebraucht. Zugriff gesperrt.", self.lines)


@needs_vertex
class VertexLoopTest(unittest.TestCase):
    URL = (
        "https://europe-west1-aiplatform.googleapis.com/v1/projects/hv-test/locations/europe-west1"
        "/publishers/anthropic/models/claude-sonnet-5-5:rawPredict"
    )

    def start(self, screen):
        self.requests = []
        replies = iter([BATCH, DONE])

        def handler(request):
            self.requests.append(request)
            return httpx2.Response(200, json=next(replies))

        client = AnthropicVertex(
            project_id="hv-test", region="europe-west1", access_token="test",
            http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        )
        self.screen = screen
        self.lines = []
        self.tenant = self._testMethodName
        with mock.patch.dict(os.environ, {"HV_PROVIDER": "vertex"}):
            self.session = agent.Session(tenant_id=self.tenant, screen=screen, client=client, log=self.lines.append)
        self.addCleanup(self.session.con.close)
        agent.ensure_tenant(self.session.con, self.tenant, 5)
        self.session.send("Öffne die Notizen-App")
        return [json.loads(r.content) for r in self.requests]

    def test_batch_runs_in_order(self):
        first, second = self.start(FakeScreen())
        for request in self.requests:
            self.assertEqual(str(request.url), self.URL)
            self.assertNotIn("anthropic-beta", request.headers)
            self.assertEqual(request.headers["authorization"], "Bearer test")
        self.assertEqual(first["tools"], [{"type": "computer_toolset_20260801", "configs": {"zoom": {"enabled": False}}}])
        self.assertNotIn("anthropic_beta", first)
        self.assertNotIn("model", first)  # model sits in the URL on Vertex
        self.assertEqual(
            self.screen.actions,
            [{"action": "left_click", "coordinate": [100, 50]}, {"action": "type", "text": "Hallo"}],
        )
        self.assertEqual([m["role"] for m in second["messages"]], ["user", "assistant", "user"])
        self.assertEqual(second["messages"][1]["content"][1]["toolset_name"], "computer")  # replayed unchanged
        results = second["messages"][2]["content"]
        self.assertEqual([r["tool_use_id"] for r in results], ["toolu_1", "toolu_2"])
        self.assertEqual([r["toolset_name"] for r in results], ["computer", "computer"])
        self.assertEqual([r["content"][0]["type"] for r in results], ["image", "image"])
        self.assertIn("KI: Fertig.", self.lines)
        # same metering as Bedrock, model column names the Vertex model
        self.assertEqual(self.session.cost_micro_usd, 2 * round((1000 * 2 + 100 * 10) * 1.1))
        models = self.session.con.execute("SELECT model FROM usage_events WHERE tenant_id = ?", (self.tenant,)).fetchall()
        self.assertEqual([m[0] for m in models], ["claude-sonnet-5-5"] * 2)

    def test_failed_action_skips_rest(self):
        _, second = self.start(FakeScreen(fail_on={"left_click"}))
        self.assertEqual(self.screen.actions, [{"action": "left_click", "coordinate": [100, 50]}])  # type never ran
        failed, skipped = second["messages"][2]["content"]
        self.assertEqual((failed["content"], failed["is_error"], failed["toolset_name"]), ("Klick ging daneben", True, "computer"))
        self.assertEqual(skipped["tool_use_id"], "toolu_2")
        self.assertEqual(skipped["content"], "Not executed: an earlier computer action in this turn failed.")
        self.assertTrue(skipped["is_error"])
        self.assertEqual(skipped["toolset_name"], "computer")

    def test_client_from_env(self):
        with mock.patch.dict(os.environ, {"HV_PROVIDER": "vertex", "HV_GCP_PROJECT": "hv-test"}):
            os.environ.pop("HV_GCP_REGION", None)  # default region; patch.dict restores env afterwards
            session = agent.Session(tenant_id="env", screen=FakeScreen())
        self.addCleanup(session.con.close)
        self.assertIsInstance(session.client, AnthropicVertex)
        self.assertEqual((session.client.project_id, session.client.region), ("hv-test", "europe-west1"))
        self.assertEqual(session.model(), "claude-sonnet-5-5")

    def test_unknown_provider_rejected(self):
        with mock.patch.dict(os.environ, {"HV_PROVIDER": "mistral"}), self.assertRaises(ValueError):
            agent.Session(tenant_id="x", screen=FakeScreen())


class _Act(dict):
    """OpenAI action stand-in: a pydantic model is read via model_dump()."""

    def model_dump(self, exclude_none=False):
        return dict(self)


def _response(rid, output, inp=1000, out=100):
    return SimpleNamespace(id=rid, output=output, usage=SimpleNamespace(input_tokens=inp, output_tokens=out))


def _computer_call(call_id, actions, checks=None):
    return SimpleNamespace(type="computer_call", call_id=call_id, actions=[_Act(a) for a in actions], pending_safety_checks=checks or [])


def _message(text):
    return SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", text=text)])


class AzureLoopTest(unittest.TestCase):
    """GPT-5.6 Luna on Azure: Responses computer tool, history on the server."""

    def _session(self, replies, screen=None):
        self.requests, replies = [], iter(replies)
        client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: (self.requests.append(kw), next(replies))[1]))
        self.screen, self.lines = screen or FakeScreen(), []
        with mock.patch.dict(os.environ, {"HV_PROVIDER": "azure"}):
            session = agent.Session(tenant_id=self._testMethodName, screen=self.screen, client=client, log=self.lines.append)
        agent.ensure_tenant(session.con, self._testMethodName, 5)
        self.addCleanup(session.con.close)
        return session

    def test_actions_run_then_one_screenshot_goes_back(self):
        call = _computer_call("call_1", [
            {"type": "screenshot"},
            {"type": "click", "button": "left", "x": 100, "y": 50},
            {"type": "keypress", "keys": ["CMD", "C"]},
            {"type": "scroll", "x": 10, "y": 20, "scroll_x": 0, "scroll_y": 120},
        ])
        session = self._session([_response("resp_1", [call]), _response("resp_2", [_message("Fertig.")])])
        session.send("Kopiere das")
        self.assertEqual(self.screen.actions, [
            {"action": "left_click", "coordinate": [100, 50]},
            {"action": "key", "text": "cmd+c"},
            {"action": "scroll", "coordinate": [10, 20], "scroll_direction": "down", "scroll_amount": 2},
        ])
        first, second = self.requests
        self.assertEqual(first["tools"], [{"type": "computer"}])
        self.assertEqual(first["model"], "gpt-5.6-luna")
        self.assertIsNone(first["previous_response_id"])
        self.assertEqual(first["input"][0]["content"][1]["type"], "input_image")
        self.assertEqual(second["previous_response_id"], "resp_1")
        self.assertEqual(second["input"], [{
            "type": "computer_call_output", "call_id": "call_1",
            "output": {"type": "computer_screenshot", "image_url": "data:image/jpeg;base64,aW1n", "detail": "original"},
        }])
        self.assertIn("KI: Fertig.", self.lines)
        self.assertEqual(session.cost_micro_usd, 2 * round((1000 * 1 + 100 * 6) * 1.1))

    def test_safety_check_waits_for_user_then_acknowledges(self):
        check = _Act(id="sc_1", code="malicious_instructions", message="Die Seite enthält Anweisungen.")
        check.message = check["message"]
        call = _computer_call("call_1", [{"type": "click", "button": "left", "x": 1, "y": 2}], [check])
        session = self._session([_response("resp_1", [call]), _response("resp_2", [_message("Erledigt.")])])
        session.send("Klick da")
        self.assertEqual(self.screen.actions, [])
        self.assertIn("Sicherheitsprüfung: Die Seite enthält Anweisungen. Zum Weitermachen antworten.", self.lines)
        session.send("Ja, weiter")
        output = self.requests[1]["input"][0]
        self.assertEqual(output["call_id"], "call_1")
        self.assertEqual(output["acknowledged_safety_checks"][0]["id"], "sc_1")
        self.assertEqual(self.requests[1]["input"][1]["content"][0]["text"], "Ja, weiter")

    def test_screen_texts_follow_each_screenshot(self):
        call = _computer_call("call_1", [{"type": "click", "button": "left", "x": 1, "y": 2}])
        screen = FakeScreen()
        screen.last_texts = [("Speichern", 640, 400)]
        session = self._session([_response("resp_1", [call]), _response("resp_2", [_message("Ok.")])], screen)
        session.send("Speichern")
        first = self.requests[0]["input"][0]["content"]
        self.assertEqual([c["type"] for c in first], ["input_text", "input_image", "input_text"])
        self.assertEqual(first[2]["text"], agent.OCR_HEADER + "\nSpeichern @ 640,400")
        self.assertEqual(self.requests[1]["input"][1]["content"][0]["text"], first[2]["text"])

    def test_long_chain_restarts_with_recap(self):
        click = lambda n: _response(f"resp_{n}", [_computer_call(f"call_{n}", [{"type": "click", "button": "left", "x": n, "y": n}])])
        replies = [click(n) for n in range(1, agent.AZURE_CHAIN_CALLS + 1)] + [_response("resp_last", [_message("Fertig.")])]
        session = self._session(replies)
        session.send("Viele Klicks")
        restart = self.requests[agent.AZURE_CHAIN_CALLS]
        self.assertIsNone(restart["previous_response_id"])
        recap = restart["input"][0]["content"][0]["text"]
        self.assertIn("Viele Klicks", recap)
        self.assertIn("left_click bei [8, 8]", recap)
        self.assertEqual(restart["input"][0]["content"][1]["type"], "input_image")

    def test_second_message_starts_new_chain_with_image(self):
        session = self._session([_response("resp_1", [_message("Fertig.")]), _response("resp_2", [_message("Auch fertig.")])])
        session.send("Erste Aufgabe")
        session.send("Zweite Aufgabe")
        second = self.requests[1]
        self.assertIsNone(second["previous_response_id"])  # Azure rejects a user image after a previous response
        content = second["input"][0]["content"]
        self.assertIn("Erste Aufgabe / Zweite Aufgabe", content[0]["text"])
        self.assertEqual(content[1]["type"], "input_image")

    def test_user_message_rides_along(self):
        call = _computer_call("call_1", [{"type": "type", "text": "Hallo"}])
        session = self._session([_response("resp_1", [call]), _response("resp_2", [_message("Ok.")])])
        session.inbox.put("Schreib lieber Moin")
        session.send("Tipp Hallo")
        follow_up = self.requests[1]["input"]
        self.assertEqual(follow_up[0]["type"], "computer_call_output")
        self.assertEqual(follow_up[1]["content"][0]["text"], "Nachricht vom Nutzer: Schreib lieber Moin")


def _chat_reply(content, calls=(), inp=1000, out=100):
    tool_calls = [SimpleNamespace(id=f"tc_{i}", function=SimpleNamespace(name="computer", arguments=json.dumps(args))) for i, args in enumerate(calls)]
    message = SimpleNamespace(content=content, tool_calls=tool_calls or None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=SimpleNamespace(prompt_tokens=inp, completion_tokens=out))


class TensorxLoopTest(unittest.TestCase):
    """DeepSeek V4.1 Flash on TensorX: chat completions with our computer function."""

    def _session(self, replies, screen=None):
        self.requests, replies = [], iter(replies)
        create = lambda **kw: (self.requests.append(json.loads(json.dumps(kw))), next(replies))[1]
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        self.screen, self.lines = screen or FakeScreen(), []
        with mock.patch.dict(os.environ, {"HV_PROVIDER": "tensorx"}):
            session = agent.Session(tenant_id=self._testMethodName, screen=self.screen, client=client, log=self.lines.append)
        agent.ensure_tenant(session.con, self._testMethodName, 5)
        self.addCleanup(session.con.close)
        return session

    def test_click_then_done(self):
        screen = FakeScreen()
        screen.last_texts = [("Speichern", 640, 400)]
        session = self._session([
            _chat_reply("Ich klicke.", [{"action": "left_click", "coordinate": [640, 400]}, {"action": "type", "text": "Hallo"}]),
            _chat_reply("Fertig."),
        ], screen)
        session.send("Speichern")
        self.assertEqual(screen.actions, [{"action": "left_click", "coordinate": [640, 400]}, {"action": "type", "text": "Hallo"}])
        first, second = self.requests
        self.assertEqual(first["model"], "deepseek/deepseek-v4.1-flash")
        self.assertEqual(first["messages"][0]["role"], "system")
        self.assertEqual([c["type"] for c in first["messages"][1]["content"]], ["text", "image_url", "text"])
        roles = [m["role"] for m in second["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "tool", "tool", "user"])
        self.assertEqual(second["messages"][3], {"role": "tool", "tool_call_id": "tc_0", "content": "OK"})
        self.assertIn("KI: Fertig.", self.lines)
        self.assertEqual(session.cost_micro_usd, 2 * round(1000 * 0.5 + 100 * 1.5))

    def test_failed_action_skips_rest(self):
        session = self._session([
            _chat_reply("", [{"action": "left_click", "coordinate": [1, 2]}, {"action": "type", "text": "x"}]),
            _chat_reply("Anders versucht."),
        ], FakeScreen(fail_on=("left_click",)))
        session.send("Klick")
        tools = [m for m in self.requests[1]["messages"] if m["role"] == "tool"]
        self.assertEqual(tools[0]["content"], "Klick ging daneben")
        self.assertIn("Nicht ausgeführt", tools[1]["content"])


@needs_bedrock
class UserControlTest(unittest.TestCase):
    """Pause, resume and messages from the user while a task runs."""

    def _session(self, replies, screen=None):
        self.bodies, replies = [], iter(replies)

        def handler(request):
            self.bodies.append(json.loads(request.content))
            return httpx2.Response(200, json=next(replies))

        client = AnthropicBedrock(
            aws_region="eu-central-1", aws_access_key="test-access-key", aws_secret_key="x",
            http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        )
        self.lines = []
        session = agent.Session(tenant_id=self._testMethodName, screen=screen or FakeScreen(), client=client, log=self.lines.append)
        agent.ensure_tenant(session.con, self._testMethodName, 5)
        self.addCleanup(session.con.close)
        return session

    def test_message_rides_along_with_tool_results(self):
        session = self._session([CLICK, DONE])
        session.inbox.put("Nimm die andere Rechnung")
        session.send("Öffne die Notizen-App")
        content = self.bodies[1]["messages"][2]["content"]
        self.assertEqual([block["type"] for block in content], ["tool_result", "text"])
        self.assertEqual(content[1]["text"], "Nachricht vom Nutzer: Nimm die andere Rechnung")

    def test_message_after_last_reply_starts_new_turn(self):
        session = self._session([DONE, DONE])
        session.inbox.put("Was hast du gemacht?")
        session.send("Irgendwas")
        self.assertEqual(len(self.bodies), 2)
        last = self.bodies[1]["messages"][-1]
        self.assertEqual(last["role"], "user")
        self.assertEqual(last["content"][0]["text"], "Nachricht vom Nutzer: Was hast du gemacht?")
        self.assertEqual(last["content"][1]["type"], "image")

    def test_pause_waits_until_resume(self):
        screen = FakeScreen()
        session = self._session([CLICK, DONE], screen)
        original_run = screen.run

        def run_and_pause(action):
            session.pause()  # user presses Pause while the click happens
            threading.Timer(0.3, session.resume).start()
            return original_run(action)

        screen.run = run_and_pause
        started = time.time()
        session.send("Öffne die Notizen-App")
        self.assertGreaterEqual(time.time() - started, 0.3)
        self.assertEqual(len(self.bodies), 2)
        self.assertIn("Pausiert.", self.lines)
        self.assertIn("Geht weiter.", self.lines)

    def test_stop_while_paused_ends_task(self):
        screen = FakeScreen()
        session = self._session([CLICK, DONE], screen)

        def run_pause_stop(action):
            session.pause()
            threading.Timer(0.2, session.stop).start()
            return "aW1n"

        screen.run = run_pause_stop
        session.send("Öffne die Notizen-App")
        self.assertEqual(len(self.bodies), 1)
        self.assertIn("Gestoppt.", self.lines)


class PruneTest(unittest.TestCase):
    def test_only_newest_screenshots_stay(self):
        def turn(n):
            return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{n}", "content": [
                {"type": "image", "source": {"data": str(n)}}, {"type": "text", "text": agent.OCR_HEADER + f"\nText {n}"}]}]}
        messages = [{"role": "user", "content": [{"type": "text", "text": "Auftrag"}, {"type": "image", "source": {"data": "0"}}]}]
        for n in range(1, 6):
            messages += [{"role": "assistant", "content": []}, turn(n)]
        agent._prune(messages, 3)
        images = [b["source"]["data"] for m in messages if m["role"] == "user" for c in m["content"]
                  for b in (c["content"] if c.get("type") == "tool_result" else [c]) if b.get("type") == "image"]
        self.assertEqual(images, ["3", "4", "5"])
        self.assertEqual(messages[0]["content"][1], {"type": "text", "text": agent.OLD_SCREENSHOT})
        self.assertEqual(messages[2]["content"][0]["content"][1], {"type": "text", "text": agent.OLD_TEXTS})
        self.assertTrue(messages[-1]["content"][0]["content"][1]["text"].endswith("Text 5"))


class KeyMappingTest(unittest.TestCase):
    def test_glide_moves_in_steps_to_target(self):
        screen = mac_control.MacScreen.__new__(mac_control.MacScreen)
        points = []
        with (
            mock.patch.object(screen, "_cursor", return_value=(0.0, 0.0), create=True),
            mock.patch.object(screen, "_post_mouse", side_effect=lambda kind, point, *a: points.append(point), create=True),
            mock.patch.object(mac_control.time, "sleep"),
        ):
            screen._move(100, 50)
        self.assertEqual(len(points), mac_control.GLIDE_STEPS)
        self.assertEqual(points[-1], (100, 50))
        self.assertTrue(all(a[0] <= b[0] for a, b in zip(points, points[1:])))

    def test_applescript_for_shortcuts(self):
        screen = mac_control.MacScreen.__new__(mac_control.MacScreen)
        with mock.patch.object(mac_control.subprocess, "run") as run:
            screen._key("cmd+c Return ctrl+shift+Tab")
        scripts = [call.args[0][2] for call in run.call_args_list]
        self.assertEqual(
            scripts,
            [
                'tell application "System Events" to keystroke "c" using {command down}',
                'tell application "System Events" to key code 36',
                'tell application "System Events" to key code 48 using {control down, shift down}',
            ],
        )

    def test_point_mapping(self):
        screen = mac_control.MacScreen.__new__(mac_control.MacScreen)
        screen.points_w, screen.points_h, screen.width, screen.height = 1512, 982, 1280, 831
        x, y = screen._point([640, 415])
        self.assertAlmostEqual(x, 756, delta=1)
        self.assertAlmostEqual(y, 490, delta=1)


HV_KEY = "hv_test_0123456789abcdef"
BUDGET_ERROR = {"type": "error", "error": {"type": "budget_exhausted", "message": "KI-Credits aufgebraucht"}}


def _pairs_valid(messages):
    """Every tool_result answers a tool_use of the assistant turn right before it, and every tool_use is answered."""
    for i, message in enumerate(messages):
        content = message["content"] if isinstance(message["content"], list) else []
        results = {b["tool_use_id"] for b in content if b.get("type") == "tool_result"}
        if message["role"] == "user" and results:
            before = messages[i - 1] if i else {"role": "none", "content": []}
            uses = {b["id"] for b in before["content"] if before["role"] == "assistant" and b.get("type") == "tool_use"}
            if results != uses:
                return False
        if message["role"] == "assistant":
            uses = {b["id"] for b in content if b.get("type") == "tool_use"}
            after = messages[i + 1]["content"] if i + 1 < len(messages) else None
            if uses and after is not None and uses != {b["tool_use_id"] for b in after if b.get("type") == "tool_result"}:
                return False
    return True


class HausverwalterLoopTest(unittest.TestCase):
    """Customer mode: Claude through api.hausverwalter.ai with the customer's key, the server bills."""

    def _session(self, replies, screen=None):
        self.requests, replies = [], iter(replies)

        def handler(request):
            self.requests.append(request)
            status, body = next(replies)
            return httpx2.Response(status, json=body)

        client = agent.hausverwalter_client(HV_KEY, http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
        self.screen, self.lines = screen or FakeScreen(), []
        with mock.patch.dict(os.environ, {"HV_PROVIDER": "hausverwalter"}):
            return agent.Session(tenant_id="kunde", screen=self.screen, client=client, log=self.lines.append)

    def test_request_shape_and_headers(self):
        session = self._session([(200, CLICK), (200, DONE)])
        session.send("Öffne die Notizen-App")
        self.assertEqual(self.screen.actions, [{"action": "left_click", "coordinate": [100, 50]}])
        request = self.requests[0]
        self.assertEqual((request.url.scheme, request.url.host, request.url.path), ("https", "api.hausverwalter.ai", "/api/v1/computer-use/v1/messages"))
        self.assertEqual(request.headers["authorization"], f"Bearer {HV_KEY}")
        self.assertEqual(request.headers["x-hv-client"], f"hausverwalter-computer/{agent.VERSION}")
        self.assertEqual(request.headers["x-hv-task-id"], session.task_id)
        self.assertEqual(request.headers["anthropic-beta"], "computer-use-2025-11-24")
        self.assertNotIn("x-amzn-bedrock-request-metadata", request.headers)
        body = json.loads(request.content)
        self.assertEqual(body["model"], "claude-sonnet-4-6")
        self.assertEqual(body["tools"][0]["type"], "computer_20251124")
        self.assertEqual(body["output_config"], {"effort": agent.EFFORT})
        self.assertEqual(self.requests[1].headers["x-hv-task-id"], session.task_id)  # same task, every call
        # nothing stored locally, only an estimate: Sonnet 4.6 $3/$15 per MTok + 10 %
        self.assertIsNone(session.con)
        self.assertEqual(session.cost_micro_usd, 2 * round((1000 * 3 + 100 * 15) * 1.1))
        self.assertEqual((session.tokens_in, session.tokens_out), (2000, 200))
        self.assertIn("KI: Fertig.", self.lines)

    def test_budget_exhausted_stops_cleanly(self):
        session = self._session([(200, CLICK), (402, BUDGET_ERROR)])
        session.send("Öffne die Notizen-App")
        self.assertEqual(len(self.requests), 2)  # no retry, no further call
        self.assertEqual(self.lines[-1], "KI-Guthaben aufgebraucht – in hausverwalter.ai aufladen.")
        self.assertEqual(session.error_count, 1)

    def test_invalid_key(self):
        session = self._session([(401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid key"}})])
        session.send("Irgendwas")
        self.assertEqual(self.lines[-1], agent.KEY_INVALID)
        self.assertNotIn(HV_KEY, "\n".join(self.lines))

    def test_key_comes_from_the_os_key_store(self):
        store = {}
        fake = ModuleType("keyring")
        fake.get_password = lambda service, account: store.get((service, account))
        with mock.patch.dict(sys.modules, {"keyring": fake}), mock.patch.dict(os.environ, {"HV_PROVIDER": "hausverwalter"}):
            with self.assertRaises(agent.MissingKey):
                agent.Session(tenant_id="kunde", screen=FakeScreen())
            store[("hausverwalter-computer", "api-key")] = HV_KEY
            session = agent.Session(tenant_id="kunde", screen=FakeScreen())
        self.assertEqual(session.client.auth_token, HV_KEY)
        self.assertEqual(str(session.client.base_url).rstrip("/"), agent.HV_API_BASE)


class _Blocking:
    """Fake model endpoint whose answers wait until release(): a model call that is still running."""

    def __init__(self, replies):
        self.replies, self.entered, self.released, self.bodies = iter(replies), threading.Event(), threading.Event(), []

    def __call__(self, request):
        self.bodies.append(json.loads(request.content))
        self.entered.set()
        self.released.wait(5)
        return httpx2.Response(200, json=next(self.replies))


@needs_bedrock
class StopAndPauseTest(unittest.TestCase):
    """Stopp acts at once (the model call in flight is dropped), Pause holds the next action."""

    def _session(self, handler, screen=None):
        client = AnthropicBedrock(
            aws_region="eu-central-1", aws_access_key="test-access-key", aws_secret_key="x",
            http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        )
        self.screen, self.lines = screen or FakeScreen(), []
        session = agent.Session(tenant_id=self._testMethodName, screen=self.screen, client=client, log=self.lines.append)
        agent.ensure_tenant(session.con, self._testMethodName, 5)
        self.addCleanup(session.con.close)
        return session

    def test_stop_drops_the_running_model_call(self):
        endpoint = _Blocking([CLICK])
        self.addCleanup(endpoint.released.set)
        session = self._session(endpoint)
        worker = threading.Thread(target=session.send, args=("Öffne die Notizen-App",))
        worker.start()
        self.assertTrue(endpoint.entered.wait(2))
        started = time.time()
        session.stop()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertLess(time.time() - started, 0.5)
        endpoint.released.set()  # the answer (a click) arrives late
        time.sleep(0.1)
        self.assertEqual(self.screen.actions, [])
        self.assertEqual(self.lines.count("Gestoppt."), 1)

    def test_stop_during_an_action_skips_the_rest_and_next_message_continues(self):
        two = {**CLICK, "content": [*CLICK["content"], {"type": "tool_use", "id": "toolu_2", "name": "computer", "input": {"action": "type", "text": "x"}}]}
        endpoint = _Blocking([two, DONE])
        endpoint.released.set()
        screen = FakeScreen()
        session = self._session(endpoint, screen)
        original = screen.run
        screen.run = lambda action, look=True: (session.stop(), original(action, look))[1]
        session.send("Öffne die Notizen-App")
        self.assertEqual([a["action"] for a in screen.actions], ["left_click"])  # type never ran
        self.assertEqual(len(endpoint.bodies), 1)
        screen.run = original
        session.send("Mach weiter")
        messages = endpoint.bodies[1]["messages"]
        roles = [m["role"] for m in messages]
        self.assertEqual(roles, ["user", "assistant", "user"])  # the new message joins the stopped turn
        self.assertTrue(_pairs_valid(messages))
        self.assertEqual(messages[2]["content"][1]["content"], agent.STOPPED)
        self.assertEqual(messages[2]["content"][2]["text"], "Mach weiter")

    def test_pause_holds_the_next_action(self):
        two = {**CLICK, "content": [*CLICK["content"], {"type": "tool_use", "id": "toolu_2", "name": "computer", "input": {"action": "type", "text": "x"}}]}
        endpoint = _Blocking([two, DONE])
        endpoint.released.set()
        screen = FakeScreen()
        session = self._session(endpoint, screen)
        original, seen = screen.run, []

        def run(action, look=True):
            if action["action"] == "left_click":
                session.pause()
                threading.Timer(0.3, lambda: (seen.append(list(screen.actions)), session.resume())).start()
            return original(action, look)

        screen.run = run
        session.send("Öffne die Notizen-App")
        self.assertEqual(seen, [[{"action": "left_click", "coordinate": [100, 50]}]])  # type waited for Weiter
        self.assertEqual([a["action"] for a in screen.actions], ["left_click", "type"])

    def test_typing_ends_mid_way_on_stop(self):
        screen = mac_control.MacScreen.__new__(mac_control.MacScreen)
        screen.stop_event = threading.Event()
        typed = []

        def post(*args):
            typed.append(args)
            if len(typed) == 6:  # three characters (down + up each)
                screen.stop_event.set()

        fake_quartz = mock.MagicMock()
        fake_quartz.CGEventPost.side_effect = post
        with mock.patch.object(mac_control, "Quartz", fake_quartz), mock.patch.object(mac_control.time, "sleep"):
            with self.assertRaises(mac_control.Interrupted):
                screen._type("Sehr langer Text")
        self.assertEqual(len(typed), 6)


@needs_bedrock
class CompactionTest(unittest.TestCase):
    """Long runs: after COMPACT_AFTER_CALLS calls a fresh conversation with a recap, tool pairs stay valid."""

    def test_claude_history_restarts_with_recap(self):
        bodies, replies = [], iter([CLICK, CLICK, CLICK, DONE])

        def handler(request):
            bodies.append(json.loads(request.content))
            return httpx2.Response(200, json=next(replies))

        client = AnthropicBedrock(
            aws_region="eu-central-1", aws_access_key="test-access-key", aws_secret_key="x",
            http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        )
        screen = FakeScreen()
        screen.last_texts = [("Notizen", 100, 50)]
        session = agent.Session(tenant_id=self._testMethodName, screen=screen, client=client, log=[].append)
        agent.ensure_tenant(session.con, self._testMethodName, 5)
        self.addCleanup(session.con.close)
        session.inbox.put("Nimm die zweite Notiz")
        with mock.patch.object(agent, "COMPACT_AFTER_CALLS", 2):
            session.send("Öffne die Notizen-App")
        self.assertEqual(len(bodies), 4)
        self.assertEqual([m["role"] for m in bodies[1]["messages"]], ["user", "assistant", "user"])
        fresh = bodies[2]["messages"]
        self.assertEqual([m["role"] for m in fresh], ["user"])
        recap = fresh[0]["content"][0]["text"]
        self.assertIn("Öffne die Notizen-App / Nimm die zweite Notiz", recap)
        self.assertIn("left_click bei [100, 50]", recap)
        self.assertIn("Ich öffne die Notizen.", recap)
        self.assertEqual([b["type"] for b in fresh[0]["content"]][-2:], ["image", "text"])  # current screen + its texts
        self.assertNotIn("tool_result", json.dumps(fresh))
        for body in bodies:
            self.assertTrue(_pairs_valid(body["messages"]))

    def test_chat_history_restarts_with_recap(self):
        click = _chat_reply("Klick.", [{"action": "left_click", "coordinate": [1, 2]}])
        requests, replies = [], iter([click, click, click, _chat_reply("Fertig.")])
        create = lambda **kw: (requests.append(json.loads(json.dumps(kw))), next(replies))[1]
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with mock.patch.dict(os.environ, {"HV_PROVIDER": "tensorx"}):
            session = agent.Session(tenant_id=self._testMethodName, screen=FakeScreen(), client=client, log=[].append)
        agent.ensure_tenant(session.con, self._testMethodName, 5)
        self.addCleanup(session.con.close)
        with mock.patch.object(agent, "COMPACT_AFTER_CALLS", 2):
            session.send("Viele Klicks")
        fresh = requests[2]["messages"]
        self.assertEqual([m["role"] for m in fresh], ["system", "user"])
        self.assertIn("Viele Klicks", fresh[1]["content"][0]["text"])
        self.assertIn("left_click bei [1, 2]", fresh[1]["content"][0]["text"])
        self.assertEqual(len(requests), 4)

    def test_recap_keeps_first_task_and_newest_messages(self):
        session = agent.Session(tenant_id="recap", screen=FakeScreen(), client=object(), log=[].append)
        self.addCleanup(session.con.close)
        session.asked = [f"Nachricht {n}" for n in range(40)]
        session.done = [f"klick {n}" for n in range(100)]
        recap = session._recap()
        self.assertIn("Nachricht 0 / … / Nachricht 20", recap)
        self.assertIn("klick 70; ", recap)
        self.assertNotIn("klick 69;", recap)


class SystemPromptTest(unittest.TestCase):
    def test_os_specific_shortcuts_and_shared_rules(self):
        mac, win = agent.system_prompt("darwin"), agent.system_prompt("win32")
        self.assertIn("cmd statt ctrl", mac)
        self.assertIn("Windows-PC", win)
        self.assertIn("„win“ allein öffnet das Startmenü", win)
        self.assertIn("Remotedesktop", win)
        self.assertNotIn("cmd statt ctrl", win)
        for prompt in (mac, win):
            self.assertIn("selbstständig, bis der Auftrag ganz erledigt ist", prompt)
            self.assertIn("hörst du sofort auf und berichtest genau", prompt)

    def test_session_follows_the_screen(self):
        screen = FakeScreen()
        screen.platform = "win32"
        session = agent.Session(tenant_id="os", screen=screen, client=object(), log=[].append)
        self.addCleanup(session.con.close)
        self.assertEqual(session.system, agent.system_prompt("win32"))

    def test_long_run_defaults(self):
        if "HV_MAX_STEPS" not in os.environ:
            self.assertEqual(agent.MAX_STEPS, 400)
        self.assertEqual(agent.COMPACT_AFTER_CALLS, 25)


class ErrorCounterTest(unittest.TestCase):
    def test_failed_actions_are_counted(self):
        session = agent.Session(tenant_id="errors", screen=FakeScreen(fail_on={"left_click"}), client=object(), log=[].append)
        self.addCleanup(session.con.close)
        tool_use = SimpleNamespace(id="t1", input={"action": "left_click", "coordinate": [1, 2]})
        result = session._execute(tool_use)
        self.assertTrue(result["is_error"])
        self.assertEqual(session.error_count, 1)
        session.stop()
        self.assertEqual(session._execute(tool_use)["content"], agent.STOPPED)
        self.assertEqual(session.error_count, 1)  # Stopp is no error


if __name__ == "__main__":
    unittest.main()
