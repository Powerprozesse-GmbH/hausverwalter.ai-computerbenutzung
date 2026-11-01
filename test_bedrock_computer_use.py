"""Offline test: checks the exact HTTP request the SDK sends to Bedrock and the metering.

Run: python -m unittest test_bedrock_computer_use.py
"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import httpx2
from anthropic import AnthropicBedrock

import bedrock_computer_use as bcu

PROFILE_ARN = "arn:aws:bedrock:eu-central-1:111122223333:application-inference-profile/abc123"

FAKE_RESPONSE = {
    "id": "msg_test",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-5-5",
    "content": [
        {
            "type": "tool_use",
            "id": "toolu_test",
            "name": "computer",
            "input": {"action": "left_click", "coordinate": [640, 400]},
        }
    ],
    "stop_reason": "tool_use",
    "stop_sequence": None,
    "usage": {
        "input_tokens": 1_000_000,
        "output_tokens": 100_000,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    },
}


@unittest.skipUnless(importlib.util.find_spec("botocore"), "anthropic[bedrock] not installed (internal provider)")
class ComputerUseStepTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = bcu.open_db(Path(self.tmp.name, "m.sqlite"))
        self.con.execute(
            "INSERT INTO tenants VALUES (?, ?, ?, ?, ?)", ("kunde_a", "Kunde A", PROFILE_ARN, 5_000_000, 0)
        )
        self.requests = []

        def handler(request):
            self.requests.append(request)
            return httpx2.Response(200, json=FAKE_RESPONSE, headers={"x-amzn-requestid": "req-1"})

        self.client = AnthropicBedrock(
            aws_region="eu-central-1",
            aws_access_key="test-access-key",
            aws_secret_key="secret",
            http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        )

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def step(self):
        messages = [{"role": "user", "content": "Öffne die Notizen-App"}]
        return bcu.computer_use_step(self.client, self.con, "kunde_a", "task-1", messages, 1280, 800)

    def test_request_shape(self):
        self.step()
        request = self.requests[0]
        self.assertEqual(request.url.host, "bedrock-runtime.eu-central-1.amazonaws.com")
        self.assertEqual(
            request.url.raw_path.decode(),
            "/model/arn:aws:bedrock:eu-central-1:111122223333:application-inference-profile%2Fabc123/invoke",
        )
        body = json.loads(request.content)
        self.assertEqual(body["anthropic_beta"], ["computer-use-2025-11-24"])
        self.assertEqual(body["tools"][0]["type"], "computer_20251124")
        self.assertNotIn("model", body)
        metadata = request.headers["x-amzn-bedrock-request-metadata"]
        self.assertEqual(json.loads(metadata), {"tenant_id": "kunde_a", "task_id": "task-1"})
        signed = request.headers["authorization"].split("SignedHeaders=")[1].split(",")[0]
        self.assertIn("x-amzn-bedrock-request-metadata", signed.split(";"))

    def test_metering_and_budget_gate(self):
        response = self.step()
        self.assertEqual(response.content[0].input["coordinate"], [640, 400])
        row = self.con.execute("SELECT * FROM usage_events").fetchone()
        # 1M input * 2.00 + 100k output * 10.00 = 3.00 USD, plus 10 % = 3.30 USD
        self.assertEqual(row["cost_micro_usd"], 3_300_000)
        self.assertEqual(row["aws_request_id"], "req-1")
        self.assertEqual(bcu.remaining_micro_usd(self.con, "kunde_a"), 1_700_000)
        self.step()  # 1.70 USD left, call allowed, overshoots to -1.60 USD
        with self.assertRaises(bcu.BudgetExhausted):
            self.step()
        self.assertEqual(len(self.requests), 2)


if __name__ == "__main__":
    unittest.main()
