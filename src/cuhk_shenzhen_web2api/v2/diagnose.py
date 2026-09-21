"""Manual login diagnostic. All saved material goes into a NEW directory.

python -m cuhk_shenzhen_web2api.v2.diagnose --data-dir NEW --backend httpx
Add --chat (and optionally --model) to submit two short diagnostic turns.
No credential entry automation, installation, legacy state, or background jobs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from urllib.parse import urlencode

from .transport import (
    AUTH_PATH,
    CHAT_PATH,
    ORIGIN,
    HTTPXTransport,
    PlaywrightTransport,
    Transport,
    TransportError,
)


def _write_new(path: Path, body: dict) -> None:
    # Exclusive creation also protects against a concurrent writer/symlink.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(body, handle, ensure_ascii=False, indent=2)


async def _check(transport: Transport) -> tuple[dict, dict | None]:
    try:
        identity = await transport.authenticate()
        return {
            "ok": True,
            "state": transport.state,
            "streaming": transport.streaming,
        }, identity
    except TransportError as exc:
        return {
            "ok": False,
            "error": exc.as_dict(),
            "streaming": transport.streaming,
        }, None


async def _context_check(context) -> tuple[dict, dict | None]:
    validator = PlaywrightTransport(None)
    response = None
    try:
        response = await context.request.get(
            ORIGIN + AUTH_PATH, max_redirects=0, timeout=30000
        )
        validator._status(response.status, response.headers.get("retry-after"))
        body = validator._json(
            AUTH_PATH,
            await response.text(),
            response.headers.get("content-type", ""),
            response.status,
        )
        return {"ok": True}, body
    except TransportError as exc:
        return {"ok": False, "error": exc.as_dict()}, None
    except Exception:  # noqa: BLE001 - never expose browser credentials in errors
        return {"ok": False, "error": {"code": "context_request_error"}}, None
    finally:
        if response is not None:
            await response.dispose()


async def _chat_check(
    transport: Transport, model: str, inspect_history: bool = False
) -> dict:
    payload = {
        "project_id": None,
        "chat_session_id": None,
        "approach_id": model,
        "params": {"tool_proxy": False},
        "content": "Reply only: OK",
        "parent_idx": -1,
        "image_ids": [],
        "file_ids": [],
        "quota_pool": "Students Pool",
    }
    start = None
    finished = False
    first_text_at = None
    finished_at = None
    if getattr(transport, "stream_mode", "buffered") == "incremental":
        payload["content"] = "Write the integers 1 through 80, one per line."
    async for event in transport.events(payload):
        if event["event"] == "start":
            start = event
        if event["event"] == "end":
            finished = event.get("status") == "finished"
            finished_at = time.perf_counter()
        if (
            event["event"] == "msg"
            and event.get("item", {}).get("type") == "text"
            and first_text_at is None
        ):
            first_text_at = time.perf_counter()
    if (
        not finished
        or not start
        or not isinstance(start.get("chat_session_id"), str)
        or not isinstance(start.get("approach_msg_idx"), int)
    ):
        raise TransportError("chat_validation_failed")
    payload.update(
        {
            "chat_session_id": start["chat_session_id"],
            "parent_idx": start["approach_msg_idx"],
            "content": "Reply only: OK again",
        }
    )
    continued = False
    last_start = None
    async for event in transport.events(payload):
        if event["event"] == "start":
            last_start = event
        if event["event"] == "end":
            continued = event.get("status") == "finished"
    if not continued:
        raise TransportError("continuation_validation_failed")
    result = {
        "ok": True,
        "turns": 2,
        "continuation": True,
        "first_text_before_end": first_text_at is not None
        and finished_at is not None
        and finished_at - first_text_at >= 0.05,
    }
    if inspect_history and last_start:
        from .recovery import history_evidence

        history = await transport.request(
            "/getHistoryItem/", {"chat_session_id": last_start["chat_session_id"]}
        )
        confirmed, reason = history_evidence({"result": last_start}, history)
        result["history_adapter"] = {
            "verified": confirmed is not None,
            "reason": reason,
            "top_level_keys": sorted(history.keys()),
        }
    return result


async def diagnose(args: argparse.Namespace) -> int:
    # Refuse an existing directory, including an empty one, before any browser work.
    root = Path(args.data_dir).expanduser().resolve()
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
    except FileExistsError:
        print("Refusing existing --data-dir; choose a NEW directory.")
        return 2
    report: dict = {
        "version": 2,
        "requested_backend": args.backend,
        "selected_backend": None,
        "chat_opt_in": args.chat,
        "checks": {},
    }
    browser = None
    runtime = None
    http = None
    try:
        from playwright.async_api import async_playwright

        runtime = await async_playwright().start()
        browser = await runtime.chromium.launch(headless=False, args=["--disable-gpu"])
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(
            ORIGIN + CHAT_PATH, wait_until="domcontentloaded", timeout=60000
        )
        print(
            "Complete login manually in the browser. Authentication files will be saved only in the NEW directory."
        )
        await asyncio.to_thread(
            input, "When the chat page is ready, press Enter here: "
        )
        browser_transport = PlaywrightTransport(
            page, stream_mode=getattr(args, "stream_mode", "buffered")
        )
        browser_check, browser_identity = await _check(browser_transport)
        report["checks"]["browser"] = browser_check
        context_check, context_identity = await _context_check(context)
        context_check["identity_matches_browser"] = bool(
            browser_identity and context_identity == browser_identity
        )
        report["checks"]["context_request"] = context_check
        state = dict(await context.storage_state())
        http = HTTPXTransport(state)
        http_check, http_identity = await _check(http)
        http_check["identity_matches_browser"] = bool(
            browser_identity and http_identity == browser_identity
        )
        report["checks"]["httpx"] = http_check
        # Never save identity bodies or login URLs in the report. Storage state is
        # intentionally secret and is the only artifact containing auth material.
        if browser_identity:
            _write_new(root / "storage_state.json", state)
        selected = http if args.backend == "httpx" else browser_transport
        validated = bool(browser_identity) and (
            args.backend == "playwright" or http_check["identity_matches_browser"]
        )
        if not validated:
            raise TransportError(
                "requested_backend_not_validated", known_not_submitted=True
            )
        config = await selected.request(
            "/config/?" + urlencode({"quota_pool": "Students Pool"})
        )
        models = config.get("availableModels")
        if (
            not isinstance(models, list)
            or not models
            or not all(isinstance(model, str) and model for model in models)
        ):
            raise TransportError("invalid_model_catalog", known_not_submitted=True)
        report["checks"]["models"] = {"ok": True, "count": len(models)}
        if args.model and args.model not in models:
            raise TransportError("model_unavailable", known_not_submitted=True)
        if args.chat:
            report["checks"]["chat"] = await _chat_check(
                selected,
                args.model or models[0],
                getattr(args, "inspect_history", False),
            )
        report["stream_protocol"] = "browser-ndjson-v1"
        report["stream_verified"] = (
            args.backend == "playwright"
            and getattr(args, "stream_mode", "buffered") == "incremental"
            and report["checks"].get("chat", {}).get("first_text_before_end", False)
        )
        report["selected_backend"] = selected.backend
        report["streaming"] = selected.streaming
        report["validation_scope"] = (
            "identity_models_chat_continuation" if args.chat else "identity_models_only"
        )
    except TransportError as exc:
        report["error"] = exc.as_dict()
    except ImportError:
        report["error"] = {"code": "optional_dependency_missing"}
    except (Exception, KeyboardInterrupt):  # noqa: BLE001 - sanitize all CLI failures
        # Browser/SDK errors can contain SSO URLs, tokens, identity or HTML.
        report["error"] = {"code": "diagnostic_failed"}
    finally:
        try:
            if http is not None:
                await http.close()
            if browser is not None:
                await browser.close()
            if runtime is not None:
                await runtime.stop()
        except Exception:  # noqa: BLE001 - cleanup errors can contain auth URLs
            report["cleanup_error"] = "browser_cleanup_failed"
        _write_new(root / "report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["selected_backend"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir", required=True, help="NEW directory; existing paths are rejected"
    )
    parser.add_argument("--backend", choices=("httpx", "playwright"), required=True)
    parser.add_argument("--model")
    parser.add_argument(
        "--stream-mode", choices=["buffered", "incremental"], default="buffered"
    )
    parser.add_argument(
        "--inspect-history",
        action="store_true",
        help="Verify strict history completion schema after diagnostic chat",
    )
    parser.add_argument(
        "--chat", action="store_true", help="Opt in to two short upstream chat turns"
    )
    return asyncio.run(diagnose(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
