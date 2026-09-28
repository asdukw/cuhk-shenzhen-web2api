"""Offline regression checks for Responses API adapter helpers."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import HTTPException

from cuhk_shenzhen_web2api.chat_client import ChatClient, ChatReply
from cuhk_shenzhen_web2api.server import (
    _compose_response_message,
    _extract_user_message,
    _previous_response_context,
    _remember_response,
    _response_sessions,
)


class FakeBrowser:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.code = ""
        self.upload_args: dict | None = None

    def js_json(self, code: str, timeout: int = 120, retries: int = 6) -> dict:
        del timeout, retries
        self.code = code
        return self.response

    def upload(
        self,
        data: bytes,
        *,
        mime: str,
        endpoint: str,
        filename: str = "upload.bin",
        timeout: int = 240,
    ) -> dict:
        self.upload_args = {
            "data": data,
            "mime": mime,
            "endpoint": endpoint,
            "filename": filename,
            "timeout": timeout,
        }
        return self.response


class ResponsesHelpersTests(unittest.TestCase):
    def setUp(self) -> None:
        _response_sessions.clear()

    def test_extracts_standard_responses_image_input(self) -> None:
        message, images = _extract_user_message(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "describe"},
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,AA==",
                        },
                    ],
                }
            ]
        )
        self.assertEqual(message, "describe")
        self.assertEqual(images, ["data:image/png;base64,AA=="])

    def test_previous_response_round_trip(self) -> None:
        _remember_response(
            "resp_test",
            ChatReply(chat_session_id="session-1", approach_msg_idx=7),
        )
        self.assertEqual(_previous_response_context("resp_test"), ("session-1", 7))

    def test_unknown_previous_response_id_is_rejected(self) -> None:
        with self.assertRaises(HTTPException):
            _previous_response_context("missing")

    def test_instructions_are_prepended(self) -> None:
        self.assertEqual(
            _compose_response_message("hello", "be brief"),
            "System instructions:\nbe brief\n\nUser message:\nhello",
        )

    def test_data_url_is_decoded_for_upload(self) -> None:
        client = ChatClient(object())  # type: ignore[arg-type]
        with patch.object(client, "upload_bytes", return_value="media-1") as upload:
            self.assertEqual(
                client.upload_data_url("data:image/png;base64,AA=="), "media-1"
            )
            upload.assert_called_once_with(b"\x00", mime="image/png", media=True)

    def test_media_upload_uses_dedicated_browser_upload(self) -> None:
        browser = FakeBrowser({"status": 200, "text": '{"media_id":"media-1"}'})
        client = ChatClient(browser)  # type: ignore[arg-type]
        self.assertEqual(
            client.upload_bytes(b"png", mime="image/png", media=True), "media-1"
        )
        self.assertIsNotNone(browser.upload_args)
        assert browser.upload_args is not None
        self.assertEqual(browser.upload_args["endpoint"], "/uploadMedia/")
        self.assertEqual(browser.upload_args["mime"], "image/png")

    def test_file_upload_accepts_file_id_response(self) -> None:
        browser = FakeBrowser({"status": 200, "text": '{"file_id":"file-1"}'})
        client = ChatClient(browser)  # type: ignore[arg-type]
        self.assertEqual(client.upload_bytes(b"pdf", media=False), "file-1")


if __name__ == "__main__":
    unittest.main()
