"""One durable FIFO for realtime and batch turns; never replay ambiguous sends.

The synchronous Store owns transactions. This engine must be its only scheduler.
Transport exceptions may opt into retries with ``known_not_submitted=True``;
absence of that proof is deliberately treated as an ambiguous submission.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import time
from typing import Any

from .store import ConversationConflict

FINAL = frozenset({"done", "error", "unknown", "cancelled"})
logger = logging.getLogger(__name__)


class Engine:
    MAX_EVENTS = 2048
    MAX_EVENT_BYTES = 64 * 1024
    MAX_TOTAL_BYTES = 2 * 1024 * 1024
    MAX_TEXT_BYTES = 1024 * 1024
    MAX_ATTEMPTS = 3
    MAX_RETRY_AFTER = 300.0
    POLL_SECONDS = 0.02

    def __init__(self, store, transport, concurrency: int = 1, interval: float = 20.0):
        if (
            isinstance(concurrency, bool)
            or int(concurrency) != concurrency
            or concurrency < 1
        ):
            raise ValueError("concurrency must be a positive integer")
        if not math.isfinite(float(interval)) or float(interval) < 0:
            raise ValueError("interval must be finite and nonnegative")
        self.store = store
        self.transport = transport
        self.concurrency = int(concurrency)
        self.interval = float(interval)
        self.paused = False
        self.maintenance = False
        self._closed = False
        self._dispatcher = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._submitted: set[str] = set()
        self._subscribers: set[str] = set()
        self._reserved: set[tuple[str, str]] = set()
        self._job_ready: dict[str, float] = {}
        self._rate_lock = asyncio.Lock()
        self._next_send = 0.0

    async def start(self):
        if self._closed:
            raise RuntimeError("engine is closed")
        if self._dispatcher is None:
            self.store.recover()
            self._dispatcher = asyncio.create_task(
                self._dispatch(), name="v2-scheduler"
            )
        return self

    async def close(self):
        if self._closed:
            return
        self._closed = True
        if self._dispatcher:
            self._dispatcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._dispatcher
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.transport.close()

    async def submit(self, payload, key=None):
        if self._closed:
            raise RuntimeError("engine is closed")
        if key is not None:
            existing = self.store.get_request_by_key(key)
            if existing and existing["status"] not in FINAL:
                raise ValueError("idempotency key already has an active request")
        return self.store.create_request(payload, key=key)

    def resume(self):
        """Resume after the caller has explicitly restored upstream authentication."""
        if self.transport.state != "authenticated":
            raise RuntimeError("transport authentication has not been restored")
        self.paused = False

    def _record(self, request_id):
        record = self.store.get_request(request_id)
        if record is None:
            raise KeyError(request_id)
        return record

    async def result(self, request_id):
        while True:
            record = self._record(request_id)
            if record["status"] in FINAL:
                return record
            await asyncio.sleep(self.POLL_SECONDS)

    async def events(self, request_id):
        """Replay persisted events, then tail. Early consumer close cancels the turn."""
        cursor = 0
        complete = False
        if request_id in self._subscribers:
            raise ValueError(
                "idempotency request already has an active stream subscriber"
            )
        self._record(request_id)
        self._subscribers.add(request_id)
        try:
            while True:
                record = self._record(request_id)
                events = record.get("events") or []
                for event in events[cursor:]:
                    cursor += 1
                    yield event
                if record["status"] in FINAL:
                    complete = True
                    return
                await asyncio.sleep(self.POLL_SECONDS)
        finally:
            self._subscribers.discard(request_id)
            if not complete:
                await self.disconnect(request_id)

    async def disconnect(self, request_id):
        """Stop a streaming client request; an attempted send is never requeued."""
        record = self._record(request_id)
        if record["status"] in FINAL:
            return
        status = "unknown" if request_id in self._submitted else "cancelled"
        self.store.update_request(request_id, status, error="client_disconnected")
        task = self._tasks.get(request_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _resolve(self, record):
        payload = dict(record["payload"])
        cid = payload.get("conversation") or payload.get("conversation_id")
        sid = payload.get("chat_session_id")
        conversation = self.store.get_conversation(cid) if cid else None
        if not sid and conversation:
            sid = conversation.get("chat_session_id")
        canonical = self.store.get_conversation(sid) if sid else None
        if canonical:
            payload["parent_idx"] = canonical["parent_idx"]
        elif conversation and conversation.get("chat_session_id") == sid:
            payload["parent_idx"] = conversation["parent_idx"]
        else:
            payload.setdefault("parent_idx", -1)
        payload["chat_session_id"] = sid
        if payload.get("parent_idx") is None:
            payload["parent_idx"] = -1
        keys = set()
        if cid:
            keys.add(("conversation", str(cid)))
        if sid:
            keys.add(("session", str(sid)))
        if record.get("job_id"):
            job = self.store.get_job(record["job_id"]) or {}
            if job.get("mode") == "thread" or job.get("pause_seconds", 20) > 0:
                keys.add(("job", str(record["job_id"])))
        return payload, cid, keys

    async def _pace(self):
        async with self._rate_lock:
            while (delay := self._next_send - time.perf_counter()) > 0:
                await asyncio.sleep(delay)
            self._next_send = time.perf_counter() + self.interval

    async def _dispatch(self):
        pending = None
        try:
            while True:
                if (
                    self.paused
                    or self.maintenance
                    or self.transport.state != "authenticated"
                    or len(self._tasks) >= self.concurrency
                ):
                    await asyncio.sleep(self.POLL_SECONDS)
                    continue
                if pending is None:
                    pending = self.store.claim_next()
                if pending is None:
                    await asyncio.sleep(self.POLL_SECONDS)
                    continue
                current = self._record(pending["id"])
                if current["status"] in FINAL:
                    pending = None
                    continue
                if (
                    current.get("job_id")
                    and (self.store.get_job(current["job_id"]) or {}).get("status")
                    == "cancelled"
                ):
                    self.store.update_request(
                        current["id"], "cancelled", error="job_cancelled_before_send"
                    )
                    pending = None
                    continue
                payload, cid, keys = self._resolve(current)
                ready = self._job_ready.get(current.get("job_id"), 0)
                if keys & self._reserved or ready > asyncio.get_running_loop().time():
                    await asyncio.sleep(self.POLL_SECONDS)
                    continue
                # Recheck after the rate wait: a previous turn can commit a newer parent.
                if self.paused:
                    continue
                current = self._record(pending["id"])
                if current["status"] in FINAL:
                    pending = None
                    continue
                if (
                    current.get("job_id")
                    and (self.store.get_job(current["job_id"]) or {}).get("status")
                    == "cancelled"
                ):
                    self.store.update_request(
                        current["id"], "cancelled", error="job_cancelled_before_send"
                    )
                    pending = None
                    continue
                payload, cid, keys = self._resolve(current)
                if keys & self._reserved:
                    continue
                self._reserved.update(keys)
                request_id = current["id"]
                task = asyncio.create_task(self._run(current, payload, cid, keys))
                self._tasks[request_id] = task
                pending = None
                # Ensure the reserved first attempt enters transport before another claim.
                await asyncio.sleep(0)
        finally:
            if pending and self._record(pending["id"])["status"] not in FINAL:
                self.store.update_request(
                    pending["id"], "cancelled", error="engine_closed_before_send"
                )

    @staticmethod
    def _wire_payload(payload):
        allowed = {
            "project_id",
            "chat_session_id",
            "approach_id",
            "params",
            "content",
            "parent_idx",
            "image_ids",
            "file_ids",
            "quota_pool",
        }
        wire = {key: value for key, value in payload.items() if key in allowed}
        wire.setdefault("content", payload.get("message", ""))
        wire.setdefault("params", {})
        wire.setdefault("image_ids", [])
        wire.setdefault("file_ids", [])
        return wire

    async def _run(self, record, payload, cid, keys):
        request_id = record["id"]
        events = list(record.get("events") or [])
        reply: dict[str, Any] = {"text": "", "status": None, "conversation": cid}
        total_bytes = 0
        text_bytes = 0
        try:
            for attempt in range(self.MAX_ATTEMPTS):
                received = False
                try:
                    # Bounce before touching the transport while paused or during
                    # an auth swap: maintenance can flip right after the
                    # dispatcher's claim-time check, and this turn has not sent
                    # yet, so return it unsent to run on the post-swap transport.
                    if self.paused or self.maintenance:
                        self.store.return_unsent(request_id)
                        return
                    await self._pace()
                    if self.paused or self.maintenance:
                        self.store.return_unsent(request_id)
                        return
                    current = self._record(request_id)
                    if current["status"] in FINAL:
                        return
                    if (
                        record.get("job_id")
                        and (self.store.get_job(record["job_id"]) or {}).get("status")
                        == "cancelled"
                    ):
                        self.store.update_request(
                            request_id, "cancelled", error="job_cancelled_before_send"
                        )
                        return
                    if self.store.unresolved_conversation(
                        cid, payload.get("chat_session_id")
                    ):
                        self.store.update_request(
                            request_id, "error", error="conversation_unresolved"
                        )
                        return
                    # Mark the conservative send boundary BEFORE entering arbitrary transport.
                    fields = {
                        "attempts": attempt + 1,
                        "last_sent_at": time.time(),
                        "submitted_parent_idx": payload.get("parent_idx", -1),
                    }
                    if not current.get("sent_at"):
                        fields["sent_at"] = fields["last_sent_at"]
                    self.store.update_request(request_id, "running", **fields)
                    self._submitted.add(request_id)
                    stream = self.transport.events(self._wire_payload(payload))
                    try:
                        async for event in stream:
                            received = True
                            size = len(
                                json.dumps(event, ensure_ascii=False).encode("utf-8")
                            )
                            total_bytes += size
                            if (
                                len(events) >= self.MAX_EVENTS
                                or size > self.MAX_EVENT_BYTES
                                or total_bytes > self.MAX_TOTAL_BYTES
                            ):
                                raise ValueError("stream_limit_exceeded")
                            events.append(event)
                            kind = event.get("event")
                            if kind == "start":
                                if reply.get("approach_msg_idx") is not None:
                                    raise ValueError("duplicate_start_event")
                                if (
                                    payload.get("chat_session_id")
                                    and event.get("chat_session_id")
                                    != payload["chat_session_id"]
                                ):
                                    raise ValueError("unexpected_session_id")
                                for field in (
                                    "chat_session_id",
                                    "user_msg_idx",
                                    "approach_msg_idx",
                                ):
                                    reply[field] = event.get(field)
                                reply["conversation"] = cid or reply.get(
                                    "chat_session_id"
                                )
                                # Include actual session in reservations as soon as it is known.
                                if reply.get("chat_session_id"):
                                    key = ("session", str(reply["chat_session_id"]))
                                    keys.add(key)
                                    self._reserved.add(key)
                            elif kind == "msg":
                                item = event.get("item") or {}
                                if item.get("type") == "text":
                                    content = item.get("content") or ""
                                    if content and not self._record(request_id).get(
                                        "first_text_at"
                                    ):
                                        self.store.update_request(
                                            request_id,
                                            "running",
                                            first_text_at=time.time(),
                                        )
                                    text_bytes += len(content.encode("utf-8"))
                                    if text_bytes > self.MAX_TEXT_BYTES:
                                        raise ValueError("stream_limit_exceeded")
                                    reply["text"] += content
                            elif kind == "end":
                                for field in (
                                    "status",
                                    "title",
                                    "ctx_token_cnt",
                                    "context_truncated",
                                ):
                                    reply[field] = event.get(field)
                            metadata = {
                                field: reply[field]
                                for field in (
                                    "chat_session_id",
                                    "user_msg_idx",
                                    "approach_msg_idx",
                                )
                                if field in reply
                            }
                            self.store.update_request(
                                request_id,
                                "running",
                                events=events,
                                result=reply,
                                **metadata,
                            )
                            if kind == "end":
                                if reply["status"] == "finished":
                                    sid = reply.get("chat_session_id")
                                    parent = reply.get("approach_msg_idx")
                                    if (
                                        not sid
                                        or not isinstance(parent, int)
                                        or isinstance(parent, bool)
                                        or parent < 0
                                    ):
                                        raise ValueError("missing_start_metadata")
                                    self.store.update_request(
                                        request_id, "done", result=reply
                                    )
                                    try:
                                        self.store.put_conversation(
                                            cid or sid, sid, parent
                                        )
                                    except ConversationConflict as exc:
                                        logger.warning(
                                            "conversation bookkeeping conflict for %s: %s",
                                            request_id,
                                            exc,
                                        )
                                else:
                                    self.store.update_request(
                                        request_id,
                                        "error",
                                        result=reply,
                                        error="upstream_" + str(reply["status"]),
                                    )
                                return
                        raise ValueError("stream_ended_without_terminal_event")
                    finally:
                        if hasattr(stream, "aclose"):
                            await stream.aclose()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - all transport failures must settle durably
                    safe = (
                        getattr(exc, "known_not_submitted", False) is True
                        and not received
                    )
                    status = getattr(exc, "status", None)
                    code = str(getattr(exc, "code", "transport_failure"))[:120]
                    auth = status in (401, 403) or code in (
                        "not_logged_in",
                        "auth_required",
                        "auth_expired",
                        "authentication_required",
                        "wrong_browser_origin",
                    )
                    if auth:
                        self.paused = True
                    if status == 429:
                        count = self._record(request_id).get("rate_limit_count", 0)
                        self.store.update_request(
                            request_id, "running", rate_limit_count=count + 1
                        )
                    if safe:
                        self._submitted.discard(request_id)
                    if safe and not auth and attempt + 1 < self.MAX_ATTEMPTS:
                        retry_after = getattr(exc, "retry_after", None)
                        delay = 0.25 * 2**attempt
                        if (
                            isinstance(retry_after, int | float)
                            and not isinstance(retry_after, bool)
                            and math.isfinite(retry_after)
                            and retry_after >= 0
                        ):
                            delay = float(retry_after)
                        delay = min(delay, self.MAX_RETRY_AFTER)
                        if status == 429:
                            self._next_send = max(
                                self._next_send, time.perf_counter() + delay
                            )
                        await asyncio.sleep(delay)
                        continue
                    diagnostics = {
                        "upstream_http_status": status,
                        "known_not_submitted": safe,
                        "upstream_received": received,
                        "error_type": type(exc).__name__,
                    }
                    if status == 429:
                        wait = getattr(exc, "retry_after", None)
                        if (
                            isinstance(wait, int | float)
                            and not isinstance(wait, bool)
                            and math.isfinite(wait)
                        ):
                            diagnostics["retry_after"] = wait
                    self.store.update_request(
                        request_id,
                        "error" if safe else "unknown",
                        result=reply,
                        error=code,
                        **diagnostics,
                    )
                    return
        except asyncio.CancelledError:
            if self._record(request_id)["status"] not in FINAL:
                status = "unknown" if request_id in self._submitted else "cancelled"
                self.store.update_request(
                    request_id, status, result=reply, error="engine_cancelled"
                )
            raise
        finally:
            job_id = record.get("job_id")
            if job_id:
                job = self.store.get_job(job_id) or {}
                pause = job.get("pause_seconds", 20)
                self._job_ready[job_id] = asyncio.get_running_loop().time() + max(
                    0, float(pause)
                )
            self._reserved.difference_update(keys)
            self._submitted.discard(request_id)
            self._tasks.pop(request_id, None)
