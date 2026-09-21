"""Loopback-only, read-only capture proxy for a real Kilo /compact request.

Kilo's Chat Completions request is recorded in a *desensitized structural
shape* BEFORE it is forwarded verbatim to the running bridge, so rejected or
oversized bodies (400/413, which the bridge never persists) are still visible.
Only field names, roles, byte counts and tool names are recorded — never
message content, tool schemas, or the Authorization header. Nothing is written
under the bridge's own data dir; reports go to a fresh output directory.

This tool only forwards to a bridge you already started; it does not perform
campus login, execute tools, or mutate the running service.
"""

from __future__ import annotations

import argparse
import http.client
import http.server
import json
import threading
import time
import uuid
from collections import Counter
from pathlib import Path

# Field sets the bridge's strict protocol models accept (extra="forbid"). A key
# outside these is what would make the bridge answer 400, so we surface it.
TOP_LEVEL = {
    "model",
    "messages",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "stream",
    "stream_options",
    "max_tokens",
    "temperature",
    "top_p",
    "n",
}
MESSAGE = {"role", "content", "name", "tool_calls", "tool_call_id"}
TOOL_CALL = {"id", "type", "function"}
FUNCTION_CALL = {"name", "arguments"}
TOOL = {"type", "function"}
TOOL_FUNCTION = {"name", "description", "parameters", "strict"}
TEXT_PART = {"type", "text"}

MAX_CAPTURE_BYTES = 8 * 1024 * 1024  # guard against pathological bodies


def _byte_len(value) -> int:
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    return 0


_MAX_SCALAR_CHARS = 120


def _scalar(value):
    """Keep a metadata field scalar and short so it cannot smuggle content.

    Roles, tool names and numeric knobs are recorded verbatim when they are
    plain scalars; anything else collapses to its type name, and long strings
    are truncated so a client cannot hide a transcript inside, say, ``role``.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= _MAX_SCALAR_CHARS:
            return value
        return value[:_MAX_SCALAR_CHARS] + "\u2026[truncated]"
    return type(value).__name__


def _content_shape(content, message_shape: dict) -> None:
    if isinstance(content, str):
        message_shape["content_type"] = "str"
        message_shape["content_bytes"] = len(content.encode("utf-8"))
    elif isinstance(content, list):
        message_shape["content_type"] = "list"
        total = 0
        part_types = set()
        part_extra = set()
        for part in content:
            if isinstance(part, dict):
                part_types.add(str(part.get("type")))
                part_extra |= {k for k in part if k not in TEXT_PART}
                total += _byte_len(part.get("text"))
            else:
                part_types.add(type(part).__name__)
        message_shape["content_bytes"] = total
        message_shape["part_types"] = sorted(part_types)
        if part_extra:
            message_shape["part_extra_keys"] = sorted(part_extra)
    elif content is None:
        message_shape["content_type"] = "null"
    else:
        message_shape["content_type"] = type(content).__name__


def describe_request(raw: bytes) -> dict:
    """Return a content-free structural summary of a Chat Completions body."""
    shape: dict = {"body_bytes": len(raw)}
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        shape["json_ok"] = False
        shape["json_error"] = type(exc).__name__
        return shape
    shape["json_ok"] = True
    if not isinstance(body, dict):
        shape["top_level_type"] = type(body).__name__
        return shape

    shape["extra_top_level_keys"] = sorted(k for k in body if k not in TOP_LEVEL)
    for field in ("max_tokens", "temperature", "top_p", "n", "stream"):
        if field in body:
            shape[field] = _scalar(body[field])
    choice = body.get("tool_choice")
    if isinstance(choice, dict):
        shape["tool_choice"] = {
            "kind": _scalar(choice.get("type")),
            "name": _scalar((choice.get("function") or {}).get("name"))
            if isinstance(choice.get("function"), dict)
            else None,
        }
    elif choice is not None:
        shape["tool_choice"] = _scalar(choice)

    messages = body.get("messages")
    message_shapes = []
    if isinstance(messages, list):
        shape["message_count"] = len(messages)
        shape["roles"] = dict(
            Counter(
                str(_scalar(m.get("role"))) for m in messages if isinstance(m, dict)
            )
        )
        message_extra: set[str] = set()
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            message_extra |= {k for k in msg if k not in MESSAGE}
            entry: dict = {"role": _scalar(msg.get("role"))}
            _content_shape(msg.get("content"), entry)
            calls = msg.get("tool_calls")
            if isinstance(calls, list):
                entry["tool_calls_count"] = len(calls)
                for call in calls:
                    if isinstance(call, dict):
                        message_extra |= {k for k in call if k not in TOOL_CALL}
                        fn = call.get("function")
                        if isinstance(fn, dict):
                            message_extra |= {k for k in fn if k not in FUNCTION_CALL}
            if msg.get("tool_call_id") is not None:
                entry["has_tool_call_id"] = True
            message_shapes.append(entry)
        if message_extra:
            shape["message_extra_keys"] = sorted(message_extra)
        shape["messages"] = message_shapes

    tools = body.get("tools")
    if isinstance(tools, list):
        shape["tools_count"] = len(tools)
        names = []
        tool_extra: set[str] = set()
        function_extra: set[str] = set()
        schema_bytes = 0
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            tool_extra |= {k for k in tool if k not in TOOL}
            fn = tool.get("function")
            if isinstance(fn, dict):
                function_extra |= {k for k in fn if k not in TOOL_FUNCTION}
                names.append(fn.get("name"))
                params = fn.get("parameters")
                if params is not None:
                    schema_bytes += len(json.dumps(params, ensure_ascii=False).encode())
        shape["tool_names"] = [_scalar(n) for n in names if isinstance(n, str)]
        shape["tool_parameters_bytes"] = schema_bytes
        if tool_extra:
            shape["tool_extra_keys"] = sorted(tool_extra)
        if function_extra:
            shape["tool_function_extra_keys"] = sorted(function_extra)
    return shape


class _Recorder:
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.jsonl = output_dir / "requests.jsonl"
        self.summary_path = output_dir / "summary.json"
        self._lock = threading.Lock()
        self._records: list[dict] = []

    def add(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self._lock:
            self._records.append(record)
            with self.jsonl.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            self._write_summary_locked()
        self._print(record)

    def _write_summary_locked(self) -> None:
        chat = [r for r in self._records if r.get("path") == "/v1/chat/completions"]
        summary = {
            "total_requests": len(self._records),
            "chat_completions": len(chat),
            "forwarded_statuses": dict(
                Counter(str(r.get("forwarded_status")) for r in self._records)
            ),
            "max_request_bytes": max(
                (r["shape"].get("body_bytes", 0) for r in chat), default=0
            ),
            "requests_with_extra_keys": [
                r["id"]
                for r in chat
                if any(k.endswith("extra_keys") for k in r.get("shape", {}))
            ],
        }
        self.summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @staticmethod
    def _print(record: dict) -> None:
        shape = record.get("shape", {})
        extra = shape.get("extra_top_level_keys") or shape.get("message_extra_keys")
        print(
            f"[capture] {record['method']} {record['path']} "
            f"bytes={shape.get('body_bytes')} -> {record.get('forwarded_status')} "
            f"msgs={shape.get('message_count')} tools={shape.get('tools_count')} "
            f"extra={extra or 'none'}",
            flush=True,
        )


_DROP_RESPONSE_HEADERS = {
    "transfer-encoding",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "trailers",
    "upgrade",
}


def make_handler(recorder: _Recorder, upstream_host: str, upstream_port: int):
    class CaptureHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, format, *args):  # name must match base signature
            return

        def _new_record(self, shape, status):
            return {
                "id": "cap_" + uuid.uuid4().hex,
                "ts": time.time(),
                "method": self.command,
                "path": self.path,
                "shape": shape,
                "forwarded_status": status,
            }

        def _record(self, shape, status):
            # Build and persist in one step: used for requests we never forward.
            record = self._new_record(shape, status)
            recorder.add(record)
            return record

        def _content_length(self) -> tuple[int, str | None, int]:
            """Return (length, error, status). Never raises on bad headers."""
            header = self.headers.get("content-length")
            if header is None:
                chunked = (self.headers.get("transfer-encoding") or "").lower()
                if "chunked" in chunked:
                    return 0, "chunked_body_unsupported", 411
                return 0, None, 0
            try:
                length = int(header)
            except ValueError:
                return 0, "invalid_content_length", 400
            if length < 0:
                return 0, "invalid_content_length", 400
            return length, None, 0

        def _relay(self):
            length, error, code = self._content_length()
            if error:
                # A body we cannot read to a bounded end must not be forwarded as
                # an empty request; refuse and record why.
                self._record({"body_bytes": None, "error": error}, "not_forwarded")
                self.send_error(code, "capture proxy cannot read this request body")
                return
            if length > MAX_CAPTURE_BYTES:
                # Only peek. Forwarding a truncated body would make the capped
                # bridge report a misleading 400 on now-invalid JSON; a body this
                # large would be a 413 from the bridge anyway.
                peek = self.rfile.read(MAX_CAPTURE_BYTES)
                shape = {"body_bytes": length, "captured": "partial_peek"}
                if self.path == "/v1/chat/completions":
                    shape.update(describe_request(peek))
                    shape["body_bytes"] = length
                self._record(shape, "not_forwarded_too_large")
                self.send_error(413, "request body exceeds the capture limit")
                return
            raw = self.rfile.read(length) if length else b""
            if self.path == "/v1/chat/completions" and raw:
                shape = describe_request(raw)
            else:
                shape = {"body_bytes": len(raw)}
            record = self._new_record(shape, "pending")
            captured = False
            connection = http.client.HTTPConnection(
                upstream_host, upstream_port, timeout=120
            )
            try:
                headers = {
                    key: value
                    for key, value in self.headers.items()
                    if key.lower() not in ("host", "connection", "content-length")
                }
                headers["Host"] = f"{upstream_host}:{upstream_port}"
                if raw:
                    headers["Content-Length"] = str(len(raw))
                connection.putrequest(self.command, self.path, skip_host=True)
                for key, value in headers.items():
                    connection.putheader(key, value)
                connection.endheaders(raw if raw else None)
                response = connection.getresponse()
                record["forwarded_status"] = response.status
                # Persist the real upstream status before relaying, so the record
                # survives a client that disconnects mid-stream.
                recorder.add(record)
                captured = True
                self.send_response(response.status)
                for key, value in response.getheaders():
                    if key.lower() not in _DROP_RESPONSE_HEADERS:
                        self.send_header(key, value)
                self.end_headers()
                while chunk := response.read(65536):
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (OSError, http.client.HTTPException) as exc:
                if not captured:
                    record["forwarded_status"] = "proxy_error"
                    record["error"] = type(exc).__name__
                    recorder.add(record)
            finally:
                connection.close()

        do_GET = do_POST = do_PUT = do_DELETE = do_OPTIONS = _relay

    return CaptureHandler


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default="127.0.0.1:8769")
    parser.add_argument("--upstream", default="127.0.0.1:8768")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)

    listen_host, _, listen_port = args.listen.partition(":")
    upstream_host, _, upstream_port = args.upstream.partition(":")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)  # never reuse an old report dir

    recorder = _Recorder(output_dir)
    handler = make_handler(recorder, upstream_host, int(upstream_port))
    server = http.server.ThreadingHTTPServer((listen_host, int(listen_port)), handler)
    print(
        f"Kilo /compact capture proxy on http://{args.listen} -> {args.upstream}\n"
        f"Writing desensitized shapes to {output_dir / 'requests.jsonl'}\n"
        f"Point Kilo Base URL at http://{args.listen}/v1, run /compact, then Ctrl+C.",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
