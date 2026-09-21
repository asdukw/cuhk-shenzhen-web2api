"""Offline engine tests: real in-memory SQLite and controlled async transport."""

import asyncio
import time
import unittest
from unittest import mock

from cuhk_shenzhen_web2api.v2.engine import Engine
from cuhk_shenzhen_web2api.v2.store import ConversationConflict, Store


class Failure(RuntimeError):
    def __init__(self, safe=False, status=None, retry_after=None):
        self.known_not_submitted = safe
        self.status = status
        self.retry_after = retry_after
        self.code = "mock_failure"


class Transport:
    state = "authenticated"
    backend = "mock"
    streaming = True

    def __init__(self):
        self.calls = []
        self.times = []
        self.failures = []
        self.gate = None
        self.end_status = "finished"
        self.disconnect = False
        self.chunk = "hello"
        self.active = 0
        self.peak = 0
        self.closed = False
        self.stream_closed = 0

    async def events(self, payload):
        self.calls.append(payload)
        self.times.append(time.perf_counter())
        if self.failures:
            raise self.failures.pop(0)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            index = len(self.calls) * 2
            yield {
                "event": "start",
                "chat_session_id": payload.get("chat_session_id") or "s" + str(index),
                "user_msg_idx": index - 1,
                "approach_msg_idx": index,
            }
            if self.gate:
                await self.gate.wait()
            yield {"event": "msg", "item": {"type": "text", "content": self.chunk}}
            if self.disconnect:
                raise Failure()
            yield {"event": "end", "status": self.end_status, "title": "title"}
        finally:
            self.active -= 1
            self.stream_closed += 1

    async def close(self):
        self.closed = True


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(":memory:")
        self.transport = Transport()
        self.engine = Engine(self.store, self.transport, interval=0)

    async def asyncTearDown(self):
        await self.engine.close()
        self.store.close()

    async def wait_for(self, predicate):
        async with asyncio.timeout(2):
            while not predicate():
                await asyncio.sleep(0.002)

    async def final(self, request):
        return await asyncio.wait_for(self.engine.result(request["id"]), 2)

    async def test_one_fifo_for_jobs_and_realtime_and_zero_pause(self):
        first = await self.engine.submit({"message": "first"})
        job = self.store.create_job(
            [{"message": "batch1"}, {"message": "batch2"}], pause_seconds=0
        )
        last = await self.engine.submit({"message": "last"})
        await self.engine.start()

        self.assertEqual((await self.final(last))["status"], "done")
        self.assertEqual(
            [p["content"] for p in self.transport.calls],
            ["first", "batch1", "batch2", "last"],
        )
        self.assertEqual(self.store.get_job(job["id"])["status"], "done")
        self.assertEqual((await self.final(first))["result"]["text"], "hello")

    async def test_concurrency_is_bounded(self):
        self.engine.concurrency = 2
        self.transport.gate = asyncio.Event()
        requests = [await self.engine.submit({"message": str(i)}) for i in range(4)]
        await self.engine.start()
        await self.wait_for(lambda: self.transport.active == 2)
        self.assertEqual(len(self.transport.calls), 2)
        self.transport.gate.set()
        await self.final(requests[-1])
        self.assertEqual(self.transport.peak, 2)

    async def test_aliases_lock_actual_session_and_read_latest_parent(self):
        self.engine.concurrency = 3
        self.store.put_conversation("alias-a", "shared", 0)
        self.store.put_conversation("alias-b", "shared", 0)
        self.transport.gate = asyncio.Event()
        a = await self.engine.submit({"message": "a", "conversation": "alias-a"})
        b = await self.engine.submit({"message": "b", "conversation": "alias-b"})
        c = await self.engine.submit(
            {"message": "c", "chat_session_id": "shared", "parent_idx": 0}
        )
        await self.engine.start()
        await self.wait_for(lambda: len(self.transport.calls) == 1)
        await asyncio.sleep(0.04)
        self.assertEqual(len(self.transport.calls), 1)
        self.transport.gate.set()
        await self.final(c)
        self.assertEqual([p["parent_idx"] for p in self.transport.calls], [0, 2, 4])
        self.assertEqual(self.store.get_conversation("alias-a")["parent_idx"], 6)
        self.assertEqual((await self.final(a))["status"], "done")
        self.assertEqual((await self.final(b))["status"], "done")

    async def test_new_named_thread_serializes_before_it_has_session(self):
        self.engine.concurrency = 2
        a = await self.engine.submit({"message": "a", "conversation": "new"})
        b = await self.engine.submit({"message": "b", "conversation": "new"})
        await self.engine.start()
        await self.final(b)
        self.assertEqual(
            self.transport.calls[1]["chat_session_id"],
            (await self.final(a))["chat_session_id"],
        )
        self.assertEqual(self.transport.calls[1]["parent_idx"], 2)
        self.assertEqual((await self.final(a))["result"]["conversation"], "new")

    async def test_start_persisted_before_finish_and_parent_not_advanced(self):
        self.store.put_conversation("alias", "shared", 0)
        self.transport.gate = asyncio.Event()
        req = await self.engine.submit({"message": "a", "conversation": "alias"})
        await self.engine.start()
        await self.wait_for(
            lambda: bool(self.store.get_request(req["id"]).get("events"))
        )
        current = self.store.get_request(req["id"])
        self.assertEqual(current["status"], "running")
        self.assertEqual(current["approach_msg_idx"], 2)
        self.assertEqual(current["events"][0]["event"], "start")
        self.assertEqual(self.store.get_conversation("alias")["parent_idx"], 0)

    async def test_aborted_turn_never_advances_parent(self):
        self.store.put_conversation("alias", "shared", 0)
        self.transport.end_status = "aborted"
        req = await self.engine.submit({"message": "a", "conversation": "alias"})
        await self.engine.start()
        self.assertEqual((await self.final(req))["status"], "error")
        self.assertEqual(self.store.get_conversation("alias")["parent_idx"], 0)

    async def test_disconnect_unknown_no_retry(self):
        self.transport.disconnect = True
        req = await self.engine.submit({"message": "a"})
        await self.engine.start()
        self.assertEqual((await self.final(req))["status"], "unknown")
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.store.list_conversations(), [])

    async def test_ambiguous_failure_before_start_is_not_retried(self):
        self.transport.failures = [Failure()]
        req = await self.engine.submit({"message": "a"})
        await self.engine.start()
        self.assertEqual((await self.final(req))["status"], "unknown")
        self.assertEqual(len(self.transport.calls), 1)

    async def test_known_rejection_retries_with_retry_after(self):
        self.transport.failures = [Failure(True, 429, 0.04)]
        req = await self.engine.submit({"message": "a"})
        await self.engine.start()
        self.assertEqual((await self.final(req))["status"], "done")
        self.assertEqual(len(self.transport.calls), 2)
        self.assertGreaterEqual(
            self.transport.times[1] - self.transport.times[0], 0.035
        )

    async def test_known_failure_retry_is_bounded(self):
        self.transport.failures = [Failure(True, 429, 0) for _ in range(5)]
        req = await self.engine.submit({"message": "a"})
        await self.engine.start()
        self.assertEqual((await self.final(req))["status"], "error")
        self.assertEqual(len(self.transport.calls), self.engine.MAX_ATTEMPTS)

    async def test_conversation_conflict_keeps_finished_turn_done(self):
        req = await self.engine.submit({"message": "a"})
        with mock.patch.object(
            self.store,
            "put_conversation",
            side_effect=ConversationConflict("conflicting parent_idx"),
        ):
            await self.engine.start()
            result = await self.final(req)
        self.assertEqual(result["status"], "done")
        self.assertEqual(self.store.get_request(req["id"])["status"], "done")

    async def test_retry_after_is_clamped_to_max(self):
        self.transport.failures = [Failure(True, 500, 9_999_999.0)]
        req = await self.engine.submit({"message": "a"})
        retry_delays = []
        real_sleep = asyncio.sleep

        async def fake_sleep(delay):
            if delay > self.engine.POLL_SECONDS:
                retry_delays.append(delay)
            await real_sleep(0)

        with mock.patch("cuhk_shenzhen_web2api.v2.engine.asyncio.sleep", fake_sleep):
            await self.engine.start()
            result = await self.final(req)
        self.assertEqual(result["status"], "done")
        self.assertEqual(len(self.transport.calls), 2)
        self.assertEqual(retry_delays, [self.engine.MAX_RETRY_AFTER])

    async def test_non_numeric_retry_after_falls_back_without_crashing(self):
        self.transport.failures = [Failure(True, 500, "soon")]
        req = await self.engine.submit({"message": "a"})
        retry_delays = []
        real_sleep = asyncio.sleep

        async def fake_sleep(delay):
            if delay > self.engine.POLL_SECONDS:
                retry_delays.append(delay)
            await real_sleep(0)

        with mock.patch("cuhk_shenzhen_web2api.v2.engine.asyncio.sleep", fake_sleep):
            await self.engine.start()
            result = await self.final(req)
        self.assertEqual(result["status"], "done")
        self.assertEqual(len(self.transport.calls), 2)
        self.assertEqual(retry_delays, [0.25])

    async def test_maintenance_bounces_a_claimed_but_unsent_turn(self):
        req = await self.engine.submit({"message": "a"})
        claimed = self.store.claim_next()
        self.assertEqual(claimed["id"], req["id"])
        self.assertEqual(claimed["status"], "running")
        # maintenance flips after the dispatcher already created this task.
        self.engine.maintenance = True
        payload, cid, keys = self.engine._resolve(claimed)
        await self.engine._run(claimed, payload, cid, keys)
        # The turn is returned to the queue and never touches the transport, so
        # it runs on the post-swap transport instead of a swapping-out one.
        self.assertEqual(self.store.get_request(req["id"])["status"], "queued")
        self.assertEqual(self.transport.calls, [])

    async def test_auth_failure_pauses_queue_until_resume(self):
        self.transport.failures = [Failure(True, 401)]
        a = await self.engine.submit({"message": "a"})
        b = await self.engine.submit({"message": "b"})
        await self.engine.start()
        self.assertEqual((await self.final(a))["status"], "error")
        self.assertTrue(self.engine.paused)
        await asyncio.sleep(0.04)
        self.assertEqual(len(self.transport.calls), 1)
        self.engine.resume()
        self.assertEqual((await self.final(b))["status"], "done")

    async def test_global_interval_applies_to_parallel_submissions(self):
        self.engine.interval = 0.04
        self.engine.concurrency = 3
        requests = [await self.engine.submit({"message": str(i)}) for i in range(3)]
        await self.engine.start()
        await self.final(requests[-1])
        for first, second in zip(self.transport.times, self.transport.times[1:]):
            self.assertGreaterEqual(second - first, 0.035)

    async def test_positive_job_pause_is_honored(self):
        job = self.store.create_job(
            [{"message": "a"}, {"message": "b"}], pause_seconds=0.05
        )
        await self.engine.start()
        await self.final(job["items"][-1])
        self.assertGreaterEqual(
            self.transport.times[1] - self.transport.times[0], 0.045
        )

    async def test_client_stream_close_stops_submitted_task(self):
        self.transport.gate = asyncio.Event()
        req = await self.engine.submit({"message": "a"})
        await self.engine.start()
        stream = self.engine.events(req["id"])
        event = await asyncio.wait_for(anext(stream), 2)
        self.assertEqual(event["event"], "start")
        await stream.aclose()
        self.assertEqual((await self.final(req))["status"], "unknown")
        self.assertEqual(self.transport.active, 0)
        self.assertEqual(len(self.transport.calls), 1)

    async def test_disconnect_queued_request_never_sends(self):
        req = await self.engine.submit({"message": "a"})
        await self.engine.disconnect(req["id"])
        await self.engine.start()
        self.assertEqual((await self.final(req))["status"], "cancelled")
        self.assertEqual(self.transport.calls, [])

    async def test_events_replay_from_persisted_final_record(self):
        req = await self.engine.submit({"message": "a"}, key="idempotent")
        await self.engine.start()
        final = await self.final(req)
        same = await self.engine.submit({"message": "a"}, key="idempotent")
        self.assertEqual(req["id"], same["id"])
        events = [event async for event in self.engine.events(req["id"])]
        self.assertEqual(events, final["events"])
        self.assertEqual([event["event"] for event in events], ["start", "msg", "end"])
        self.assertEqual(len(self.transport.calls), 1)

    async def test_duplicate_active_idempotency_is_rejected(self):
        await self.engine.submit({"message": "a"}, key="active")
        with self.assertRaisesRegex(ValueError, "idempotency"):
            await self.engine.submit({"message": "a"}, key="active")

    async def test_second_stream_subscriber_rejected_without_cancelling_first(self):
        self.transport.gate = asyncio.Event()
        req = await self.engine.submit({"message": "a"})
        await self.engine.start()
        first = self.engine.events(req["id"])
        await asyncio.wait_for(anext(first), 2)
        second = self.engine.events(req["id"])
        with self.assertRaisesRegex(ValueError, "subscriber"):
            await anext(second)
        self.assertEqual(self.store.get_request(req["id"])["status"], "running")
        await first.aclose()

    async def test_event_size_limit_stops_and_preserves_bounded_prefix(self):
        self.engine.MAX_EVENT_BYTES = 150
        self.transport.chunk = "x" * 200
        req = await self.engine.submit({"message": "a"})
        await self.engine.start()
        final = await self.final(req)
        self.assertEqual(final["status"], "unknown")
        self.assertEqual(len(final["events"]), 1)
        self.assertEqual(self.transport.stream_closed, 1)

    async def test_recover_running_never_resubmits(self):
        old = self.store.create_request({"message": "old"})
        self.store.claim_next()
        new = await self.engine.submit({"message": "new"})
        await self.engine.start()
        self.assertEqual((await self.final(old))["status"], "unknown")
        await self.final(new)
        self.assertEqual([p["content"] for p in self.transport.calls], ["new"])

    async def test_independent_zero_pause_batch_can_overlap(self):
        self.engine.concurrency = 2
        self.transport.gate = asyncio.Event()
        job = self.store.create_job(
            [{"message": "one"}, {"message": "two"}], pause_seconds=0
        )
        await self.engine.start()
        await self.wait_for(lambda: self.transport.active == 2)
        self.transport.gate.set()
        for item in job["items"]:
            await self.final(item)
        self.assertEqual(self.transport.peak, 2)

    async def test_no_claim_without_authentication(self):
        self.transport.state = "not_logged_in"
        req = await self.engine.submit({"message": "a"})
        await self.engine.start()
        await asyncio.sleep(0.05)
        self.assertEqual(self.store.get_request(req["id"])["status"], "queued")
        self.assertEqual(self.transport.calls, [])

    async def test_close_marks_active_unknown_and_closes_transport(self):
        self.transport.gate = asyncio.Event()
        req = await self.engine.submit({"message": "a"})
        await self.engine.start()
        await self.wait_for(lambda: self.transport.active == 1)
        await self.engine.close()
        self.assertEqual((await self.final(req))["status"], "unknown")
        self.assertTrue(self.transport.closed)
        self.assertEqual(self.transport.active, 0)

    async def test_cancel_during_pacing_never_sends(self):
        waiting, release = asyncio.Event(), asyncio.Event()

        async def pace():
            waiting.set()
            await release.wait()

        self.engine._pace = pace
        job = self.store.create_job([{"message": "must not send"}], pause_seconds=0)
        await self.engine.start()
        await asyncio.wait_for(waiting.wait(), 2)
        self.store.cancel_job(job["id"])
        release.set()
        result = await self.final(job["items"][0])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.transport.calls, [])

    async def test_unknown_first_turn_blocks_same_name(self):
        self.transport.disconnect = True
        first = await self.engine.submit({"message": "first", "conversation": "name"})
        await self.engine.start()
        self.assertEqual((await self.final(first))["status"], "unknown")
        self.transport.disconnect = False
        second = await self.engine.submit({"message": "second", "conversation": "name"})
        result = await self.final(second)
        self.assertEqual(result["error"], "conversation_unresolved")
        self.assertEqual(len(self.transport.calls), 1)

    async def test_unknown_existing_session_blocks_other_alias(self):
        self.store.put_conversation("first-alias", "campus", 2)
        self.store.put_conversation("second-alias", "campus", 2)
        self.transport.disconnect = True
        first = await self.engine.submit(
            {"message": "first", "conversation": "first-alias"}
        )
        await self.engine.start()
        self.assertEqual((await self.final(first))["status"], "unknown")
        self.transport.disconnect = False
        second = await self.engine.submit(
            {"message": "second", "conversation": "second-alias"}
        )
        result = await self.final(second)
        self.assertEqual(result["error"], "conversation_unresolved")
        self.assertEqual(len(self.transport.calls), 1)


if __name__ == "__main__":
    unittest.main()
