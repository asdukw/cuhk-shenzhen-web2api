"""Offline transport contracts; no browser, credentials, or network required."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx

from cuhk_shenzhen_web2api.v2.transport import (
    CHAT_PATH,
    HTTPXTransport,
    PlaywrightTransport,
    TransportError,
    build_transport,
    cookies_from_storage_state,
    csrf_for_url,
    retry_after_seconds,
    valid_identity,
)


def cookie(
    name="csrftoken", value="correct", domain="ai.cuhk.edu.cn", path="/", **extra
):
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": path,
        "secure": True,
        "httpOnly": True,
        "expires": -1,
        "sameSite": "Lax",
        **extra,
    }


class Chunks(httpx.AsyncByteStream):
    def __init__(self, parts):
        self.parts = parts
        self.closed = False
        self.reads = 0

    async def __aiter__(self):
        for part in self.parts:
            self.reads += 1
            if isinstance(part, Exception):
                raise part
            yield part

    async def aclose(self):
        self.closed = True


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def client(self, handler, state=None, **kwargs):
        client = HTTPXTransport(state, transport=httpx.MockTransport(handler), **kwargs)
        self.addAsyncCleanup(client.close)
        return client

    async def test_initial_state_and_authentication(self):
        client = self.client(
            lambda r: httpx.Response(200, json={"username": "private"})
        )
        self.assertEqual(client.state, "not_logged_in")
        self.assertEqual(client.backend, "httpx")
        self.assertTrue(client.streaming)
        with self.assertRaises(TransportError) as caught:
            await client.request("/config/")
        self.assertTrue(caught.exception.known_not_submitted)
        await client.authenticate()
        self.assertEqual(client.state, "authenticated")
        await client.close()
        with self.assertRaises(TransportError):
            await client.probe()

    async def test_identity_requires_real_fields(self):
        for body in (
            {},
            {"ok": True},
            {"authenticated": True},
            {"error": "private"},
            {"username": "x", "authenticated": False},
            [],
            None,
        ):
            with self.subTest(body=body):
                client = self.client(
                    lambda r, body=body: httpx.Response(200, json=body)
                )
                with self.assertRaises(TransportError):
                    await client.authenticate()
                self.assertEqual(client.state, "expired")
        self.assertTrue(valid_identity({"user": {"id": 123}}))
        self.assertTrue(valid_identity([{"user_id": "id"}]))

    async def test_html_login_rejected_and_sanitized(self):
        client = self.client(
            lambda r: httpx.Response(200, text="<html>secret-token</html>")
        )
        with self.assertRaises(TransportError) as caught:
            await client.authenticate()
        self.assertNotIn("secret", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    async def test_redirect_never_followed(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(
                302, headers={"location": "https://evil.invalid/?token=secret"}
            )

        client = self.client(handler)
        with self.assertRaises(TransportError) as caught:
            await client.authenticate()
        self.assertEqual(len(requests), 1)
        self.assertEqual(caught.exception.status, 302)
        self.assertNotIn("secret", str(caught.exception.as_dict()))

    async def test_scoped_cookies_csrf_and_empty_post(self):
        state = {
            "cookies": [
                cookie(value="root"),
                cookie(value="narrow", path="/chat/"),
                cookie(value="wrong", domain="other.invalid"),
                cookie("old", expires=1),
            ]
        }
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"id": "x"})

        client = self.client(handler, state)
        await client.authenticate()
        await client.request("/chat/", {})
        request = requests[-1]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.headers["x-csrftoken"], "narrow")
        self.assertNotIn("wrong", request.headers["cookie"])
        self.assertNotIn("old=", request.headers["cookie"])
        self.assertEqual(json.loads(request.content), {})

    async def test_host_only_and_secure_cookie_scope(self):
        cookies = cookies_from_storage_state({"cookies": [cookie()]})
        self.assertEqual(
            csrf_for_url(cookies, "https://ai.cuhk.edu.cn/chat/"), "correct"
        )
        self.assertIsNone(csrf_for_url(cookies, "https://sub.ai.cuhk.edu.cn/chat/"))
        self.assertIsNone(csrf_for_url(cookies, "http://ai.cuhk.edu.cn/chat/"))
        self.assertIsNone(csrf_for_url(cookies, "https://evil.invalid/chat/"))

    async def test_domain_cookie_subdomain_scope(self):
        cookies = cookies_from_storage_state(
            {"cookies": [cookie(domain=".cuhk.edu.cn")]}
        )
        self.assertEqual(csrf_for_url(cookies, "https://ai.cuhk.edu.cn/"), "correct")
        self.assertIsNone(csrf_for_url(cookies, "https://notcuhk.edu.cn/"))

    async def test_untrusted_paths_rejected_before_io(self):
        handler = AsyncMock(return_value=httpx.Response(200, json={"id": "x"}))
        client = self.client(handler)
        for path in (
            "https://evil.invalid/",
            "//evil.invalid/",
            "/%2fevil.invalid/",
            "/\\evil.invalid/",
            "/x\n",
            "/x#token",
        ):
            with self.subTest(path=path), self.assertRaises(TransportError):
                await client.request(path)
        handler.assert_not_called()

    async def test_incremental_ndjson_split_utf8_and_no_read_ahead(self):
        raw = json.dumps(
            {"event": "msg", "item": {"type": "text", "content": "中文"}},
            ensure_ascii=False,
        ).encode()
        stream = Chunks(
            [
                b'{"event":"start","chat_session_id":"x"}\n',
                raw[:58],
                raw[58:] + b"\n",
                b'{"event":"end","status":"finished"}\n',
            ]
        )
        client = self.client(
            lambda r: httpx.Response(
                200,
                stream=stream,
                headers={"content-type": "application/x-ndjson+json"},
            )
        )
        client.state = "authenticated"
        events = client.events({"content": "test"})
        first = await anext(events)
        self.assertEqual(first["event"], "start")
        self.assertEqual(stream.reads, 1)
        rest = [item async for item in events]
        self.assertEqual(rest[0]["item"]["content"], "中文")
        self.assertTrue(stream.closed)

    async def test_final_line_without_newline(self):
        stream = Chunks([b'\n{"event":"hb"}\r\n{"event":"end","status":"finished"}'])
        client = self.client(
            lambda r: httpx.Response(
                200, stream=stream, headers={"content-type": "application/x-ndjson"}
            )
        )
        client.state = "authenticated"
        self.assertEqual(len([event async for event in client.events({})]), 2)

    async def test_incomplete_and_invalid_streams_are_ambiguous(self):
        for body, content_type in (
            (b'{"event":"start"}\n', "application/x-ndjson"),
            (b"secret\n", "application/x-ndjson"),
            (b"<html>secret</html>", "text/html"),
            (b"[]\n", "application/x-ndjson"),
        ):
            client = self.client(
                lambda r, body=body, content_type=content_type: httpx.Response(
                    200, content=body, headers={"content-type": content_type}
                )
            )
            client.state = "authenticated"
            with self.assertRaises(TransportError) as caught:
                _ = [item async for item in client.events({})]
            self.assertFalse(caught.exception.known_not_submitted)
            self.assertNotIn("secret", str(caught.exception))

    async def test_login_wall_html_is_classified_as_auth_expiry(self):
        client = self.client(
            lambda r: httpx.Response(
                200, text="<html>sign in</html>", headers={"content-type": "text/html"}
            )
        )
        client.state = "authenticated"
        with self.assertRaises(TransportError) as caught:
            _ = [item async for item in client.events({})]
        self.assertEqual(caught.exception.code, "authentication_required")
        self.assertEqual(caught.exception.status, 200)
        self.assertNotIn("sign in", str(caught.exception))

    async def test_early_generator_close_closes_response(self):
        stream = Chunks([b'{"event":"start"}\n', b'{"event":"end"}\n'])
        client = self.client(
            lambda r: httpx.Response(
                200, stream=stream, headers={"content-type": "application/x-ndjson"}
            )
        )
        client.state = "authenticated"
        events = client.events({})
        await anext(events)
        await events.aclose()
        self.assertTrue(stream.closed)
        self.assertEqual(stream.reads, 1)

    async def test_rejections_have_retry_metadata_without_bodies(self):
        for status in (401, 403, 429, 503):
            client = self.client(
                lambda r, status=status: httpx.Response(
                    status, text="secret", headers={"retry-after": "7"}
                )
            )
            client.state = "authenticated"
            with self.assertRaises(TransportError) as caught:
                _ = [event async for event in client.events({})]
            error = caught.exception
            self.assertEqual(error.status, status)
            self.assertEqual(error.known_not_submitted, status in (401, 403, 429))
            self.assertNotIn("secret", str(error.as_dict()))
            if status == 429:
                self.assertEqual(error.retry_after, 7)
        self.assertEqual(retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT"), 0)
        self.assertIsNone(retry_after_seconds("private-token"))

    async def test_network_submission_classification(self):
        for error_type, safe in (
            (httpx.ConnectError, True),
            (httpx.ConnectTimeout, True),
            (httpx.PoolTimeout, True),
            (httpx.ReadTimeout, False),
            (httpx.WriteError, False),
        ):

            def handler(request, error_type=error_type):
                raise error_type("secret", request=request)

            client = self.client(handler)
            client.state = "authenticated"
            with self.assertRaises(TransportError) as caught:
                _ = [event async for event in client.events({})]
            self.assertEqual(caught.exception.known_not_submitted, safe)
            self.assertEqual(str(caught.exception), "network_error")

    async def test_browser_is_buffered_and_rejects_wrong_origin(self):
        page = AsyncMock()
        page.context.cookies.return_value = []
        page.evaluate.return_value = {"status": 0, "preflight": True}
        client = PlaywrightTransport(page)
        self.assertFalse(client.streaming)
        with self.assertRaises(TransportError) as caught:
            await client.authenticate()
        self.assertTrue(caught.exception.known_not_submitted)
        await client.close()
        page.close.assert_not_called()

    async def test_browser_buffered_auth_and_stream(self):
        page = AsyncMock()
        page.context.cookies.return_value = []
        page.evaluate.return_value = {
            "status": 200,
            "ct": "application/json",
            "txt": '{"id":"x"}',
        }
        client = PlaywrightTransport(page)
        await client.authenticate()
        page.evaluate.return_value = {
            "status": 200,
            "ct": "application/x-ndjson",
            "txt": '{"event":"end","status":"finished"}\n',
        }
        self.assertEqual(
            [event async for event in client.events({})],
            [{"event": "end", "status": "finished"}],
        )
        expression, args = page.evaluate.call_args.args
        self.assertIn("location.origin !== origin", expression)
        self.assertIn("redirect: 'manual'", expression)
        self.assertEqual(args["url"], "https://ai.cuhk.edu.cn/chat/")

    async def test_dead_page_is_dropped_after_a_broken_fetch(self):
        page = AsyncMock()
        page.context.cookies.return_value = []
        page.is_closed = Mock(return_value=True)
        page.evaluate.side_effect = RuntimeError("Target page has been closed")
        client = PlaywrightTransport(page)
        client.state = "authenticated"
        with self.assertRaises(TransportError) as caught:
            await client._fetch(CHAT_PATH, {})
        self.assertEqual(str(caught.exception), "browser_error")
        # The dead handle is cleared so the next turn relaunches instead of
        # reusing a closed page and settling the whole queue unknown.
        self.assertIsNone(client.page)

    async def test_live_page_is_kept_when_fetch_fails_transiently(self):
        page = AsyncMock()
        page.context.cookies.return_value = []
        page.is_closed = Mock(return_value=False)
        page.evaluate.side_effect = RuntimeError("js threw once")
        client = PlaywrightTransport(page)
        client.state = "authenticated"
        with self.assertRaises(TransportError):
            await client._fetch(CHAT_PATH, {})
        self.assertIs(client.page, page)

    async def test_factory_is_offline_and_requires_explicit_auth(self):
        client = await build_transport("httpx", Path("unused"), {})
        assert isinstance(client, HTTPXTransport)
        self.addAsyncCleanup(client.close)
        self.assertEqual(client.state, "not_logged_in")
        self.assertEqual(len(client.client.cookies), 0)
        browser = await build_transport("playwright", Path("unused"), {})
        assert isinstance(browser, PlaywrightTransport)
        self.assertIsNone(browser.page)
        await browser.close()
        with self.assertRaises(TransportError):
            await build_transport(
                "httpx", Path("unused"), {"auth_dir": "missing-auth-directory"}
            )

    async def test_diagnostic_refuses_existing_directory(self):
        from argparse import Namespace

        from cuhk_shenzhen_web2api.v2.diagnose import diagnose

        # Existing temp directory: no browser import, launch, or output occurs.
        with tempfile.TemporaryDirectory() as directory:
            result = await diagnose(
                Namespace(data_dir=directory, backend="httpx", chat=False, model=None)
            )
            self.assertEqual(result, 2)
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
