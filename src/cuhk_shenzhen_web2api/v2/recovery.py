"""Conservative evidence checks and atomic authentication handover."""

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

from .transport import TransportError, build_transport


def auth_directory(root: Path, name: str) -> Path:
    """Only a named direct child; reject symlinks/junctions escaping the root."""
    if not name or name in {".", ".."} or any(c in name for c in "/\\:"):
        raise ValueError("auth_directory must be a single child directory name")
    parent = root.resolve(strict=True)
    target = (parent / name).resolve(strict=True)
    if target.parent != parent or not target.is_dir():
        raise ValueError("auth_directory is outside the configured root")
    state = (target / "storage_state.json").resolve(strict=True)
    if state.parent != target or state.stat().st_size > 1024 * 1024:
        raise ValueError("Invalid authentication state file")
    return target


@asynccontextmanager
async def drained(engine, timeout=180):
    engine.maintenance = True
    try:
        async with asyncio.timeout(timeout):
            while engine._tasks:
                await asyncio.sleep(0.02)
        yield
    finally:
        engine.maintenance = False


async def replace_auth(engine, settings, name, factory=build_transport, timeout=180):
    directory = auth_directory(settings.auth_root, name)
    candidate = None
    async with drained(engine, timeout):
        try:
            candidate = await factory(
                settings.backend,
                settings.data_dir,
                replace(settings, auth_dir=directory),
            )
            await candidate.authenticate()
            if candidate.state != "authenticated":
                raise TransportError("authentication_required")
        except BaseException:
            if candidate is not None:
                await candidate.close()
            raise
        old = engine.transport
        engine.transport = candidate
        engine.resume()
        # Handover is committed. Old-browser cleanup must not undo new auth.
        try:
            await old.close()
        except Exception:  # noqa: BLE001 - cleanup failure cannot expose private SDK text
            logging.getLogger(__name__).warning("Previous backend cleanup failed")
        return candidate


def history_evidence(record: dict, history: dict) -> tuple[dict | None, str]:
    """Accept ONLY explicit matching session, assistant index and finished state.

    Other campus schemas remain unresolved until a diagnostic sample validates
    a new adapter. A history record's presence alone never proves completion.
    """
    observed = record.get("result") or {}
    sid = observed.get("chat_session_id") or record.get("chat_session_id")
    index = observed.get("approach_msg_idx")
    if not sid or type(index) is not int:
        return None, "missing_start_metadata"
    if history.get("chat_session_id") != sid:
        return None, "session_not_confirmed"
    messages = history.get("messages")
    if not isinstance(messages, list):
        return None, "unrecognized_history_schema"
    matches = [
        m
        for m in messages
        if isinstance(m, dict)
        and type(m.get("self_idx")) is int
        and m["self_idx"] == index
    ]
    if len(matches) != 1:
        return None, "message_not_unique"
    message = matches[0]
    if message.get("role") != "approach" or message.get("status") != "finished":
        return None, "completion_not_confirmed"
    if (
        observed.get("user_msg_idx") is not None
        and message.get("parent_idx") != observed["user_msg_idx"]
    ):
        return None, "parent_not_confirmed"
    items = message.get("items")
    if not isinstance(items, list) or any(
        not isinstance(item, dict)
        or item.get("type") != "text"
        or not isinstance(item.get("content"), str)
        for item in items
    ):
        return None, "unsupported_history_items"
    text = "".join(item["content"] for item in items)
    if not text or len(text.encode()) > 1024 * 1024:
        return None, "invalid_history_text"
    return {
        **observed,
        "chat_session_id": sid,
        "approach_msg_idx": index,
        "text": text,
        "status": "finished",
    }, "confirmed"
