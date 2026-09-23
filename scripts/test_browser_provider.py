"""Offline smoke test for local Steel configuration and result adaptation."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from cuhk_shenzhen_web2api import browser_provider as bp
from cuhk_shenzhen_web2api.steel_browser import SteelBrowser, SteelBrowserError

FAILURES: list[str] = []


def check(label: str, got: object, want: object) -> None:
    ok = got == want
    if not ok:
        FAILURES.append(f"{label}: got {got!r}, want {want!r}")
    print(f"{'ok  ' if ok else 'FAIL'} {label}: {got!r}")


def expect_error(label: str, values: dict[str, str], fragment: str) -> None:
    try:
        bp.resolve_settings(values)
    except bp.ConfigurationError as exc:
        check(label, fragment in str(exc), True)
    else:
        check(label, "no exception", "ConfigurationError")


local = bp.resolve_settings({})
check("local default", local.backend, "steel-local")
check("local executor", local.executor_url, "http://127.0.0.1:3003")
check("local session file", local.session_id_file.name, "steel_session_id.txt")
check("local token redacted", "steel-local-only" in str(local.describe()), False)
check(
    "steel alias",
    bp.resolve_settings({"BROWSER_BACKEND": "steel"}).backend,
    "steel-local",
)
check(
    "local alias",
    bp.resolve_settings({"BROWSER_BACKEND": "local"}).backend,
    "steel-local",
)
expect_error(
    "cloud backend rejected", {"BROWSER_BACKEND": "cloud"}, "must be steel-local"
)
expect_error("unknown backend", {"BROWSER_BACKEND": "other"}, "must be steel-local")
expect_error("bad Steel timeout", {"STEEL_EXECUTOR_TIMEOUT": "slow"}, "is not a number")

with (
    TemporaryDirectory() as directory,
    patch.object(bp, "STEEL_SESSION_ID_FILE", Path(directory) / "session.txt"),
):
    settings = bp.resolve_settings({})
    check("missing session", bp.load_session_id(settings), None)
    bp.save_session_id(settings, "steel-session")
    check("saved session", bp.load_session_id(settings), "steel-session")

with patch.dict(os.environ, {"NO_PROXY": "example.com", "no_proxy": "example.com"}):
    bp._bypass_proxy("http://127.0.0.1:3003")
    first = os.environ["NO_PROXY"]
    bp._bypass_proxy("http://127.0.0.1:3003")
    check("proxy bypass preserves entries", first.startswith("example.com,"), True)
    check("proxy bypass idempotent", os.environ["NO_PROXY"], first)
    check("lowercase proxy bypass", os.environ["no_proxy"], first)


class FakeSteelBrowser(SteelBrowser):
    def __init__(self, responses: list[dict[str, Any] | Exception]) -> None:
        super().__init__("http://127.0.0.1:1", "token", "sid")
        self.responses = responses

    def _request(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        del path, payload, timeout
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


fake = FakeSteelBrowser([{"result": "  ok  "}])
check("Steel result trimming", fake.js("1", retries=1), "ok")

fake = FakeSteelBrowser([{"result": '{"ok":true}'}])
check("Steel JSON decoding", fake.js_json("1", retries=1), {"ok": True})

fake = FakeSteelBrowser([{"error": "boom"}])
check("Steel execution error", fake.js("1", retries=1), "ERROR boom")

fake = FakeSteelBrowser([SteelBrowserError("offline")])
check("Steel transport error", fake.js("1", retries=1).startswith("EXEC_ERR"), True)

print()
print(f"FAILURES: {len(FAILURES)}")
for failure in FAILURES:
    print(" -", failure)
sys.exit(1 if FAILURES else 0)
