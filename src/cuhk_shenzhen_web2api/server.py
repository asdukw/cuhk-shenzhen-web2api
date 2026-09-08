"""Local HTTP facade (web2api) over the CUHK AI chat.

A FastAPI app that owns one shared Firecrawl cloud-browser session and exposes
the chat as a JSON API. Every request goes through the live page context
(cookies + CSRF + IP-bound aTrust session), so the underlying service considers
it a normal browser conversation.

Concurrency: the shared browser + the ~3 req/min free-tier rate limit mean calls
must be serialized. A module-level async lock guards all ChatClient work.

The default model can be switched at runtime (POST /model); a `message` that
starts with /model is handled as a slash command instead of being sent to the
model, e.g. `/model glm-5.3-flash`.

Run with:
    python src/cuhk_shenzhen_web2api/scripts/server.py   (uvicorn, 127.0.0.1:8765)
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import FastAPI, HTTPException
from firecrawl import Firecrawl
from pydantic import BaseModel, Field

from . import cloud_browser, env, login
from .chat_client import ChatClient, ChatReply

_shared: dict[str, Any] = {
    "cb": None,
    "client": None,
    "lock": asyncio.Lock(),
    "init_lock": asyncio.Lock(),
}
_default_model = "claude-haiku-4-5"
_models_cache: dict[str, Any] = {"at": 0.0, "available": []}
_MODELS_TTL = 300.0


def _available_models(client: ChatClient) -> list[str]:
    """Client model catalog with a short TTL (config fetch costs a browser call)."""
    cached = _models_cache
    if time.monotonic() - cached["at"] < _MODELS_TTL:
        return cached["available"]
    try:
        available = client.models()
    except Exception:  # noqa: BLE001 - fall back to cache on transient failure
        return cached["available"]
    cached["at"] = time.monotonic()
    cached["available"] = available
    return available


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    approach_id: str | None = None
    quota_pool: str = "Students Pool"
    chat_session_id: str | None = None
    parent_idx: int = -1
    project_id: str | None = None
    tool_proxy: bool = False
    prompt_id: str | None = None


class ModelSwitch(BaseModel):
    approach_id: str = Field(min_length=1)


class SessionSummary(BaseModel):
    chat_session_id: str
    title: str | None
    source: str | None
    project: Any = None
    session_type: str | None
    created_at: int | None
    updated_at: int | None


class Health(BaseModel):
    session_id: str
    url: str
    user: Any = None


def _ensure_runtime() -> ChatClient:
    """Lazily open/resume the browser session and log in (first call only)."""
    if _shared["client"] is not None:
        return _shared["client"]
    config = env.load_env()
    app = Firecrawl(api_key=env.firecrawl_api_key(config))
    sid = cloud_browser.load_session_id()
    cb = cloud_browser.get_or_create_session(app, sid)
    final = login.ensure_on_chat(
        cb, env.chat_username(config), env.chat_password(config)
    )
    if "/chat" not in (final or ""):
        raise RuntimeError(f"login did not reach /chat/: {final}")
    cloud_browser.save_session_id(cb.sid)
    client = ChatClient(cb)
    _shared["cb"] = cb
    _shared["client"] = client
    return client


async def _runtime() -> ChatClient:
    """Lazily create/resume the browser session exactly once (thread-safe)."""
    if _shared["client"] is not None:
        return _shared["client"]
    async with _shared["init_lock"]:
        if _shared["client"] is None:
            _shared["client"] = await asyncio.to_thread(_ensure_runtime)
    return _shared["client"]


def _reply_summary(reply: ChatReply) -> dict[str, Any]:
    return {
        "chat_session_id": reply.chat_session_id,
        "user_msg_idx": reply.user_msg_idx,
        "approach_msg_idx": reply.approach_msg_idx,
        "title": reply.title,
        "status": reply.status,
        "ctx_token_cnt": reply.ctx_token_cnt,
        "tools": reply.tools,
        "text": reply.text,
    }


def _slash(payload: str, client: ChatClient) -> dict[str, Any] | None:
    """Handle /model slash commands; return None when the message is not one."""
    global _default_model
    if not payload.startswith("/"):
        return None
    parts = payload.split(maxsplit=1)
    cmd = parts[0].lower()
    if cmd == "/help":
        return {
            "mode": "slash",
            "command": "/help",
            "help": {
                "/model": "show current model",
                "/model <name>": "switch default model (e.g. /model glm-5.3-flash)",
            },
        }
    if cmd == "/model":
        arg = parts[1].strip() if len(parts) > 1 else ""
        if not arg:
            return {
                "mode": "slash",
                "command": "/model",
                "current": _default_model,
                "available": _available_models(client),
            }
        if arg not in _available_models(client):
            raise HTTPException(400, f"unknown model {arg!r}")
        _default_model = arg
        return {"mode": "slash", "command": "/model", "set": arg, "current": arg}
    return {"mode": "slash", "command": cmd, "unknown": True}


app = FastAPI(title="cuhk-shenzhen-web2api", version="0.2.0")


@app.get("/health")
async def health() -> Health:
    try:
        client = await _runtime()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    cb = _shared["cb"]
    return Health(
        session_id=cb.sid,
        url=cb.url(),
        user=client.whoami().get("body"),
    )


@app.get("/model")
async def model_info() -> dict[str, Any]:
    """Current default model and the available model catalog."""
    client = await _runtime()
    return {
        "current": _default_model,
        "available": await asyncio.to_thread(_available_models, client),
    }


@app.post("/model")
async def switch_model(switch: ModelSwitch) -> dict[str, Any]:
    """Switch the server-wide default model (validates against the catalog)."""
    global _default_model
    client = await _runtime()
    available = await asyncio.to_thread(_available_models, client)
    if switch.approach_id not in available:
        raise HTTPException(400, f"unknown model {switch.approach_id!r}")
    _default_model = switch.approach_id
    return {"current": _default_model, "available": available}


@app.post("/chat")
async def chat(req: ChatRequest) -> dict[str, Any]:
    """Send one message (new session or continuation); returns the full reply.

    For a follow-up, pass chat_session_id and the previous turn's
    approach_msg_idx as parent_idx (from a prior reply). Messages starting
    with / are treated as slash commands (see /help), not sent to the model.
    """
    client = await _runtime()
    slash = _slash(req.message, client)
    if slash is not None:
        return slash
    async with _shared["lock"]:
        reply = await asyncio.to_thread(
            client.send_stream,
            req.message,
            approach_id=req.approach_id or _default_model,
            quota_pool=req.quota_pool,
            chat_session_id=req.chat_session_id,
            parent_idx=req.parent_idx,
            project_id=req.project_id,
            params={"tool_proxy": req.tool_proxy} if req.tool_proxy else None,
        )
    return _reply_summary(reply)


@app.get("/sessions")
async def sessions(limit: int = 30) -> list[SessionSummary]:
    """Most recent conversations, with titles."""
    client = await _runtime()
    async with _shared["lock"]:
        items = await asyncio.to_thread(client.list_sessions, need=limit)
    out: list[SessionSummary] = []
    for it in items:
        out.append(
            SessionSummary(
                chat_session_id=it.get("chat_session_id", ""),
                title=it.get("title"),
                source=it.get("source"),
                project=it.get("project"),
                session_type=(it.get("layout") or {}).get("session_type"),
                created_at=it.get("created_at"),
                updated_at=it.get("updated_at"),
            )
        )
    return out


@app.get("/sessions/{session_id}")
async def session_item(session_id: str) -> dict[str, Any]:
    """Full conversation history for one session (messages array)."""
    client = await _runtime()
    async with _shared["lock"]:
        data = await asyncio.to_thread(client.history_item, session_id)
    return data
