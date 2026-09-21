"""Offline smoke test for browser backend selection and Steel adaptation."""

from __future__ import annotations

import sys
from typing import Any

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


cloud = bp.resolve_settings({"FIRECRAWL_API_KEY": "fc-test"})
check("cloud default", cloud.backend, "firecrawl-cloud")
check("cloud session file", cloud.session_id_file.name, "session_id.txt")
check("cloud key redacted", "fc-test" in str(cloud.describe()), False)

local = bp.resolve_settings({"FIRECRAWL_MODE": "local"})
check("local follows Firecrawl mode", local.backend, "steel-local")
check("local executor", local.executor_url, "http://127.0.0.1:3003")
check("local session file", local.session_id_file.name, "steel_session_id.txt")
check("local token redacted", "steel-local-only" in str(local.describe()), False)

split = bp.resolve_settings(
    {
        "FIRECRAWL_MODE": "local",
        "FIRECRAWL_API_URL": "http://127.0.0.1:3002",
        "FIRECRAWL_API_KEY": "fc-test",
        "BROWSER_BACKEND": "firecrawl-cloud",
    }
)
check("split browser is cloud", split.backend, "firecrawl-cloud")
check(
    "split browser ignores scrape URL",
    split.firecrawl.api_url if split.firecrawl else None,
    "https://api.firecrawl.dev",
)
check(
    "cloud browser URL override",
    bp.resolve_settings(
        {
            "FIRECRAWL_MODE": "local",
            "FIRECRAWL_API_KEY": "fc-test",
            "BROWSER_BACKEND": "firecrawl-cloud",
            "FIRECRAWL_BROWSER_API_URL": "https://browser.example/v2/",
        }
    ).firecrawl.api_url,  # type: ignore[union-attr]
    "https://browser.example/v2",
)
check(
    "cloud alias keeps explicit API URL",
    bp.resolve_settings(
        {
            "FIRECRAWL_MODE": "hosted",
            "FIRECRAWL_API_KEY": "fc-test",
            "FIRECRAWL_API_URL": "https://custom.example/v2/",
        }
    ).firecrawl.api_url,  # type: ignore[union-attr]
    "https://custom.example/v2",
)

check(
    "steel alias",
    bp.resolve_settings({"BROWSER_BACKEND": "steel"}).backend,
    "steel-local",
)
expect_error("unknown backend", {"BROWSER_BACKEND": "other"}, "is not one of")
expect_error("invalid inherited Firecrawl mode", {"FIRECRAWL_MODE": "other"}, "is not one of")
expect_error(
    "bad Steel timeout",
    {"BROWSER_BACKEND": "steel-local", "STEEL_EXECUTOR_TIMEOUT": "slow"},
    "is not a number",
)
expect_error(
    "cloud browser needs key",
    {"FIRECRAWL_MODE": "local", "BROWSER_BACKEND": "firecrawl-cloud"},
    "FIRECRAWL_API_KEY is required",
)


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
check(
    "Steel transport error",
    fake.js("1", retries=1).startswith("EXEC_ERR"),
    True,
)

print()
print(f"FAILURES: {len(FAILURES)}")
for failure in FAILURES:
    print(" -", failure)
sys.exit(1 if FAILURES else 0)
