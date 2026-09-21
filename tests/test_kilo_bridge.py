"""Offline bridge contracts: no campus access, no actual tool execution."""

import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from cuhk_shenzhen_web2api.kilo_bridge import app as bridge_app
from cuhk_shenzhen_web2api.kilo_bridge.app import create_app
from cuhk_shenzhen_web2api.kilo_bridge.protocol import (
    SYSTEM_PREAMBLE,
    CompletionRequest,
    input_composition,
    loads,
    prompt,
    reply,
)
from cuhk_shenzhen_web2api.kilo_bridge.runtime import TextTransport
from cuhk_shenzhen_web2api.v2.config import Settings
from cuhk_shenzhen_web2api.v2.engine import Engine
from cuhk_shenzhen_web2api.v2.store import Store
from cuhk_shenzhen_web2api.v2.transport import TransportError

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a fixture",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "enum": ["fixture.txt"]}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    }
]
BODY = {
    "model": "glm-5.3",
    "messages": [{"role": "user", "content": "Read fixture.txt"}],
    "tools": TOOLS,
}
ACTION = {
    "type": "tool_call",
    "name": "read_file",
    "arguments": {"path": "fixture.txt"},
}


class FakeCampus:
    state = "authenticated"

    def __init__(self):
        self.actions = [ACTION]
        self.sent = []
        self.builtin = False
        self.gate = None
        self.cleaned = False

    async def events(self, payload):
        self.sent.append(payload)
        try:
            yield {
                "event": "start",
                "chat_session_id": "sid-" + str(len(self.sent)),
                "user_msg_idx": 1,
                "approach_msg_idx": 2,
            }
            yield {
                "event": "msg",
                "item": {
                    "type": "thought",
                    "content": "PRIVATE_THOUGHT_MUST_NOT_ESCAPE",
                },
            }
            if self.gate:
                await self.gate.wait()
            if self.builtin:
                yield {
                    "event": "msg",
                    "item": {
                        "type": "tool",
                        "tool_name": "web_search",
                        "status": "calling",
                    },
                }
            action = self.actions[min(len(self.sent) - 1, len(self.actions) - 1)]
            raw = (
                action
                if isinstance(action, str)
                else json.dumps(action, ensure_ascii=False)
            )
            for part in raw:
                yield {"event": "msg", "item": {"type": "text", "content": part}}
            yield {"event": "end", "status": "finished"}
        finally:
            self.cleaned = True

    async def close(self):
        pass

    async def authenticate(self):
        self.state = "authenticated"
        return {"state": self.state}

    async def probe(self):
        return {"state": self.state}


class DropsAfterStart:
    """Emits a valid start event, then fails mid-stream (already submitted)."""

    state = "authenticated"

    def __init__(self, status):
        self.status = status

    async def events(self, payload):
        yield {
            "event": "start",
            "chat_session_id": "sid",
            "user_msg_idx": 1,
            "approach_msg_idx": 2,
        }
        raise TransportError("http_error", status=self.status)

    async def close(self):
        pass


class RejectsBeforeSend:
    """Fails before any event; known_not_submitted so the engine retries."""

    state = "authenticated"

    def __init__(self, status, code, retry_after=None):
        self.status = status
        self.code = code
        self.retry_after = retry_after

    async def events(self, payload):
        if False:
            yield {}
        raise TransportError(
            self.code,
            status=self.status,
            retry_after=self.retry_after,
            known_not_submitted=True,
        )

    async def close(self):
        pass


class ProtocolTests(unittest.TestCase):
    def test_preserve_roles_and_history_without_duplication(self):
        body = copy.deepcopy(BODY)
        body["messages"].insert(
            0, {"role": "system", "content": "Only synthetic files"}
        )
        req = CompletionRequest.model_validate(body).checked()
        result = reply(json.dumps(ACTION), req, "req1", 1)
        body["messages"].append(result["choices"][0]["message"])
        body["messages"].append(
            {"role": "tool", "tool_call_id": "call_req1", "content": "42"}
        )
        compiled = prompt(CompletionRequest.model_validate(body).checked())
        transcript = json.loads(compiled.split("TRANSCRIPT_JSON:\n")[1])
        self.assertEqual(
            [m["role"] for m in transcript["messages"]],
            ["system", "user", "assistant", "tool"],
        )
        self.assertEqual(transcript["messages"][-1]["tool_call_id"], "call_req1")
        self.assertEqual(transcript["messages"][-1]["content"], "42")

    def test_reject_unmatched_tool_result(self):
        body = {
            **BODY,
            "messages": [{"role": "tool", "tool_call_id": "fake", "content": "x"}],
        }
        with self.assertRaises(ValueError):
            CompletionRequest.model_validate(body).checked()

    def test_reject_missing_result(self):
        req = CompletionRequest.model_validate(BODY).checked()
        message = reply(json.dumps(ACTION), req, "req1", 1)["choices"][0]["message"]
        with self.assertRaises(ValueError):
            CompletionRequest.model_validate(
                {**BODY, "messages": [*BODY["messages"], message]}
            ).checked()

    def test_reject_duplicate_result(self):
        req = CompletionRequest.model_validate(BODY).checked()
        assistant = reply(json.dumps(ACTION), req, "req1", 1)["choices"][0]["message"]
        tool = {"role": "tool", "tool_call_id": "call_req1", "content": "x"}
        with self.assertRaises(ValueError):
            CompletionRequest.model_validate(
                {**BODY, "messages": [*BODY["messages"], assistant, tool, tool]}
            ).checked()

    def test_reject_remote_schema_references_before_send(self):
        body = copy.deepcopy(BODY)
        body["tools"][0]["function"]["parameters"]["properties"]["path"] = {
            "$ref": "http://127.0.0.1/secret"
        }
        with self.assertRaises(ValueError):
            CompletionRequest.model_validate(body).checked()

    def test_invalid_proposals_never_become_calls(self):
        req = CompletionRequest.model_validate(BODY).checked()
        actions = [
            {**ACTION, "name": "shell"},
            {**ACTION, "arguments": {"path": "../secret"}},
            {**ACTION, "arguments": {"path": "fixture.txt", "command": "x"}},
            {**ACTION, "arguments": {"path": 1}},
            {"type": "other"},
        ]
        for action in actions:
            with self.subTest(action=action), self.assertRaises(ValueError):
                reply(json.dumps(action), req, "id", 1)
        for text in [
            '{"type":"final","type":"tool_call"}',
            "```json\n{}\n```",
            "[]",
            "NaN",
        ]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                reply(text, req, "id", 1)

    def test_choice_enforcement(self):
        none = CompletionRequest.model_validate(
            {**BODY, "tool_choice": "none"}
        ).checked()
        with self.assertRaises(ValueError):
            reply(json.dumps(ACTION), none, "id", 1)
        required = CompletionRequest.model_validate(
            {**BODY, "tool_choice": "required"}
        ).checked()
        with self.assertRaises(ValueError):
            reply('{"type":"final","content":"no"}', required, "id", 1)

    def test_final_code_is_data(self):
        req = CompletionRequest.model_validate(BODY).checked()
        answer = reply('{"type":"final","content":"print(42)"}', req, "id", 1)
        self.assertNotIn("tool_calls", answer["choices"][0]["message"])

    def test_input_composition_reconciles_to_prompt(self):
        req = CompletionRequest.model_validate(BODY).checked()
        comp = input_composition(req)
        self.assertEqual(comp["total"], len(prompt(req).encode()))
        self.assertEqual(sum(v for k, v in comp.items() if k != "total"), comp["total"])
        self.assertGreaterEqual(comp["structural_and_control"], 0)
        self.assertEqual(comp["system_preamble"], len(SYSTEM_PREAMBLE.encode()))
        self.assertGreater(comp["tool_definitions"], 0)
        self.assertGreater(comp["message_history"], 0)
        self.assertEqual(comp["tool_results"], 0)

    def test_input_composition_isolates_tool_result_growth(self):
        req = CompletionRequest.model_validate(BODY).checked()
        base = input_composition(req)
        message = reply(json.dumps(ACTION), req, "req1", 1)["choices"][0]["message"]
        body = copy.deepcopy(BODY)
        body["messages"] += [
            message,
            {"role": "tool", "tool_call_id": "call_req1", "content": "X" * 4000},
        ]
        grown = input_composition(CompletionRequest.model_validate(body).checked())
        self.assertGreater(grown["tool_results"], 3900)
        self.assertEqual(grown["system_preamble"], base["system_preamble"])
        self.assertEqual(grown["tool_definitions"], base["tool_definitions"])
        self.assertGreater(grown["total"], base["total"])
        self.assertTrue(
            all(isinstance(v, int) for v in grown.values())
        )  # bytes only, no estimates/objects

    def test_compaction_shaped_history_round_trips(self):
        body = copy.deepcopy(BODY)
        # Kilo /compact collapses older turns into a leading summary text turn;
        # the bridge treats it as ordinary transcript text (no special field).
        body["messages"] = [
            {
                "role": "system",
                "content": "COMPACTED SUMMARY: edits to a.py are complete; only b.py remains.",
            },
            {"role": "user", "content": "Continue editing b.py"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_b",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "fixture.txt"}),
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_b", "content": "file bytes"},
        ]
        req = CompletionRequest.model_validate(body).checked()
        transcript = json.loads(prompt(req).split("TRANSCRIPT_JSON:\n")[1])
        self.assertEqual(
            [m["role"] for m in transcript["messages"]],
            ["system", "user", "assistant", "tool"],
        )
        self.assertEqual(transcript["messages"][-1]["tool_call_id"], "call_b")

    def test_compact_request_shape_round_trips_as_transcript(self):
        # Real capture: Kilo /compact re-sends the FULL unabridged transcript
        # (every tool result kept) plus the whole tool catalog with
        # max_tokens=32000 to have the model write the summary. It is an
        # ordinary chat request to us, not a special field. This guards the
        # exact knobs that sample used: many turns, tools present, big budget,
        # and intact call/result pairing across a long history.
        messages = [{"role": "system", "content": "You edit files."}]
        messages.append({"role": "user", "content": "Read and edit fixture.txt"})
        for i in range(6):
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"c{i}",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps({"path": "fixture.txt"}),
                            },
                        }
                    ],
                }
            )
            messages.append(
                {"role": "tool", "tool_call_id": f"c{i}", "content": "X" * (40 + i)}
            )
        messages.append({"role": "user", "content": "Summarize the conversation so far."})
        body = {**BODY, "messages": messages, "max_tokens": 32000, "stream": True}
        req = CompletionRequest.model_validate(body).checked()
        transcript = json.loads(prompt(req).split("TRANSCRIPT_JSON:\n")[1])
        self.assertEqual(len(transcript["messages"]), len(messages))
        # Long call/result pairing survives serialization.
        self.assertEqual(transcript["messages"][-2]["tool_call_id"], "c5")
        # The tool catalog and the large output budget are carried through, and
        # tool results dominate the growth bucket for a compact-sized turn.
        self.assertIn("read_file", prompt(req))
        self.assertEqual(req.max_tokens, 32000)
        comp = input_composition(req)
        self.assertGreater(comp["tool_results"], 0)

    def test_compaction_dropping_a_tool_result_is_refused(self):
        # A compaction that removes a tool result but keeps the call must not be
        # silently accepted (no hidden truncation): the dangling call is caught.
        body = copy.deepcopy(BODY)
        body["messages"] = [
            {"role": "user", "content": "continue"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_b",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "fixture.txt"}),
                        },
                    }
                ],
            },
        ]
        with self.assertRaises(ValueError) as ctx:
            CompletionRequest.model_validate(body).checked()
        self.assertEqual(str(ctx.exception), "missing_tool_results")


class BridgeAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inner = FakeCampus()
        self.settings = Settings(Path(self.tmp.name), "test-key", interval=0)
        self.app = create_app(self.settings, TextTransport(self.inner))
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.headers = {"Authorization": "Bearer test-key"}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def post(self, body=None, headers=None):
        return self.client.post(
            "/v1/chat/completions",
            json=BODY if body is None else body,
            headers=headers or self.headers,
        )

    def test_auth_health_and_no_executor(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/v1/models").status_code, 401)
        self.assertEqual(self.client.get("/docs").status_code, 401)
        self.assertEqual(
            self.client.post("/tools/call", headers=self.headers).status_code, 404
        )
        ready = self.client.get("/ready", headers=self.headers).json()
        self.assertFalse(ready["streaming"])
        self.assertEqual(self.inner.sent, [])

    def test_tool_roundtrip_and_final(self):
        self.inner.actions = [ACTION, {"type": "final", "content": "Value is 42"}]
        first = self.post()
        self.assertEqual(first.status_code, 200, first.text)
        assistant = first.json()["choices"][0]["message"]
        self.assertEqual(first.json()["choices"][0]["finish_reason"], "tool_calls")
        body = copy.deepcopy(BODY)
        body["messages"] += [
            assistant,
            {
                "role": "tool",
                "tool_call_id": assistant["tool_calls"][0]["id"],
                "content": "42",
            },
        ]
        second = self.post(body)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(
            second.json()["choices"][0]["message"]["content"], "Value is 42"
        )
        self.assertEqual(len(self.inner.sent), 2)
        self.assertIsNone(self.inner.sent[1]["chat_session_id"])
        self.assertNotIn("tools", self.inner.sent[0])

    def test_buffered_sse_tool_shape(self):
        response = self.post(
            {**BODY, "stream": True, "stream_options": {"include_usage": True}}
        )
        self.assertEqual(response.status_code, 200)
        events = [
            loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]
        call = events[0]["choices"][0]["delta"]["tool_calls"][0]
        self.assertEqual(call["index"], 0)
        self.assertEqual(json.loads(call["function"]["arguments"]), ACTION["arguments"])
        self.assertEqual(events[-1]["choices"][0]["finish_reason"], "tool_calls")
        self.assertIn("data: [DONE]", response.text)
        self.assertNotIn("PRIVATE_THOUGHT", response.text)

    def test_idempotency_and_conflict(self):
        first = self.post()
        self.assertEqual(first.json(), self.post().json())
        self.assertEqual(len(self.inner.sent), 1)
        headers = {**self.headers, "Idempotency-Key": "explicit"}
        self.assertEqual(self.post(headers=headers).status_code, 200)
        changed = {**BODY, "messages": [{"role": "user", "content": "different"}]}
        self.assertEqual(self.post(changed, headers).status_code, 409)

    def test_bad_action_caches_and_implicit_replay_is_refused(self):
        self.inner.actions = ["not JSON"]
        response = self.post()
        self.assertEqual(response.status_code, 502)
        replay = self.post()
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(replay.json()["detail"]["code"], "cached_action_invalid")
        self.assertEqual(len(self.inner.sent), 1)
        headers = {**self.headers, "Idempotency-Key": "force-retry"}
        self.assertEqual(self.post(headers=headers).status_code, 502)
        self.assertEqual(len(self.inner.sent), 2)

    def test_delivery_mode_change_does_not_resubmit(self):
        first = self.post()
        streamed = self.post({**BODY, "stream": True})
        self.assertEqual(
            streamed.headers["x-request-id"], first.headers["x-request-id"]
        )
        self.assertIn("data: [DONE]", streamed.text)
        self.assertEqual(len(self.inner.sent), 1)

    def test_sse_invalid_action_has_no_tool_delta(self):
        self.inner.actions = [{**ACTION, "name": "not_registered"}]
        response = self.post({**BODY, "stream": True})
        self.assertIn('"error"', response.text)
        self.assertNotIn('"delta"', response.text)
        self.assertNotIn("[DONE]", response.text)

    def test_thoughts_not_persisted(self):
        response = self.post()
        record = self.app.state.engine.store.get_request(
            response.headers["x-request-id"]
        )
        self.assertNotIn("PRIVATE_THOUGHT", json.dumps(record))

    def test_auth_expired_prevents_submission(self):
        self.inner.state = "expired"
        self.assertEqual(self.post().status_code, 503)
        self.assertEqual(self.inner.sent, [])

    def test_builtin_tool_aborts_and_unknown_is_not_replayed(self):
        self.inner.builtin = True
        response = self.post()
        self.assertEqual(response.status_code, 502)
        rid = response.headers["x-request-id"]
        status = self.client.get("/requests/" + rid, headers=self.headers).json()
        self.assertEqual(status["upstream_status"], "unknown")
        self.assertEqual(status["error"], "campus_builtin_tool_activity")
        self.assertEqual(self.post().status_code, 502)
        self.assertEqual(len(self.inner.sent), 1)
        self.assertTrue(self.inner.cleaned)

    def test_action_validation_recorded_when_upstream_done(self):
        self.inner.actions = [{**ACTION, "name": "not_registered"}]
        response = self.post()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json()["error"]["message"],
            "Upstream or action validation failed; no retry",
        )
        self.assertEqual(response.json()["error"]["code"], "unknown_tool_or_arguments")
        rid = response.headers["x-request-id"]
        status = self.client.get("/requests/" + rid, headers=self.headers).json()
        self.assertEqual(status["upstream_status"], "done")
        self.assertIsNone(status["error"])
        self.assertEqual(status["action_validation"], "unknown_tool_or_arguments")
        self.assertIsInstance(status["diagnostics"]["encoded_bytes"], int)
        self.assertGreater(status["diagnostics"]["encoded_bytes"], 0)
        self.assertIsNone(status["diagnostics"]["upstream_http_status"])
        self.assertIn("elapsed_ms", status["diagnostics"])

    def test_malformed_json_action_code_is_neutral(self):
        self.inner.actions = ["[]"]
        response = self.post()
        self.assertEqual(response.json()["error"]["code"], "expected_action_object")
        rid = response.headers["x-request-id"]
        status = self.client.get("/requests/" + rid, headers=self.headers).json()
        self.assertEqual(status["action_validation"], "expected_action_object")

    def test_sse_error_frame_carries_code(self):
        self.inner.actions = ["not JSON"]
        response = self.post({**BODY, "stream": True})
        events = [
            loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]
        self.assertEqual(events[0]["error"]["code"], "request_rejected")
        self.assertNotIn("[DONE]", response.text)

    def test_reject_paths_report_codes(self):
        unsupported = self.post({**BODY, "tool_choice": "unsupported"})
        self.assertEqual(unsupported.status_code, 400)
        self.assertEqual(
            unsupported.json()["detail"]["code"], "unsupported_tool_choice"
        )
        self.assertIn("nothing submitted", unsupported.json()["detail"]["message"])
        unknown_field = self.post({**BODY, "unknown": 1})
        self.assertEqual(unknown_field.status_code, 400)
        self.assertEqual(unknown_field.json()["detail"]["code"], "request_rejected")
        oversize = self.client.post(
            "/v1/chat/completions", content="x" * 262145, headers=self.headers
        )
        self.assertEqual(oversize.status_code, 413)
        self.assertEqual(oversize.json()["detail"]["code"], "request_body_too_large")
        self.assertEqual(self.inner.sent, [])

    def test_successful_request_omits_action_validation(self):
        response = self.post()
        self.assertEqual(response.status_code, 200)
        status = self.client.get(
            "/requests/" + response.headers["x-request-id"], headers=self.headers
        ).json()
        self.assertIsNone(status["action_validation"])
        self.assertIsInstance(status["diagnostics"]["encoded_bytes"], int)

    def test_requests_flag_approaching_size_limit(self):
        response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        rid = response.headers["x-request-id"]
        small = self.client.get(
            "/requests/" + rid, headers=self.headers
        ).json()["diagnostics"]
        self.assertIn("approaching_size_limit", small)
        self.assertFalse(small["approaching_size_limit"])
        # The flag is derived from the stored encoded_bytes at read time; nudging
        # that value past the advisory threshold must flip only this hint.
        self.app.state.engine.store.annotate_request(
            rid, encoded_bytes=bridge_app.SOFT_SIZE_LIMIT
        )
        large = self.client.get(
            "/requests/" + rid, headers=self.headers
        ).json()["diagnostics"]
        self.assertTrue(large["approaching_size_limit"])
        self.assertEqual(large["encoded_bytes"], bridge_app.SOFT_SIZE_LIMIT)

    def test_input_composition_reported_and_reconciles(self):
        response = self.post()
        status = self.client.get(
            "/requests/" + response.headers["x-request-id"], headers=self.headers
        ).json()
        comp = status["diagnostics"]["input_composition"]
        self.assertIsNotNone(comp)
        self.assertEqual(comp["total"], status["diagnostics"]["encoded_bytes"])
        self.assertEqual(sum(v for k, v in comp.items() if k != "total"), comp["total"])
        self.assertGreater(comp["tool_definitions"], 0)
        self.assertEqual(comp["tool_results"], 0)

    def test_invalid_inputs_send_nothing(self):
        bad = [
            {**BODY, "model": "other"},
            {**BODY, "unknown": 1},
            {
                **BODY,
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "image_url", "image_url": "secret"}],
                    }
                ],
            },
            {**BODY, "tool_choice": "unsupported"},
        ]
        for body in bad:
            self.assertEqual(self.post(body).status_code, 400)
        self.assertEqual(self.inner.sent, [])

    def test_oversize_request_send_nothing(self):
        response = self.client.post(
            "/v1/chat/completions", content="x" * 262145, headers=self.headers
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.inner.sent, [])

    def test_schema_error_send_nothing(self):
        body = copy.deepcopy(BODY)
        body["tools"][0]["function"]["parameters"]["required"] = "bad"
        self.assertEqual(self.post(body).status_code, 400)
        self.assertEqual(self.inner.sent, [])


class EngineDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def settle(self, inner):
        store = Store(":memory:")
        engine = Engine(store, TextTransport(inner), interval=0)
        await engine.start()
        try:
            record = await engine.submit({"message": "synthetic"}, "k1")
            await engine.result(record["id"])
        finally:
            await engine.close()
        return store.get_request(record["id"])

    async def test_drop_after_start_is_unknown_with_http_status(self):
        record = await self.settle(DropsAfterStart(500))
        self.assertEqual(record["status"], "unknown")
        self.assertEqual(record["error"], "http_error")
        self.assertEqual(record["upstream_http_status"], 500)
        self.assertFalse(record["known_not_submitted"])
        self.assertTrue(record["upstream_received"])
        self.assertEqual(record["error_type"], "TransportError")

    async def test_bounded_retry_records_status_and_rate_limit(self):
        record = await self.settle(
            RejectsBeforeSend(429, "rate_limited", retry_after=0.001)
        )
        self.assertEqual(record["status"], "error")
        self.assertEqual(record["error"], "rate_limited")
        self.assertEqual(record["upstream_http_status"], 429)
        self.assertTrue(record["known_not_submitted"])
        self.assertFalse(record["upstream_received"])
        self.assertEqual(record["attempts"], 3)
        self.assertEqual(record["retry_after"], 0.001)
        self.assertEqual(record["rate_limit_count"], 3)


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_duplicate_rejected(self):
        inner = FakeCampus()
        inner.gate = asyncio.Event()
        store = Store(":memory:")
        engine = Engine(store, TextTransport(inner), interval=0)
        try:
            await engine.start()
            await engine.submit({"message": "synthetic"}, "same")
            with self.assertRaises(ValueError):
                await engine.submit({"message": "synthetic"}, "same")
        finally:
            await engine.close()
            store.close()

    async def test_restart_never_replays_inflight(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.sqlite3"
            store = Store(path)
            record = store.create_request({"message": "synthetic"}, key="same")
            store.claim_next()
            store.close()
            store = Store(path)
            inner = FakeCampus()
            engine = Engine(store, TextTransport(inner), interval=0)
            try:
                await engine.start()
                restored = await engine.submit({"message": "synthetic"}, "same")
                self.assertEqual(restored["id"], record["id"])
                self.assertEqual(restored["status"], "unknown")
                self.assertEqual(inner.sent, [])
            finally:
                await engine.close()
                store.close()

    async def test_disconnect_aborts_stream_and_records_unknown(self):
        inner = FakeCampus()
        inner.gate = asyncio.Event()
        store = Store(":memory:")
        engine = Engine(store, TextTransport(inner), interval=0)
        try:
            await engine.start()
            record = await engine.submit({"message": "synthetic"}, "test")
            for _ in range(100):
                if inner.sent:
                    break
                await asyncio.sleep(0.01)
            await engine.disconnect(record["id"])
            self.assertEqual(store.get_request(record["id"])["status"], "unknown")
            self.assertTrue(inner.cleaned)
            again = await engine.submit({"message": "synthetic"}, "test")
            self.assertEqual(again["status"], "unknown")
            self.assertEqual(len(inner.sent), 1)
        finally:
            await engine.close()
            store.close()


class RecordingInner:
    """Minimal stand-in exposing the full Transport surface for delegation checks."""

    def __init__(self):
        self.state = "not_logged_in"
        self.calls = []

    async def authenticate(self):
        self.calls.append(("authenticate",))
        self.state = "authenticated"
        return {"result": "authenticate"}

    async def probe(self):
        self.calls.append(("probe",))
        return {"result": "probe"}

    async def request(self, path, payload=None):
        self.calls.append(("request", path, payload))
        return {"path": path, "payload": payload}

    async def events(self, payload):
        if False:
            yield {}

    async def close(self):
        self.calls.append(("close",))


class TextTransportDelegationTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_lifecycle_delegates_and_state_is_live(self):
        inner = RecordingInner()
        wrapped = TextTransport(inner)
        self.assertEqual(wrapped.state, "not_logged_in")
        self.assertEqual(await wrapped.authenticate(), {"result": "authenticate"})
        # The property must reflect the inner transport, never a cached copy.
        self.assertEqual(wrapped.state, "authenticated")
        self.assertEqual(await wrapped.probe(), {"result": "probe"})

    async def test_request_forwards_path_and_default_payload(self):
        inner = RecordingInner()
        wrapped = TextTransport(inner)
        self.assertEqual(
            await wrapped.request("/getHistoryItem/", {"chat_session_id": "s"}),
            {"path": "/getHistoryItem/", "payload": {"chat_session_id": "s"}},
        )
        self.assertEqual(
            await wrapped.request("/ping"),
            {"path": "/ping", "payload": None},
        )
        self.assertEqual(
            inner.calls,
            [
                ("request", "/getHistoryItem/", {"chat_session_id": "s"}),
                ("request", "/ping", None),
            ],
        )


class WrappedTransportFactoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_factory_wraps_inner_and_keeps_event_filter(self):
        campus = FakeCampus()
        campus.builtin = True

        async def fake_build(backend, data_dir, config):
            self.assertEqual(backend, "playwright")
            return campus

        with mock.patch.object(bridge_app, "build_transport", fake_build):
            transport = await bridge_app.wrapped_transport("playwright", "d", "cfg")
        self.assertIsInstance(transport, TextTransport)
        self.assertIs(transport.inner, campus)
        # A swapped transport must still reject campus built-in tool activity.
        with self.assertRaises(TransportError):
            async for _ in transport.events({"message": "x"}):
                pass


class AuthRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inner = FakeCampus()
        self.settings = Settings(Path(self.tmp.name), "test-key", interval=0)
        self.app = create_app(self.settings, TextTransport(self.inner))
        self.engine = self.app.state.engine
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.headers = {"Authorization": "Bearer test-key"}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def post_completion(self):
        return self.client.post(
            "/v1/chat/completions", json=BODY, headers=self.headers
        )

    def test_ready_and_gate_follow_engine_transport(self):
        replacement = FakeCampus()
        replacement.state = "expired"
        self.engine.transport = TextTransport(replacement)
        ready = self.client.get("/ready", headers=self.headers)
        self.assertEqual(ready.status_code, 503)
        self.assertEqual(ready.json()["authentication"], "expired")
        self.assertEqual(self.post_completion().status_code, 503)
        self.assertEqual(self.inner.sent, [])  # original never consulted

    def test_ready_reports_maintenance_as_not_ready(self):
        self.engine.maintenance = True
        try:
            response = self.client.get("/ready", headers=self.headers)
        finally:
            self.engine.maintenance = False
        self.assertEqual(response.status_code, 503)
        self.assertTrue(response.json()["maintenance"])

    def test_auth_check_resumes_when_probe_confirms(self):
        self.engine.paused = True
        self.inner.state = "authenticated"
        response = self.client.post("/auth/check", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.engine.paused)

    def test_auth_check_keeps_pause_when_probe_still_expired(self):
        self.engine.paused = True
        self.inner.state = "expired"
        response = self.client.post("/auth/check", headers=self.headers)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["authentication"], "expired")
        self.assertTrue(self.engine.paused)

    def test_auth_reload_uses_wrapping_factory_and_commits(self):
        replacement = FakeCampus()
        captured = {}

        async def fake_replace(eng, settings, name, factory=None, timeout=180):
            captured["name"] = name
            captured["factory"] = factory
            eng.transport = TextTransport(replacement)

        with mock.patch.object(bridge_app, "replace_auth", fake_replace):
            response = self.client.post(
                "/auth/reload",
                json={"directory": "auth-2"},
                headers=self.headers,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["name"], "auth-2")
        self.assertIs(captured["factory"], bridge_app.wrapped_transport)
        self.assertIs(self.engine.transport.inner, replacement)

    def test_auth_reload_requires_directory_string(self):
        response = self.client.post("/auth/reload", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 400)

    def test_auth_reload_returns_409_when_drain_or_dir_fails(self):
        async def boom(*args, **kwargs):
            raise TimeoutError()

        with mock.patch.object(bridge_app, "replace_auth", boom):
            response = self.client.post(
                "/auth/reload", json={"directory": "x"}, headers=self.headers
            )
        self.assertEqual(response.status_code, 409)

    def test_auth_check_records_verified_time(self):
        self.inner.state = "authenticated"
        checked = self.client.post("/auth/check", headers=self.headers)
        self.assertEqual(checked.status_code, 200)
        self.assertEqual(checked.json()["status"], "ready")
        self.assertIsNotNone(checked.json()["identity_last_verified_at"])

    def test_ready_reports_failed_after_handover_error(self):
        self.inner.state = "expired"

        async def boom(*args, **kwargs):
            raise TimeoutError()

        with mock.patch.object(bridge_app, "replace_auth", boom):
            self.client.post(
                "/auth/reload", json={"directory": "x"}, headers=self.headers
            )
        ready = self.client.get("/ready", headers=self.headers)
        self.assertEqual(ready.json()["status"], "failed")
        self.assertEqual(ready.status_code, 503)

    def test_auth_check_probe_failure_reports_failed_not_500(self):
        self.inner.state = "expired"

        async def boom():
            raise TransportError("authentication_required")

        with mock.patch.object(self.engine.transport, "probe", boom):
            response = self.client.post("/auth/check", headers=self.headers)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "failed")

    def test_auth_reload_rejects_traversal_directory_name(self):
        # A str with a path separator passes the isinstance gate, then
        # auth_directory raises ValueError; that must map to 409, not a 500.
        response = self.client.post(
            "/auth/reload", json={"directory": "a/b"}, headers=self.headers
        )
        self.assertEqual(response.status_code, 409)

    def test_auth_reload_returns_409_on_transport_error(self):
        self.inner.state = "expired"

        async def boom(*args, **kwargs):
            raise TransportError("authentication_required")

        with mock.patch.object(bridge_app, "replace_auth", boom):
            response = self.client.post(
                "/auth/reload", json={"directory": "x"}, headers=self.headers
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            self.client.get("/ready", headers=self.headers).json()["status"], "failed"
        )

    def test_reconcile_history_failure_is_not_500(self):
        rid = self._seed_unknown(
            "rec_err", {"chat_session_id": "sid", "approach_msg_idx": 2}
        )

        async def boom(path=None, payload=None):
            raise TransportError("authentication_required")

        with mock.patch.object(self.engine.transport, "request", boom):
            response = self.client.post(
                f"/requests/{rid}/reconcile", headers=self.headers
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.inner.sent, [])  # never re-submits on a failed probe

    def _seed_unknown(self, key, result):
        store = self.engine.store
        self.engine.paused = True  # stop the dispatcher from claiming the seed
        record = store.create_request({"message": "synthetic"}, key=key)
        store.claim_next()
        store.update_request(record["id"], "unknown", result=result)
        return record["id"]

    def test_reconcile_confirms_unknown_without_touching_campus_submit(self):
        rid = self._seed_unknown(
            "rec1", {"chat_session_id": "sid", "approach_msg_idx": 2}
        )
        history = {"chat_session_id": "sid", "messages": []}
        seen = {}

        async def fake_request(path, payload=None):
            seen["request"] = (path, payload)
            return history

        def fake_reconcile(reconcile_id, received):
            seen["reconcile"] = (reconcile_id, received)
            return {"status": "done", "reason": "confirmed"}

        with (
            mock.patch.object(self.engine.transport, "request", fake_request),
            mock.patch.object(self.engine.store, "reconcile_request", fake_reconcile),
        ):
            response = self.client.post(
                f"/requests/{rid}/reconcile", headers=self.headers
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "done", "reason": "confirmed"})
        self.assertEqual(
            seen["request"], ("/getHistoryItem/", {"chat_session_id": "sid"})
        )
        self.assertEqual(seen["reconcile"], (rid, history))
        self.assertEqual(self.inner.sent, [])  # reconciliation never re-submits

    def test_reconcile_non_unknown_short_circuits_without_a_history_call(self):
        store = self.engine.store
        self.engine.paused = True
        record = store.create_request({"message": "synthetic"}, key="rec2")
        calls = []

        async def fake_request(path, payload=None):
            calls.append(path)
            return {}

        with mock.patch.object(self.engine.transport, "request", fake_request):
            response = self.client.post(
                f"/requests/{record['id']}/reconcile", headers=self.headers
            )
        self.assertEqual(
            response.json(),
            {"status": "queued", "reason": "not_unknown_or_missing_session"},
        )
        self.assertEqual(calls, [])

    def test_reconcile_missing_record_is_404(self):
        response = self.client.post("/requests/nope/reconcile", headers=self.headers)
        self.assertEqual(response.status_code, 404)
