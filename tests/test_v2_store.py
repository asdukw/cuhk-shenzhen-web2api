"""Offline SQLite tests. All fixtures use new, retained temporary directories."""

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cuhk_shenzhen_web2api.v2.store import (
    ConversationConflict,
    IdempotencyConflict,
    InvalidTransition,
    QueueFull,
    Store,
)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="v2-store-offline-"))
        self.path = self.root / "new.sqlite3"
        self.store = Store(self.path)
        self.addCleanup(self.store.close)

    def other(self, limit=1000):
        store = Store(self.path, queue_limit=limit)
        self.addCleanup(store.close)
        return store

    def fixture(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_idempotency_and_detachment(self):
        first = self.store.create_request({"message": "hi", "a": 1}, key="key")
        self.assertEqual(
            first, self.store.create_request({"a": 1, "message": "hi"}, key="key")
        )
        self.assertEqual(first, self.store.get_request_by_key("key"))
        with self.assertRaises(IdempotencyConflict):
            self.store.create_request({"message": "changed"}, key="key")
        first["payload"]["message"] = "local edit"
        self.assertEqual(
            self.store.get_request(first["id"])["payload"]["message"], "hi"
        )
        self.assertEqual(self.store.pending_count(), 1)

    def test_claim_fields_terminal_immutability(self):
        request = self.store.create_request({"message": "hello"})
        with self.assertRaises(InvalidTransition):
            self.store.update_request(request["id"], "running")
        self.assertEqual(self.store.claim_next()["id"], request["id"])
        self.assertIsNone(self.store.claim_next())
        events = [{"type": "start", "chat_session_id": "upstream"}]
        result = {"text": "reply", "approach_msg_idx": 2}
        self.store.update_request(
            request["id"], "running", events=events, result=result
        )
        done = self.store.update_request(
            request["id"], "done", result=result, error=None
        )
        self.assertEqual(done["events"], events)
        self.assertEqual(self.other().get_request(request["id"])["result"], result)
        self.assertEqual(
            self.store.update_request(request["id"], "done", result={}), done
        )
        for state in ("queued", "running", "error", "unknown", "cancelled"):
            with self.assertRaises(InvalidTransition):
                self.store.update_request(request["id"], state)
        self.assertEqual(self.store.pending_count(), 0)

    def test_annotate_request_is_diagnostic_only(self):
        request = self.store.create_request({"message": "hello"})
        self.store.claim_next()
        result = {"text": "reply"}
        self.store.update_request(request["id"], "running", result=result)
        self.store.update_request(request["id"], "done", result=result, error=None)
        annotated = self.store.annotate_request(
            request["id"], action_validation="unknown_tool_or_arguments", attempts=3
        )
        self.assertEqual(annotated["status"], "done")
        self.assertEqual(annotated["result"], result)
        self.assertIsNone(annotated["error"])
        self.assertEqual(annotated["action_validation"], "unknown_tool_or_arguments")
        self.assertEqual(self.other().get_request(request["id"])["attempts"], 3)
        with self.assertRaises(ValueError):
            self.store.annotate_request(request["id"], status="queued")
        with self.assertRaises(ValueError):
            self.store.annotate_request(request["id"], error="override")
        with self.assertRaises(ValueError):
            self.store.annotate_request(request["id"], result={})
        self.assertIsNone(self.store.annotate_request("missing", attempts=1))
        self.assertEqual(
            self.store.annotate_request(request["id"]),
            self.store.get_request(request["id"]),
        )
        with self.assertRaises(TypeError):
            self.store.annotate_request(request["id"], attempts=object())
        queued = self.store.create_request({"message": "pre-dispatch"})
        self.assertEqual(
            self.store.annotate_request(queued["id"], encoded_bytes=2048)[
                "encoded_bytes"
            ],
            2048,
        )

    def test_recover_never_requeues_and_open_does_not_recover(self):
        active = self.store.create_request({"message": "active"})
        queued = self.store.create_request({"message": "next"})
        self.store.claim_next()
        other = self.other()
        self.assertEqual(other.get_request(active["id"])["status"], "running")
        self.assertEqual(other.recover(), 1)
        self.assertEqual(other.recover(), 0)
        self.assertEqual(other.get_request(active["id"])["status"], "unknown")
        self.assertEqual(other.claim_next()["id"], queued["id"])
        with self.assertRaises(InvalidTransition):
            other.update_request(active["id"], "queued")

    def test_atomic_limit_dedupe_at_capacity(self):
        store = self.other(limit=3)
        items = [{"message": str(i)} for i in range(3)]
        job = store.create_job(items, key="batch")
        self.assertEqual(store.create_job(items, key="batch")["id"], job["id"])
        with self.assertRaises(QueueFull):
            store.create_job([{"message": "four"}, {"message": "five"}])
        self.assertEqual(len(store.list_jobs()), 1)
        self.assertEqual(store.pending_count(), 3)
        with self.assertRaises(IdempotencyConflict):
            store.create_job([{"message": "changed"}], key="batch")

    def test_default_limit_1000(self):
        with self.assertRaises(QueueFull):
            self.store.create_job([{"message": "x"}] * 1001)
        self.assertEqual(self.store.list_jobs(), [])
        self.store.create_job([{"message": "x"}] * 1000)
        self.assertEqual(self.store.pending_count(), 1000)
        with self.assertRaises(QueueFull):
            self.store.create_request({"message": "overflow"})

    def test_invalid_batch_atomic(self):
        for items in ([], [{"message": "ok"}, {}], [None], [{"message": 1}]):
            with self.assertRaises((ValueError, TypeError)):
                self.store.create_job(items)
        with self.assertRaises(ValueError):
            self.store.create_job([{"message": "x"}], pause_seconds=float("nan"))
        self.assertEqual(self.store.list_jobs(), [])
        self.assertEqual(self.store.pending_count(), 0)

    def test_concurrent_submit_and_claim(self):
        stores = [self.other(limit=8) for _ in range(4)]

        def submit(i):
            try:
                return stores[i % 4].create_request({"message": str(i)})
            except QueueFull:
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            records = list(pool.map(submit, range(24)))
        self.assertEqual(sum(r is not None for r in records), 8)
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda i: stores[i % 4].claim_next(), range(16)))
        ids = [r["id"] for r in claims if r is not None]
        self.assertEqual(len(ids), 8)
        self.assertEqual(len(set(ids)), 8)

    def test_concurrent_same_key(self):
        stores = [self.other() for _ in range(4)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            records = list(
                pool.map(
                    lambda i: stores[i].create_request({"message": "same"}, key="race"),
                    range(4),
                )
            )
        self.assertEqual(len({r["id"] for r in records}), 1)
        self.assertEqual(self.store.pending_count(), 1)

    def test_cancel_stable_after_finish_or_recover(self):
        for recover in (False, True):
            job = self.store.create_job([{"message": "a"}, {"message": "b"}])
            active = self.store.claim_next()
            cancelled = self.store.cancel_job(job["id"])
            self.assertEqual(
                [i["status"] for i in cancelled["items"]], ["running", "cancelled"]
            )
            if recover:
                self.store.recover()
            else:
                self.store.update_request(active["id"], "done", text="finished")
            self.assertEqual(self.store.get_job(job["id"])["status"], "cancelled")
            self.assertEqual(self.store.cancel_job(job["id"])["status"], "cancelled")
            self.assertIsNone(self.store.claim_next())

    def test_thread_blocks_error_independent_continues(self):
        job = self.store.create_job([{"message": "a"}, {"message": "b"}], mode="thread")
        active = self.store.claim_next()
        self.assertIsNone(self.store.claim_next())
        self.store.update_request(active["id"], "error", error="fail")
        self.assertIsNone(self.store.claim_next())
        self.assertEqual(self.store.get_job(job["id"])["status"], "error")
        other = self.store.create_job([{"message": "c"}, {"message": "d"}])
        active = self.store.claim_next()
        self.store.update_request(active["id"], "error")
        self.assertEqual(self.store.claim_next()["id"], other["items"][1]["id"])

    def test_thread_defaults_completion(self):
        job = self.store.create_job(
            [{"content": "a"}, {"message": "b"}],
            mode="thread",
            approach_id="model",
            pause_seconds=0,
        )
        self.assertEqual(job["pause_seconds"], 0)
        self.assertEqual(
            job["items"][0]["payload"]["conversation"],
            job["items"][1]["payload"]["conversation"],
        )
        for _ in range(2):
            active = self.store.claim_next()
            self.assertEqual(active["payload"]["approach_id"], "model")
            self.store.update_request(active["id"], "done")
        self.assertEqual(self.store.get_job(job["id"])["status"], "done")
        self.assertEqual(self.store.cancel_job(job["id"])["status"], "done")

    def test_canonical_aliases_and_parent(self):
        self.store.put_conversation("alias-a", "session", 1)
        self.store.put_conversation("alias-b", "session", 3)
        for alias in ("alias-a", "alias-b", "session"):
            self.assertEqual(self.store.get_conversation(alias)["parent_idx"], 3)
        self.assertEqual(len(self.store.list_conversations()), 1)
        with self.assertRaises(ConversationConflict):
            self.store.put_conversation("alias-a", "other", 5)
        self.assertIsNone(self.store.get_conversation("other"))
        with self.assertRaises(ConversationConflict):
            self.store.put_conversation("new", "alias-a", 6)
        for invalid in (-1, True, "1"):
            with self.assertRaises(ValueError):
                self.store.put_conversation("invalid", "sid", invalid)

    def test_events_persist_and_cursor(self):
        rid = self.store.create_request({"message": "x"})["id"]
        event = {"type": "start"}
        self.assertEqual(self.store.append_event(rid, event)["seq"], 1)
        self.store.append_event(rid, {"type": "text", "text": "hello"})
        self.assertNotIn("seq", event)
        self.assertEqual(
            self.other().list_events(rid, after=1),
            [{"seq": 2, "type": "text", "text": "hello"}],
        )
        with self.assertRaises(KeyError):
            self.store.append_event("missing", {})

    def test_request_status_is_a_cheap_projection(self):
        rid = self.store.create_request({"message": "x"})["id"]
        self.assertEqual(self.store.request_status(rid), "queued")
        self.store.claim_next()
        self.assertEqual(self.store.request_status(rid), "running")
        self.assertIsNone(self.store.request_status("missing"))

    def test_return_unsent_releases_a_truly_unsent_claim(self):
        rid = self.store.create_request({"message": "x"})["id"]
        self.store.claim_next()
        self.store.return_unsent(rid)
        self.assertEqual(self.store.get_request(rid)["status"], "queued")

    def test_return_unsent_blocks_on_dedicated_event_rows(self):
        rid = self.store.create_request({"message": "x"})["id"]
        self.store.claim_next()
        self.store.append_event(rid, {"type": "start"})
        with self.assertRaises(InvalidTransition):
            self.store.return_unsent(rid)
        self.assertEqual(self.store.get_request(rid)["status"], "running")

    def test_import_conversations_conflicts_invalids_readonly(self):
        data = {
            "threads": {
                "first": {"chat_session_id": "sid", "parent_idx": 1},
                "same": {"chat_session_id": "sid", "parent_idx": 1},
                "conflicting": {"chat_session_id": "sid", "parent_idx": 2},
                "bad": {"chat_session_id": None},
            },
            "openai_ids": {"response-id": "first", "bad-alias": "missing"},
            "fingerprints": {"fingerprint": "same"},
        }
        path = self.fixture("conversations.json", data)
        before = path.read_bytes()
        report = self.store.import_legacy(path)
        self.assertEqual(report["imported"], 4)
        self.assertEqual(len(report["conflicts"]), 1)
        self.assertEqual(len(report["invalids"]), 2)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.store.get_conversation("response-id")["parent_idx"], 1)
        self.assertIsNone(self.store.get_conversation("conflicting"))
        self.assertEqual(self.store.import_legacy(path)["skipped"], 4)

    def test_import_jobs_recovery_dedupe_atomic_failure(self):
        data = {
            "id": "job_legacy",
            "status": "running",
            "items": [
                {"message": "a", "status": "done", "text": "answer"},
                {"message": "b", "status": "running"},
                {"message": "c", "status": "queued"},
            ],
        }
        path = self.fixture("job.json", data)
        before = path.read_bytes()
        self.assertEqual(self.store.import_legacy(path)["imported"], 1)
        items = self.store.get_job("job_legacy")["items"]
        self.assertEqual([i["status"] for i in items], ["done", "unknown", "queued"])
        self.assertEqual(items[0]["text"], "answer")
        self.assertEqual(self.store.import_legacy(path)["skipped"], 1)
        self.assertEqual(path.read_bytes(), before)
        conflict = self.fixture("conflict.json", {**data, "status": "cancelled"})
        self.assertEqual(len(self.store.import_legacy(conflict)["conflicts"]), 1)
        invalid = self.fixture(
            "invalid.json",
            {
                "id": "invalid",
                "items": [{"message": "ok"}, {"message": "bad", "status": "invalid"}],
            },
        )
        self.assertEqual(len(self.store.import_legacy(invalid)["invalids"]), 1)
        self.assertIsNone(self.store.get_job("invalid"))
        self.assertEqual(self.store.pending_count(), 1)

    def test_import_cancelled_cannot_revive(self):
        path = self.fixture(
            "cancelled.json",
            {
                "id": "cancelled",
                "status": "cancelled",
                "items": [
                    {"message": "a", "status": "queued"},
                    {"message": "b", "status": "running"},
                ],
            },
        )
        self.assertEqual(self.store.import_legacy(path)["imported"], 1)
        job = self.store.get_job("cancelled")
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual([i["status"] for i in job["items"]], ["cancelled", "unknown"])
        self.assertIsNone(self.store.claim_next())

    def test_import_limit_malformed_missing(self):
        store = self.other(limit=1)
        path = self.fixture(
            "large.json", {"id": "large", "items": [{"message": "a"}] * 2}
        )
        self.assertEqual(len(store.import_legacy(path)["invalids"]), 1)
        self.assertEqual(store.list_jobs(), [])
        invalid = self.root / "malformed.json"
        invalid.write_text("not JSON", encoding="utf-8")
        self.assertEqual(len(store.import_legacy(invalid)["invalids"]), 1)
        self.assertEqual(
            len(store.import_legacy(self.root / "absent.json")["invalids"]), 1
        )

    def test_missing_and_protected_fields(self):
        self.assertIsNone(self.store.get_request("missing"))
        self.assertIsNone(self.store.get_job("missing"))
        self.assertIsNone(self.store.cancel_job("missing"))
        self.assertIsNone(self.store.update_request("missing", "done"))
        rid = self.store.create_request({"message": "hi"})["id"]
        with self.assertRaises(ValueError):
            self.store.update_request(rid, "queued", payload={})
        with self.assertRaises(ValueError):
            self.store.create_request({}, job_id="missing")


if __name__ == "__main__":
    unittest.main()
