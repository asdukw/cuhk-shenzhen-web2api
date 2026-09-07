"""Programmatic SSO login to the CUHK-Shenzhen AI chat.

Chain: ai.cuhk.edu.cn/chat -> aTrust SDP -> ADFS SSO -> AI platform OAuth -> /chat/

All page automation runs inside a Firecrawl cloud browser session; the aTrust
session is tied to the cloud run's IP, so cookies are only usable from that
same browser session.
"""

from __future__ import annotations

import json
import time

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


def ensure_on_chat(
    cb: CloudBrowser, username: str, password: str, url: str = CHAT_URL
) -> str:
    """If the page is not on the chat app yet, run the login flow."""
    cur = cb.url()
    if cur and "/chat" in cur:
        return cur
    return login_flow(cb, username, password, url=url)
