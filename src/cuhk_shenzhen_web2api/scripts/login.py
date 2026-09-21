"""Task: bring a browser session onto the AI chat app and persist it.

Creates a new Firecrawl browser session (or resumes one with --resume), runs
the SSO login flow if needed, and saves session-id + cookies under data/.

The browser backend (Firecrawl Cloud or a locally deployed instance) comes from
`FIRECRAWL_MODE` in .env; see `firecrawl_provider`.

Usage:
    python src/cuhk_shenzhen_web2api/scripts/login.py [--resume SID] [--no-save]
"""

from __future__ import annotations

import argparse
import sys

from cuhk_shenzhen_web2api import cloud_browser, env, firecrawl_provider, login
from cuhk_shenzhen_web2api.paths import CHAT_URL, COOKIES_FILE, SESSION_ID_FILE


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--resume", default=None, help="Resume existing Firecrawl browser sid"
    )
    parser.add_argument("--url", default=CHAT_URL, help="Login target URL")
    parser.add_argument(
        "--no-save", action="store_true", help="Do not persist session id/cookies"
    )
    args = parser.parse_args()

    config = env.load_env()
    username = env.chat_username(config)
    password = env.chat_password(config)

    try:
        settings = firecrawl_provider.resolve_settings(config)
        print(f"backend   {settings.mode} ({settings.api_url})", flush=True)
        _app, cb = firecrawl_provider.open_browser_session(
            settings, args.resume or cloud_browser.load_session_id()
        )
    except firecrawl_provider.ConfigurationError as exc:
        print(f"firecrawl backend unusable: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print("session ", cb.sid, flush=True)

    final_url = login.ensure_on_chat(cb, username, password, url=args.url)
    print("final   ", final_url, flush=True)
    if (
        final_url.startswith(("ERROR", "EXEC_ERR", "RATE_LIMIT"))
        or "/chat" not in final_url
    ):
        print("login did not land on /chat/", file=sys.stderr)
        raise SystemExit(1)

    if args.no_save:
        return
    cloud_browser.save_session_id(cb.sid, SESSION_ID_FILE)
    cb.save_cookies(COOKIES_FILE)
    scan = cb.page_scan()
    print(f"saved   {cb.sid} -> {SESSION_ID_FILE.name}")
    print(f"cookies -> {COOKIES_FILE.name} ({len(cb.cookies())})")
    print(
        f"html    {scan.get('htmlLen', 0)} bytes | scripts {len(scan.get('scripts', []))}"
    )


if __name__ == "__main__":
    main()
