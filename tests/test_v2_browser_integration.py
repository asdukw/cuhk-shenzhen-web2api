"""Real Chromium + loopback upstream only; no campus credentials or network."""

import asyncio
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.async_api import async_playwright

from cuhk_shenzhen_web2api.v2.transport import PlaywrightTransport


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = (
            b'{"username":"offline-test"}'
            if self.path == "/.auth/me/"
            else b"<html>test</html>"
        )
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "application/json" if self.path == "/.auth/me/" else "text/html",
        )
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson+json")
        self.end_headers()
        try:
            events = [
                {
                    "event": "start",
                    "chat_session_id": data["content"],
                    "approach_msg_idx": 2,
                }
            ]
            events += [
                {"event": "msg", "item": {"type": "text", "content": "中文"}}
            ] * (40 if data["content"] == "burst" else 1)
            for event in events:
                raw = (json.dumps(event, ensure_ascii=False) + "\n").encode()
                for byte in raw:
                    self.wfile.write(bytes([byte]))
                self.wfile.flush()
            if data["content"] == "delayed":
                self.server.finish_gate.wait(3)
            self.wfile.write(b'{"event":"end","status":"finished"}\n')
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


class BrowserIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        local_browsers = Path(__file__).resolve().parents[1] / ".playwright-browsers"
        if local_browsers.exists():
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(local_browsers)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.finish_gate = threading.Event()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.runtime = await async_playwright().start()
        self.browser = await self.runtime.chromium.launch(
            headless=True, args=["--disable-gpu"]
        )
        self.page = await self.browser.new_page()
        await self.page.goto(self.origin)
        self.transport = PlaywrightTransport(
            self.page, base_url=self.origin, stream_mode="incremental"
        )
        await self.transport.authenticate()

    async def asyncTearDown(self):
        self.server.finish_gate.set()
        await self.browser.close()
        await self.runtime.stop()
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
        self.thread.join(2)

    async def test_first_text_before_end_and_split_utf8(self):
        source = self.transport.events({"content": "delayed"})
        self.assertEqual((await anext(source))["event"], "start")
        msg = await asyncio.wait_for(anext(source), 2)
        self.assertEqual(msg["item"]["content"], "中文")
        self.assertFalse(self.server.finish_gate.is_set())
        self.server.finish_gate.set()
        self.assertEqual((await anext(source))["event"], "end")
        await source.aclose()
        self.assertEqual(
            await self.page.evaluate("globalThis.__campusV2Streams.size"), 0
        )

    async def test_backpressure_and_cancel_cleanup(self):
        source = self.transport.events({"content": "burst"})
        await anext(source)
        await asyncio.sleep(0.1)
        size = await self.page.evaluate(
            "[...globalThis.__campusV2Streams.values()][0].queue.length"
        )
        self.assertLessEqual(size, 8)
        await source.aclose()
        self.assertEqual(
            await self.page.evaluate("globalThis.__campusV2Streams.size"), 0
        )

    async def test_parallel_request_isolation(self):
        async def collect(name):
            return [event async for event in self.transport.events({"content": name})]

        first, second = await asyncio.gather(collect("one"), collect("two"))
        self.assertEqual(first[0]["chat_session_id"], "one")
        self.assertEqual(second[0]["chat_session_id"], "two")
        self.assertEqual(first[-1]["event"], "end")
        self.assertEqual(second[-1]["event"], "end")
        self.assertEqual(
            await self.page.evaluate("globalThis.__campusV2Streams.size"), 0
        )
