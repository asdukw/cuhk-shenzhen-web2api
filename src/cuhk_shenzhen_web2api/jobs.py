"""Disk-backed batch job queue for rate-limited chat turns.

Firecrawl free tier is about 3 browser steps/min and the campus session is
single-threaded, so bulk callers should enqueue work instead of bursting HTTP.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from typing import Any

from .paths import JOBS_DIR

_lock = threading.Lock()
# Job ids we create are ``job_<hex>``. Restricting reads to filename-safe
# tokens stops /jobs/{job_id} from escaping JOBS_DIR via ../ or encoded slashes.
_JOB_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def _path(job_id: str):
    return JOBS_DIR / f"{job_id}.json"


def _read(job_id: str) -> dict[str, Any] | None:
    if not _JOB_ID.fullmatch(job_id or ""):
        return None
    path = _path(job_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _write(job: dict[str, Any]) -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job["updated_at"] = time.time()
    _path(job["id"]).write_text(
        json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def recover_running() -> None:
    """If the server died mid-item, put those items back on the queue."""
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        for path in JOBS_DIR.glob("job_*.json"):
            job = _read(path.stem)
            if not job:
                continue
            changed = False
            for item in job.get("items") or []:
                if item.get("status") == "running":
                    item["status"] = "queued"
                    changed = True
            if changed and job.get("status") == "running":
                job["status"] = "queued"
            if changed:
                _write(job)


def create(
    items: list[dict[str, Any]],
    *,
    mode: str = "independent",
    conversation: str | None = None,
    approach_id: str | None = None,
    pause_seconds: float = 20.0,
) -> dict[str, Any]:
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    packed: list[dict[str, Any]] = []
    for i, raw in enumerate(items):
        message = (raw.get("message") or raw.get("content") or "").strip()
        if not message:
            continue
        conv = raw.get("conversation") or conversation
        if mode == "independent" and not conv:
            conv = f"{job_id}-{i}"
        packed.append(
            {
                "index": i,
                "id": raw.get("id") or f"item-{i}",
                "message": message,
                "conversation": conv,
                "approach_id": raw.get("approach_id") or approach_id,
                "status": "queued",
                "text": None,
                "error": None,
                "chat_session_id": None,
            }
        )
    if not packed:
        raise ValueError("no items with a message")
    job = {
        "id": job_id,
        "status": "queued",
        "mode": mode if mode in ("independent", "thread") else "independent",
        "conversation": conversation,
        "approach_id": approach_id,
        "pause_seconds": max(0.0, float(pause_seconds)),
        "created_at": time.time(),
        "updated_at": time.time(),
        "items": packed,
    }
    with _lock:
        _write(job)
    return job


def get(job_id: str) -> dict[str, Any] | None:
    with _lock:
        job = _read(job_id)
        return dict(job) if job else None


def list_jobs(limit: int = 30) -> list[dict[str, Any]]:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(
        JOBS_DIR.glob("job_*.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    out: list[dict[str, Any]] = []
    with _lock:
        for path in files[:limit]:
            job = _read(path.stem)
            if not job:
                continue
            items = job.get("items") or []
            done = sum(1 for it in items if it.get("status") in ("done", "error"))
            out.append(
                {
                    "id": job.get("id"),
                    "status": job.get("status"),
                    "mode": job.get("mode"),
                    "conversation": job.get("conversation"),
                    "created_at": job.get("created_at"),
                    "updated_at": job.get("updated_at"),
                    "total": len(items),
                    "done": done,
                }
            )
    return out


def cancel(job_id: str) -> dict[str, Any] | None:
    with _lock:
        job = _read(job_id)
        if not job:
            return None
        if job.get("status") in ("completed", "cancelled"):
            return job
        job["status"] = "cancelled"
        for item in job.get("items") or []:
            if item.get("status") == "queued":
                item["status"] = "cancelled"
        _write(job)
        return job


def claim_next_item() -> tuple[str, dict[str, Any], dict[str, Any]] | None:
    """Pick the next queued item from the oldest running/queued job."""
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(JOBS_DIR.glob("job_*.json"), key=lambda p: p.stat().st_mtime)
    with _lock:
        for path in files:
            job = _read(path.stem)
            if not job or job.get("status") in ("completed", "cancelled"):
                continue
            for item in job.get("items") or []:
                if item.get("status") == "queued":
                    item["status"] = "running"
                    job["status"] = "running"
                    _write(job)
                    return str(job["id"]), dict(job), dict(item)
            # no queued items left
            errors = any(it.get("status") == "error" for it in job.get("items") or [])
            job["status"] = "failed" if errors else "completed"
            _write(job)
    return None


def finish_item(
    job_id: str,
    index: int,
    *,
    text: str | None = None,
    error: str | None = None,
    chat_session_id: str | None = None,
    conversation_id: str | None = None,
) -> None:
    with _lock:
        job = _read(job_id)
        if not job:
            return
        for item in job.get("items") or []:
            if item.get("index") == index:
                item["status"] = "error" if error else "done"
                item["text"] = text
                item["error"] = error
                item["chat_session_id"] = chat_session_id
                item["conversation"] = conversation_id or item.get("conversation")
                break
        remaining = any(
            it.get("status") in ("queued", "running") for it in job.get("items") or []
        )
        if not remaining and job.get("status") != "cancelled":
            # A cancellation is terminal: finishing the one in-flight item must
            # not resurrect the job as completed/failed once it was cancelled.
            errors = any(it.get("status") == "error" for it in job.get("items") or [])
            job["status"] = "failed" if errors else "completed"
        _write(job)
