"""Scrape https://ai.cuhk.edu.cn/chat/ via the Firecrawl Python SDK.

Reverse-engineering step 1: pull the page HTML/markdown/links and inspect
whatever renders (the ChatUI app or the aTrust/SDP verification gateway).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from firecrawl import FirecrawlApp
from firecrawl.v2.types import ScreenshotFormat

BASE_URL = "https://api.firecrawl.dev"
TARGET = "https://ai.cuhk.edu.cn/chat/"

OUTPUT_DIR = Path(__file__).resolve().parents[3] / "data" / "chat"


def load_env() -> dict[str, str]:
    env_path = Path(__file__).resolve().parents[3] / ".env"
    env: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    for key in ("FIRECRAWL_API_KEY", "CHAT_USERNAME", "CHAT_PASSWORD", "CHAT_COOKIE"):
        if key not in env and os.environ.get(key):
            env[key] = os.environ[key]
    return env


def load_env_key() -> str:
    return load_env().get("FIRECRAWL_API_KEY", "")


def run_scrape(
    api_key: str,
    url: str,
    headers: dict[str, str] | None = None,
    wait_for: int = 5000,
) -> dict:
    app = FirecrawlApp(api_key=api_key, api_url=BASE_URL)
    result = app.scrape(
        url,
        formats=[
            "markdown",
            "html",
            "links",
            "rawHtml",
            ScreenshotFormat(type="screenshot", full_page=True),
        ],
        headers=headers,
        only_main_content=False,
        timeout=45000,
        store_in_cache=False,
        # Give the SPA time to boot (and possibly the aTrust check to resolve).
        wait_for=wait_for,
    )
    return result if isinstance(result, dict) else result.model_dump(mode="json")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Crawl ai.cuhk.edu.cn chat page with Firecrawl."
    )
    parser.add_argument("--url", default=TARGET, help="URL to scrape")
    parser.add_argument("--out", type=Path, default=OUTPUT_DIR, help="Output directory")
    parser.add_argument("--raw", action="store_true", help="Print raw JSON to stdout")
    parser.add_argument(
        "--cookie",
        metavar="COOKIE",
        help="Authenticated session cookie(s), e.g. 'name1=val1; name2=val2'",
    )
    parser.add_argument(
        "--wait-for",
        type=int,
        default=5000,
        help="Milliseconds to wait for the SPA to boot",
    )
    args = parser.parse_args()

    api_key = load_env_key()
    if not api_key:
        print("FIRECRAWL_API_KEY not found in .env or environment.", file=sys.stderr)
        sys.exit(1)

    cookie = args.cookie or load_env().get("CHAT_COOKIE")
    if not cookie:
        print(
            "No auth: pass --cookie or set CHAT_COOKIE / CHAT_USERNAME+CHAT_PASSWORD in .env.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Scraping {args.url} ...")
    headers = {"Cookie": cookie}
    doc = run_scrape(api_key, args.url, headers=headers, wait_for=args.wait_for)

    if args.raw:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return

    out_dir = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    base = out_dir / f"chat_{ts}"

    meta = {
        "url": args.url,
        "metadata": doc.get("metadata"),
        "warnings": doc.get("warnings"),
        "error": doc.get("error"),
    }
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if doc.get("markdown"):
        (base.with_suffix(".md")).write_text(doc["markdown"], encoding="utf-8")
        print(f"[markdown] {len(doc['markdown'])} chars -> {base}.md")
    if doc.get("html"):
        (base.with_name(base.stem + ".html")).write_text(doc["html"], encoding="utf-8")
        print(f"[html]     {len(doc['html'])} chars -> {base}.html")
    if doc.get("rawHtml"):
        (base.with_name(base.stem + ".raw.html")).write_text(
            doc["rawHtml"], encoding="utf-8"
        )
        print(f"[rawHtml]  {len(doc['rawHtml'])} chars -> {base}.raw.html")
    if doc.get("links"):
        (base.with_name(base.stem + ".links.json")).write_text(
            json.dumps(doc["links"], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[links]    {len(doc['links'])} links -> {base}.links.json")
    if doc.get("screenshot"):
        b64 = doc["screenshot"]
        if isinstance(b64, dict):
            b64 = b64.get("base64") or b64.get("url") or ""
        if b64.startswith("data:"):
            _, b64 = b64.split(",", 1)
        if b64:
            (base.with_name(base.stem + ".png")).write_bytes(b64.encode("utf-8"))
            print(f"[shot]     {len(b64)} b64 chars -> {base}.png")

    print("Saved metadata ->", out_dir / "meta.json")


if __name__ == "__main__":
    main()
