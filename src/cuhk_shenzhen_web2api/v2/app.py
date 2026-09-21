"""Isolated authenticated API. Importing this module does not open any session."""

import asyncio
import hmac
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from . import wire
from .config import Settings
from .engine import Engine
from .metrics import durations
from .recovery import drained, replace_auth
from .store import IdempotencyConflict, QueueFull, Store
from .transport import TransportError


class Chat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=200000)
    approach_id: str | None = None
    quota_pool: str = "Students Pool"
    chat_session_id: str | None = None
    parent_idx: int = -1
    conversation: str | None = None
    project_id: str | None = None
    tool_proxy: bool = False
    stream: bool = False
    params: dict[str, Any] = Field(default_factory=dict)
    image_ids: list[str] = Field(default_factory=list)
    file_ids: list[str] = Field(default_factory=list)


class BatchItem(Chat):
    id: str | None = None


class Batch(BaseModel):
    items: list[BatchItem] = Field(min_length=1, max_length=1000)
    mode: str = Field(default="independent", pattern="^(independent|thread)$")
    conversation: str | None = None
    approach_id: str | None = None
    pause_seconds: float = Field(default=20.0, ge=0, le=120)


def job_wire(job: dict) -> dict:
    value = dict(job)
    value["status"] = {"done": "completed", "error": "failed"}.get(
        job["status"], job["status"]
    )
    value["items"] = []
    for record in job.get("items", []):
        payload = record.get("payload") or {}
        result = record.get("result") or {}
        value["items"].append(
            {
                **payload,
                **result,
                "id": payload.get("id") or record["id"],
                "request_id": record["id"],
                "index": record.get("index"),
                "status": record["status"],
                "error": record.get("error"),
            }
        )
    return value


def create_app(
    settings: Settings, transport: Any, store: Store | None = None
) -> FastAPI:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    database = store or Store(settings.data_dir / "runtime.sqlite3")
    engine = Engine(
        database,
        transport,
        concurrency=settings.concurrency,
        interval=settings.interval,
    )
    model = settings.model
    auth_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        await engine.start()
        try:
            yield
        finally:
            await engine.close()
            database.close()

    app = FastAPI(title="Campus API v2", lifespan=lifespan)
    app.state.engine = engine
    app.state.store = database

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        if request.url.path != "/health":
            token = request.headers.get("authorization", "")
            supplied = (
                token[7:]
                if token.lower().startswith("bearer ")
                else request.headers.get("x-api-key", "")
            )
            if not hmac.compare_digest(supplied.encode(), settings.api_key.encode()):
                return JSONResponse({"error": "Invalid API key"}, status_code=401)
        return await call_next(request)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse(
            {"error": str(exc)},
            status_code=409 if "idempoten" in str(exc).lower() else 400,
        )

    @app.exception_handler(IdempotencyConflict)
    async def conflict(request, exc):
        return JSONResponse(
            {"error": "Idempotency-Key conflicts with previous input"}, status_code=409
        )

    @app.exception_handler(QueueFull)
    async def queue_full(request, exc):
        return JSONResponse({"error": "Queue capacity exceeded"}, status_code=429)

    @app.exception_handler(TransportError)
    async def upstream_error(request, exc):
        return JSONResponse({"error": exc.code}, status_code=503)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/ready")
    async def ready():
        state = engine.transport.state
        return JSONResponse(
            {
                "backend": settings.backend,
                "authentication": state,
                "streaming": engine.transport.streaming,
                "stream_mode": getattr(
                    engine.transport,
                    "stream_mode",
                    "incremental" if engine.transport.streaming else "buffered",
                ),
                "maintenance": engine.maintenance,
                "pending": database.pending_count(),
                "concurrency": settings.concurrency,
                "interval": settings.interval,
            },
            status_code=200
            if state == "authenticated" and not engine.paused and not engine.maintenance
            else 503,
        )

    @app.post("/auth/check")
    async def check_auth():
        async with auth_lock, drained(engine):
            await engine.transport.probe()
            if engine.transport.state == "authenticated":
                engine.resume()
        return await ready()

    @app.post("/auth/reload")
    async def reload_auth(body: dict):
        name = body.get("directory")
        if not isinstance(name, str):
            raise HTTPException(400, "directory name required")
        async with auth_lock:
            try:
                await replace_auth(engine, settings, name)
            except (OSError, TimeoutError):
                raise HTTPException(
                    409,
                    "Authentication directory unavailable or active requests did not drain",
                ) from None
        return await ready()

    @app.post("/requests/{rid}/reconcile")
    async def reconcile(rid: str):
        async with auth_lock, drained(engine):
            record = database.get_request(rid)
            if record is None:
                raise HTTPException(404, "Request not found")
            sid = (record.get("result") or {}).get("chat_session_id") or record.get(
                "chat_session_id"
            )
            if record["status"] != "unknown" or not sid:
                return {
                    "status": record["status"],
                    "reason": "not_unknown_or_missing_session",
                }
            history = await engine.transport.request(
                "/getHistoryItem/", {"chat_session_id": sid}
            )
            return database.reconcile_request(rid, history)

    async def models():
        async with auth_lock:
            result = await engine.transport.request(
                "/config/?quota_pool=Students%20Pool"
            )
        return result.get("body", result).get("availableModels", [])

    @app.get("/model")
    async def model_info():
        return {"current": model, "available": await models()}

    @app.get("/v1/models")
    async def model_list():
        return {
            "object": "list",
            "data": [
                {"id": m, "object": "model", "owned_by": "campus"}
                for m in await models()
            ],
        }

    @app.post("/model")
    async def switch_model(body: dict):
        nonlocal model
        candidate = body.get("approach_id")
        if candidate not in await models():
            raise HTTPException(400, "Model not present in current campus catalog")
        model = candidate
        return {"current": model}

    async def execute(body: Chat, request: Request, style: str = "native"):
        if engine.transport.state != "authenticated":
            raise HTTPException(
                503, "Authentication required; complete diagnosis and POST /auth/check"
            )
        if body.tool_proxy:
            raise HTTPException(400, "Remote tool execution is disabled")
        payload = body.model_dump(exclude={"stream"})
        payload["approach_id"] = body.approach_id or model
        if not payload["approach_id"]:
            raise HTTPException(400, "Select an approach_id from /v1/models")
        key = request.headers.get("idempotency-key")
        if key:
            key = style + ":" + key
        record = await engine.submit(payload, key=key)
        rid = record["id"]
        if body.stream:
            return StreamingResponse(
                stream(rid, payload["approach_id"], style),
                media_type="text/event-stream",
                headers={
                    "X-Request-Id": rid,
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )
        record = await engine.result(rid)
        if record["status"] != "done":
            return JSONResponse(
                {
                    "request_id": rid,
                    "status": record["status"],
                    "error": record.get("error"),
                },
                status_code=502,
            )
        result = record.get("result") or {}
        if style == "completion":
            return wire.completion(result, rid, payload["approach_id"])
        if style == "response":
            return wire.response(result, rid, payload["approach_id"])
        return {**result, "request_id": rid}

    async def stream(rid, selected_model, style):
        complete = False
        try:
            if style == "completion":
                yield wire.frame(wire.chunk(rid, selected_model, {"role": "assistant"}))
            if style == "response":
                yield wire.frame(
                    {
                        "type": "response.created",
                        "response": {
                            "id": "resp_" + rid,
                            "status": "in_progress",
                            "output": [],
                        },
                    },
                    "response.created",
                )
            async for event in engine.events(rid):
                delta = wire.text_delta(event)
                if style == "native":
                    yield wire.frame(event)
                elif delta is not None:
                    if style == "completion":
                        yield wire.frame(
                            wire.chunk(rid, selected_model, {"content": delta})
                        )
                    else:
                        yield wire.frame(
                            {
                                "type": "response.output_text.delta",
                                "item_id": "msg_" + rid,
                                "output_index": 0,
                                "content_index": 0,
                                "delta": delta,
                            },
                            "response.output_text.delta",
                        )
            record = await engine.result(rid)
            complete = True
            if record["status"] != "done":
                yield wire.frame(
                    {
                        "error": {"message": record.get("error") or record["status"]},
                        "request_id": rid,
                    }
                )
                return
            if style == "completion":
                yield wire.frame(wire.chunk(rid, selected_model, {}, "stop"))
                yield wire.frame("[DONE]")
            elif style == "response":
                yield wire.frame(
                    {
                        "type": "response.completed",
                        "response": wire.response(
                            record.get("result") or {}, rid, selected_model
                        ),
                    },
                    "response.completed",
                )
            else:
                yield wire.frame({"event": "done"})
        finally:
            if not complete:
                await engine.disconnect(rid)

    @app.post("/chat")
    @app.post("/response")
    async def chat(body: Chat, request: Request):
        return await execute(body, request)

    @app.post("/chat/stream")
    async def chat_stream(body: Chat, request: Request):
        return await execute(body.model_copy(update={"stream": True}), request)

    @app.post("/v1/chat/completions")
    @app.post("/v1/responses")
    async def compatible(body: dict, request: Request):
        if body.get("tools"):
            raise HTTPException(400, "Tool protocol not enabled in v2")
        style = "response" if request.url.path.endswith("responses") else "completion"
        cid = body.get("conversation") or request.headers.get("x-conversation-id")
        previous = body.get("previous_response_id")
        if previous:
            old = database.get_request(str(previous).removeprefix("resp_"))
            if not old or old["status"] != "done":
                raise HTTPException(409, "Previous response unavailable")
            cid = (old.get("result") or {}).get("conversation")
        text = wire.latest_text(
            body.get("input", "") if style == "response" else body.get("messages", [])
        )
        if body.get("instructions"):
            text = str(body["instructions"]) + "\n\n" + text
        params = {
            k: body[k]
            for k in ("temperature", "max_tokens", "max_output_tokens")
            if k in body
        }
        return await execute(
            Chat(
                message=text,
                approach_id=body.get("model"),
                conversation=cid,
                stream=bool(body.get("stream", False)),
                params=params,
            ),
            request,
            style,
        )

    @app.post("/jobs")
    async def create_job(body: Batch, request: Request):
        items = [item.model_dump(exclude={"stream"}) for item in body.items]
        for item in items:
            item["approach_id"] = item.get("approach_id") or body.approach_id or model
            if not item["approach_id"] or item.get("tool_proxy"):
                raise HTTPException(400, "Model required; remote tools disabled")
        return job_wire(
            database.create_job(
                items,
                mode=body.mode,
                conversation=body.conversation,
                approach_id=body.approach_id or model,
                pause_seconds=body.pause_seconds,
                key=request.headers.get("idempotency-key"),
            )
        )

    @app.get("/jobs")
    async def list_jobs():
        return [job_wire(job) for job in database.list_jobs() if job is not None]

    @app.get("/jobs/{job_id}")
    async def get_job(job_id: str):
        result = database.get_job(job_id)
        if result is None:
            raise HTTPException(404, "Job not found")
        return job_wire(result)

    @app.delete("/jobs/{job_id}")
    async def cancel_job(job_id: str):
        result = database.cancel_job(job_id)
        if result is None:
            raise HTTPException(404, "Job not found")
        return job_wire(result)

    @app.get("/requests/{rid}")
    async def get_request(rid: str):
        result = database.get_request(rid)
        if result is None:
            raise HTTPException(404, "Request not found")
        return {**result, "timings": durations(result)}

    @app.get("/conversations")
    async def conversations():
        return database.list_conversations()

    @app.get("/sessions")
    async def sessions(limit: int = 30):
        if not 1 <= limit <= 100:
            raise HTTPException(400, "limit must be 1..100")
        async with auth_lock:
            result = await engine.transport.request(
                "/getNextHistoryMeta/", {"need": limit}
            )
        return result.get("body", result)

    @app.get("/sessions/{sid}")
    async def history(sid: str):
        async with auth_lock:
            return await engine.transport.request(
                "/getHistoryItem/", {"chat_session_id": sid}
            )

    return app
