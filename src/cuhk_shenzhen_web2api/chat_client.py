"""Programmatic client for the (reverse-engineered) CUHK-Shenzhen AI chat API.

The browser session must already be logged in and sitting on /chat/. All HTTP
calls go through the page context so the aTrust/ADFS-cookie + CSRF session is
attached automatically (the session is IP-bound to the cloud run).

Core endpoint: POST /chat/  ->  application/x-ndjson+json  (one JSON line each),
events: start / hb / msg / end.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .cloud_browser import CloudBrowser

CHAT_ENDPOINT = "/chat/"
ABORT_ENDPOINT = "/chat/abort/"
RECOVER_ENDPOINT = "/chat/recover/"

_DEFAULT_PARAMS = {"tool_proxy": False}


class ChatAPIError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None, detail=None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.detail = detail
        self.status = status


@dataclass
class ChatReply:
    """Parsed result of one POST /chat/ exchange."""

    text: str = ""
    chat_session_id: str | None = None
    status: str | None = None
    title: str | None = None
    user_msg_idx: int | None = None
    approach_msg_idx: int | None = None
    ctx_token_cnt: int | None = None
    context_truncated: bool | None = None
    tools: list[dict] = field(default_factory=list)
    lines: list[dict] = field(default_factory=list)


def parse_ndjson(text: str) -> list[dict]:
    """Split an application/x-ndjson+json body into events."""
    events: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            events.append({"_raw": line})
    return events


def parse_chat_stream(text: str) -> ChatReply:
    """Turn an NDJSON chat body into a ChatReply (concatenated text + tools)."""
    reply = ChatReply(lines=parse_ndjson(text))
    for ev in reply.lines:
        kind = ev.get("event")
        if kind == "start":
            reply.chat_session_id = ev.get("chat_session_id")
            reply.user_msg_idx = ev.get("user_msg_idx")
            reply.approach_msg_idx = ev.get("approach_msg_idx")
        elif kind == "msg":
            item = ev.get("item") or {}
            if item.get("type") == "text":
                reply.text += item.get("content") or ""
            elif item.get("type") == "tool":
                reply.tools.append(item)
        elif kind == "end":
            reply.status = ev.get("status")
            reply.title = ev.get("title")
            reply.ctx_token_cnt = ev.get("ctx_token_cnt")
            reply.context_truncated = ev.get("context_truncated")
    return reply


class ChatClient:
    def __init__(self, cb: CloudBrowser, *, quota_pool: str = "Students Pool"):
        self.cb = cb
        self.quota_pool = quota_pool

    # ---- session/config reads ----

    def whoami(self) -> dict:
        result = self.cb.js_json('''
          var __r = await page.evaluate(async () => { const r = await fetch("/.auth/me/", {redirect:"manual"});
            return {status: r.status, body: JSON.parse(await r.text())}; });
          JSON.stringify(__r)
        ''')
        return result or {}

    def quota_pools(self) -> dict:
        result = self.cb.js_json('''
          var __r = await page.evaluate(async () => { const r = await fetch("/api/config_available_quotapools/");
            return {status: r.status, body: JSON.parse(await r.text())}; });
          JSON.stringify(__r)
        ''')
        return result or {}

    def config(self, quota_pool: str | None = None) -> dict:
        pool = quota_pool or self.quota_pool
        result = self.cb.js_json('''
          var __u = "/config/?quota_pool=" + encodeURIComponent(__POOL);
          var __r = await page.evaluate(async (u) => { const r = await fetch(u);
            return {status: r.status, body: JSON.parse(await r.text())}; }, __u);
          JSON.stringify(__r)
        '''.replace("__POOL", json.dumps(pool)))
        return result or {}

    def models(self, quota_pool: str | None = None) -> list[str]:
        cfg = self.config(quota_pool)
        return cfg.get("body", {}).get("availableModels", []) or []

    def approaches(self, quota_pool: str | None = None) -> dict:
        cfg = self.config(quota_pool)
        return cfg.get("body", {}).get("approachAttributes", {}) or {}

    # ---- conversation ----

    def send_stream(self, content: str, *, approach_id: str, quota_pool: str | None = None,
                    chat_session_id: str | None = None, project_id: str | None = None,
                    parent_idx: int = -1, params: dict | None = None,
                    image_ids: tuple[str, ...] = (), file_ids: tuple[str, ...] = (),
                    timeout: int = 300) -> ChatReply:
        """Send one user message to /chat/ and return the parsed stream reply.

        Use parent_idx=-1 + no chat_session_id to start a fresh session;
        to continue, reuse the returned chat_session_id / parent_idx.
        """
        pool = quota_pool or self.quota_pool
        payload = {
            "project_id": project_id,
            "chat_session_id": chat_session_id,
            "approach_id": approach_id,
            "params": params or _DEFAULT_PARAMS,
            "content": content,
            "parent_idx": parent_idx,
            "image_ids": list(image_ids),
            "file_ids": list(file_ids),
            "quota_pool": pool,
        }
        res = self.cb.js_json(f'''
          var __p = {json.dumps(payload)};
          var __r = await page.evaluate(async (p) => {{
            var csrf = document.cookie.split(";").map(s=>s.trim()).find(s=>s.indexOf("csrftoken=")===0);
            var headers = {{"Content-Type": "application/json"}};
            if (csrf) headers["X-CSRFToken"] = csrf.slice(10);
            var resp = await fetch({json.dumps(CHAT_ENDPOINT)}, {{method: "POST", headers: headers,
              body: JSON.stringify(p), credentials: "include", redirect: "manual"}});
            return {{status: resp.status, ct: resp.headers.get("content-type"), txt: await resp.text(), trace: resp.headers.get("Trace-Id")}};
          }}, __p);
          JSON.stringify(__r)
        ''', timeout=timeout)
        if res is None:
            raise ChatAPIError("browser_execute failed; see script output")
        if res.get("status", 0) != 200:
            body = res.get("txt", "")
            try:
                err = json.loads(body)["error"]
            except (json.JSONDecodeError, KeyError, TypeError):
                err = {"message": body}
            raise ChatAPIError(
                err.get("message") or f"HTTP {res.get('status')}",
                code=err.get("error_code"),
                detail=err.get("detail"),
                status=res.get("status"),
            )
        return parse_chat_stream(res.get("txt", ""))

    def chat(self, content: str, *, approach_id: str, **kwargs) -> ChatReply:
        """Convenience wrapper around send_stream."""
        return self.send_stream(content, approach_id=approach_id, **kwargs)

    def abort(self, chat_session_id: str, message_idx: int) -> dict:
        return self._post_json(ABORT_ENDPOINT, {"chat_session_id": chat_session_id, "message_idx": message_idx})

    def recover(self, chat_session_id: str, message_idx: int, timeout: int = 300) -> ChatReply:
        res = self._post_json(RECOVER_ENDPOINT, {"chat_session_id": chat_session_id, "message_idx": message_idx}, timeout=timeout)
        return parse_chat_stream(res.get("txt", ""))

    def _post_json(self, path: str, payload: dict, timeout: int = 120) -> dict:
        res = self.cb.js_json(f'''
          var __p = {json.dumps(payload)};
          var __r = await page.evaluate(async (p) => {{
            var csrf = document.cookie.split(";").map(s=>s.trim()).find(s=>s.indexOf("csrftoken=")===0);
            var headers = {{"Content-Type": "application/json"}};
            if (csrf) headers["X-CSRFToken"] = csrf.slice(10);
            var resp = await fetch({json.dumps(path)}, {{method: "POST", headers: headers,
              body: JSON.stringify(p), credentials: "include", redirect: "manual"}});
            return {{status: resp.status, ct: resp.headers.get("content-type"), txt: await resp.text()}};
          }}, __p);
          JSON.stringify(__r)
        ''', timeout=timeout)
        return res or {"status": -1, "txt": ""}