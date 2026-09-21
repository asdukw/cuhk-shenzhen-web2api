"""Offline tests for the /compact capture proxy. No campus, no 8768 required.

Asserts the whole reason Route B exists: a request is recorded in a
content-free shape even when the upstream rejects it (400/413), and message
content, tool schemas and the Authorization header never reach the report.
"""

import http.client
import http.server
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from cuhk_shenzhen_web2api.kilo_bridge import capture_proxy
from cuhk_shenzhen_web2api.kilo_bridge.capture_proxy import (
    _Recorder,
    describe_request,
    make_handler,
)

MARKER = "TOP_SECRET_FILE_CONTENT_DO_NOT_LEAK"


class DescribeRequestTests(unittest.TestCase):
    def shape(self, body):
        return describe_request(json.dumps(body).encode())

    def test_compaction_shape_is_content_free_and_reconciles(self):
        result = self.shape(
            {
                "model": "glm-5.3",
                "messages": [
                    {"role": "system", "content": "SUMMARY " + MARKER},
                    {"role": "user", "content": "continue"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "edit_file",
                                    "arguments": json.dumps({"p": MARKER}),
                                },
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
                ],
                "max_tokens": 4096,
            }
        )
        self.assertEqual(result["json_ok"], True)
        self.assertEqual(result["extra_top_level_keys"], [])
        self.assertEqual(result["message_count"], 4)
        self.assertEqual(result["roles"]["tool"], 1)
        self.assertEqual(result["max_tokens"], 4096)
        self.assertNotIn(MARKER, json.dumps(result))

    def test_forbidden_fields_and_image_parts_are_surfaced(self):
        result = self.shape(
            {
                "model": "glm-5.3",
                "provider": "sneaky",
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "image_url", "image_url": {"url": "x"}}],
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "read",
                            "parameters": {"type": "object"},
                            "extra_fn_key": 1,
                        },
                    }
                ],
            }
        )
        self.assertIn("provider", result["extra_top_level_keys"])
        self.assertIn("cache_control", result["message_extra_keys"])
        self.assertEqual(result["messages"][0]["part_types"], ["image_url"])
        self.assertIn("image_url", result["messages"][0]["part_extra_keys"])
        self.assertIn("extra_fn_key", result["tool_function_extra_keys"])
        self.assertEqual(result["tool_names"], ["read"])

    def test_tool_choice_named_and_non_json(self):
        named = self.shape(
            {
                "model": "glm-5.3",
                "messages": [{"role": "user", "content": "hi"}],
                "tool_choice": {"type": "function", "function": {"name": "read"}},
            }
        )
        self.assertEqual(named["tool_choice"], {"kind": "function", "name": "read"})
        broken = describe_request(b"not json")
        self.assertFalse(broken["json_ok"])

    def test_non_scalar_metadata_is_collapsed_not_persisted(self):
        result = self.shape(
            {
                "model": "glm-5.3",
                "max_tokens": {"nested": MARKER},
                "tool_choice": [MARKER],
                "messages": [{"role": {"hidden": MARKER}, "content": "hi"}],
            }
        )
        # Where a scalar is expected, a container collapses to its type name so
        # no nested content can hide in max_tokens / tool_choice / role.
        self.assertEqual(result["max_tokens"], "dict")
        self.assertEqual(result["tool_choice"], "list")
        self.assertEqual(result["messages"][0]["role"], "dict")
        self.assertEqual(result["roles"], {"dict": 1})
        self.assertNotIn(MARKER, json.dumps(result))

    def test_long_string_metadata_is_truncated(self):
        long_role = "r" * 300 + MARKER
        result = self.shape(
            {"model": "glm-5.3", "messages": [{"role": long_role, "content": "x"}]}
        )
        self.assertTrue(result["messages"][0]["role"].endswith("[truncated]"))
        self.assertNotIn(MARKER, json.dumps(result))


class ProxyForwardingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kilo-capture-"))
        self.recorder = _Recorder(self.tmp)

        class Upstream(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                return

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                self.rfile.read(length)
                # Reject like the bridge would for an oversize transcript.
                payload = b'{"detail":"Transcript too large"}'
                self.send_response(413)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.upstream_port = self.upstream.server_address[1]
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()

        handler = make_handler(self.recorder, "127.0.0.1", self.upstream_port)
        self.proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.proxy_port = self.proxy.server_address[1]
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()

    def tearDown(self):
        self.proxy.shutdown()
        self.proxy.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()

    def test_rejected_request_is_still_captured_without_content(self):
        body = json.dumps(
            {
                "model": "glm-5.3",
                "messages": [{"role": "user", "content": MARKER}],
            }
        ).encode()
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.proxy_port, timeout=10
        )
        connection.putrequest("POST", "/v1/chat/completions", skip_host=True)
        connection.putheader("Host", "127.0.0.1")
        connection.putheader("Authorization", "Bearer SUPER_SECRET_KEY")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
        response = connection.getresponse()
        forwarded = response.read()
        connection.close()

        self.assertEqual(response.status, 413)
        self.assertNotIn(b"SUPER_SECRET_KEY", forwarded)  # body relayed unchanged
        text = (self.tmp / "requests.jsonl").read_text(encoding="utf-8")
        record = json.loads(text.splitlines()[0])
        self.assertEqual(record["forwarded_status"], 413)
        self.assertEqual(record["shape"]["message_count"], 1)
        self.assertEqual(record["shape"]["messages"][0]["content_bytes"], len(MARKER))
        # Neither the message content nor the credential appears anywhere.
        self.assertNotIn(MARKER, text)
        self.assertNotIn("SUPER_SECRET_KEY", text)
        self.assertNotIn("authorization", text.lower())

    def test_malformed_content_length_is_refused_and_recorded(self):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.proxy_port, timeout=10
        )
        connection.putrequest("POST", "/v1/chat/completions", skip_host=True)
        connection.putheader("Host", "127.0.0.1")
        connection.putheader("Content-Length", "not-a-number")
        connection.endheaders()
        response = connection.getresponse()
        response.read()
        connection.close()
        self.assertEqual(response.status, 400)
        record = json.loads(
            (self.tmp / "requests.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        self.assertEqual(record["forwarded_status"], "not_forwarded")
        self.assertEqual(record["shape"]["error"], "invalid_content_length")

    def test_oversize_body_is_recorded_not_forwarded_truncated(self):
        body = json.dumps(
            {"model": "glm-5.3", "messages": [{"role": "user", "content": "x" * 300}]}
        ).encode()
        with mock.patch.object(capture_proxy, "MAX_CAPTURE_BYTES", 100):
            connection = http.client.HTTPConnection(
                "127.0.0.1", self.proxy_port, timeout=10
            )
            connection.putrequest("POST", "/v1/chat/completions", skip_host=True)
            connection.putheader("Host", "127.0.0.1")
            connection.putheader("Content-Length", str(len(body)))
            connection.endheaders(body)
            response = connection.getresponse()
            response.read()
            connection.close()
        self.assertEqual(response.status, 413)
        record = json.loads(
            (self.tmp / "requests.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        # A string status (not the integer a real forward would relay) proves the
        # truncated body was never relayed as if it were complete.
        self.assertEqual(record["forwarded_status"], "not_forwarded_too_large")
        self.assertEqual(record["shape"]["body_bytes"], len(body))
        self.assertEqual(record["shape"]["captured"], "partial_peek")


if __name__ == "__main__":
    unittest.main()
