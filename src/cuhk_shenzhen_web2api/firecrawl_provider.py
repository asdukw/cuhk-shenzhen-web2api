"""Select between Firecrawl Cloud and a locally deployed Firecrawl instance.

Every call in this package speaks the Firecrawl v2 protocol, so a cloud backend
and a self-hosted one are interchangeable at the transport level; only the base
URL, the credentials, and a couple of tuning knobs differ. The backend is chosen
by configuration (``.env`` or the process environment) and resolved in exactly
one place, so no call site has to know which one is active:

``FIRECRAWL_MODE``
    ``cloud`` (default) or ``local``. Aliases such as ``self-hosted`` or
    ``docker`` are accepted and normalised to ``local``.
``FIRECRAWL_API_URL``
    Optional base-URL override. Defaults to ``https://api.firecrawl.dev`` for
    cloud and ``http://127.0.0.1:3002`` — the port the self-hosted
    ``docker-compose.yaml`` publishes — for local.
``FIRECRAWL_API_KEY``
    Required in cloud mode. Optional in local mode: a stock self-hosted stack
    runs with ``USE_DB_AUTHENTICATION=false`` and ignores the header.
``FIRECRAWL_RATE_SLEEP`` / ``FIRECRAWL_MAX_TTL`` / ``FIRECRAWL_TIMEOUT``
    Optional tuning. Defaults are mode-specific: the cloud free tier allows only
    ~3 browser-execute requests per minute, whereas a self-hosted stack can use
    a shorter delay. The v2 browser API caps session TTL at 3600 seconds on both
    backends.

Firecrawl's browser-session API is not part of its stock self-hosted stack.
Application workflows select Firecrawl Cloud or the repository-managed local
Steel backend through ``browser_provider``. ``open_browser_session`` remains as
the Firecrawl-specific adapter and reports the upstream HTTP 503 clearly when
called against an unconfigured self-hosted Firecrawl instance.

Local mode additionally extends ``NO_PROXY`` so loopback traffic is not handed
to a system HTTP proxy; see ``ensure_local_bypasses_proxy``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

from firecrawl import Firecrawl

from . import cloud_browser, env
from .cloud_browser import (
    FIRECRAWL_MAX_TTL_SECONDS,
    RATE_SLEEP_SECONDS,
    BrowserServiceUnavailable,
    CloudBrowser,
)

MODE_CLOUD = "cloud"
MODE_LOCAL = "local"
MODES = (MODE_CLOUD, MODE_LOCAL)

DEFAULT_CLOUD_API_URL = "https://api.firecrawl.dev"
DEFAULT_LOCAL_API_URL = "http://127.0.0.1:3002"

DEFAULT_CLOUD_RATE_SLEEP = RATE_SLEEP_SECONDS
DEFAULT_LOCAL_RATE_SLEEP = 0.5
DEFAULT_CLOUD_MAX_TTL = FIRECRAWL_MAX_TTL_SECONDS
DEFAULT_LOCAL_MAX_TTL = FIRECRAWL_MAX_TTL_SECONDS

# Upstream marker for "this deployment has no browser service".
BROWSER_SERVICE_MISSING_MARKER = "BROWSER_SERVICE_URL is missing"

_MODE_ALIASES = {
    "cloud": MODE_CLOUD,
    "hosted": MODE_CLOUD,
    "remote": MODE_CLOUD,
    "api": MODE_CLOUD,
    "local": MODE_LOCAL,
    "self-hosted": MODE_LOCAL,
    "selfhosted": MODE_LOCAL,
    "self_hosted": MODE_LOCAL,
    "docker": MODE_LOCAL,
    "offline": MODE_LOCAL,
}

_BROWSER_SERVICE_HINT = (
    "the configured Firecrawl backend has no browser service, so browser "
    "sessions (/v2/browser) are unavailable. The repository-managed local "
    "Firecrawl stack supports scrape calls only. Set BROWSER_BACKEND=steel-local "
    "and start scripts/local_steel.ps1 for local login and chat workflows, or "
    "use the Firecrawl Cloud browser backend."
)


class ConfigurationError(RuntimeError):
    """The Firecrawl backend selection cannot be used as configured."""


@dataclass(frozen=True)
class FirecrawlSettings:
    """Resolved Firecrawl backend configuration."""

    mode: str = MODE_CLOUD
    api_url: str = DEFAULT_CLOUD_API_URL
    api_key: str = ""
    timeout: float | None = None
    rate_sleep: float = DEFAULT_CLOUD_RATE_SLEEP
    max_ttl: int = DEFAULT_CLOUD_MAX_TTL

    @property
    def is_local(self) -> bool:
        return self.mode == MODE_LOCAL

    def describe(self) -> dict[str, object]:
        """A log-safe summary; the key itself is never included."""
        return {
            "mode": self.mode,
            "api_url": self.api_url,
            "api_key": "set" if self.api_key else "unset",
            "rate_sleep": self.rate_sleep,
            "max_ttl": self.max_ttl,
        }


def normalise_mode(raw: str | None) -> str:
    value = (raw or "").strip().lower()
    if not value:
        return MODE_CLOUD
    mode = _MODE_ALIASES.get(value)
    if mode is None:
        raise ConfigurationError(
            f"FIRECRAWL_MODE={raw!r} is not one of {', '.join(MODES)}"
        )
    return mode


def _optional_float(values: dict[str, str], key: str) -> float | None:
    raw = (values.get(key) or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{key}={raw!r} is not a number") from exc


def _optional_int(values: dict[str, str], key: str) -> int | None:
    raw = (values.get(key) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{key}={raw!r} is not an integer") from exc


def default_api_url(mode: str) -> str:
    return DEFAULT_LOCAL_API_URL if mode == MODE_LOCAL else DEFAULT_CLOUD_API_URL


def resolve_settings(
    env_values: dict[str, str] | None = None,
) -> FirecrawlSettings:
    """Read the backend configuration, applying mode-specific defaults.

    Raises:
        ConfigurationError: unknown mode, non-numeric tuning value, or a cloud
            selection without an API key.
    """
    values = env.load_env() if env_values is None else env_values
    mode = normalise_mode(values.get("FIRECRAWL_MODE"))

    api_url = (values.get("FIRECRAWL_API_URL") or "").strip().rstrip("/")
    if not api_url:
        api_url = default_api_url(mode)

    api_key = (values.get("FIRECRAWL_API_KEY") or "").strip()
    if mode == MODE_CLOUD and not api_key:
        raise ConfigurationError(
            "FIRECRAWL_API_KEY is required when FIRECRAWL_MODE=cloud; "
            "set it in .env, or switch to FIRECRAWL_MODE=local"
        )

    rate_sleep = _optional_float(values, "FIRECRAWL_RATE_SLEEP")
    if rate_sleep is None:
        rate_sleep = (
            DEFAULT_LOCAL_RATE_SLEEP if mode == MODE_LOCAL else DEFAULT_CLOUD_RATE_SLEEP
        )

    max_ttl = _optional_int(values, "FIRECRAWL_MAX_TTL")
    if max_ttl is None:
        max_ttl = DEFAULT_LOCAL_MAX_TTL if mode == MODE_LOCAL else DEFAULT_CLOUD_MAX_TTL
    max_ttl = min(max_ttl, FIRECRAWL_MAX_TTL_SECONDS)

    return FirecrawlSettings(
        mode=mode,
        api_url=api_url,
        api_key=api_key,
        timeout=_optional_float(values, "FIRECRAWL_TIMEOUT"),
        rate_sleep=max(0.0, rate_sleep),
        max_ttl=max(1, max_ttl),
    )


def ensure_local_bypasses_proxy(api_url: str) -> list[str]:
    """Keep local backend traffic out of the system HTTP proxy.

    The SDK calls ``requests.post`` without a session, so it honours
    ``HTTP_PROXY``/``HTTPS_PROXY`` from the environment. On a machine running a
    system proxy (Clash, a corporate MITM) every request to the local Firecrawl
    would be sent to that proxy instead of the loopback address and fail with
    ``502 upstream connect failed``. Extending ``NO_PROXY`` is the only lever
    available without forking the SDK.

    Returns the host entries added, for logging.
    """
    host = urlparse(api_url).hostname
    if not host:
        return []
    candidates = [host, "127.0.0.1", "localhost"]
    added: list[str] = []
    for name in ("NO_PROXY", "no_proxy"):
        current = [p.strip() for p in os.environ.get(name, "").split(",") if p.strip()]
        new = [h for h in candidates if h not in current]
        if new:
            os.environ[name] = ",".join([*current, *new])
            added.extend(h for h in new if h not in added)
    return added


def build_client(settings: FirecrawlSettings | None = None) -> Firecrawl:
    """Build the SDK client for the selected backend.

    ``api_key`` is always passed explicitly (possibly as ``""``) so the SDK
    cannot silently fall back to a stray ``FIRECRAWL_API_KEY`` in the process
    environment — which would send a cloud key to a local instance.
    """
    settings = settings or resolve_settings()
    if settings.is_local:
        ensure_local_bypasses_proxy(settings.api_url)
    if settings.timeout is None:
        return Firecrawl(api_key=settings.api_key, api_url=settings.api_url)
    return Firecrawl(
        api_key=settings.api_key,
        api_url=settings.api_url,
        timeout=settings.timeout,
    )


def client_and_settings(
    env_values: dict[str, str] | None = None,
) -> tuple[Firecrawl, FirecrawlSettings]:
    """Resolve the backend once and return both the client and its settings."""
    settings = resolve_settings(env_values)
    return build_client(settings), settings


def open_browser_session(
    settings: FirecrawlSettings | None = None,
    sid: str | None = None,
) -> tuple[Firecrawl, CloudBrowser]:
    """Resolve the backend and resume `sid`, or create a fresh session.

    Raises:
        ConfigurationError: the backend has no browser service, or the session
            could not be established.
    """
    settings = settings or resolve_settings()
    app = build_client(settings)
    try:
        cb = cloud_browser.get_or_create_session(
            app,
            sid,
            rate_sleep=settings.rate_sleep,
            max_ttl=settings.max_ttl,
        )
    except BrowserServiceUnavailable as exc:
        raise ConfigurationError(f"{_BROWSER_SERVICE_HINT} (upstream: {exc})") from exc
    except Exception as exc:
        # Anything else (bad URL, connection refused, auth failure) is wrapped
        # so callers see which backend was in use.
        raise ConfigurationError(
            f"could not open a browser session on {settings.api_url}: {exc}"
        ) from exc
    return app, cb
