"""Loopback-only experimental API. No filesystem or command-execution endpoint."""

import asyncio
import hashlib
import hmac
import json
import re
import time
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from jsonschema.exceptions import SchemaError
from pydantic import ValidationError

from ..v2.engine import FINAL, Engine
from ..v2.recovery import drained, replace_auth
from ..v2.store import IdempotencyConflict, QueueFull, Store
from ..v2.transport import TransportError, build_transport
from ..v2.wire import frame
from .protocol import (
    MAX_BODY,
    MODEL,
    CompletionRequest,
    chunks,
    input_composition,
    loads,
    prompt,
    reply,
)
from .runtime import TextTransport

_REJECT_CODE = re.compile(r"[a-z0-9_]{1,64}")

# Advisory (never enforced): flag requests whose encoded transcript is already
# close to the hard body cap so an operator can compact before the next turn
# 413s. Derived from MAX_BODY so the hint tracks the cap; not a stored field.
SOFT_SIZE_LIMIT = int(MAX_BODY * 0.85)


class UpstreamNotDone(Exception):
    """The request settled but the upstream never produced a validated answer."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class ActionRejected(Exception):
    """Upstream answered, but the model action failed local validation."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _reject_code(exc):
    if isinstance(exc, ValueError) and exc.args and isinstance(exc.args[0], str):
        token = exc.args[0]
        if _REJECT_CODE.fullmatch(token):
            return token
    return "request_rejected"


def _error_payload(message, code, rid):
    return {"error": {"message": message, "code": code, "request_id": rid}}


async def wrapped_transport(backend, data_dir, config):
    """Auth-refreshed transport that keeps the campus-event safety filter."""
    return TextTransport(await build_transport(backend, data_dir, config))


def create_app(settings, transport, database=None):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = database or Store(settings.data_dir / "runtime.sqlite3", queue_limit=16)
    engine = Engine(store, transport, concurrency=1, interval=settings.interval)
    auth_lock = asyncio.Lock()
    # Identity is updated only when a probe/handover actually confirms auth, so
    # a cached transport state never masquerades as a live verification.
    identity: dict[str, float | str | None] = {
        "last_verified_at": None,
        "last_error": None,
    }

    @asynccontextmanager
    async def lifespan(app):
        try:
            await engine.start()
        except Exception:
            store.close()
            raise
        try:
            yield
        finally:
            await engine.close()
            store.close()

    app = FastAPI(title="Experimental Campus Kilo Bridge", lifespan=lifespan)
    app.state.engine = engine

    @app.middleware("http")
    async def authenticate(request, call_next):
        if request.url.path != "/health":
            supplied = request.headers.get("authorization", "")
            expected = "Bearer " + settings.api_key
            if not hmac.compare_digest(supplied.encode(), expected.encode()):
                return JSONResponse(
                    {"error": {"message": "Unauthorized"}}, status_code=401
                )
        return await call_next(request)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/ready")
    async def ready():
        state = engine.transport.state
        unhealthy = state != "authenticated" or engine.paused
        if engine.maintenance:
            status = "recovering"
        elif unhealthy and identity["last_error"]:
            status = "failed"
        elif unhealthy:
            status = "waiting_login"
        else:
            status = "ready"
        return JSONResponse(
            {
                "status": status,
                "authentication": state,
                "maintenance": engine.maintenance,
                "identity_last_verified_at": identity["last_verified_at"],
                "model": MODEL,
                "experimental": True,
                "streaming": False,
                "output_mode": "validated_buffered_sse",
                "pending": store.pending_count(),
                "concurrency": 1,
                "interval": settings.interval,
                "upstream_tools_cannot_be_disabled_reliably": True,
            },
            status_code=200 if status == "ready" else 503,
        )

    @app.post("/auth/check")
    async def check_auth():
        async with auth_lock, drained(engine):
            try:
                await engine.transport.probe()
                confirmed = engine.transport.state == "authenticated"
            except (TransportError, OSError, TimeoutError, ValueError):
                # A real expired session raises instead of returning a state
                # string; report it through /ready as failed, not a bare 500.
                confirmed = False
                identity["last_error"] = "probe_failed"
            if confirmed:
                engine.resume()
                identity["last_verified_at"] = time.time()
                identity["last_error"] = None
        return await ready()

    @app.post("/auth/reload")
    async def reload_auth(body: dict):
        name = body.get("directory")
        if not isinstance(name, str):
            raise HTTPException(400, "directory name required")
        async with auth_lock:
            try:
                await replace_auth(engine, settings, name, factory=wrapped_transport)
            except (OSError, TimeoutError, TransportError, ValueError):
                # auth_directory rejects bad names with ValueError and a stale
                # session makes candidate.authenticate raise TransportError;
                # neither should escape as a bare 500.
                identity["last_error"] = "reload_failed"
                raise HTTPException(
                    409,
                    "Authentication directory unavailable or active requests did not drain",
                ) from None
        if engine.transport.state == "authenticated":
            identity["last_verified_at"] = time.time()
            identity["last_error"] = None
        return await ready()

    @app.post("/requests/{rid}/reconcile")
    async def reconcile(rid: str):
        async with auth_lock, drained(engine):
            record = store.get_request(rid)
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
            try:
                history = await engine.transport.request(
                    "/getHistoryItem/", {"chat_session_id": sid}
                )
            except (TransportError, OSError, TimeoutError):
                raise HTTPException(
                    503, "Campus history unavailable; retry after /auth/check"
                ) from None
            return store.reconcile_request(rid, history)

    @app.get("/v1/models")
    async def models():
        return {
            "object": "list",
            "data": [{"id": MODEL, "object": "model", "owned_by": "campus"}],
        }

    @app.get("/requests/{rid}")
    async def status(rid: str):
        record = store.get_request(rid)
        if record is None:
            raise HTTPException(404, "Request not found")
        # Do not expose transcripts via routine status queries.
        created = record.get("created_at")
        sent = record.get("sent_at")
        finished = record.get("finished_at")

        def elapsed_ms(start, end):
            if start is None or end is None or end < start:
                return None
            return int((end - start) * 1000)

        encoded = record.get("encoded_bytes")
        return {
            "id": rid,
            "upstream_status": record["status"],
            "error": record.get("error"),
            "action_validation": record.get("action_validation"),
            "diagnostics": {
                "upstream_http_status": record.get("upstream_http_status"),
                "known_not_submitted": record.get("known_not_submitted"),
                "upstream_received": record.get("upstream_received"),
                "encoded_bytes": encoded,
                "approaching_size_limit": (encoded or 0) >= SOFT_SIZE_LIMIT,
                "input_composition": record.get("input_composition"),
                "attempts": record.get("attempts"),
                "elapsed_ms": {
                    "queue": elapsed_ms(created, sent),
                    "run": elapsed_ms(sent, finished),
                    "total": elapsed_ms(created, finished),
                },
            },
        }

    def note_action_failure(rid, code):
        with suppress(Exception):
            store.annotate_request(
                rid, action_validation=code, action_failed_at=time.time()
            )

    def completion(record, body):
        if record["status"] != "done":
            raise UpstreamNotDone(record.get("error") or "upstream_not_done")
        try:
            return reply(
                (record.get("result") or {}).get("text", ""),
                body,
                record["id"],
                int(record["created_at"]),
            )
        except (ValueError, TypeError, RecursionError) as exc:
            raise ActionRejected(_reject_code(exc)) from None

    async def wait_result(request, rid):
        while True:
            status = store.request_status(rid)
            if status is None:
                raise HTTPException(404, "Request no longer available")
            if status in FINAL:
                record = store.get_request(rid)
                if record is None:
                    raise HTTPException(404, "Request no longer available")
                return record
            if await request.is_disconnected():
                await engine.disconnect(rid)
                raise HTTPException(
                    499, "Client disconnected; do not automatically replay"
                )
            await asyncio.sleep(0.05)

    async def stream(rid, body):
        complete = False
        try:
            yield ": queued; actions are buffered until validated\n\n"
            while True:
                status = store.request_status(rid)
                if status is None:
                    raise HTTPException(404, "Request no longer available")
                if status in FINAL:
                    record = store.get_request(rid)
                    if record is None:
                        raise HTTPException(404, "Request no longer available")
                    complete = True
                    break
                await asyncio.sleep(1)
                yield ": waiting\n\n"
            try:
                result = completion(record, body)
            except ActionRejected as exc:
                note_action_failure(rid, exc.code)
                yield frame(
                    _error_payload(
                        "Upstream or action validation failed; inspect request status",
                        exc.code,
                        rid,
                    )
                )
                return
            except UpstreamNotDone as exc:
                yield frame(
                    _error_payload(
                        "Upstream or action validation failed; inspect request status",
                        exc.code,
                        rid,
                    )
                )
                return
            for chunk in chunks(result):
                if body.stream_options and body.stream_options.include_usage:
                    chunk["usage"] = (
                        None  # Campus token usage is not OpenAI billing usage.
                    )
                yield frame(chunk)
            yield frame("[DONE]")
        finally:
            if not complete:
                await asyncio.shield(engine.disconnect(rid))

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_BODY:
                raise HTTPException(
                    413,
                    {
                        "message": "Transcript too large; nothing submitted",
                        "code": "request_body_too_large",
                    },
                )
        try:
            body = CompletionRequest.model_validate(
                loads(raw.decode("utf-8"))
            ).checked()
            encoded = prompt(body)
        except (
            ValueError,
            TypeError,
            SchemaError,
            ValidationError,
            RecursionError,
        ) as exc:
            raise HTTPException(
                400,
                {
                    "message": "Unsupported messages, schema or request fields; nothing submitted",
                    "code": _reject_code(exc),
                },
            ) from None
        if len(encoded.encode()) > MAX_BODY:
            raise HTTPException(
                413,
                {
                    "message": "Encoded transcript too large; nothing submitted",
                    "code": "encoded_transcript_too_large",
                },
            )
        if engine.transport.state != "authenticated" or engine.paused:
            raise HTTPException(
                503, "Authentication unavailable; no automatic recovery"
            )
        canonical = json.dumps(
            body.model_dump(exclude={"stream", "stream_options"}),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
        supplied_key = request.headers.get("idempotency-key")
        if supplied_key is not None and (
            not supplied_key.strip() or len(supplied_key) > 256
        ):
            raise HTTPException(400, "Invalid idempotency key")
        key = (
            ("explicit:" + supplied_key)
            if supplied_key
            else "body:" + hashlib.sha256(canonical.encode()).hexdigest()
        )
        payload = {
            "message": encoded,
            "approach_id": MODEL,
            "params": {"tool_proxy": False, "max_tokens": body.max_tokens},
            "quota_pool": "Students Pool",
            "bridge_request": canonical,
        }
        for field in ("temperature", "top_p"):
            if (value := getattr(body, field)) is not None:
                payload["params"][field] = value
        try:
            record = await engine.submit(payload, key)
        except QueueFull:
            raise HTTPException(429, "Local queue full; nothing submitted") from None
        except (IdempotencyConflict, ValueError):
            raise HTTPException(
                409, "Conflicting or active duplicate request; no resubmission"
            ) from None
        rid = record["id"]
        if not supplied_key and record.get("action_validation"):
            raise HTTPException(
                409,
                {
                    "message": "Cached reply for this transcript failed action "
                    "validation; resend with an Idempotency-Key to force a "
                    "fresh attempt",
                    "code": "cached_action_invalid",
                },
            )
        with suppress(Exception):
            store.annotate_request(
                rid,
                encoded_bytes=len(encoded.encode()),
                input_composition=input_composition(body),
            )
        headers = {"X-Request-Id": rid, "Cache-Control": "no-store"}
        if body.stream:
            return StreamingResponse(
                stream(rid, body), media_type="text/event-stream", headers=headers
            )
        try:
            record = await wait_result(request, rid)
            result = completion(record, body)
            return JSONResponse(result, headers=headers)
        except ActionRejected as exc:
            note_action_failure(rid, exc.code)
            return JSONResponse(
                _error_payload(
                    "Upstream or action validation failed; no retry", exc.code, rid
                ),
                status_code=502,
                headers=headers,
            )
        except UpstreamNotDone as exc:
            return JSONResponse(
                _error_payload(
                    "Upstream or action validation failed; no retry", exc.code, rid
                ),
                status_code=502,
                headers=headers,
            )
        except asyncio.CancelledError:
            await asyncio.shield(engine.disconnect(rid))
            raise

    return app
