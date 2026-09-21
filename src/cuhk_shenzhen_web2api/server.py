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
import hashlib
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from firecrawl import Firecrawl
from pydantic import BaseModel, Field

log = logging.getLogger("web2api")

from . import cloud_browser, conversation, env, jobs, login
from .chat_client import ChatClient, ChatReply
from .tool_proxy import (
    ToolCall,
    ToolDefinition,
    get_default_proxy,
)

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
    conversation: str | None = None
    project_id: str | None = None
    tool_proxy: bool = False
    prompt_id: str | None = None
    tool_proxy_config: ToolProxyConfig | None = None
    stream: bool = False


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
    ready: bool = True
    session_id: str = ""
    url: str = ""
    user: Any = None


class ToolRegistration(BaseModel):
    """Register a new tool with the proxy."""

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    parameters: dict[str, Any] | None = None


class ToolCallRequest(BaseModel):
    """Execute a tool call."""

    tool_name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolProxyConfig(BaseModel):
    """Configure tool proxy behavior."""

    enabled: bool = False
    tools: list[str] = Field(default_factory=list)


# ---- OpenAI Responses API models ----

# Model alias map: Cursor often rejects / drops CUHK catalog IDs on Add Model.
# Use the left-hand names in Cursor; we rewrite them to real approach_ids.
_MODEL_ALIASES: dict[str, str] = {
    "gpt-5.6-sol": "gpt-5.6-luna",
    # Cursor-friendly aliases (Add Model with these)
    "cuhk-haiku": "claude-haiku-4-5",
    "cuhk-claude-haiku": "claude-haiku-4-5",
    "cuhk-gpt": "gpt-5.6-luna",
    "cuhk-luna": "gpt-5.6-luna",
    "cuhk-glm-flash": "glm-5.3-flash",
    "cuhk-glm": "glm-5.3",
    "cuhk-deepseek": "deepseek-v4-pro",
    "cuhk-qwen": "qwen3.8-27b",
    "cuhk-gemini-flash": "gemini-3.8-flash",
}


def _resolve_model(name: str | None) -> str:
    """Resolve an incoming model name to one we actually support."""
    if not name:
        return _default_model
    key = name.strip()
    return _MODEL_ALIASES.get(key) or _MODEL_ALIASES.get(key.lower()) or key


class ResponsesRequest(BaseModel):
    """OpenAI Responses API request body."""

    model: str = "gpt-5.6-luna"
    input: str | list[dict[str, Any]] = ""
    stream: bool = False
    tools: list[dict[str, Any]] | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    previous_response_id: str | None = None
    instructions: str | None = None
    conversation: str | None = None
    user: str | None = None


class BatchItem(BaseModel):
    message: str = Field(min_length=1)
    id: str | None = None
    conversation: str | None = None
    approach_id: str | None = None


class BatchJobRequest(BaseModel):
    items: list[BatchItem] = Field(min_length=1)
    conversation: str | None = None
    approach_id: str | None = None
    mode: str = Field(default="independent", pattern="^(independent|thread)$")
    pause_seconds: float = Field(default=20.0, ge=0, le=120)


class ChatCompletionsRequest(BaseModel):
    """OpenAI Chat Completions request body."""

    model: str = "claude-haiku-4-5"
    messages: list[dict[str, Any]]
    stream: bool = False
    tools: list[dict[str, Any]] | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    conversation: str | None = None
    previous_response_id: str | None = None
    user: str | None = None


def _ensure_runtime() -> ChatClient:
    """Lazily open/resume the browser session and log in (first call only)."""
    if _shared["client"] is not None:
        return _shared["client"]
    config = env.load_env()
    app = Firecrawl(api_key=env.firecrawl_api_key(config))
    sid = cloud_browser.load_session_id()
    cb = cloud_browser.get_or_create_session(app, sid)
    username = env.chat_username(config)
    password = env.chat_password(config)
    if cb.live_url():
        # The live-view URL embeds a session token; only note that it exists.
        log.info("cloud browser live view available (url redacted)")
    final = login.ensure_on_chat(cb, username, password)
    # Trust ensure_on_chat — do not re-probe /.auth/me (extra browser_execute
    # under Firecrawl rate limits can false-fail a good session into 503).
    if not login.looks_on_chat(final):
        raise RuntimeError(f"login did not reach authenticated /chat/: {final}")
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


def _reply_summary(
    reply: ChatReply, conversation_id: str | None = None
) -> dict[str, Any]:
    return {
        "chat_session_id": reply.chat_session_id,
        "user_msg_idx": reply.user_msg_idx,
        "approach_msg_idx": reply.approach_msg_idx,
        "title": reply.title,
        "status": reply.status,
        "ctx_token_cnt": reply.ctx_token_cnt,
        "tools": reply.tools,
        "text": reply.text,
        "conversation": conversation_id,
    }


def _resolve_thread(
    conversation_id: str | None,
    *,
    chat_session_id: str | None = None,
    parent_idx: int = -1,
    fingerprint: str | None = None,
    openai_id: str | None = None,
) -> tuple[str | None, str | None, int]:
    """Map a named/OpenAI thread onto CUHK session + parent_idx."""
    if openai_id and not conversation_id:
        hit = conversation.get_by_openai_id(openai_id)
        if hit:
            conversation_id = hit.get("conversation_id")
    if fingerprint and not conversation_id:
        hit = conversation.get_by_fingerprint(fingerprint)
        if hit:
            conversation_id = hit.get("conversation_id")
    if chat_session_id:
        return conversation_id, chat_session_id, parent_idx
    if conversation_id:
        hit = conversation.get(conversation_id)
        if hit and hit.get("chat_session_id") and hit.get("parent_idx") is not None:
            return conversation_id, str(hit["chat_session_id"]), int(hit["parent_idx"])
    return conversation_id, None, -1


def _remember_thread(
    conversation_id: str | None,
    reply: ChatReply,
    *,
    model: str,
    fingerprint: str | None = None,
    openai_id: str | None = None,
) -> str | None:
    cid = conversation_id or reply.chat_session_id
    if not cid or not reply.chat_session_id or reply.approach_msg_idx is None:
        return conversation_id
    conversation.upsert(
        cid,
        chat_session_id=reply.chat_session_id,
        parent_idx=reply.approach_msg_idx,
        model=model,
        title=reply.title,
        fingerprint=fingerprint,
        openai_id=openai_id,
    )
    return cid


def _send_continued(
    client: ChatClient,
    content: str,
    *,
    approach_id: str,
    params: dict[str, Any] | None = None,
    conversation_id: str | None = None,
    chat_session_id: str | None = None,
    parent_idx: int = -1,
    fingerprint: str | None = None,
    openai_id: str | None = None,
    quota_pool: str | None = None,
    project_id: str | None = None,
    timeout: int = 600,
) -> tuple[ChatReply, str | None]:
    cid, session, parent = _resolve_thread(
        conversation_id,
        chat_session_id=chat_session_id,
        parent_idx=parent_idx,
        fingerprint=fingerprint,
        openai_id=openai_id,
    )
    reply = client.send_stream(
        content,
        approach_id=approach_id,
        quota_pool=quota_pool,
        chat_session_id=session,
        parent_idx=parent,
        project_id=project_id,
        params=params,
        timeout=timeout,
    )
    cid = _remember_thread(
        cid,
        reply,
        model=approach_id,
        fingerprint=fingerprint,
        openai_id=openai_id,
    )
    return reply, cid


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


async def _job_worker() -> None:
    """Drain the batch queue one chat turn at a time (Firecrawl is serial)."""
    await asyncio.to_thread(jobs.recover_running)
    while True:
        claimed = await asyncio.to_thread(jobs.claim_next_item)
        if claimed is None:
            await asyncio.sleep(1.0)
            continue
        job_id, job, item = claimed
        try:
            client = await _runtime()
            model = _resolve_model(
                item.get("approach_id") or job.get("approach_id") or _default_model
            )
            conv = item.get("conversation")
            if job.get("mode") == "thread":
                conv = job.get("conversation") or conv or job_id
            async with _shared["lock"]:
                reply, cid = await asyncio.to_thread(
                    _send_continued,
                    client,
                    item["message"],
                    approach_id=model,
                    conversation_id=conv,
                )
            await asyncio.to_thread(
                jobs.finish_item,
                job_id,
                int(item["index"]),
                text=reply.text,
                chat_session_id=reply.chat_session_id,
                conversation_id=cid,
            )
        except Exception as exc:
            log.exception("job %s item %s failed", job_id, item.get("index"))
            await asyncio.to_thread(
                jobs.finish_item,
                job_id,
                int(item["index"]),
                error=str(exc),
            )
        pause = float(job.get("pause_seconds") or 20.0)
        if pause > 0:
            await asyncio.sleep(pause)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    task = asyncio.create_task(_job_worker())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="cuhk-shenzhen-web2api", version="0.2.0", lifespan=_lifespan)

_OPEN_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}


@app.middleware("http")
async def log_requests(request: Request, call_next):
    if request.method != "OPTIONS" and request.url.path not in _OPEN_PATHS:
        expected = env.web2api_api_key()
        if not expected:
            # Fail closed: a protected path with no key configured must never be
            # served unauthenticated just because the operator forgot to set it.
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "message": "Server is not configured with WEB2API_API_KEY.",
                        "type": "server_error",
                        "code": "server_not_configured",
                    }
                },
            )
        auth = request.headers.get("authorization") or ""
        token = ""
        if auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()
        elif request.headers.get("x-api-key"):
            token = request.headers["x-api-key"].strip()
        if token != expected:
            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "message": "Invalid API key. Use WEB2API_API_KEY from .env.",
                        "type": "invalid_request_error",
                        "code": "invalid_api_key",
                    }
                },
            )
    body_bytes = await request.body()
    # Log size only: request bodies carry user prompts/tool output that must not
    # reach INFO logs.
    log.info(
        ">>> %s %s [%s] body_bytes=%d",
        request.method,
        request.url.path,
        request.client.host if request.client else "?",
        len(body_bytes),
    )
    response = await call_next(request)
    log.info("<<< %s %s -> %d", request.method, request.url.path, response.status_code)
    return response


@app.get("/health")
async def health() -> Health:
    client = _shared["client"]
    if client is None:
        # A health probe must never create the browser session or log in; it
        # only reports whether a prior request already warmed the runtime.
        return Health(ready=False)
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

    For a follow-up, pass the same `conversation` name (preferred) or
    chat_session_id plus the previous turn's approach_msg_idx as parent_idx.
    Messages starting with / are treated as slash commands (see /help).
    """
    client = await _runtime()
    slash = _slash(req.message, client)
    if slash is not None:
        return slash
    async with _shared["lock"]:
        params: dict[str, Any] | None = None
        if req.tool_proxy:
            params = {"tool_proxy": True}
        if req.tool_proxy_config:
            if params is None:
                params = {}
            params["tool_proxy_config"] = req.tool_proxy_config.model_dump()

        reply, cid = await asyncio.to_thread(
            _send_continued,
            client,
            req.message,
            approach_id=req.approach_id or _default_model,
            quota_pool=req.quota_pool,
            conversation_id=req.conversation,
            chat_session_id=req.chat_session_id,
            parent_idx=req.parent_idx,
            project_id=req.project_id,
            params=params,
        )
    return _reply_summary(reply, cid)


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    """SSE streaming: yields NDJSON events as they arrive (text chunks, tools, end).

    Response format: `data: <json>\n\n` per event. Client can parse incrementally.
    """
    client = await _runtime()
    slash = _slash(req.message, client)
    if slash is not None:
        return slash

    async def event_generator():
        # Build params dict
        params: dict[str, Any] | None = None
        if req.tool_proxy:
            params = {"tool_proxy": True}
        if req.tool_proxy_config:
            if params is None:
                params = {}
            params["tool_proxy_config"] = req.tool_proxy_config.model_dump()

        # Run the blocking send_stream in thread pool
        async with _shared["lock"]:
            reply, _cid = await asyncio.to_thread(
                _send_continued,
                client,
                req.message,
                approach_id=req.approach_id or _default_model,
                quota_pool=req.quota_pool,
                conversation_id=req.conversation,
                chat_session_id=req.chat_session_id,
                parent_idx=req.parent_idx,
                project_id=req.project_id,
                params=params,
            )
        # Yield each parsed event as SSE
        for ev in reply.lines:
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        # Final sentinel
        yield 'data: {"event": "done"}\n\n'

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@app.post("/response", response_model=None)
async def response(req: ChatRequest) -> dict[str, Any] | StreamingResponse:
    """Unified response endpoint for chat messages.

    Supports both streaming and non-streaming responses via the `stream` parameter.
    When stream=false (default), returns the complete reply as JSON.
    When stream=true, returns SSE events as they arrive.
    """
    client = await _runtime()
    slash = _slash(req.message, client)
    if slash is not None:
        return slash

    # Build params dict
    params: dict[str, Any] | None = None
    if req.tool_proxy:
        params = {"tool_proxy": True}
    if req.tool_proxy_config:
        if params is None:
            params = {}
        params["tool_proxy_config"] = req.tool_proxy_config.model_dump()

    if req.stream:
        # Streaming response
        async def event_generator():
            # Run the blocking send_stream in thread pool
            async with _shared["lock"]:
                reply, _cid = await asyncio.to_thread(
                    _send_continued,
                    client,
                    req.message,
                    approach_id=req.approach_id or _default_model,
                    quota_pool=req.quota_pool,
                    conversation_id=req.conversation,
                    chat_session_id=req.chat_session_id,
                    parent_idx=req.parent_idx,
                    project_id=req.project_id,
                    params=params,
                )
            # Yield each parsed event as SSE
            for ev in reply.lines:
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            # Final sentinel
            yield 'data: {"event": "done"}\n\n'

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )
    else:
        # Non-streaming response
        async with _shared["lock"]:
            reply, cid = await asyncio.to_thread(
                _send_continued,
                client,
                req.message,
                approach_id=req.approach_id or _default_model,
                quota_pool=req.quota_pool,
                conversation_id=req.conversation,
                chat_session_id=req.chat_session_id,
                parent_idx=req.parent_idx,
                project_id=req.project_id,
                params=params,
            )
        return _reply_summary(reply, cid)


# ---- OpenAI Responses API (/v1/responses) ----


def _extract_user_message(
    inp: str | list[dict[str, Any]],
) -> tuple[str, list[str]]:
    """Pull the plain-text user message and any image URLs out of `input`."""
    if isinstance(inp, str):
        return inp, []
    parts: list[str] = []
    image_urls: list[str] = []
    for msg in inp:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for c in content:
                    if isinstance(c, dict):
                        if c.get("type") == "input_text":
                            parts.append(c.get("text", ""))
                        elif c.get("type") == "input_image":
                            image_urls.append(c.get("url", "") or c.get("file_id", ""))
                        elif c.get("type") == "image_url":
                            url_obj = c.get("image_url", {})
                            image_urls.append(
                                url_obj.get("url", "")
                                if isinstance(url_obj, dict)
                                else ""
                            )
                    elif isinstance(c, str):
                        parts.append(c)
    return "\n".join(parts) if parts else "", image_urls


def _last_user_from_input(inp: str | list[dict[str, Any]]) -> tuple[str, list[str]]:
    """Only the latest user turn — earlier turns live on the CUHK session."""
    text, images = _extract_user_message(inp)
    if isinstance(inp, str) or not isinstance(inp, list):
        return text, images
    last_text = ""
    last_images: list[str] = []
    for msg in inp:
        if msg.get("role") != "user":
            continue
        piece, imgs = _extract_user_message([msg])
        if piece or imgs:
            last_text = piece
            last_images = imgs
    return last_text or text, last_images or images


def _format_responses_output(
    reply: ChatReply,
    model: str,
    *,
    resp_id: str | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Convert a ChatReply into the OpenAI Responses API JSON shape."""
    resp_id = resp_id or f"resp_{uuid.uuid4().hex[:24]}"
    now = int(time.time())
    output: list[dict[str, Any]] = []
    if reply.text:
        output.append(
            {
                "type": "message",
                "id": f"msg_{uuid.uuid4().hex[:24]}",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": reply.text, "annotations": []}
                ],
            }
        )
    return {
        "id": resp_id,
        "object": "response",
        "created_at": now,
        "status": "completed",
        "model": model,
        "output": output,
        "usage": {
            "input_tokens": reply.ctx_token_cnt or 0,
            "output_tokens": len(reply.text) // 4 if reply.text else 0,
            "total_tokens": (reply.ctx_token_cnt or 0)
            + (len(reply.text) // 4 if reply.text else 0),
        },
        "metadata": {
            "chat_session_id": reply.chat_session_id,
            "approach_msg_idx": reply.approach_msg_idx,
            "title": reply.title,
            "conversation": conversation_id,
        },
    }


def _format_responses_error(
    message: str, model: str, *, resp_id: str | None = None
) -> dict[str, Any]:
    """Return an OpenAI Responses API error response."""
    return {
        "id": resp_id or f"resp_{uuid.uuid4().hex[:24]}",
        "object": "response",
        "created_at": int(time.time()),
        "status": "failed",
        "model": model,
        "error": {
            "message": message,
            "type": "invalid_request_error",
            "code": "model_not_supported",
        },
        "output": [],
    }


@app.post("/v1/responses", response_model=None)
async def openai_responses(
    req: ResponsesRequest,
) -> dict[str, Any] | StreamingResponse:
    """OpenAI-compatible Responses API endpoint.

    Accepts the format used by Codex / CC Switch and translates it into
    the underlying CUHK chat API, then re-formats the reply to match
    the Responses API schema.
    """
    model = _resolve_model(req.model)
    user_msg, image_urls = _last_user_from_input(req.input)
    if not user_msg:
        raise HTTPException(400, "input must contain a user message")
    conv_id = req.conversation or req.user
    content = user_msg
    cid_probe, session_probe, _parent = _resolve_thread(
        conv_id, openai_id=req.previous_response_id
    )
    if req.instructions and not session_probe:
        content = f"{req.instructions.strip()}\n\n{user_msg}"

    client = await _runtime()

    # Check for image input — only vision-capable models can handle this
    _VISION_MODELS = {"claude-haiku-4-5", "gpt-5.6-luna"}
    if image_urls and model not in _VISION_MODELS:
        err_msg = (
            f"This model ({model}) does not support image input. "
            f"Use a vision-capable model: {', '.join(sorted(_VISION_MODELS))}"
        )
        resp_id = f"resp_{uuid.uuid4().hex[:24]}"
        if req.stream:

            async def error_gen():
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "type": "response.created",
                            "response": {
                                "id": resp_id,
                                "object": "response",
                                "status": "failed",
                                "model": model,
                                "output": [],
                            },
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "type": "response.completed",
                            "response": _format_responses_error(
                                err_msg, model, resp_id=resp_id
                            ),
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )

            return StreamingResponse(
                error_gen(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache"},
            )
        return _format_responses_error(err_msg, model, resp_id=resp_id)

    # Build params
    params: dict[str, Any] | None = None
    if req.tools:
        params = {"tool_proxy": True}

    if req.stream:

        async def responses_event_generator():
            resp_id = f"resp_{uuid.uuid4().hex[:24]}"
            now = int(time.time())
            # response.created
            yield (
                "data: "
                + json.dumps(
                    {
                        "type": "response.created",
                        "response": {
                            "id": resp_id,
                            "object": "response",
                            "created_at": now,
                            "status": "in_progress",
                            "model": model,
                            "output": [],
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n\n"
            )
            # response.in_progress
            yield (
                "data: "
                + json.dumps(
                    {
                        "type": "response.in_progress",
                        "response": {"id": resp_id, "status": "in_progress"},
                    },
                    ensure_ascii=False,
                )
                + "\n\n"
            )

            async with _shared["lock"]:
                reply, remembered = await asyncio.to_thread(
                    _send_continued,
                    client,
                    content,
                    approach_id=model,
                    params=params,
                    conversation_id=cid_probe or conv_id,
                    openai_id=req.previous_response_id,
                )
            if remembered:
                conversation.bind_openai_id(resp_id, remembered)

            # Emit text delta events
            if reply.text:
                msg_id = f"msg_{uuid.uuid4().hex[:24]}"
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "type": "response.output_item.added",
                            "output_index": 0,
                            "item": {
                                "type": "message",
                                "id": msg_id,
                                "role": "assistant",
                                "status": "in_progress",
                                "content": [],
                            },
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "type": "response.content_part.added",
                            "output_index": 0,
                            "content_index": 0,
                            "part": {"type": "output_text", "text": ""},
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "type": "response.content_part.delta",
                            "output_index": 0,
                            "content_index": 0,
                            "delta": reply.text,
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "type": "response.content_part.done",
                            "output_index": 0,
                            "content_index": 0,
                            "part": {
                                "type": "output_text",
                                "text": reply.text,
                                "annotations": [],
                            },
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "type": "response.output_item.done",
                            "output_index": 0,
                            "item": {
                                "type": "message",
                                "id": msg_id,
                                "role": "assistant",
                                "status": "completed",
                                "content": [
                                    {
                                        "type": "output_text",
                                        "text": reply.text,
                                        "annotations": [],
                                    }
                                ],
                            },
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )

            # response.completed
            completed = _format_responses_output(
                reply, model, resp_id=resp_id, conversation_id=remembered
            )
            yield (
                "data: "
                + json.dumps(
                    {"type": "response.completed", "response": completed},
                    ensure_ascii=False,
                )
                + "\n\n"
            )

        return StreamingResponse(
            responses_event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )
    else:
        async with _shared["lock"]:
            reply, conv_id = await asyncio.to_thread(
                _send_continued,
                client,
                content,
                approach_id=model,
                params=params,
                conversation_id=conv_id or cid_probe,
                openai_id=req.previous_response_id,
            )
        out = _format_responses_output(reply, model, conversation_id=conv_id)
        if conv_id:
            conversation.bind_openai_id(out["id"], conv_id)
        return out


@app.get("/v1/models")
async def openai_models() -> dict[str, Any]:
    """OpenAI-compatible model list endpoint."""
    client = await _runtime()
    available = await asyncio.to_thread(_available_models, client)
    # Advertise Cursor-friendly aliases first, then real CUHK approach_ids.
    names = list(dict.fromkeys([*_MODEL_ALIASES.keys(), *available]))
    models = []
    for name in names:
        models.append(
            {
                "id": name,
                "object": "model",
                "created": 0,
                "owned_by": "cuhk-shenzhen",
            }
        )
    return {"object": "list", "data": models}


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") in (
                "text",
                "input_text",
            ):
                parts.append(item.get("text", ""))
        return "\n".join(p for p in parts if p)
    return ""


def _latest_user_message(messages: list[dict[str, Any]]) -> str:
    latest = ""
    for msg in messages:
        role = str(msg.get("role") or "").strip().lower()
        if role != "user":
            continue
        text = _message_text(msg.get("content", "")).strip()
        if text:
            latest = text
    return latest


def _system_instructions(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for msg in messages:
        role = str(msg.get("role") or "").strip().lower()
        if role not in ("system", "developer"):
            continue
        text = _message_text(msg.get("content", "")).strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def _first_user_fingerprint(messages: list[dict[str, Any]]) -> str | None:
    for msg in messages:
        if str(msg.get("role") or "").strip().lower() != "user":
            continue
        text = _message_text(msg.get("content", "")).strip()
        if text:
            return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return None


def _has_assistant_history(messages: list[dict[str, Any]]) -> bool:
    return any(
        str(msg.get("role") or "").strip().lower() == "assistant" for msg in messages
    )


def _prepare_completion_turn(
    req: ChatCompletionsRequest, *, header_conversation: str | None = None
) -> tuple[str, str | None, str | None]:
    """Latest user turn + named thread. History stays on the CUHK session."""
    latest = _latest_user_message(req.messages)
    if not latest:
        return "", None, None
    fingerprint = _first_user_fingerprint(req.messages)
    conv_id = req.conversation or req.user or header_conversation
    if not conv_id and (
        _has_assistant_history(req.messages) or req.previous_response_id
    ):
        conv_id = f"fp-{fingerprint}" if fingerprint else None
    cid, session, _parent = _resolve_thread(
        conv_id, fingerprint=fingerprint, openai_id=req.previous_response_id
    )
    content = latest
    if not session:
        system = _system_instructions(req.messages)
        if system:
            content = f"{system}\n\n{latest}"
    return content, cid or conv_id, fingerprint


def _format_chat_completion(
    reply: ChatReply, model: str, *, conversation_id: str | None = None
) -> dict[str, Any]:
    created = int(time.time())
    out = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": reply.text or ""},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": reply.ctx_token_cnt or 0,
            "completion_tokens": len(reply.text) // 4 if reply.text else 0,
            "total_tokens": (reply.ctx_token_cnt or 0)
            + (len(reply.text) // 4 if reply.text else 0),
        },
        "conversation": conversation_id,
    }
    return out


@app.post("/v1/chat/completions", response_model=None)
async def openai_chat_completions(
    req: ChatCompletionsRequest,
    request: Request,
) -> dict[str, Any] | StreamingResponse:
    """OpenAI-compatible Chat Completions; continues a named CUHK thread when possible."""
    model = _resolve_model(req.model)
    header_conv = request.headers.get("x-conversation-id")
    user_msg, conv_id, fingerprint = _prepare_completion_turn(
        req, header_conversation=header_conv
    )
    if not user_msg:
        raise HTTPException(400, "messages must contain a user message")

    client = await _runtime()
    params: dict[str, Any] | None = {"tool_proxy": True} if req.tools else None

    if req.stream:
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())

        async def chat_event_generator():
            async with _shared["lock"]:
                reply, cid = await asyncio.to_thread(
                    _send_continued,
                    client,
                    user_msg,
                    approach_id=model,
                    params=params,
                    conversation_id=conv_id,
                    fingerprint=fingerprint,
                    openai_id=req.previous_response_id,
                )
            if cid:
                conversation.bind_openai_id(completion_id, cid)
            yield (
                "data: "
                + json.dumps(
                    {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": ""},
                                "finish_reason": None,
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
                + "\n\n"
            )
            if reply.text:
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": reply.text},
                                    "finish_reason": None,
                                }
                            ],
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
            yield (
                "data: "
                + json.dumps(
                    {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    },
                    ensure_ascii=False,
                )
                + "\n\n"
            )
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            chat_event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    async with _shared["lock"]:
        reply, cid = await asyncio.to_thread(
            _send_continued,
            client,
            user_msg,
            approach_id=model,
            params=params,
            conversation_id=conv_id,
            fingerprint=fingerprint,
            openai_id=req.previous_response_id,
        )
    out = _format_chat_completion(reply, model, conversation_id=cid)
    if cid:
        conversation.bind_openai_id(out["id"], cid)
    return out


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


@app.get("/conversations")
async def list_conversations() -> list[dict[str, Any]]:
    """Named local threads (conversation id → CUHK session + parent_idx)."""
    return conversation.list_threads()


@app.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str) -> dict[str, str]:
    """Drop a named thread so the next message starts a fresh CUHK chat."""
    if not conversation.delete(conversation_id):
        raise HTTPException(404, f"unknown conversation {conversation_id!r}")
    return {"status": "deleted", "conversation": conversation_id}


@app.post("/jobs")
async def create_job(req: BatchJobRequest) -> dict[str, Any]:
    """Enqueue many chat turns. Returns immediately; poll GET /jobs/{id}.

    mode=independent: each item is its own thread (bulk Q&A).
    mode=thread: items run in order on one `conversation` (long task steps).
    pause_seconds sleeps between items (default 20s, ~3 campus turns/min).
    """
    try:
        job = jobs.create(
            [item.model_dump() for item in req.items],
            mode=req.mode,
            conversation=req.conversation,
            approach_id=req.approach_id or _default_model,
            pause_seconds=req.pause_seconds,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return job


@app.get("/jobs")
async def list_job_summaries(limit: int = 30) -> list[dict[str, Any]]:
    return jobs.list_jobs(limit=limit)


@app.get("/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, f"unknown job {job_id!r}")
    return job


@app.delete("/jobs/{job_id}")
async def cancel_job(job_id: str) -> dict[str, Any]:
    job = jobs.cancel(job_id)
    if not job:
        raise HTTPException(404, f"unknown job {job_id!r}")
    return job


# ---- Tool Proxy Endpoints ----


@app.get("/tools")
async def list_tools() -> list[dict[str, Any]]:
    """List all registered tools."""
    proxy = get_default_proxy()
    definitions = proxy.registry.get_definitions()
    return [defn.model_dump() for defn in definitions]


@app.post("/tools")
async def register_tool_endpoint(registration: ToolRegistration) -> dict[str, Any]:
    """Register a new tool with the proxy.

    The tool will be available for execution via /tools/call.
    """
    proxy = get_default_proxy()
    if proxy.registry.has_tool(registration.name):
        raise HTTPException(400, f"Tool '{registration.name}' is already registered")

    # Create a simple function handler
    from .tool_proxy import ToolHandler, ToolParameterProperty, ToolParameters

    parameters = ToolParameters()
    if registration.parameters:
        for prop_name, prop_def in registration.parameters.items():
            if isinstance(prop_def, dict):
                parameters.properties[prop_name] = ToolParameterProperty(
                    type=prop_def.get("type", "string"),
                    description=prop_def.get("description"),
                    enum=prop_def.get("enum"),
                )
                if prop_def.get("required"):
                    parameters.required.append(prop_name)

    definition = ToolDefinition(
        name=registration.name,
        description=registration.description,
        parameters=parameters,
    )

    class DynamicToolHandler(ToolHandler):
        def execute(self, arguments: dict[str, Any]) -> str:
            # This is a placeholder - in real usage, you'd implement the actual tool logic
            return f"Tool '{registration.name}' executed with arguments: {arguments}"

        def get_definition(self) -> ToolDefinition:
            return definition

    proxy.registry.register(registration.name, DynamicToolHandler())
    return {"status": "registered", "name": registration.name}


@app.get("/tools/log")
async def get_tool_log() -> list[dict[str, Any]]:
    """Get the tool execution log."""
    proxy = get_default_proxy()
    return proxy.get_execution_log()


@app.delete("/tools/log")
async def clear_tool_log() -> dict[str, Any]:
    """Clear the tool execution log."""
    proxy = get_default_proxy()
    proxy.clear_execution_log()
    return {"status": "cleared"}


# Registered after the literal /tools/log routes: FastAPI matches in
# declaration order, so a parameterised /tools/{tool_name} first would swallow
# DELETE /tools/log as tool_name="log".
@app.delete("/tools/{tool_name}")
async def unregister_tool_endpoint(tool_name: str) -> dict[str, Any]:
    """Unregister a tool from the proxy."""
    proxy = get_default_proxy()
    if not proxy.registry.has_tool(tool_name):
        raise HTTPException(404, f"Tool '{tool_name}' not found")
    proxy.registry.unregister(tool_name)
    return {"status": "unregistered", "name": tool_name}


@app.post("/tools/call")
async def call_tool(req: ToolCallRequest) -> dict[str, Any]:
    """Execute a tool call."""
    proxy = get_default_proxy()
    if not proxy.registry.has_tool(req.tool_name):
        raise HTTPException(404, f"Tool '{req.tool_name}' not found")

    tool_call = ToolCall(
        id=f"call_{hash(req.tool_name) % 10000}",
        function={"name": req.tool_name, "arguments": json.dumps(req.arguments)},
    )
    result = proxy.execute_tool_call(tool_call)
    return {
        "tool_call_id": result.tool_call_id,
        "content": result.content,
        "status": "success",
    }
