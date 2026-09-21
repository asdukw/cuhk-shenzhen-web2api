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
FIRECRAWL_MAX_TTL_SECONDS = 3600


class CloudBrowser:
    def __init__(
        self,
        app: Firecrawl,
        sid: str,
        rate_sleep: float = RATE_SLEEP_SECONDS,
        live_view_url: str | None = None,
        interactive_live_view_url: str | None = None,
    ):
        self.app = app
        self.sid = sid
        self.rate_sleep = rate_sleep
        self.live_view_url = live_view_url
        self.interactive_live_view_url = interactive_live_view_url

    def live_url(self) -> str:
        """Prefer the click-and-type live view so a human can finish SSO."""
        return self.interactive_live_view_url or self.live_view_url or ""

    def _capture_live_urls(self, res: object) -> None:
        interactive = getattr(res, "interactive_live_view_url", None)
        view = getattr(res, "live_view_url", None)
        if interactive:
            self.interactive_live_view_url = interactive
        if view:
            self.live_view_url = view

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
            self._capture_live_urls(res)
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

    def is_alive(self) -> bool:
        """Cheap liveness probe: true unless the session is gone/errored."""
        value = self.js("await page.url()", timeout=60, retries=2)
        return not value.startswith(("EXEC_ERR", "ERROR", "RATE_LIMIT"))

    def cookies(self) -> list[dict]:
        data = self.js_json("""
          var __co = await page.context().cookies();
          JSON.stringify(__co.map(x=>({name:x.name, domain:x.domain, value:x.value,
            path:x.path, httpOnly:x.httpOnly, secure:x.secure})))
        """)
        return data if isinstance(data, list) else []

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
        return data if isinstance(data, dict) else {}

    # ---- session lifecycle ----

    def close(self) -> None:
        try:
            self.app.delete_browser(self.sid)
        except Exception:  # noqa: BLE001 - non-fatal on close
            return


def create_session(
    app: Firecrawl, ttl: int = 1800, activity_ttl: int = 900
) -> CloudBrowser:
    # Firecrawl rejects browser sessions whose TTL exceeds one hour.
    ttl = max(1, min(ttl, FIRECRAWL_MAX_TTL_SECONDS))
    activity_ttl = max(1, min(activity_ttl, ttl))
    session = app.browser(ttl=ttl, activity_ttl=activity_ttl, stream_web_view=True)
    sid = session.id
    if sid is None:
        raise RuntimeError("browser session created without an id")
    return CloudBrowser(
        app,
        sid,
        live_view_url=getattr(session, "live_view_url", None),
        interactive_live_view_url=getattr(session, "interactive_live_view_url", None),
    )


def resume_session(app: Firecrawl, sid: str) -> CloudBrowser:
    cb = CloudBrowser(app, sid)
    _attach_live_urls(cb)
    return cb


def _attach_live_urls(cb: CloudBrowser) -> None:
    """Fill live-view URLs for a resumed session from list_browsers if possible."""
    list_fn = getattr(cb.app, "list_browsers", None)
    if list_fn is None:
        return
    try:
        listing = list_fn()
    except Exception:  # noqa: BLE001 - live view is optional
        return
    sessions = getattr(listing, "sessions", None) or []
    for session in sessions:
        if getattr(session, "id", None) == cb.sid:
            cb.live_view_url = getattr(session, "live_view_url", None)
            cb.interactive_live_view_url = getattr(
                session, "interactive_live_view_url", None
            )
            return


def get_or_create_session(
    app: Firecrawl,
    sid: str | None = None,
    ttl: int = 14400,
    activity_ttl: int = 3600,
) -> CloudBrowser:
    """Resume `sid` if it is still alive, otherwise create a fresh session.

    One liveness probe call is consumed in the resumed case.
    """
    if sid:
        cb = resume_session(app, sid)
        if cb.is_alive():
            return cb
        print(f"session {sid} is expired/destroyed — creating a new one", flush=True)
        cb.close()
    return create_session(app, ttl=ttl, activity_ttl=activity_ttl)


def load_session_id(path: Path = SESSION_ID_FILE) -> str | None:
    if not path.exists():
        return None
    sid = path.read_text(encoding="utf-8").strip()
    return sid or None


def save_session_id(sid: str, path: Path = SESSION_ID_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sid, encoding="utf-8")
