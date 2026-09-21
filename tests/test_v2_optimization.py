"""Offline timing, benchmark gates, auth handover and reconciliation contracts."""

import argparse
import asyncio
import tempfile
import unittest
from pathlib import Path

from cuhk_shenzhen_web2api.v2.benchmark import validate_previous
from cuhk_shenzhen_web2api.v2.config import Settings
from cuhk_shenzhen_web2api.v2.engine import Engine
from cuhk_shenzhen_web2api.v2.metrics import durations, summarize
from cuhk_shenzhen_web2api.v2.recovery import auth_directory, replace_auth
from cuhk_shenzhen_web2api.v2.store import Store
from cuhk_shenzhen_web2api.v2.transport import TransportError


class Transport:
    state = "authenticated"
    closed = False

    def __init__(self, fail=False):
        self.fail = fail
        self.gate = None
        self.calls = 0

    async def authenticate(self):
        if self.fail:
            raise TransportError("authentication_required")

    async def events(self, payload):
        self.calls += 1
        yield {
            "event": "start",
            "chat_session_id": "sid",
            "user_msg_idx": 1,
            "approach_msg_idx": 2,
        }
        if self.gate:
            await self.gate.wait()
        yield {"event": "msg", "item": {"type": "text", "content": "ok"}}
        yield {"event": "end", "status": "finished"}

    async def close(self):
        self.closed = True


class OptimizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_unsent_paced_request_survives_auth_pause(self):
        store = Store(":memory:")
        transport = Transport()
        engine = Engine(store, transport, interval=0)
        entered, release = asyncio.Event(), asyncio.Event()

        async def pace():
            entered.set()
            await release.wait()

        engine._pace = pace
        try:
            await engine.start()
            request = await engine.submit({"message": "test"})
            await asyncio.wait_for(entered.wait(), 2)
            engine.paused = True
            release.set()
            for _ in range(100):
                if not engine._tasks:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(store.get_request(request["id"])["status"], "queued")
            self.assertEqual(transport.calls, 0)
            engine.resume()
            result = await asyncio.wait_for(engine.result(request["id"]), 2)
            self.assertEqual(result["status"], "done")
            self.assertEqual(transport.calls, 1)
        finally:
            await engine.close()
            store.close()

    async def test_persisted_timings_and_summary(self):
        store = Store(":memory:")
        transport = Transport()
        engine = Engine(store, transport, interval=0)
        try:
            await engine.start()
            request = await engine.submit({"message": "test"})
            result = await asyncio.wait_for(engine.result(request["id"]), 2)
            self.assertLessEqual(result["created_at"], result["sent_at"])
            self.assertLessEqual(result["sent_at"], result["first_text_at"])
            self.assertLessEqual(result["first_text_at"], result["finished_at"])
            self.assertEqual(result["attempts"], 1)
            report = summarize([{"passed": True, "server": durations(result)}], 2)
            self.assertEqual(report["success_rate"], 1)
            self.assertEqual(report["successful_requests_per_minute"], 30)
        finally:
            await engine.close()
            store.close()

    async def test_auth_handover_waits_active_and_preserves_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "new").mkdir()
            (root / "new" / "storage_state.json").write_text("{}")
            settings = Settings(root / "runtime", "key", auth_root=root)
            store = Store(":memory:")
            old = Transport()
            old.gate = asyncio.Event()
            engine = Engine(store, old, interval=0)
            candidate = Transport()

            async def factory(*args):
                return candidate

            await engine.start()
            first = await engine.submit({"message": "first"})
            while not old.calls:
                await asyncio.sleep(0.001)
            second = await engine.submit({"message": "second"})
            switch = asyncio.create_task(
                replace_auth(engine, settings, "new", factory=factory, timeout=2)
            )
            await asyncio.sleep(0.02)
            self.assertFalse(switch.done())
            self.assertEqual(store.get_request(second["id"])["status"], "queued")
            old.gate.set()
            await switch
            await asyncio.wait_for(engine.result(first["id"]), 2)
            await asyncio.wait_for(engine.result(second["id"]), 2)
            self.assertTrue(old.closed)
            self.assertEqual(candidate.calls, 1)
            await engine.close()
            store.close()

    async def test_auth_failure_keeps_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "new").mkdir()
            (root / "new" / "storage_state.json").write_text("{}")
            store = Store(":memory:")
            old = Transport()
            engine = Engine(store, old, interval=0)
            candidate = Transport(fail=True)

            async def factory(*args):
                return candidate

            with self.assertRaises(TransportError):
                await replace_auth(
                    engine,
                    Settings(root / "runtime", "key", auth_root=root),
                    "new",
                    factory=factory,
                )
            self.assertIs(engine.transport, old)
            self.assertFalse(old.closed)
            self.assertFalse(engine.maintenance)
            self.assertTrue(candidate.closed)
            await engine.close()
            store.close()

    def test_auth_path_escape_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ["../other", "C:\\secret", ".", "..", "/tmp", "new/sub"]:
                with self.assertRaises(ValueError):
                    auth_directory(Path(directory), name)

    def test_benchmark_gates(self):
        args = argparse.Namespace(stage=1, interval=20, model="m", count=12)
        validate_previous(args, None)
        args.interval = 5
        with self.assertRaises(ValueError):
            validate_previous(args, None)
        previous = {
            "passed": True,
            "stage": 1,
            "interval": 20,
            "model": "m",
            "requested": 12,
            "protocol": "short-marker-v2",
        }
        with self.assertRaises(ValueError):
            validate_previous(args, previous)
        args.interval = 10
        validate_previous(args, previous)
        args.stage = 4
        args.interval = 20
        with self.assertRaises(ValueError):
            validate_previous(args, previous)


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.store.put_conversation("named", "sid", 2)
        record = self.store.create_request(
            {"message": "hello", "conversation": "named", "parent_idx": 2}
        )
        self.rid = record["id"]
        self.store.claim_next()
        self.store.update_request(
            self.rid,
            "unknown",
            result={"chat_session_id": "sid", "user_msg_idx": 3, "approach_msg_idx": 4},
        )
        self.history = {
            "chat_session_id": "sid",
            "messages": [
                {
                    "role": "approach",
                    "self_idx": 4,
                    "parent_idx": 3,
                    "status": "finished",
                    "items": [{"type": "text", "content": "complete"}],
                }
            ],
        }

    def tearDown(self):
        self.store.close()

    def test_matching_finished_evidence_commits_and_unlocks(self):
        self.assertEqual(
            self.store.reconcile_request(self.rid, self.history)["status"], "done"
        )
        self.assertEqual(self.store.get_conversation("named")["parent_idx"], 4)
        self.assertFalse(self.store.unresolved_conversation("named", "sid"))
        self.assertEqual(self.store.get_request(self.rid)["result"]["text"], "complete")

    def test_missing_completion_keeps_unknown(self):
        del self.history["messages"][0]["status"]
        self.assertEqual(
            self.store.reconcile_request(self.rid, self.history)["status"], "unknown"
        )
        self.assertEqual(self.store.get_conversation("named")["parent_idx"], 2)

    def test_wrong_session_keeps_unknown(self):
        self.history["chat_session_id"] = "other"
        self.assertEqual(
            self.store.reconcile_request(self.rid, self.history)["reason"],
            "session_not_confirmed",
        )

    def test_newer_parent_never_rolled_back(self):
        self.store.put_conversation("named", "sid", 6)
        self.assertEqual(
            self.store.reconcile_request(self.rid, self.history)["reason"],
            "conversation_conflict",
        )
        self.assertEqual(self.store.get_conversation("named")["parent_idx"], 6)
