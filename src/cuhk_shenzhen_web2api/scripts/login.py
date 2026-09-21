"""Task: bring a cloud browser session onto the AI chat app and persist it.

Creates a new Firecrawl browser session (or resumes one with --resume), runs
the SSO login flow if needed, and saves session-id + cookies under data/.

Usage:
    python src/cuhk_shenzhen_web2api/scripts/login.py [--resume SID] [--no-save]
"""

from __future__ import annotations

import argparse
import sys

from firecrawl import Firecrawl

from cuhk_shenzhen_web2api import cloud_browser, env, login
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
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Skip filling USERNAME/PASSWORD; wait for SSO in the live view",
    )
    args = parser.parse_args()

    config = env.load_env()
    api_key = env.firecrawl_api_key(config)
    if not api_key:
        print("FIRECRAWL_API_KEY missing in .env", file=sys.stderr)
        sys.exit(1)
    username = env.chat_username(config)
    password = env.chat_password(config)
    manual = args.manual or not (username and password)

    app = Firecrawl(api_key=api_key)
    sid = args.resume or cloud_browser.load_session_id()
    cb = cloud_browser.get_or_create_session(app, sid)
    print("session ", cb.sid, flush=True)
    if cb.live_url():
        print("live    ", cb.live_url(), flush=True)

    final_url = login.ensure_on_chat(
        cb, username, password, url=args.url, manual=manual
    )
    print("final   ", final_url, flush=True)
    if final_url.startswith(
        ("ERROR", "EXEC_ERR", "RATE_LIMIT")
    ) or not login.looks_on_chat(final_url):
        print("login did not land on authenticated /chat/", file=sys.stderr)
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
