"""Explicitly authenticated transports; no login, persistence, or retry side effects.

Call ``request('/.auth/me/')`` before other operations. Only HTTPX streams on
the wire. Browser backends return buffered events and advertise that limitation.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

import httpx

ORIGIN = "https://ai.cuhk.edu.cn"
AUTH_PATH = "/.auth/me/"
CHAT_PATH = "/chat/"


class TransportError(RuntimeError):
    """Safe metadata only; never attach server bodies, cookies, or SDK errors."""

    def __init__(
        self,
        code: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
        known_not_submitted: bool = False,
    ):
        super().__init__(code)
        self.code = code
        self.status = status
        self.retry_after = retry_after
        self.known_not_submitted = known_not_submitted
        self.not_submitted = known_not_submitted

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "status": self.status,
            "retry_after": self.retry_after,
            "known_not_submitted": self.known_not_submitted,
        }


class Transport(Protocol):
    state: str
    backend: str
    streaming: bool

    async def request(self, path: str, payload: dict | None = None) -> dict: ...

    def events(self, payload: dict) -> AsyncIterator[dict]: ...

    async def close(self) -> None: ...

    async def authenticate(self) -> dict: ...

    async def probe(self) -> dict: ...


def retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        if value.strip().isdigit():
            return float(value.strip())
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=UTC)
        return max(0.0, (date - datetime.now(UTC)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None


def valid_identity(body: Any) -> bool:
    """Require an identity, not merely a 200 JSON success/error envelope."""
    if isinstance(body, list):
        return bool(body) and all(valid_identity(item) for item in body)
    if not isinstance(body, dict) or not body or body.get("error"):
        return False
    if any(
        body.get(key) is False
        for key in ("authenticated", "is_authenticated", "success")
    ):
        return False
    for key in (
        "id",
        "user_id",
        "userId",
        "username",
        "user_name",
        "email",
        "userDetails",
    ):
        value = body.get(key)
        if (
            isinstance(value, (str, int))
            and not isinstance(value, bool)
            and str(value).strip()
        ):
            return True
    return any(
        valid_identity(body.get(key))
        for key in ("user", "clientPrincipal", "identity", "data")
    )


def cookies_from_storage_state(state: dict) -> httpx.Cookies:
    # Python's default policy otherwise sends host-only cookies to subdomains.
    jar = CookieJar(
        policy=DefaultCookiePolicy(
            strict_ns_domain=DefaultCookiePolicy.DomainStrictNonDomain
        )
    )
    for item in state.get("cookies", []):
        domain = item["domain"]
        expiry = item.get("expires", -1)
        expires = int(expiry) if expiry and expiry > 0 else None
        jar.set_cookie(
            Cookie(
                version=0,
                name=item["name"],
                value=item["value"],
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=domain.startswith("."),
                domain_initial_dot=domain.startswith("."),
                path=item.get("path", "/"),
                path_specified=True,
                secure=bool(item.get("secure", False)),
                expires=expires,
                discard=expires is None,
                comment=None,
                comment_url=None,
                rest={
                    "HttpOnly": item.get("httpOnly", False),
                    "SameSite": item.get("sameSite", "Lax"),
                },
            )
        )
    return httpx.Cookies(jar)


def csrf_for_url(cookies: httpx.Cookies, url: str) -> str | None:
    # Let the jar apply domain/path/secure/expiry rules, including duplicate names.
    probe = httpx.Request("POST", url)
    cookies.set_cookie_header(probe)
    for pair in probe.headers.get("cookie", "").split(";"):
        name, _, value = pair.strip().partition("=")
        if name == "csrftoken":
            return value
    return None


class _Base:
    backend = ""
    streaming = False

    def __init__(self, base_url: str = ORIGIN):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise TransportError("invalid_origin", known_not_submitted=True)
        self.base_url = base_url.rstrip("/")
        self.state = "not_logged_in"

    def _url(self, path: str) -> str:
        decoded = unquote(path)
        if (
            not path.startswith("/")
            or decoded.startswith("//")
            or "\\" in decoded
            or any(ord(c) < 32 for c in decoded)
            or urlsplit(path).fragment
        ):
            raise TransportError("invalid_path", known_not_submitted=True)
        return self.base_url + path

    def _guard(self, path: str) -> str:
        url = self._url(path)
        if self.state == "closed":
            raise TransportError("closed", known_not_submitted=True)
        if path != AUTH_PATH and self.state != "authenticated":
            raise TransportError("not_logged_in", known_not_submitted=True)
        return url

    def _status(self, status: int, retry_after: str | None = None) -> None:
        if status in (401, 403) or 300 <= status < 400 or status == 0:
            self.state = "expired"
            raise TransportError(
                "authentication_required",
                status=status,
                known_not_submitted=status in (401, 403),
            )
        if not 200 <= status < 300:
            raise TransportError(
                "rate_limited" if status == 429 else "http_error",
                status=status,
                retry_after=retry_after_seconds(retry_after),
                known_not_submitted=status == 429,
            )

    def _json(self, path: str, text: str, content_type: str, status: int) -> dict:
        try:
            if "json" not in content_type.lower():
                raise ValueError
            body = json.loads(text)
            if path == AUTH_PATH:
                if not valid_identity(body):
                    raise ValueError
                self.state = "authenticated"
                return body if isinstance(body, dict) else {"identities": body}
            if isinstance(body, list) and path == "/getNextHistoryMeta/":
                return {"body": body}
            if not isinstance(body, dict) or body.get("error"):
                raise ValueError
            return body
        except (ValueError, TypeError):
            if path == AUTH_PATH:
                self.state = "expired"
            raise TransportError(
                "invalid_identity" if path == AUTH_PATH else "invalid_json",
                status=status,
            ) from None

    def _event(self, line: str) -> dict:
        try:
            item = json.loads(line)
            if not isinstance(item, dict) or item.get("event") not in (
                "start",
                "hb",
                "msg",
                "end",
            ):
                raise ValueError
            return item
        except (ValueError, TypeError):
            raise TransportError("invalid_event", status=200) from None

    def _stream_type(self, content_type: str, status: int) -> None:
        if "ndjson" in content_type.lower():
            return
        # A 2xx that answers with a login wall (non-ndjson, e.g. HTML) means the
        # campus session expired mid-stream. Classify it as auth so the engine
        # pauses the queue instead of draining every request into unknown.
        if 200 <= status < 300:
            raise TransportError("authentication_required", status=status)
        raise TransportError("invalid_stream", status=status)

    async def request(self, path: str, payload: dict | None = None) -> dict:
        raise NotImplementedError

    async def authenticate(self) -> dict:
        return await self.request(AUTH_PATH)

    async def probe(self) -> dict:
        return await self.authenticate()


class HTTPXTransport(_Base):
    backend = "httpx"
    streaming = True

    def __init__(
        self,
        storage_state: dict | str | Path | None = None,
        *,
        base_url: str = ORIGIN,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 120,
    ):
        super().__init__(base_url)
        if isinstance(storage_state, (str, Path)):
            storage_state = json.loads(Path(storage_state).read_text(encoding="utf-8"))
        if storage_state is not None and not isinstance(storage_state, dict):
            raise TransportError("invalid_auth_state", known_not_submitted=True)
        jar = cookies_from_storage_state(storage_state or {}).jar
        self.client = httpx.AsyncClient(
            cookies=jar,
            follow_redirects=False,
            transport=transport,
            timeout=timeout,
            trust_env=False,
        )

    def _headers(self, url: str, payload: dict | None) -> dict:
        probe = httpx.Request("GET", url)
        self.client.cookies.set_cookie_header(probe)
        # HTTPX merges cookies using a new jar with the permissive default
        # policy. Set the header explicitly to preserve host-only semantics.
        headers = {
            "Accept": "application/json",
            "Cookie": probe.headers.get("cookie", ""),
        }
        if payload is not None:
            headers.update(
                {"Origin": self.base_url, "Referer": self.base_url + CHAT_PATH}
            )
            csrf = csrf_for_url(self.client.cookies, url)
            if csrf:
                headers["X-CSRFToken"] = csrf
        return headers

    @staticmethod
    def _network_error(exc: httpx.RequestError) -> TransportError:
        return TransportError(
            "network_error",
            known_not_submitted=isinstance(
                exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
            ),
        )

    async def request(self, path: str, payload: dict | None = None) -> dict:
        url = self._guard(path)
        try:
            response = await self.client.request(
                "GET" if payload is None else "POST",
                url,
                json=payload,
                headers=self._headers(url, payload),
            )
            self._status(response.status_code, response.headers.get("retry-after"))
            return self._json(
                path,
                response.text,
                response.headers.get("content-type", ""),
                response.status_code,
            )
        except httpx.RequestError as exc:
            raise self._network_error(exc) from None

    async def events(self, payload: dict) -> AsyncGenerator[dict, None]:
        url = self._guard(CHAT_PATH)
        headers = self._headers(url, payload)
        headers["Accept"] = "application/x-ndjson+json"
        response_started = False
        try:
            async with self.client.stream(
                "POST", url, json=payload, headers=headers
            ) as response:
                response_started = True
                self._status(response.status_code, response.headers.get("retry-after"))
                self._stream_type(
                    response.headers.get("content-type", ""), response.status_code
                )
                ended = False
                async for line in self._lines(response):
                    if not line.strip():
                        continue
                    event = self._event(line)
                    ended = event["event"] == "end"
                    yield event
                    if ended:
                        break
                if not ended:
                    raise TransportError(
                        "incomplete_stream", status=response.status_code
                    )
        except httpx.RequestError as exc:
            if response_started:
                raise TransportError("network_error") from None
            raise self._network_error(exc) from None

    async def _lines(self, response: httpx.Response):
        pending = b""
        total = 0
        async for block in response.aiter_bytes():
            total += len(block)
            if total > 2 * 1024 * 1024:
                raise TransportError("stream_limit_exceeded")
            pending += block
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                if len(line) > 65536:
                    raise TransportError("stream_limit_exceeded")
                try:
                    yield line.decode("utf-8")
                except UnicodeDecodeError:
                    raise TransportError("invalid_utf8") from None
            if len(pending) > 65536:
                raise TransportError("stream_limit_exceeded")
        if pending:
            try:
                yield pending.decode("utf-8")
            except UnicodeDecodeError:
                raise TransportError("invalid_utf8") from None

    async def close(self) -> None:
        await self.client.aclose()
        self.state = "closed"


# Evaluated in the page, never in Node's global context. Redirects are opaque
# and rejected before body parsing. No arbitrary URL or credential forwarding.
_FETCH = """async ({url, origin, payload, csrf, timeout}) => {
  if (location.origin !== origin || new URL(url).origin !== origin)
    return {status: 0, preflight: true};
  const headers = {Accept: 'application/json, application/x-ndjson+json'};
  if (payload !== null) {
    headers['Content-Type'] = 'application/json';
    if (csrf) headers['X-CSRFToken'] = csrf;
  }
  const r = await fetch(url, {method: payload === null ? 'GET' : 'POST',
    headers, body: payload === null ? undefined : JSON.stringify(payload),
    credentials: 'same-origin', redirect: 'manual', signal: AbortSignal.timeout(timeout)});
  return {status: r.status, ct: r.headers.get('content-type') || '',
    retry_after: r.headers.get('retry-after'), txt: await r.text()};
}"""


class PlaywrightTransport(_Base):
    """Borrow a manually authenticated Page; close does not close its context."""

    backend = "playwright"
    streaming = False

    def __init__(
        self,
        page: Any = None,
        *,
        base_url: str = ORIGIN,
        timeout: float = 120,
        storage_state: dict | None = None,
        stream_mode: str = "buffered",
        streaming_verified: bool = False,
    ):
        super().__init__(base_url)
        self.page: Any = page
        self.timeout = timeout
        self._storage_state = storage_state or {"cookies": [], "origins": []}
        self._playwright: Any = None
        self._browser: Any = None
        self.stream_mode = stream_mode
        self.streaming = stream_mode == "incremental" and streaming_verified
        self._page_lock = asyncio.Lock()

    async def _ensure_page(self) -> None:
        async with self._page_lock:
            await self._open_page()

    def _page_dead(self) -> bool:
        try:
            return self.page is None or self.page.is_closed() is True
        except Exception:  # noqa: BLE001 - an unusable handle counts as dead
            return True

    async def _reset_page(self) -> None:
        # A closed page/browser would otherwise linger: _open_page returns early
        # while self.page is truthy, so every later turn would re-hit the dead
        # handle and settle unknown. Drop it so the next call relaunches.
        with contextlib.suppress(Exception):
            if self._browser is not None:
                await self._browser.close()
        with contextlib.suppress(Exception):
            if self._playwright is not None:
                await self._playwright.stop()
        self.page = None
        self._browser = self._playwright = None

    async def _open_page(self) -> None:
        if self.page is not None:
            return
        try:
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True, args=["--disable-gpu"]
            )
            context = await self._browser.new_context(storage_state=self._storage_state)
            self.page = await context.new_page()
            await self.page.goto(
                self.base_url + CHAT_PATH, wait_until="domcontentloaded", timeout=30000
            )
        except Exception:  # noqa: BLE001 - SDK exceptions may contain credentials
            await self.close()
            self.state = "not_logged_in"
            raise TransportError(
                "browser_unavailable", known_not_submitted=True
            ) from None

    async def _fetch(self, path: str, payload: dict | None) -> dict:
        url = self._guard(path)
        await self._ensure_page()
        try:
            cookies = await self.page.context.cookies([url])
            csrf = csrf_for_url(cookies_from_storage_state({"cookies": cookies}), url)
            result = await self.page.evaluate(
                _FETCH,
                {
                    "url": url,
                    "origin": self.base_url,
                    "payload": payload,
                    "csrf": csrf,
                    "timeout": int(self.timeout * 1000),
                },
            )
        except Exception:  # noqa: BLE001 - SDK exceptions may contain credentials
            async with self._page_lock:
                if self._page_dead():
                    await self._reset_page()
            raise TransportError("browser_error") from None
        if not isinstance(result, dict):
            raise TransportError("browser_error")
        if result.get("preflight"):
            self.state = "expired"
            raise TransportError("wrong_browser_origin", known_not_submitted=True)
        self._status(result.get("status", 0), result.get("retry_after"))
        return result

    async def request(self, path: str, payload: dict | None = None) -> dict:
        result = await self._fetch(path, payload)
        return self._json(
            path, result.get("txt", ""), result.get("ct", ""), result["status"]
        )

    async def events(self, payload: dict) -> AsyncGenerator[dict, None]:
        if getattr(self, "stream_mode", "buffered") == "incremental":
            source = self._incremental_events(payload)
            try:
                async for event in source:
                    yield event
            finally:
                await source.aclose()
            return
        result = await self._fetch(CHAT_PATH, payload)
        self._stream_type(result.get("ct", ""), result["status"])
        for line in result.get("txt", "").splitlines():
            if line.strip():
                event = self._event(line)
                yield event
                if event["event"] == "end":
                    return
        raise TransportError("incomplete_stream", status=result["status"])

    async def _incremental_events(self, payload: dict) -> AsyncGenerator[dict, None]:
        from .browser_stream import packets

        url = self._guard(CHAT_PATH)
        await self._ensure_page()
        cookies = await self.page.context.cookies([url])
        csrf = csrf_for_url(cookies_from_storage_state({"cookies": cookies}), url)
        source = packets(
            self.page,
            {
                "url": url,
                "origin": self.base_url,
                "payload": payload,
                "csrf": csrf,
                "timeout": int(self.timeout * 1000),
            },
        )
        try:
            async for packet in source:
                kind = packet["kind"]
                if kind == "headers":
                    self._status(packet["status"], packet.get("retry_after"))
                    self._stream_type(packet["ct"], packet["status"])
                elif kind == "line":
                    event = self._event(packet["text"])
                    yield event
                    if event["event"] == "end":
                        return
                elif kind == "error":
                    if packet["code"] == "wrong_browser_origin":
                        self.state = "expired"
                    raise TransportError(packet["code"])
            raise TransportError("incomplete_stream")
        except TransportError:
            raise
        except Exception:  # noqa: BLE001 - sanitize browser exception URLs
            raise TransportError("browser_stream_failed") from None
        finally:
            await source.aclose()

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        finally:
            if self._playwright is not None:
                await self._playwright.stop()
            self.page = None
            self._browser = self._playwright = None
            self.state = "closed"


class FirecrawlTransport(PlaywrightTransport):
    """Reuse a supplied cloud session. Never create, login, save, or delete it.

    ChatClient is retained for compatibility; its browser is restricted to one
    execution attempt so a cloud timeout cannot silently resubmit a chat.
    """

    backend = "firecrawl"
    streaming = False

    def __init__(
        self, session_id: str, api_key: str, *, base_url: str = ORIGIN, app: Any = None
    ):
        _Base.__init__(self, base_url)
        if not session_id or not api_key:
            raise TransportError("missing_firecrawl_config", known_not_submitted=True)
        from firecrawl import Firecrawl

        from ..chat_client import ChatClient
        from ..cloud_browser import CloudBrowser

        self.chat_client = ChatClient(
            CloudBrowser(
                app if app is not None else Firecrawl(api_key=api_key), session_id
            )
        )
        self._lock = asyncio.Lock()

    async def _fetch(self, path: str, payload: dict | None) -> dict:
        url = self._guard(path)
        # Browser cookies are read inside this same execution; never persisted.
        args = json.dumps(
            {"url": url, "origin": self.base_url, "payload": payload, "timeout": 120000}
        )
        code = f"""var __v2args = {args};
var __v2cookies = await page.context().cookies([__v2args.url]);
__v2cookies.sort((a,b) => b.path.length - a.path.length);
__v2args.csrf = (__v2cookies.find(c => c.name === 'csrftoken') || {{}}).value;
var __v2result = await page.evaluate({_FETCH}, __v2args);
JSON.stringify(__v2result)"""
        try:
            async with self._lock:
                # Shield cancellation and retain the lock until the worker exits.
                task = asyncio.create_task(
                    asyncio.to_thread(
                        self.chat_client.cb.js_json, code, timeout=130, retries=1
                    )
                )
                try:
                    result = await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                    raise
        except Exception:  # noqa: BLE001 - cloud errors may contain session secrets
            raise TransportError("cloud_browser_error") from None
        if not isinstance(result, dict):
            raise TransportError("cloud_browser_error")
        if result.get("preflight"):
            self.state = "expired"
            raise TransportError("wrong_browser_origin", known_not_submitted=True)
        self._status(result.get("status", 0), result.get("retry_after"))
        return result

    async def close(self) -> None:
        # Session ownership remains with its creator.
        self.state = "closed"


async def build_transport(
    backend: str, data_dir: str | Path, config: Any = None
) -> Transport:
    """Construct without network calls. Explicit ``config.auth_dir`` selects auth.

    Config may be a mapping or settings object: auth_dir (NEW diagnose output),
    base_url, timeout, firecrawl_session_id, firecrawl_api_key. ``data_dir`` is
    deliberately not searched for legacy credentials or implicitly selected auth.
    Caller explicitly awaits authenticate(); health handlers only read state.
    """

    def setting(key: str, default: Any = None) -> Any:
        return (
            config.get(key, default)
            if isinstance(config, dict)
            else getattr(config, key, default)
        )

    base_url = setting("base_url", ORIGIN)
    if backend == "firecrawl":
        return FirecrawlTransport(
            setting("firecrawl_session_id", ""),
            setting("firecrawl_api_key", ""),
            base_url=base_url,
        )
    if backend not in ("httpx", "playwright"):
        raise TransportError("invalid_backend", known_not_submitted=True)
    state: dict = {"cookies": [], "origins": []}
    auth_dir = setting("auth_dir")
    if auth_dir:
        try:
            state = json.loads(
                (Path(auth_dir) / "storage_state.json").read_text(encoding="utf-8")
            )
            if not isinstance(state, dict):
                raise TypeError
        except (OSError, ValueError, TypeError):
            raise TransportError(
                "invalid_auth_state", known_not_submitted=True
            ) from None
    try:
        if backend == "httpx":
            return HTTPXTransport(
                state, base_url=base_url, timeout=setting("timeout", 120)
            )
        return PlaywrightTransport(
            base_url=base_url,
            storage_state=state,
            timeout=setting("timeout", 120),
            stream_mode=setting("stream_mode", "buffered"),
            streaming_verified=setting("streaming_verified", False),
        )
    except (KeyError, TypeError, ValueError):
        raise TransportError("invalid_auth_state", known_not_submitted=True) from None
