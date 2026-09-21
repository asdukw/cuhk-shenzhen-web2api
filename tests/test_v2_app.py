"""Offline API contracts, authentication and streaming with no campus traffic."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from cuhk_shenzhen_web2api.v2.app import create_app
from cuhk_shenzhen_web2api.v2.config import Settings


class FakeTransport:
    state = "authenticated"
    streaming = True
    backend = "httpx"

    def __init__(self):
        self.calls = []
        self.probes = 0

    async def request(self, path, payload=None):
        self.calls.append(path)
        return {"availableModels": ["test-model"]}

    async def probe(self):
        self.probes += 1
        self.state = "authenticated"

    async def events(self, payload):
        self.calls.append(payload)
        yield {
            "event": "start",
            "chat_session_id": payload.get("chat_session_id") or "session-one",
            "approach_msg_idx": 2,
            "user_msg_idx": 1,
        }
        await asyncio.sleep(0.01)
        yield {"event": "msg", "item": {"type": "text", "content": "你好"}}
        yield {"event": "end", "status": "finished", "title": "test"}

    async def close(self):
        pass


class APIContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            Path(self.tmp.name), "test-secret", interval=0, model="test-model"
        )
        self.transport = FakeTransport()
        self.client = TestClient(create_app(self.settings, self.transport))
        self.client.__enter__()
        self.headers = {"Authorization": "Bearer test-secret"}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def test_health_is_local_docs_protected(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        self.assertEqual(self.transport.calls, [])
        for path in ["/docs", "/ready", "/v1/models", "/conversations"]:
            self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(
            self.client.get("/ready", headers=self.headers).status_code, 200
        )
        self.assertEqual(self.transport.probes, 0)

    def test_native_idempotency_and_conflict(self):
        headers = {**self.headers, "Idempotency-Key": "one"}
        first = self.client.post("/chat", json={"message": "hi"}, headers=headers)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["text"], "你好")
        second = self.client.post("/chat", json={"message": "hi"}, headers=headers)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(len(self.transport.calls), 1)
        conflict = self.client.post(
            "/chat", json={"message": "different"}, headers=headers
        )
        self.assertEqual(conflict.status_code, 409)

    def test_completion_and_response_shapes(self):
        completion = self.client.post(
            "/v1/chat/completions",
            headers=self.headers,
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        self.assertEqual(completion.status_code, 200, completion.text)
        self.assertEqual(completion.json()["choices"][0]["message"]["content"], "你好")
        response = self.client.post(
            "/v1/responses",
            headers=self.headers,
            json={"model": "test-model", "input": "hi"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["output"][0]["content"][0]["text"], "你好")

    def test_streams_and_disabled_tools(self):
        result = self.client.post(
            "/response", headers=self.headers, json={"message": "hi", "stream": True}
        )
        self.assertIn('"event": "msg"', result.text)
        self.assertIn('"event": "done"', result.text)
        result = self.client.post(
            "/v1/chat/completions",
            headers=self.headers,
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        self.assertIn("[DONE]", result.text)
        self.assertEqual(
            self.client.post("/tools/call", headers=self.headers, json={}).status_code,
            404,
        )

    def test_auth_pauses_submissions(self):
        self.transport.state = "expired"
        self.assertEqual(
            self.client.get("/ready", headers=self.headers).status_code, 503
        )
        self.assertEqual(
            self.client.post(
                "/chat", headers=self.headers, json={"message": "hi"}
            ).status_code,
            503,
        )
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(
            self.client.post("/auth/check", headers=self.headers).status_code, 200
        )

    def test_batch_preserves_item_id_and_replays_submission(self):
        headers = {**self.headers, "Idempotency-Key": "batch-test"}
        body = {"items": [{"id": "my-item", "message": "hi"}], "pause_seconds": 0}
        result = self.client.post("/jobs", headers=headers, json=body)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["items"][0]["id"], "my-item")
        again = self.client.post("/jobs", headers=headers, json=body)
        self.assertEqual(again.json()["id"], result.json()["id"])

    def test_history_and_system_messages_rejected_without_submission(self):
        for path, field in [
            ("/v1/chat/completions", "messages"),
            ("/v1/responses", "input"),
        ]:
            for prefix in [
                [{"role": "system", "content": "Only JSON"}],
                [
                    {"role": "user", "content": "My name is Alice"},
                    {"role": "assistant", "content": "Hello"},
                ],
            ]:
                result = self.client.post(
                    path,
                    headers=self.headers,
                    json={
                        field: prefix
                        + [{"role": "user", "content": "What is my name?"}]
                    },
                )
                self.assertEqual(result.status_code, 400, result.text)
                self.assertIn("not supported", result.json()["error"])
        self.assertEqual(self.transport.calls, [])


class SettingsTests(unittest.TestCase):
    def test_key_and_bind_validation(self):
        with self.assertRaises(ValueError):
            Settings(Path("isolated"), "")
        with self.assertRaises(ValueError):
            Settings(Path("isolated"), "key", host="0.0.0.0")
        with self.assertRaises(ValueError):
            Settings(Path("isolated"), "key", backend="playwright", concurrency=2)
        with self.assertRaises(ValueError):
            Settings(Path("isolated"), "key", port=0)
        with self.assertRaises(ValueError):
            Settings(Path("isolated"), "key", port=70000)

    def test_process_variables_override_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / ".env.v2").write_text("WEB2API_V2_API_KEY=file\n", encoding="utf-8")
            with (
                patch("cuhk_shenzhen_web2api.v2.config.BASE_DIR", path),
                patch.dict(os.environ, {"WEB2API_V2_API_KEY": "process"}),
            ):
                self.assertEqual(Settings.load().api_key, "process")


if __name__ == "__main__":
    unittest.main()
