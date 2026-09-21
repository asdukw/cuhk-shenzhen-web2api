"""Programmatic SSO login to the CUHK-Shenzhen AI chat.

Chain: ai.cuhk.edu.cn/chat -> aTrust SDP -> ADFS SSO -> AI platform OAuth -> /chat/

All page automation runs inside a Firecrawl cloud browser session; the aTrust
session is tied to the cloud run's IP, so cookies are only usable from that
same browser session.
"""

from __future__ import annotations

import contextlib
import json
import time
import webbrowser

from .cloud_browser import CloudBrowser
from .paths import CHAT_URL


def login_flow(
    cb: CloudBrowser,
    username: str,
    password: str,
    url: str = CHAT_URL,
    max_polls: int = 12,
) -> str:
    """Idempotent multi-stage login: brings the page to the real /chat/ app.

    Stages (each tolerant of already-completed state):
      0. goto the chat URL (keeps returnURL through aTrust)
      1. wait for the ADFS form, fill credentials
      2. AI platform login: click Login -> accept disclaimer -> OAuth round trip
    Returns the final page URL (expected https://ai.cuhk.edu.cn/chat/).
    """
    # Stage 0
    cb.js(
        f'await page.goto({json.dumps(url)}, {{waitUntil: "domcontentloaded"}}); '
        f"await page.waitForTimeout(2500); await page.url()"
    )

    # Stage 1: ADFS sign-in (two-step paginated form). No-op if already past it.
    body = (
        f"var __u = {json.dumps(username)}; var __p = {json.dumps(password)}; "
        "var __seen = false;"
    )
    cb.js(
        f"""
      {body}
      try {{
        await page.waitForSelector("#userNameInput", {{timeout: 50000}});
        __seen = true;
        await page.fill("#userNameInput", __u);
        await page.click("#nextButton");
        await page.waitForSelector("#passwordInput", {{timeout: 20000}});
        await page.fill("#passwordInput", __p);
        await page.click("#submitButton");
        await page.waitForTimeout(9000);
      }} catch(e) {{}}
      JSON.stringify({{form: __seen, url: await page.url()}})
    """,
        timeout=150,
    )

    # Stage 2: platform login (window only exists in the page, so page.evaluate)
    cb.js("""
      await page.evaluate(() => {
        const q = (sel) => { const el = document.querySelector(sel); if (el) { el.click(); return true; } return false; };
        q(".login-button");
        setTimeout(() => {
          q("#notice-accept");
          q("#browser-warning-dismiss");
          try { if (window.jumpToAI) window.jumpToAI(); } catch(e) {}
        }, 1200);
      });
      await page.waitForTimeout(2500);
      await page.url()
    """)

    # Poll until we land on /chat/
    for _ in range(max_polls):
        cur = cb.url()
        if "/chat" in cur:
            return cur
        if cur.startswith(("ERROR", "EXEC_ERR", "RATE_LIMIT")):
            break
        time.sleep(4)
    return cb.url()


def looks_on_chat(url: str) -> bool:
    """Cheap URL check: on the chat app, not an SSO / aTrust gate."""
    if not url or "/chat" not in url:
        return False
    lowered = url.lower()
    return not any(token in lowered for token in ("adfs", "atrust", "login.microsoft"))


def is_authenticated(cb: CloudBrowser, cur: str | None = None) -> bool:
    """True when the cloud page is on /chat/ and /.auth/me/ succeeds."""
    cur = cb.url() if cur is None else cur
    if not looks_on_chat(cur):
        return False
    data = cb.js_json("""
      var __r = await page.evaluate(async () => {
        try {
          const r = await fetch("/.auth/me/", {redirect: "manual"});
          const t = await r.text();
          let body = null;
          try { body = JSON.parse(t); } catch (e) {}
          return {status: r.status, body: body};
        } catch (e) {
          return {status: 0, error: String(e)};
        }
      });
      JSON.stringify(__r)
    """)
    if not isinstance(data, dict):
        return False
    return data.get("status") == 200


def wait_for_manual_login(
    cb: CloudBrowser,
    url: str = CHAT_URL,
    max_polls: int = 40,
) -> str:
    """Open the chat URL and wait while a human finishes SSO in the live view."""
    landed = cb.js(
        f'await page.goto({json.dumps(url)}, {{waitUntil: "domcontentloaded"}}); '
        f"await page.waitForTimeout(2000); await page.url()"
    )
    live = cb.live_url()
    print("=" * 60, flush=True)
    print("Please log in manually at https://ai.cuhk.edu.cn/chat/", flush=True)
    if live:
        print("Open this cloud-browser live view and complete SSO there:", flush=True)
        print(live, flush=True)
        with contextlib.suppress(Exception):
            webbrowser.open(live)
    else:
        print(
            "No live-view URL yet; keep this process running and retry after the "
            "first browser call returns a live view.",
            flush=True,
        )
    print("Waiting until the session lands on the logged-in chat app...", flush=True)
    print("=" * 60, flush=True)

    if is_authenticated(cb, landed):
        return landed

    for i in range(max_polls):
        cur = cb.url()
        live = cb.live_url() or live
        print(f"[login {i + 1}/{max_polls}] {cur}", flush=True)
        if live:
            print(f"live view: {live}", flush=True)
        if is_authenticated(cb, cur):
            return cur
        time.sleep(6)
    return cb.url()


def ensure_on_chat(
    cb: CloudBrowser,
    username: str = "",
    password: str = "",
    url: str = CHAT_URL,
    *,
    manual: bool | None = None,
) -> str:
    """If the page is not on the chat app yet, run SSO or wait for manual login."""
    cur = cb.url()
    if looks_on_chat(cur) and is_authenticated(cb, cur):
        return cur
    if manual is None:
        manual = not (username and password)
    if manual:
        return wait_for_manual_login(cb, url=url)
    final = login_flow(cb, username, password, url=url)
    # Prefer URL landing over a second /.auth/me probe: after SSO the free
    # tier is often rate-limited, and a transient probe miss must not restart
    # the whole flow as manual login.
    if looks_on_chat(final):
        return final
    print("automatic SSO did not finish — switching to manual login", flush=True)
    return wait_for_manual_login(cb, url=url)
