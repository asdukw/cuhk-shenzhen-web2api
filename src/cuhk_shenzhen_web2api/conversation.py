"""Named, durable chat threads mapped onto CUHK chat_session_id + parent_idx.

The campus API keeps context server-side. Follow-ups must reuse the previous
assistant message index; stuffing the whole history into one user turn does not
give a long-running task. This store is the local handle for cron / OpenAI clients.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from .paths import CONVERSATIONS_FILE

_lock = threading.Lock()
_cache: dict[str, Any] | None = None


def _empty() -> dict[str, Any]:
    return {"threads": {}, "openai_ids": {}, "fingerprints": {}}


def _load() -> dict[str, Any]:
    global _cache
    if _cache is not None:
        return _cache
    if CONVERSATIONS_FILE.exists():
        try:
            data = json.loads(CONVERSATIONS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("threads", {})
                data.setdefault("openai_ids", {})
                data.setdefault("fingerprints", {})
                _cache = data
                return _cache
        except (json.JSONDecodeError, OSError):
            pass
    _cache = _empty()
    return _cache


def _save(data: dict[str, Any]) -> None:
    CONVERSATIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONVERSATIONS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def get(conversation_id: str) -> dict[str, Any] | None:
    with _lock:
        thread = _load()["threads"].get(conversation_id)
        return dict(thread) if isinstance(thread, dict) else None


def get_by_openai_id(openai_id: str) -> dict[str, Any] | None:
    with _lock:
        data = _load()
        cid = data["openai_ids"].get(openai_id)
        if not cid:
            return None
        thread = data["threads"].get(cid)
        return dict(thread) if isinstance(thread, dict) else None


def get_by_fingerprint(fingerprint: str) -> dict[str, Any] | None:
    with _lock:
        data = _load()
        cid = data["fingerprints"].get(fingerprint)
        if not cid:
            return None
        thread = data["threads"].get(cid)
        return dict(thread) if isinstance(thread, dict) else None


def list_threads() -> list[dict[str, Any]]:
    with _lock:
        threads = _load()["threads"]
        items = [dict(v) for v in threads.values() if isinstance(v, dict)]
    items.sort(key=lambda t: t.get("updated_at") or 0, reverse=True)
    return items


def upsert(
    conversation_id: str,
    *,
    chat_session_id: str | None,
    parent_idx: int | None,
    model: str | None = None,
    title: str | None = None,
    fingerprint: str | None = None,
    openai_id: str | None = None,
) -> dict[str, Any]:
    now = time.time()
    with _lock:
        data = _load()
        prev = data["threads"].get(conversation_id) or {}
        thread = {
            "conversation_id": conversation_id,
            "chat_session_id": chat_session_id or prev.get("chat_session_id"),
            "parent_idx": parent_idx
            if parent_idx is not None
            else prev.get("parent_idx"),
            "model": model or prev.get("model"),
            "title": title or prev.get("title"),
            "fingerprint": fingerprint or prev.get("fingerprint"),
            "turns": int(prev.get("turns") or 0) + 1,
            "created_at": prev.get("created_at") or now,
            "updated_at": now,
        }
        data["threads"][conversation_id] = thread
        if fingerprint:
            data["fingerprints"][fingerprint] = conversation_id
        if openai_id:
            data["openai_ids"][openai_id] = conversation_id
        _save(data)
        return dict(thread)


def bind_openai_id(openai_id: str, conversation_id: str) -> None:
    with _lock:
        data = _load()
        data["openai_ids"][openai_id] = conversation_id
        _save(data)


def delete(conversation_id: str) -> bool:
    with _lock:
        data = _load()
        thread = data["threads"].pop(conversation_id, None)
        if thread is None:
            return False
        fp = thread.get("fingerprint")
        if fp and data["fingerprints"].get(fp) == conversation_id:
            data["fingerprints"].pop(fp, None)
        stale = [k for k, v in data["openai_ids"].items() if v == conversation_id]
        for key in stale:
            data["openai_ids"].pop(key, None)
        _save(data)
        return True
