"""Manage the native Node Steel stack without Docker."""

from __future__ import annotations

import argparse
import json
import sys

from cuhk_shenzhen_web2api import browser_provider
from cuhk_shenzhen_web2api.local_steel import (
    ensure_steel_source,
    log_paths,
    start_detached,
    status,
    stop_detached,
)
from cuhk_shenzhen_web2api.steel_browser import SteelBrowser, SteelBrowserError


def _setup() -> None:
    source = ensure_steel_source(install=True)
    print(f"steel source ready: {source}")


def _up() -> None:
    stack = start_detached()
    if stack.api is None and stack.executor is None:
        print("local Steel already running")
    else:
        print(json.dumps(status(), indent=2))


def _verify() -> None:
    settings = browser_provider.resolve_settings()
    cb = SteelBrowser.open(
        settings.executor_url,
        settings.executor_token,
        None,
        request_timeout=60,
    )
    code = """
var __steel_verify_page = await context.newPage();
await __steel_verify_page.goto("https://example.com", {waitUntil: "domcontentloaded"});
var __steel_verify_result = JSON.stringify({url: await __steel_verify_page.url(), title: await __steel_verify_page.title()});
await __steel_verify_page.close();
__steel_verify_result
"""
    raw = cb.js(code, timeout=60, retries=1)
    if raw.startswith(("ERROR", "EXEC_ERR", "RATE_LIMIT")):
        raise RuntimeError(f"Steel verification failed: {raw}")
    print(raw)


def _logs() -> None:
    for path in log_paths():
        print(f"=== {path} ===")
        if not path.exists():
            print("(missing)")
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        print("\n".join(lines[-200:]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=("setup", "up", "down", "restart", "status", "verify", "logs"),
        nargs="?",
        default="status",
    )
    args = parser.parse_args()

    try:
        if args.action == "setup":
            _setup()
        elif args.action == "up":
            _up()
        elif args.action == "down":
            stop_detached()
        elif args.action == "restart":
            stop_detached()
            _up()
        elif args.action == "status":
            print(json.dumps(status(), indent=2))
        elif args.action == "verify":
            _verify()
        elif args.action == "logs":
            _logs()
    except (RuntimeError, SteelBrowserError) as exc:
        print(f"steel-node: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
