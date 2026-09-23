"""Browser adapter for the repository-managed local Steel executor."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .paths import COOKIES_FILE


class SteelBrowserError(RuntimeError):
    """The local Steel executor could not create or resume a browser session."""


class SteelBrowser:
    """Execute persistent browser JavaScript through the local Steel executor."""

    def __init__(
        self,
        executor_url: str,
        token: str,
        sid: str,
        *,
        request_timeout: float = 130.0,
    ) -> None:
        self.executor_url = executor_url.rstrip("/")
        self.token = token
        self.sid = sid
        self.request_timeout = request_timeout

    @classmethod
    def open(
        cls,
        executor_url: str,
        token: str,
        sid: str | None = None,
        *,
        request_timeout: float = 130.0,
    ) -> SteelBrowser:
        browser = cls(
            executor_url,
            token,
            sid or "pending",
            request_timeout=request_timeout,
        )
        payload: dict[str, str] = {}
        if sid:
            payload["sessionId"] = sid
        result = browser._request("/session/open", payload, timeout=request_timeout)
        opened_sid = result.get("sessionId")
        if not isinstance(opened_sid, str) or not opened_sid:
            raise SteelBrowserError("Steel executor returned no session id")
        browser.sid = opened_sid
        return browser

    def _request(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        body = json.dumps(payload or {}).encode("utf-8")
        request = Request(
            f"{self.executor_url}{path}",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=timeout or self.request_timeout) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise SteelBrowserError(
                f"Steel executor HTTP {exc.code}: {detail[:500]}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise SteelBrowserError(f"Steel executor unavailable: {exc}") from exc
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SteelBrowserError("Steel executor returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise SteelBrowserError("Steel executor returned a non-object response")
        return result

    def js(self, code: str, timeout: int = 120, retries: int = 6) -> str:
        for attempt in range(max(1, retries)):
            try:
                result = self._request(
                    "/execute",
                    {"sessionId": self.sid, "code": code, "timeout": timeout},
                    timeout=max(self.request_timeout, timeout + 10),
                )
            except SteelBrowserError as exc:
                if attempt < retries - 1:
                    time.sleep(min(1.0 + attempt, 3.0))
                    continue
                return f"EXEC_ERR {exc}"
            error = result.get("error")
            if error:
                return f"ERROR {error}"
            value = result.get("result", "")
            return value.strip() if isinstance(value, str) else str(value)
        return "EXEC_ERR Steel executor request failed"

    def js_json(
        self, code: str, timeout: int = 120, retries: int = 6
    ) -> dict | list | None:
        raw = self.js(code, timeout=timeout, retries=retries)
        if not raw or raw.startswith(("ERROR", "EXEC_ERR", "RATE_LIMIT")):
            return None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, (dict, list)) else None

    def url(self) -> str:
        return self.js("await page.url()")

    def is_alive(self) -> bool:
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
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.cookies(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    def page_scan(self) -> dict:
        data = self.js_json("""
          var __h1 = await page.content();
          var __sc = [...__h1.matchAll(/<script[^>]+src=["']([^"']+)["']/g)].map(m=>m[1]);
          var __ln = [...__h1.matchAll(/<link[^>]+href=["']([^"']+)["']/g)].map(m=>m[1]);
          JSON.stringify({url: await page.url(), title: await page.title(),
            htmlLen: __h1.length, scripts:[...new Set(__sc)], links:[...new Set(__ln)]})
        """)
        return data if isinstance(data, dict) else {}

    def close(self) -> None:
        try:
            self._request("/session/release", {"sessionId": self.sid}, timeout=30)
        except SteelBrowserError:
            return
