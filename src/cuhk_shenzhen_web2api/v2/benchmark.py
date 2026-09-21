"""Explicit online benchmark: bounded stages, new reports, no configuration writes."""

import argparse
import asyncio
import json
import time
import uuid
from pathlib import Path

import httpx

from .config import Settings
from .metrics import summarize


def validate_previous(args, previous):
    if previous is None:
        if args.stage != 1 or args.interval != 20:
            raise ValueError(
                "Start with concurrency=1 interval=20, or supply a passing previous report"
            )
        return
    if not previous.get("passed") or previous.get("model") != args.model:
        raise ValueError("Previous stage must pass with the same model")
    if (
        previous.get("protocol") != "short-marker-v2"
        or previous.get("requested") != args.count
    ):
        raise ValueError(
            "Previous stage must use the same prompt protocol and sample count"
        )
    old_stage, old_interval = previous.get("stage"), previous.get("interval")
    same = old_stage == args.stage and old_interval == args.interval
    interval_step = args.stage == old_stage == 1 and (old_interval, args.interval) in [
        (20, 10),
        (10, 5),
    ]
    concurrency_step = args.stage == 2 * old_stage and args.interval == old_interval
    if not (same or interval_step or concurrency_step):
        raise ValueError(
            "Only same-setting comparisons, 20->10->5, or concurrency 1->2->4 are allowed"
        )


async def measure(client, args, index, session_ids):
    marker = uuid.uuid4().hex[:12]
    started = time.perf_counter()
    row = {
        "index": index,
        "passed": False,
        "status": "failed",
        "client": {},
        "server": {},
    }
    text, sid, request_id = "", None, None
    terminal, first = None, None
    try:
        async with client.stream(
            "POST",
            "/response",
            headers={"Idempotency-Key": "bench-" + marker},
            json={
                "message": "只回复以下标识，不添加其他文字：" + marker,
                "approach_id": args.model,
                "conversation": "bench-" + marker,
                "params": {"max_tokens": 64},
                "stream": True,
            },
        ) as response:
            row["http_status"] = response.status_code
            request_id = response.headers.get("x-request-id")
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                if "error" in event:
                    raise ValueError("stream_error")
                if event.get("event") == "start":
                    sid = event.get("chat_session_id")
                if (
                    event.get("event") == "msg"
                    and event.get("item", {}).get("type") == "text"
                ):
                    if first is None:
                        first = time.perf_counter() - started
                    text += event["item"].get("content", "")
                    if len(text) > 4096:
                        raise ValueError("output_limit")
                if event.get("event") == "end":
                    terminal = event.get("status")
        row["status"] = terminal or "incomplete"
        row["passed"] = (
            terminal == "finished"
            and text.strip() == marker
            and bool(sid)
            and sid not in session_ids
        )
        if sid:
            session_ids.add(sid)
    except (httpx.HTTPError, ValueError, KeyError):
        row["passed"] = False
    finally:
        row["client"] = {
            "first_text_seconds": first,
            "total_seconds": time.perf_counter() - started,
        }
    if request_id:
        try:
            response = await client.get("/requests/" + request_id)
            response.raise_for_status()
            record = response.json()
            row["server"] = record.get("timings") or {}
            row["status"] = record["status"]
            row["rate_limit_count"] = record.get("rate_limit_count", 0)
            row["passed"] = (
                row["passed"]
                and record["status"] == "done"
                and row["rate_limit_count"] == 0
            )
        except (httpx.HTTPError, ValueError, KeyError):
            row["passed"] = False
    else:
        row["passed"] = False
    return row


async def run(args):
    settings = Settings.load()
    previous = (
        json.loads(args.previous.read_text(encoding="utf-8")) if args.previous else None
    )
    validate_previous(args, previous)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    report = {
        "protocol": "short-marker-v2",
        "stage": args.stage,
        "interval": args.interval,
        "model": args.model,
        "requested": args.count,
        "passed": False,
        "results": [],
        "limits": "Small sample, not a quota or long-term guarantee. Token cap depends on upstream support.",
    }
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            base_url=args.url,
            headers={"Authorization": "Bearer " + settings.api_key},
            timeout=180,
            trust_env=False,
        ) as client:
            ready = await client.get("/ready")
            ready.raise_for_status()
            state = ready.json()
            if (
                state.get("concurrency") != args.stage
                or state.get("interval") != args.interval
            ):
                raise ValueError(
                    "Server concurrency/interval does not match this stage"
                )
            report["stream_mode"] = state.get("stream_mode", "buffered")
            report["streaming_validated"] = state.get("streaming", False)
            catalog = await client.get("/v1/models")
            catalog.raise_for_status()
            if args.model not in [m["id"] for m in catalog.json()["data"]]:
                raise ValueError("Model is absent from current catalog")
            session_ids = set()
            started = time.perf_counter()
            for offset in range(0, args.count, args.stage):
                # Finish already-in-flight requests; never cancel/replay them on another failure.
                rows = await asyncio.gather(
                    *(
                        measure(client, args, i, session_ids)
                        for i in range(offset, min(args.count, offset + args.stage))
                    )
                )
                report["results"].extend(rows)
                if not all(row["passed"] for row in rows):
                    break
            report["passed"] = len(report["results"]) == args.count and all(
                r["passed"] for r in report["results"]
            )
    except (httpx.HTTPError, ValueError, KeyError):
        report["error"] = "stage_failed_or_configuration_mismatch"
    finally:
        report["elapsed_seconds"] = time.perf_counter() - started
        report["summary"] = summarize(report["results"], report["elapsed_seconds"])
        if previous and report["passed"]:
            prior = previous.get("summary", {}).get("successful_requests_per_minute", 0)
            current = report["summary"]["successful_requests_per_minute"]
            report["throughput_ratio"] = current / prior if prior else None
            report["recommendation"] = "candidate_only_no_config_change"
            if args.stage > previous["stage"] and (not prior or current < prior * 1.10):
                report["recommendation"] = "keep_previous_concurrency_no_clear_gain"
        with (args.output_dir / "benchmark.json").open("x", encoding="utf-8") as file:
            json.dump(report, file, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8767")
    parser.add_argument("--stage", type=int, choices=[1, 2, 4], default=1)
    parser.add_argument("--interval", type=int, choices=[20, 10, 5], default=20)
    parser.add_argument("--count", type=int, choices=range(1, 13), default=12)
    parser.add_argument("--model", required=True)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    raise SystemExit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
