"""User-started, single-process experiment on 127.0.0.1:8768 only."""

import argparse
import asyncio
import os
from dataclasses import replace
from pathlib import Path

import uvicorn

from ..paths import BASE_DIR
from ..v2.__main__ import instance_lock
from ..v2.config import Settings
from ..v2.transport import TransportError, build_transport
from .app import create_app
from .protocol import MODEL
from .runtime import TextTransport


async def serve(settings):
    browser = await build_transport("playwright", settings.data_dir, settings)
    try:
        await browser.authenticate()
        catalog = await browser.request("/config/?quota_pool=Students%20Pool")
        if MODEL not in catalog.get("body", catalog).get("availableModels", []):
            raise TransportError("model_unavailable")
        app = create_app(settings, TextTransport(browser))
        await uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=8768,
                workers=1,
                access_log=False,
            )
        ).serve()
    finally:
        await browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth-dir", type=Path, required=True)
    parser.add_argument("--acknowledge-upstream-tools", action="store_true")
    args = parser.parse_args()
    if not args.acknowledge_upstream_tools:
        parser.error(
            "Campus may execute built-in web tools despite tool_proxy=false. Use synthetic, non-sensitive data and explicitly acknowledge this risk."
        )
    original = Settings.load()
    if len(original.api_key) < 24:
        parser.error("Use an API key of at least 24 characters in .env.v2")
    directory = (BASE_DIR / ".web2api-v2" / "kilo-bridge").resolve()
    baseline = original.data_dir.resolve()
    if (
        baseline == directory
        or directory in baseline.parents
        or baseline in directory.parents
    ):
        parser.error("Kilo data directory overlaps configured v2 runtime")
    settings = replace(
        original,
        data_dir=directory,
        auth_dir=args.auth_dir.resolve(strict=True),
        host="127.0.0.1",
        port=8768,
        backend="playwright",
        model=MODEL,
        concurrency=1,
        interval=20.0,
        stream_mode="incremental",
        stream_validation=None,
    )
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BASE_DIR / ".playwright-browsers")
    print(
        "Experimental: buffered validated actions; campus tools not controllable. No local tool execution."
    )
    try:
        with instance_lock(directory):
            asyncio.run(serve(settings))
    except TransportError as exc:
        print("Startup failed:", exc.code)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
