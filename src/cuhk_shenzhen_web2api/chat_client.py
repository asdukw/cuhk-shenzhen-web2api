"""Programmatic client for the (reverse-engineered) CUHK-Shenzhen AI chat API.

The browser session must already be logged in and sitting on /chat/. All HTTP
calls go through the page context so the aTrust/ADFS-cookie + CSRF session is
attached automatically (the session is IP-bound to the browser environment).

Core endpoint: POST /chat/  ->  application/x-ndjson+json  (one JSON line each),
events: start / hb / msg / end.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from .browser_backend import BrowserBackend

CHAT_ENDPOINT = "/chat/"
ABORT_ENDPOINT = "/chat/abort/"
RECOVER_ENDPOINT = "/chat/recover/"
HISTORY_META_ENDPOINT = "/getNextHistoryMeta/"
HISTORY_ITEM_ENDPOINT = "/getHistoryItem/"
UPLOAD_FILE_ENDPOINT = "/uploadFile/"
UPLOAD_MEDIA_ENDPOINT = "/uploadMedia/"

_DEFAULT_PARAMS = {"tool_proxy": False}


class ChatAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        detail=None,
        status: int | None = None,
    ):
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
    return list(iter_ndjson(text))


def iter_ndjson(text: str) -> Iterator[dict]:
    """Yield parsed NDJSON events one line at a time (skips blank lines)."""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            yield {"_raw": line}


def assemble_reply(events: Iterator[dict]) -> ChatReply:
    """Fold an event stream into a ChatReply (concatenated text + tools)."""
    reply = ChatReply(lines=[ev for ev in events])
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


def parse_chat_stream(text: str) -> ChatReply:
    """Turn an NDJSON chat body into a ChatReply (concatenated text + tools)."""
    return assemble_reply(iter_ndjson(text))


class ChatClient:
    def __init__(self, cb: BrowserBackend, *, quota_pool: str = "Students Pool"):
        self.cb = cb
        self.quota_pool = quota_pool

    # ---- session/config reads ----

    def whoami(self) -> dict:
        result = self.cb.js_json("""
          var __r = await page.evaluate(async () => { const r = await fetch("/.auth/me/", {redirect:"manual"});
            return {status: r.status, body: JSON.parse(await r.text())}; });
          JSON.stringify(__r)
        """)
        return cast(dict, result) if isinstance(result, dict) else {}

    def quota_pools(self) -> dict:
        result = self.cb.js_json("""
          var __r = await page.evaluate(async () => { const r = await fetch("/api/config_available_quotapools/");
            return {status: r.status, body: JSON.parse(await r.text())}; });
          JSON.stringify(__r)
        """)
        return cast(dict, result) if isinstance(result, dict) else {}

    def config(self, quota_pool: str | None = None) -> dict:
        pool = quota_pool or self.quota_pool
        result = self.cb.js_json(
            """
          var __u = "/config/?quota_pool=" + encodeURIComponent(__POOL);
          var __r = await page.evaluate(async (u) => { const r = await fetch(u);
            return {status: r.status, body: JSON.parse(await r.text())}; }, __u);
          JSON.stringify(__r)
        """.replace("__POOL", json.dumps(pool))
        )
        return cast(dict, result) if isinstance(result, dict) else {}

    def models(self, quota_pool: str | None = None) -> list[str]:
        cfg = self.config(quota_pool)
        return cfg.get("body", {}).get("availableModels", []) or []

    def approaches(self, quota_pool: str | None = None) -> dict:
        cfg = self.config(quota_pool)
        return cfg.get("body", {}).get("approachAttributes", {}) or {}

    # ---- conversation ----

    def send_stream(
        self,
        content: str,
        *,
        approach_id: str,
        quota_pool: str | None = None,
        chat_session_id: str | None = None,
        project_id: str | None = None,
        parent_idx: int = -1,
        params: dict | None = None,
        image_ids: tuple[str, ...] = (),
        file_ids: tuple[str, ...] = (),
        timeout: int = 300,
    ) -> ChatReply:
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
        res = self.cb.js_json(
            f"""
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
        """,
            timeout=timeout,
        )
        if not isinstance(res, dict):
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

    def continue_stream(
        self, content: str, *, approach_id: str, last: ChatReply, **kwargs
    ) -> ChatReply:
        """Continue a conversation from a previous ChatReply (or start event).

        Uses the returned chat_session_id and the assistant message idx as the
        parent, which is what the API requires for a follow-up turn.
        """
        if not last.chat_session_id or last.approach_msg_idx is None:
            raise ChatAPIError("cannot continue: reply has no session/message index")
        return self.send_stream(
            content,
            approach_id=approach_id,
            chat_session_id=last.chat_session_id,
            parent_idx=last.approach_msg_idx,
            **kwargs,
        )

    def abort(self, chat_session_id: str, message_idx: int) -> dict:
        return self._post_json(
            ABORT_ENDPOINT,
            {"chat_session_id": chat_session_id, "message_idx": message_idx},
        )

    def recover(
        self, chat_session_id: str, message_idx: int, timeout: int = 300
    ) -> ChatReply:
        res = self._post_json(
            RECOVER_ENDPOINT,
            {"chat_session_id": chat_session_id, "message_idx": message_idx},
            timeout=timeout,
        )
        return parse_chat_stream(res.get("txt", ""))

    # ---- history ----

    def list_sessions(
        self,
        need: int = 30,
        time_offset: int | None = None,
        count_offset: int = 0,
        includes_pinned: bool = True,
    ) -> list[dict]:
        """List past chat sessions with titles (recency-ordered)."""
        res = self._post_json(
            HISTORY_META_ENDPOINT,
            {
                "need": need,
                "time_offset": time_offset
                if time_offset is not None
                else int(time.time() * 1000),
                "count_offset": count_offset,
                "includes_pinned": includes_pinned,
            },
        )
        data = self._decode_json_body(res)
        return data if isinstance(data, list) else []

    def history_item(self, chat_session_id: str) -> dict:
        """Fetch one conversation (messages array with role/items/self_idx)."""
        res = self._post_json(
            HISTORY_ITEM_ENDPOINT, {"chat_session_id": chat_session_id}
        )
        data = self._decode_json_body(res)
        return data if isinstance(data, dict) else {}

    # ---- media/native upload ----

    def upload_path(self, path: str | Path, *, media: bool = False) -> str:
        """Upload a local file to the chat service; return its media_id.

        media=True goes to /uploadMedia/ (images/video), otherwise /uploadFile/.
        The bytes are pushed through the browser session, so keep files modest.
        """
        p = Path(path)
        payload = base64.b64encode(p.read_bytes()).decode("ascii")
        endpoint = UPLOAD_MEDIA_ENDPOINT if media else UPLOAD_FILE_ENDPOINT
        mime = _guess_mime(p)
        res = self.cb.js_json(
            f"""
          var __b64 = {json.dumps(payload)};
          var __mime = {json.dumps(mime)};
          var __bin = atob(__b64);
          var __arr = new Uint8Array(__bin.length);
          for (var __i2 = 0; __i2 < __bin.length; __i2++) __arr[__i2] = __bin.charCodeAt(__i2);
          var __blob = new Blob([__arr], {{type: __mime}});
          var __r = await page.evaluate(async (blob) => {{
            var csrf = document.cookie.split(";").map(s=>s.trim()).find(s=>s.indexOf("csrftoken=")===0);
            var headers = {{"Content-Type": "application/octet-stream"}};
            if (csrf) headers["X-CSRFToken"] = csrf.slice(10);
            var resp = await fetch({json.dumps(endpoint)}, {{method: "POST", headers: headers,
              body: blob, credentials: "include"}});
            return {{status: resp.status, txt: await resp.text()}};
          }}, __blob);
          JSON.stringify(__r)
        """,
            timeout=240,
        )
        if not isinstance(res, dict):
            raise ChatAPIError("upload failed: browser_execute error")
        if res.get("status", 0) != 200:
            raise ChatAPIError(
                f"upload HTTP {res.get('status')}: {res.get('txt')}",
                status=res.get("status"),
            )
        body = res.get("txt", "")
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ChatAPIError(f"upload bad response: {body[:200]}") from exc
        media_id = data.get("media_id")
        if not media_id:
            raise ChatAPIError("upload response has no media_id", detail=data)
        return media_id

    def send_with_files(
        self,
        content: str,
        *,
        approach_id: str,
        image_paths: tuple[str | Path, ...] = (),
        file_paths: tuple[str | Path, ...] = (),
        **kwargs,
    ) -> ChatReply:
        """Upload local images/files then send a message with them attached."""
        image_ids = tuple(self.upload_path(p, media=True) for p in image_paths)
        file_ids = tuple(self.upload_path(p) for p in file_paths)
        return self.send_stream(
            content,
            approach_id=approach_id,
            image_ids=image_ids,
            file_ids=file_ids,
            **kwargs,
        )

    # ---- helpers ----

    def _post_json(self, path: str, payload: dict, timeout: int = 120) -> dict:
        res = self.cb.js_json(
            f"""
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
        """,
            timeout=timeout,
        )
        return cast(dict, res) if isinstance(res, dict) else {"status": -1, "txt": ""}

    def _decode_json_body(self, res: dict) -> Any:
        """Decode an application/json body from _post_json; raise on error."""
        if res.get("status", 0) != 200:
            raise ChatAPIError(
                f"HTTP {res.get('status')}: {res.get('txt')}", status=res.get("status")
            )
        try:
            return json.loads(res.get("txt", "") or "null")
        except json.JSONDecodeError as exc:
            raise ChatAPIError("invalid JSON response") from exc


def _guess_mime(path: Path) -> str:
    ext = path.suffix.lower()
    mapping = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".pdf": "application/pdf",
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xls": "application/vnd.ms-excel",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".ppt": "application/vnd.ms-powerpoint",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".csv": "text/csv",
        ".zip": "application/zip",
    }
    return mapping.get(ext, "application/octet-stream")
