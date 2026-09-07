"""Thin wrapper around Firecrawl's cloud browser / browser_execute.

The sandbox runs Node with a persistent `page` handle for the lifetime of the
session. Two constraints discovered empirically:

* Only the LAST expression's value is returned (console.log output is dropped).
* Top-level `const`/`let`/`function` bindings persist across calls in the same
  session, so emitted JS must use `var` or plain assignment only.
* `window`/`document` do not exist at the Node level; page logic goes through
  `page.evaluate(...)`.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from firecrawl import Firecrawl

from .paths import COOKIES_FILE, SESSION_ID_FILE

RATE_SLEEP_SECONDS = 3.5  # free tier is ~3 browser-execute req/min


class CloudBrowser:
    def __init__(
        self, app: Firecrawl, sid: str, rate_sleep: float = RATE_SLEEP_SECONDS
    ):
        self.app = app
        self.sid = sid
        self.rate_sleep = rate_sleep

    # ---- low-level ----

    def js(self, code: str, timeout: int = 120, retries: int = 6) -> str:
        """Run node code against the page; return the last-expression value.

        On error returns a string starting with ``ERROR `` / ``EXEC_ERR ``.
        """
        for attempt in range(retries):
            time.sleep(self.rate_sleep)
            try:
                res = self.app.browser_execute(
                    self.sid, code, language="node", timeout=timeout
                )
            except Exception as exc:  # noqa: BLE001 - surface retryable API errors
                if "Rate" in str(exc) and attempt < retries - 1:
                    time.sleep(5)
                    continue
                return f"EXEC_ERR {exc}"
            if getattr(res, "error", None):
                return f"ERROR {res.error}"
            return (res.result or res.stdout or "").strip()
        return "RATE_LIMIT"

    def js_json(
        self, code: str, timeout: int = 120, retries: int = 6
    ) -> dict | list | None:
        """Run node code expected to return a JSON value."""
        raw = self.js(code, timeout=timeout, retries=retries)
        if not raw or raw.startswith(("ERROR", "EXEC_ERR", "RATE_LIMIT")):
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    # ---- page helpers ----

    def url(self) -> str:
        return self.js("await page.url()")

    def cookies(self) -> list[dict]:
        data = self.js_json("""
          var __co = await page.context().cookies();
          JSON.stringify(__co.map(x=>({name:x.name, domain:x.domain, value:x.value,
            path:x.path, httpOnly:x.httpOnly, secure:x.secure})))
        """)
        return data or []

    def save_cookies(self, path: Path = COOKIES_FILE) -> Path:
        path.write_text(
            json.dumps(self.cookies(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    def page_scan(self) -> dict:
        """Current URL + title + HTML length + script/link inventory."""
        data = self.js_json("""
          var __h1 = await page.content();
          var __sc = [...__h1.matchAll(/<script[^>]+src=["']([^"']+)["']/g)].map(m=>m[1]);
          var __ln = [...__h1.matchAll(/<link[^>]+href=["']([^"']+)["']/g)].map(m=>m[1]);
          JSON.stringify({url: await page.url(), title: await page.title(),
            htmlLen: __h1.length, scripts:[...new Set(__sc)], links:[...new Set(__ln)]})
        """)
        return data or {}

    # ---- session lifecycle ----

    def close(self) -> None:
        try:
            self.app.delete_browser(self.sid)
        except Exception:  # noqa: BLE001 - non-fatal on close
            pass


def create_session(
    app: Firecrawl, ttl: int = 1800, activity_ttl: int = 900
) -> CloudBrowser:
    session = app.browser(ttl=ttl, activity_ttl=activity_ttl)
    return CloudBrowser(app, session.id)


def resume_session(app: Firecrawl, sid: str) -> CloudBrowser:
    return CloudBrowser(app, sid)


def load_session_id(path: Path = SESSION_ID_FILE) -> str | None:
    if not path.exists():
        return None
    sid = path.read_text(encoding="utf-8").strip()
    return sid or None


def save_session_id(sid: str, path: Path = SESSION_ID_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sid, encoding="utf-8")
