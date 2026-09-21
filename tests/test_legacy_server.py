"""Offline regression tests for the legacy web2api server facade and job queue.

These cover the security hardening from the global review, all without a
browser, campus login, or network: a path-traversal job id never touches the
filesystem, the auth middleware fails *closed* when no key is configured,
/health never warms the runtime, request bodies stay out of INFO logs, and the
literal /tools/log routes win over the parameterised /tools/{tool_name}.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from cuhk_shenzhen_web2api import jobs, server


class JobsTraversalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jobs-"))
        patcher = mock.patch.object(jobs, "JOBS_DIR", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_traversal_id_is_never_read_or_written(self):
        # A real job so the directory is non-empty and a match is plausible.
        created = jobs.create([{"message": "hi"}], mode="independent")
        self.assertTrue(jobs.get(created["id"]))
        for evil in (
            "../../../etc/passwd",
            "..\\..\\windows\\system32\\config",
            "../../secret",
            "sub/dir/name",
            ".",
            "..",
            "",
        ):
            with self.subTest(job_id=evil):
                self.assertIsNone(jobs.get(evil))
                self.assertIsNone(jobs.cancel(evil))
        # Nothing was written outside JOBS_DIR.
        self.assertEqual([p.name for p in self.tmp.iterdir()], [f"{created['id']}.json"])

    def test_crlf_and_encoded_slash_are_rejected(self):
        self.assertIsNone(jobs.get("job_abc\ndef"))
        self.assertIsNone(jobs.get("job%2f..%2f.."))

    def test_finish_item_does_not_resurrect_a_cancelled_job(self):
        created = jobs.create([{"message": "a"}, {"message": "b"}], mode="independent")
        jid = created["id"]
        job_id, _job, item = jobs.claim_next_item()
        self.assertEqual(job_id, jid)
        jobs.cancel(jid)  # user cancels while the first item is still in flight
        jobs.finish_item(jid, item["index"], text="ok")
        self.assertEqual(jobs.get(jid)["status"], "cancelled")

    def test_finish_item_completes_a_runnable_job(self):
        created = jobs.create([{"message": "only"}], mode="independent")
        jid = created["id"]
        _job_id, _job, item = jobs.claim_next_item()
        jobs.finish_item(jid, item["index"], text="ok")
        self.assertEqual(jobs.get(jid)["status"], "completed")


class ChatClientMaskingTests(unittest.TestCase):
    def test_clip_bounds_and_flattens_raw_upstream_text(self):
        from cuhk_shenzhen_web2api import chat_client

        self.assertEqual(chat_client._clip("  a\n\tb  "), "a b")
        self.assertEqual(chat_client._clip(None), "")
        out = chat_client._clip("x" * 500, limit=200)
        self.assertTrue(out.endswith("[clipped]"))
        self.assertEqual(len(out), 200 + len("\u2026[clipped]"))


class MiddlewareAuthTests(unittest.TestCase):
    def setUp(self):
        # No context manager: /health and middleware need no lifespan worker.
        self.client = TestClient(server.app)

    def test_missing_key_fails_closed(self):
        with mock.patch.object(server.env, "web2api_api_key", return_value=""):
            response = self.client.get("/conversations")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["error"]["code"], "server_not_configured"
        )

    def test_protected_path_without_key_matches_401(self):
        with mock.patch.object(server.env, "web2api_api_key", return_value="secret"):
            missing = self.client.get("/conversations")
            wrong = self.client.get(
                "/conversations", headers={"authorization": "Bearer nope"}
            )
            good = self.client.post(
                "/health", headers={"authorization": "Bearer secret"}
            )
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(wrong.status_code, 401)
        # Authenticated but wrong method: past the guard, into routing (405).
        self.assertEqual(good.status_code, 405)

    def test_open_paths_need_no_key(self):
        with mock.patch.object(server.env, "web2api_api_key", return_value=""):
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)


class HealthNoLoginTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)

    def test_health_reports_not_ready_without_warming_runtime(self):
        original = dict(server._shared)
        server._shared["client"] = None
        self.addCleanup(server._shared.update, original)
        with mock.patch.object(server.env, "web2api_api_key", return_value=""):
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["ready"])
        # A health probe must never create the shared browser client.
        self.assertIsNone(server._shared["client"])


class LogRedactionTests(unittest.TestCase):
    def test_request_body_content_is_not_logged(self):
        client = TestClient(server.app)
        marker = "TOPSECRET_PROMPT_DO_NOT_LEAK"
        with self.assertLogs("web2api", level="INFO") as captured:
            # /health is an open path, so this reaches the middleware logger
            # without touching auth or the browser; POST -> 405 after logging.
            client.post("/health", json={"content": marker})
        text = "\n".join(captured.output)
        self.assertIn("body_bytes=", text)
        self.assertNotIn(marker, text)
        self.assertNotIn("body=", text.replace("body_bytes=", ""))


class ToolRouteOrderTests(unittest.TestCase):
    def test_delete_tools_log_clears_rather_than_unregisters(self):
        client = TestClient(server.app)
        headers = {"authorization": "Bearer secret"}
        with mock.patch.object(server.env, "web2api_api_key", return_value="secret"):
            response = client.delete("/tools/log", headers=headers)
        # If /tools/{tool_name} shadowed it we would get 404 "Tool 'log' not found".
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "cleared"})

    def test_literal_log_route_registered_before_parameterised(self):
        paths = [
            (route.path, "DELETE" in getattr(route, "methods", set()))
            for route in server.app.routes
            if getattr(route, "path", None) in {"/tools/log", "/tools/{tool_name}"}
        ]
        order = {path: i for i, (path, _del) in enumerate(paths)}
        self.assertLess(order["/tools/log"], order["/tools/{tool_name}"])


if __name__ == "__main__":
    unittest.main()
