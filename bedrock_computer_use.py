"""Prototype: Claude computer use on Amazon Bedrock (EU) with per-tenant metering.

Commands:
  setup-tenant  create a tagged application inference profile for a tenant + DB row
  step          one computer-use step on a screenshot of this Mac; Claude proposes
                the next action, nothing is executed
  report        calls, tokens and cost per tenant

Credentials come from the normal AWS chain (AWS_PROFILE, SSO, env vars).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from anthropic import AnthropicBedrock

REGION = "eu-central-1"
# EU geo inference profile. From eu-central-1 it routes only to Frankfurt, Stockholm,
# Milan, Spain, Ireland and Paris. Claude Sonnet 5.5 has no Frankfurt-only option.
SOURCE_PROFILE_ID = "eu.anthropic.claude-sonnet-5-5"
COMPUTER_USE_BETA = "computer-use-2025-11-24"
MAX_SCREENSHOT_EDGE = 1280
DB_PATH = Path(os.environ.get("HV_METERING_DB", "metering.sqlite"))
SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# USD per 1M tokens = micro-USD per token. Bedrock rate card for Claude Sonnet 5.5,
# "Standard, Global" (offer read 31.10.2026 via list-foundation-model-agreement-offers).
PRICE_PER_MTOK = {"input": 2.00, "output": 10.00, "cache_write": 2.50, "cache_read": 0.10}
REGIONAL_PREMIUM = 1.10  # geo/regional endpoints cost 10 % more than global (Anthropic docs)


class BudgetExhausted(Exception):
    pass


def open_db(path: Path = DB_PATH) -> sqlite3.Connection:
    # The desktop server opens the connection in one thread and runs each task in another (one at a time).
    con = sqlite3.connect(path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA_PATH.read_text())
    return con


def get_tenant(con: sqlite3.Connection, tenant_id: str) -> sqlite3.Row:
    row = con.execute("SELECT * FROM tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()
    if row is None:
        raise KeyError(f"unknown tenant {tenant_id!r}, run setup-tenant first")
    return row


def remaining_micro_usd(con: sqlite3.Connection, tenant_id: str) -> int:
    tenant = get_tenant(con, tenant_id)
    (spent,) = con.execute(
        "SELECT COALESCE(SUM(cost_micro_usd), 0) FROM usage_events WHERE tenant_id = ?",
        (tenant_id,),
    ).fetchone()
    return tenant["budget_micro_usd"] - spent


def cost_micro_usd(usage: Any, price: dict[str, float] = PRICE_PER_MTOK, premium: float = REGIONAL_PREMIUM) -> int:
    rate = {kind: value * premium for kind, value in price.items()}
    return round(
        usage.input_tokens * rate["input"]
        + usage.output_tokens * rate["output"]
        + (usage.cache_creation_input_tokens or 0) * rate.get("cache_write", 0)
        + (usage.cache_read_input_tokens or 0) * rate.get("cache_read", 0)
    )


def record_usage(
    con: sqlite3.Connection,
    tenant_id: str,
    task_id: str,
    usage: Any,
    aws_request_id: str | None,
    model: str = SOURCE_PROFILE_ID,
    price: dict[str, float] = PRICE_PER_MTOK,
    premium: float = REGIONAL_PREMIUM,
) -> int:
    cost = cost_micro_usd(usage, price, premium)
    con.execute(
        "INSERT INTO usage_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(uuid.uuid4()),
            tenant_id,
            task_id,
            int(time.time()),
            model,
            aws_request_id,
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_creation_input_tokens or 0,
            usage.cache_read_input_tokens or 0,
            cost,
        ),
    )
    con.commit()
    return cost


def setup_tenant(con: sqlite3.Connection, tenant_id: str, name: str, budget_usd: float) -> str:
    """Creates one application inference profile per tenant, tagged for Cost Explorer."""
    import boto3  # only here: the desktop agent imports this module for metering and needs no boto3

    account = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
    source_arn = f"arn:aws:bedrock:{REGION}:{account}:inference-profile/{SOURCE_PROFILE_ID}"
    profile = boto3.client("bedrock", region_name=REGION).create_inference_profile(
        inferenceProfileName=f"hv-{tenant_id}-sonnet-5-5",
        description=f"hausverwalter.ai computer use tenant {tenant_id}",  # AWS allows no commas here
        modelSource={"copyFrom": source_arn},
        tags=[
            {"key": "TenantID", "value": tenant_id},
            {"key": "Product", "value": "hv-computer-use"},
        ],
    )
    arn = profile["inferenceProfileArn"]
    con.execute(
        "INSERT INTO tenants VALUES (?, ?, ?, ?, ?)",
        (tenant_id, name, arn, round(budget_usd * 1_000_000), int(time.time())),
    )
    con.commit()
    return arn


def computer_use_step(
    client: AnthropicBedrock,
    con: sqlite3.Connection,
    tenant_id: str,
    task_id: str,
    messages: list[dict[str, Any]],
    display_width: int,
    display_height: int,
) -> Any:
    """One model call of a computer-use task: budget gate, call, metering."""
    if remaining_micro_usd(con, tenant_id) <= 0:
        raise BudgetExhausted(tenant_id)
    tenant = get_tenant(con, tenant_id)
    raw = client.beta.messages.with_raw_response.create(
        model=tenant["inference_profile_arn"],
        max_tokens=16000,
        betas=[COMPUTER_USE_BETA],
        tools=[
            {
                "type": "computer_20251124",
                "name": "computer",
                "display_width_px": display_width,
                "display_height_px": display_height,
            }
        ],
        output_config={"effort": "medium"},
        messages=messages,
        # Lands in Bedrock model invocation logs (only if logging is switched on).
        extra_headers={
            "X-Amzn-Bedrock-Request-Metadata": json.dumps(
                {"tenant_id": tenant_id, "task_id": task_id}
            )
        },
    )
    response = raw.parse()
    record_usage(con, tenant_id, task_id, response.usage, raw.headers.get("x-amzn-requestid"))
    return response


def take_screenshot() -> tuple[str, int, int, int, int]:
    """Main display as base64 PNG, long edge scaled to MAX_SCREENSHOT_EDGE.

    Returns (data, width, height, original_width, original_height).
    """
    with tempfile.TemporaryDirectory() as tmp:
        raw_png, small_png = Path(tmp, "raw.png"), Path(tmp, "small.png")
        subprocess.run(["screencapture", "-x", "-m", str(raw_png)], check=True)
        orig_w, orig_h = _png_size(raw_png)
        subprocess.run(
            ["sips", "-Z", str(MAX_SCREENSHOT_EDGE), str(raw_png), "--out", str(small_png)],
            check=True,
            capture_output=True,
        )
        width, height = _png_size(small_png)
        return base64.b64encode(small_png.read_bytes()).decode(), width, height, orig_w, orig_h


def _png_size(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[16:24]
    return int.from_bytes(header[:4], "big"), int.from_bytes(header[4:], "big")


def cmd_step(con: sqlite3.Connection, tenant_id: str, task: str) -> None:
    data, width, height, orig_w, orig_h = take_screenshot()
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": task},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}},
            ],
        }
    ]
    task_id = str(uuid.uuid4())
    response = computer_use_step(
        AnthropicBedrock(aws_region=REGION), con, tenant_id, task_id, messages, width, height
    )
    if response.stop_reason == "refusal":
        print("Claude hat die Aufgabe abgelehnt.")
    scale_x, scale_y = orig_w / width, orig_h / height
    for block in response.content:
        if block.type == "text":
            print(f"Claude: {block.text}")
        elif block.type == "tool_use":
            action = dict(block.input)
            if "coordinate" in action:
                x, y = action["coordinate"]
                action["coordinate_on_screen"] = [round(x * scale_x), round(y * scale_y)]
            print(f"Nächste Aktion (nicht ausgeführt): {json.dumps(action, ensure_ascii=False)}")
    usage = response.usage
    print(
        f"Tokens: {usage.input_tokens} rein, {usage.output_tokens} raus | "
        f"Restbudget {tenant_id}: {remaining_micro_usd(con, tenant_id) / 1_000_000:.4f} USD"
    )


def cmd_report(con: sqlite3.Connection) -> None:
    rows = con.execute(
        """
        SELECT t.tenant_id, t.budget_micro_usd, COUNT(u.id) AS calls,
               COALESCE(SUM(u.input_tokens), 0) AS input_tokens,
               COALESCE(SUM(u.output_tokens), 0) AS output_tokens,
               COALESCE(SUM(u.cost_micro_usd), 0) AS cost
        FROM tenants t LEFT JOIN usage_events u USING (tenant_id)
        GROUP BY t.tenant_id ORDER BY t.tenant_id
        """
    ).fetchall()
    for r in rows:
        print(
            f"{r['tenant_id']}: {r['calls']} Aufrufe, {r['input_tokens']} / {r['output_tokens']} Tokens, "
            f"{r['cost'] / 1_000_000:.4f} von {r['budget_micro_usd'] / 1_000_000:.2f} USD"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("setup-tenant")
    setup.add_argument("--tenant", required=True)
    setup.add_argument("--name", required=True)
    setup.add_argument("--budget-usd", type=float, required=True)
    step = sub.add_parser("step")
    step.add_argument("--tenant", required=True)
    step.add_argument("--task", required=True)
    sub.add_parser("report")
    args = parser.parse_args()

    con = open_db()
    try:
        if args.command == "setup-tenant":
            print(setup_tenant(con, args.tenant, args.name, args.budget_usd))
        elif args.command == "step":
            cmd_step(con, args.tenant, args.task)
        else:
            cmd_report(con)
    except BudgetExhausted as exc:
        sys.exit(f"Budget aufgebraucht für {exc}. Zugriff gesperrt.")


if __name__ == "__main__":
    main()
