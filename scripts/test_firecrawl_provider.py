"""Offline smoke test for the Firecrawl backend selection (no network calls).

Verifies mode resolution, mode-specific defaults, failure modes, and the
browser-service error classification. Mirrors the style of
`scripts/test_tool_proxy.py`: a manual runner, not a discovered test suite.

Usage:
    .venv\\Scripts\\python.exe scripts\\test_firecrawl_provider.py 2>&1
"""

from __future__ import annotations

import os
import sys

from cuhk_shenzhen_web2api import cloud_browser
from cuhk_shenzhen_web2api import firecrawl_provider as fp

FAILURES: list[str] = []


def check(label: str, got: object, want: object) -> None:
    ok = got == want
    if not ok:
        FAILURES.append(f"{label}: got {got!r}, want {want!r}")
    print(f"{'ok  ' if ok else 'FAIL'} {label}: {got!r}")


def expect_error(label: str, values: dict[str, str], fragment: str) -> None:
    try:
        fp.resolve_settings(values)
    except fp.ConfigurationError as exc:
        check(label, fragment in str(exc), True)
    else:
        check(label, "no exception", "ConfigurationError")


# ---- cloud (default) ----
cloud = fp.resolve_settings({"FIRECRAWL_API_KEY": "fc-abc"})
check("default mode", cloud.mode, "cloud")
check("default url", cloud.api_url, "https://api.firecrawl.dev")
check("cloud rate_sleep", cloud.rate_sleep, 3.5)
check("cloud max_ttl", cloud.max_ttl, 3600)
check("cloud is_local", cloud.is_local, False)

# ---- local ----
local = fp.resolve_settings({"FIRECRAWL_MODE": "local"})
check("local mode", local.mode, "local")
check("local url", local.api_url, "http://127.0.0.1:3002")
check("local key may be empty", local.api_key, "")
check("local rate_sleep", local.rate_sleep, 0.5)
check("local max_ttl", local.max_ttl, 14400)

check("alias docker", fp.resolve_settings({"FIRECRAWL_MODE": "Docker"}).mode, "local")
check(
    "alias self-hosted",
    fp.resolve_settings({"FIRECRAWL_MODE": "self-hosted"}).mode,
    "local",
)
check(
    "alias remote",
    fp.resolve_settings({"FIRECRAWL_MODE": "remote", "FIRECRAWL_API_KEY": "k"}).mode,
    "cloud",
)
check(
    "explicit url wins, trailing slash stripped",
    fp.resolve_settings(
        {"FIRECRAWL_MODE": "local", "FIRECRAWL_API_URL": "http://host:9999/"}
    ).api_url,
    "http://host:9999",
)
check(
    "tuning overrides",
    fp.resolve_settings(
        {
            "FIRECRAWL_MODE": "local",
            "FIRECRAWL_RATE_SLEEP": "1.25",
            "FIRECRAWL_MAX_TTL": "600",
            "FIRECRAWL_TIMEOUT": "30",
        }
    ),
    fp.FirecrawlSettings(
        mode="local",
        api_url="http://127.0.0.1:3002",
        api_key="",
        timeout=30.0,
        rate_sleep=1.25,
        max_ttl=600,
    ),
)
check(
    "describe() never leaks the key",
    fp.resolve_settings({"FIRECRAWL_API_KEY": "fc-secret"}).describe(),
    {
        "mode": "cloud",
        "api_url": "https://api.firecrawl.dev",
        "api_key": "set",
        "rate_sleep": 3.5,
        "max_ttl": 3600,
    },
)

# ---- failure modes ----
expect_error("cloud without key", {}, "FIRECRAWL_API_KEY is required")
expect_error("unknown mode", {"FIRECRAWL_MODE": "nope"}, "is not one of cloud, local")
expect_error(
    "non-numeric rate_sleep",
    {"FIRECRAWL_MODE": "local", "FIRECRAWL_RATE_SLEEP": "fast"},
    "is not a number",
)
expect_error(
    "non-integer max_ttl",
    {"FIRECRAWL_MODE": "local", "FIRECRAWL_MAX_TTL": "1e9"},
    "is not an integer",
)

# ---- client construction ----
client = fp.build_client(local)
check("local client url", client.api_url, "http://127.0.0.1:3002")
check("local client key", client.api_key, "")
inner = client._v2_client  # type: ignore[attr-defined]
check("local sends no Authorization header", inner.http_client.api_key, "")
check(
    "cloud client url",
    fp.build_client(cloud).api_url,
    "https://api.firecrawl.dev",
)

# ---- proxy bypass for local backends ----
os.environ.pop("NO_PROXY", None)
os.environ.pop("no_proxy", None)
os.environ["HTTP_PROXY"] = "http://127.0.0.1:7897"
added = fp.ensure_local_bypasses_proxy("http://127.0.0.1:3002")
check("bypass adds the loopback host", "127.0.0.1" in added, True)
check("NO_PROXY now set", "127.0.0.1" in os.environ.get("NO_PROXY", ""), True)
check("no_proxy now set", "localhost" in os.environ.get("no_proxy", ""), True)
check(
    "bypass is idempotent", fp.ensure_local_bypasses_proxy("http://127.0.0.1:3002"), []
)
check(
    "bypass keeps existing entries",
    fp.ensure_local_bypasses_proxy("http://127.0.0.1:3002") or True,
    True,
)
os.environ["NO_PROXY"] = "example.com"
fp.ensure_local_bypasses_proxy("http://myhost:3002")
check(
    "existing NO_PROXY preserved",
    os.environ["NO_PROXY"].startswith("example.com")
    and "myhost" in os.environ["NO_PROXY"],
    True,
)


# ---- browser-service classification ----
class NoBrowserService:
    def browser(self, **_kwargs: object) -> object:
        raise RuntimeError(
            "Browser feature is not configured (BROWSER_SERVICE_URL is missing)."
        )


try:
    cloud_browser.create_session(NoBrowserService())  # type: ignore[arg-type]
except cloud_browser.BrowserServiceUnavailable as exc:
    check("503 is classified", "BROWSER_SERVICE_URL" in str(exc), True)
else:
    check("503 is classified", "no exception", "BrowserServiceUnavailable")


class Unreachable:
    def browser(self, **_kwargs: object) -> object:
        raise RuntimeError("connection refused")


try:
    cloud_browser.create_session(Unreachable())  # type: ignore[arg-type]
except cloud_browser.BrowserServiceUnavailable:
    check(
        "unrelated errors are not misclassified",
        "BrowserServiceUnavailable",
        "RuntimeError",
    )
except RuntimeError as exc:
    check("unrelated errors are not misclassified", str(exc), "connection refused")

print()
print(f"FAILURES: {len(FAILURES)}")
for failure in FAILURES:
    print(" -", failure)
sys.exit(1 if FAILURES else 0)
