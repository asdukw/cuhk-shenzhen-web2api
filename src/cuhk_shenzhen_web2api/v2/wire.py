"""Small text-only compatibility adapters, independent of transport."""

import json
import time
from typing import Any


def frame(value: dict | str, event: str | None = None) -> str:
    data = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return (f"event: {event}\n" if event else "") + f"data: {data}\n\n"


def latest_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise ValueError("Expected text or a message list")  # noqa: TRY004
    if (
        len(value) != 1
        or not isinstance(value[0], dict)
        or value[0].get("role") != "user"
    ):
        raise ValueError(
            "Only one user message is supported; system/developer messages and inline history are not supported. Continue with conversation or previous_response_id."
        )
    for message in reversed(value):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            if any(
                not isinstance(p, dict)
                or p.get("type") not in {"text", "input_text"}
                or not isinstance(p.get("text"), str)
                for p in content
            ):
                raise ValueError("This adapter currently accepts text content only")
            return "\n".join(p.get("text", "") for p in content)
    raise ValueError("No user message provided")


def completion(result: dict, rid: str, model: str) -> dict:
    return {
        "id": "chatcmpl_" + rid,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": result.get("text", "")},
                "finish_reason": "stop",
            }
        ],
        "conversation": result.get("conversation"),
        "chat_session_id": result.get("chat_session_id"),
        "approach_msg_idx": result.get("approach_msg_idx"),
    }


def response(result: dict, rid: str, model: str) -> dict:
    return {
        "id": "resp_" + rid,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": model,
        "output": [
            {
                "id": "msg_" + rid,
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": result.get("text", ""),
                        "annotations": [],
                    }
                ],
            }
        ],
        "metadata": {
            k: result.get(k)
            for k in ("conversation", "chat_session_id", "approach_msg_idx", "title")
        },
    }


def text_delta(event: dict) -> str | None:
    item = event.get("item") or {}
    return (
        item.get("content", "")
        if event.get("event") == "msg" and item.get("type") == "text"
        else None
    )


def chunk(rid: str, model: str, delta: dict, finish: str | None = None) -> dict:
    return {
        "id": "chatcmpl_" + rid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
