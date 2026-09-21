"""Opt-in two-turn synthetic API test; no listening socket or tool execution."""

import argparse
import asyncio
import json
import os
import uuid
from dataclasses import replace
from pathlib import Path

import httpx

from ..paths import BASE_DIR
from ..v2.config import Settings
from ..v2.transport import build_transport
from .app import create_app
from .protocol import MODEL
from .runtime import TextTransport


async def run(args):
    root = args.output_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    settings = replace(
        Settings.load(),
        data_dir=root / "runtime",
        auth_dir=args.auth_dir.resolve(strict=True),
        backend="playwright",
        concurrency=1,
        interval=20.0,
        stream_mode="incremental",
        stream_validation=None,
    )
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BASE_DIR / ".playwright-browsers")
    report = {
        "model": MODEL,
        "passed": False,
        "scope": "real_campus_synthetic_bridge_roundtrip_not_Kilo_UI",
        "tool_execution": False,
        "turns": [],
    }
    inner = await build_transport("playwright", settings.data_dir, settings)
    try:
        await inner.authenticate()
        catalog = await inner.request("/config/?quota_pool=Students%20Pool")
        if MODEL not in catalog.get("body", catalog).get("availableModels", []):
            raise ValueError("model_unavailable")
        app = create_app(settings, TextTransport(inner))
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://bridge.local",
                headers={"Authorization": "Bearer " + settings.api_key},
                timeout=180,
            ) as client,
        ):
            body = {
                "model": MODEL,
                "max_tokens": 2048,
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "read_fixture",
                            "description": "Return the synthetic test value from the client, no actual filesystem access.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string", "enum": ["fixture.txt"]}
                                },
                                "required": ["path"],
                                "additionalProperties": False,
                            },
                        },
                    }
                ],
                "tool_choice": "required",
                "messages": [
                    {
                        "role": "system",
                        "content": "This is a synthetic integration test. Ask the client tool for the fixture. When the tool result arrives, answer with its exact value only. Do not browse or use campus tools.",
                    },
                    {
                        "role": "user",
                        "content": "Please get the value of fixture.txt through read_fixture.",
                    },
                ],
            }
            print("Starting synthetic bridge tool request", flush=True)
            async with asyncio.timeout(180):
                first = await client.post("/v1/chat/completions", json=body)
            report["turns"].append(
                {"http_status": first.status_code, "body": first.json()}
            )
            first.raise_for_status()
            assistant = first.json()["choices"][0]["message"]
            call = assistant["tool_calls"][0]
            if call["function"]["name"] != "read_fixture" or json.loads(
                call["function"]["arguments"]
            ) != {"path": "fixture.txt"}:
                raise ValueError("fixture_call_mismatch")
            value = "fixture-" + uuid.uuid4().hex
            body["messages"].extend(
                [
                    assistant,
                    {"role": "tool", "tool_call_id": call["id"], "content": value},
                ]
            )
            body["tool_choice"] = "none"
            print("Starting synthetic bridge tool-result response", flush=True)
            async with asyncio.timeout(180):
                second = await client.post("/v1/chat/completions", json=body)
            report["turns"].append(
                {"http_status": second.status_code, "body": second.json()}
            )
            second.raise_for_status()
            choice = second.json()["choices"][0]
            report["passed"] = (
                choice["finish_reason"] == "stop"
                and choice["message"]["content"] == value
            )
    except Exception as exc:  # noqa: BLE001 - report no credential-bearing SDK traces
        report["error"] = {"code": getattr(exc, "code", type(exc).__name__)}
    finally:
        await inner.close()
        with (root / "report.json").open("x", encoding="utf-8") as file:
            json.dump(report, file, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--acknowledge-upstream-tools", action="store_true")
    args = parser.parse_args()
    if not args.acknowledge_upstream_tools:
        parser.error(
            "Explicit acknowledgement required: campus built-in tools cannot be reliably disabled"
        )
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
