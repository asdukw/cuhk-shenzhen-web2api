"""Reuse the durable v2 queue without exposing campus thoughts or built-in tools."""

import json

from ..v2.transport import TransportError


class TextTransport:
    backend = "playwright-text-bridge"
    streaming = False  # Fully validate actions before returning them to Kilo.

    def __init__(self, inner):
        self.inner = inner

    @property
    def state(self):
        return self.inner.state

    async def authenticate(self):
        return await self.inner.authenticate()

    async def probe(self):
        return await self.inner.probe()

    async def request(self, path, payload=None):
        return await self.inner.request(path, payload)

    async def close(self):
        await self.inner.close()

    async def events(self, payload):
        source = self.inner.events(payload)
        pending = ""
        total = 0
        try:
            async for event in source:
                total += len(json.dumps(event, ensure_ascii=False).encode())
                if total > 2 * 1024 * 1024:
                    raise TransportError("upstream_response_limit")
                if event.get("event") == "msg":
                    item = event.get("item") or {}
                    kind = item.get("type")
                    if kind in {"tool", "tool_call", "function_call"}:
                        # It might already have executed upstream. Never translate it.
                        raise TransportError("campus_builtin_tool_activity")
                    if kind == "thought":
                        continue
                    if kind != "text" or not isinstance(item.get("content"), str):
                        raise TransportError("unsupported_campus_item")
                    pending += item["content"]
                    if len(pending.encode()) >= 1024:
                        yield {
                            "event": "msg",
                            "item": {"type": "text", "content": pending},
                        }
                        pending = ""
                elif event.get("event") != "hb":
                    if pending:
                        yield {
                            "event": "msg",
                            "item": {"type": "text", "content": pending},
                        }
                        pending = ""
                    yield event
        finally:
            await source.aclose()
