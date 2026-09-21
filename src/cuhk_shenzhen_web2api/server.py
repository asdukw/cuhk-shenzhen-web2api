"""Local HTTP facade (web2api) over the CUHK AI chat.

A FastAPI app that owns one shared browser session and exposes
the chat as a JSON API. Every request goes through the live page context
(cookies + CSRF + IP-bound aTrust session), so the underlying service considers
it a normal browser conversation.

The browser backend is Firecrawl Cloud or local Steel, selected by
`BROWSER_BACKEND` (see `browser_provider`).

Concurrency: the shared browser + the cloud free tier's ~3 req/min limit mean
calls must be serialized. A module-level async lock guards all ChatClient work.

The default model can be switched at runtime (POST /model); a `message` that
starts with /model is handled as a slash command instead of being sent to the
model, e.g. `/model glm-5.3-flash`.

Run with:
    python src/cuhk_shenzhen_web2api/scripts/server.py   (uvicorn, 127.0.0.1:8765)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

log = logging.getLogger("web2api")

from . import browser_provider, env, firecrawl_provider, login
from .chat_client import ChatClient, ChatReply
from .tool_proxy import (
    ToolCall,
    ToolDefinition,
    get_default_proxy,
)

_shared: dict[str, Any] = {
    "cb": None,
    "client": None,
    "settings": None,
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
    session_id: str
    url: str
    user: Any = None
    firecrawl: dict[str, Any] | None = None
    browser: dict[str, Any] | None = None


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

# Model alias map: CC Switch / Codex may send names we don't have — map them.
_MODEL_ALIASES: dict[str, str] = {
    "gpt-5.6-sol": "gpt-5.6-luna",
}


def _resolve_model(name: str | None) -> str:
    """Resolve an incoming model name to one we actually support."""
    if not name:
        return _default_model
    return _MODEL_ALIASES.get(name, name)


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


def _ensure_runtime() -> ChatClient:
    """Lazily open/resume the browser session and log in (first call only)."""
    if _shared["client"] is not None:
        return _shared["client"]
    config = env.load_env()
    firecrawl_settings = firecrawl_provider.resolve_settings(config)
    browser_settings = browser_provider.resolve_settings(config)
    log.info("firecrawl backend: %s", firecrawl_settings.describe())
    log.info("browser backend: %s", browser_settings.describe())
    cb = browser_provider.open_browser_session(
        browser_settings, browser_provider.load_session_id(browser_settings)
    )
    final = login.ensure_on_chat(
        cb, env.chat_username(config), env.chat_password(config)
    )
    if "/chat" not in (final or ""):
        raise RuntimeError(f"login did not reach /chat/: {final}")
    browser_provider.save_session_id(browser_settings, cb.sid)
    client = ChatClient(cb)
    _shared["cb"] = cb
    _shared["client"] = client
    _shared["settings"] = {
        "firecrawl": firecrawl_settings,
        "browser": browser_settings,
    }
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


@app.middleware("http")
async def log_requests(request: Request, call_next):
    body_bytes = await request.body()
    body_preview = (
        body_bytes[:500].decode("utf-8", errors="replace") if body_bytes else ""
    )
    log.info(
        ">>> %s %s [%s] body=%s",
        request.method,
        request.url.path,
        request.client.host if request.client else "?",
        body_preview,
    )
    response = await call_next(request)
    log.info("<<< %s %s -> %d", request.method, request.url.path, response.status_code)
    return response


@app.get("/health")
async def health() -> Health:
    try:
        client = await _runtime()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    cb = _shared["cb"]
    settings = _shared["settings"] or {}
    firecrawl_settings = settings.get("firecrawl")
    browser_settings = settings.get("browser")
    return Health(
        session_id=cb.sid,
        url=cb.url(),
        user=client.whoami().get("body"),
        firecrawl=firecrawl_settings.describe()
        if firecrawl_settings is not None
        else None,
        browser=browser_settings.describe() if browser_settings is not None else None,
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
        # Build params dict
        params: dict[str, Any] | None = None
        if req.tool_proxy:
            params = {"tool_proxy": True}
        if req.tool_proxy_config:
            if params is None:
                params = {}
            params["tool_proxy_config"] = req.tool_proxy_config.model_dump()

        reply = await asyncio.to_thread(
            client.send_stream,
            req.message,
            approach_id=req.approach_id or _default_model,
            quota_pool=req.quota_pool,
            chat_session_id=req.chat_session_id,
            parent_idx=req.parent_idx,
            project_id=req.project_id,
            params=params,
        )
    return _reply_summary(reply)


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
            reply = await asyncio.to_thread(
                client.send_stream,
                req.message,
                approach_id=req.approach_id or _default_model,
                quota_pool=req.quota_pool,
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
                reply = await asyncio.to_thread(
                    client.send_stream,
                    req.message,
                    approach_id=req.approach_id or _default_model,
                    quota_pool=req.quota_pool,
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
            reply = await asyncio.to_thread(
                client.send_stream,
                req.message,
                approach_id=req.approach_id or _default_model,
                quota_pool=req.quota_pool,
                chat_session_id=req.chat_session_id,
                parent_idx=req.parent_idx,
                project_id=req.project_id,
                params=params,
            )
        return _reply_summary(reply)


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


def _format_responses_output(reply: ChatReply, model: str) -> dict[str, Any]:
    """Convert a ChatReply into the OpenAI Responses API JSON shape."""
    resp_id = f"resp_{uuid.uuid4().hex[:24]}"
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
    user_msg, image_urls = _extract_user_message(req.input)
    if not user_msg:
        raise HTTPException(400, "input must contain a user message")

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
                reply = await asyncio.to_thread(
                    client.send_stream,
                    user_msg,
                    approach_id=model,
                    params=params,
                )

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
            completed = _format_responses_output(reply, model)
            completed["id"] = resp_id
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
            reply = await asyncio.to_thread(
                client.send_stream,
                user_msg,
                approach_id=model,
                params=params,
            )
        return _format_responses_output(reply, model)


@app.get("/v1/models")
async def openai_models() -> dict[str, Any]:
    """OpenAI-compatible model list endpoint."""
    client = await _runtime()
    available = await asyncio.to_thread(_available_models, client)
    models = []
    for name in available:
        models.append(
            {
                "id": name,
                "object": "model",
                "created": 0,
                "owned_by": "cuhk-shenzhen",
            }
        )
    return {"object": "list", "data": models}


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
