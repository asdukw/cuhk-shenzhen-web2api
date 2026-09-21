"""Select Firecrawl Cloud or local Steel for browser-session workflows."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import cloud_browser, env, firecrawl_provider
from .browser_backend import BrowserBackend
from .paths import SESSION_ID_FILE, STEEL_SESSION_ID_FILE
from .steel_browser import SteelBrowser, SteelBrowserError

BACKEND_FIRECRAWL_CLOUD = "firecrawl-cloud"
BACKEND_STEEL_LOCAL = "steel-local"
BACKENDS = (BACKEND_FIRECRAWL_CLOUD, BACKEND_STEEL_LOCAL)

DEFAULT_STEEL_EXECUTOR_URL = "http://127.0.0.1:3003"
DEFAULT_STEEL_EXECUTOR_TOKEN = "steel-local-only"
DEFAULT_STEEL_TIMEOUT = 130.0

_ALIASES = {
    "cloud": BACKEND_FIRECRAWL_CLOUD,
    "firecrawl": BACKEND_FIRECRAWL_CLOUD,
    "firecrawl-cloud": BACKEND_FIRECRAWL_CLOUD,
    "local": BACKEND_STEEL_LOCAL,
    "steel": BACKEND_STEEL_LOCAL,
    "steel-local": BACKEND_STEEL_LOCAL,
}


class ConfigurationError(RuntimeError):
    """The selected browser backend cannot be used as configured."""


@dataclass(frozen=True)
class BrowserSettings:
    backend: str
    executor_url: str = ""
    executor_token: str = ""
    timeout: float = DEFAULT_STEEL_TIMEOUT
    firecrawl: firecrawl_provider.FirecrawlSettings | None = None

    @property
    def is_local(self) -> bool:
        return self.backend == BACKEND_STEEL_LOCAL

    @property
    def session_id_file(self) -> Path:
        if self.is_local:
            return STEEL_SESSION_ID_FILE
        return SESSION_ID_FILE

    def describe(self) -> dict[str, object]:
        result: dict[str, object] = {"backend": self.backend}
        if self.is_local:
            result.update(
                {
                    "executor_url": self.executor_url,
                    "executor_token": "set" if self.executor_token else "unset",
                    "timeout": self.timeout,
                }
            )
        elif self.firecrawl is not None:
            result["firecrawl"] = self.firecrawl.describe()
        return result


def _normalise_backend(raw: str | None, values: dict[str, str]) -> str:
    value = (raw or "").strip().lower()
    if not value:
        try:
            firecrawl_mode = firecrawl_provider.normalise_mode(
                values.get("FIRECRAWL_MODE")
            )
        except firecrawl_provider.ConfigurationError as exc:
            raise ConfigurationError(str(exc)) from exc
        return (
            BACKEND_STEEL_LOCAL
            if firecrawl_mode == firecrawl_provider.MODE_LOCAL
            else BACKEND_FIRECRAWL_CLOUD
        )
    backend = _ALIASES.get(value)
    if backend is None:
        raise ConfigurationError(
            f"BROWSER_BACKEND={raw!r} is not one of {', '.join(BACKENDS)}"
        )
    return backend


def resolve_settings(env_values: dict[str, str] | None = None) -> BrowserSettings:
    values = env.load_env() if env_values is None else env_values
    backend = _normalise_backend(values.get("BROWSER_BACKEND"), values)
    if backend == BACKEND_STEEL_LOCAL:
        executor_url = (
            (values.get("STEEL_EXECUTOR_URL") or DEFAULT_STEEL_EXECUTOR_URL)
            .strip()
            .rstrip("/")
        )
        token = (
            values.get("STEEL_EXECUTOR_TOKEN") or DEFAULT_STEEL_EXECUTOR_TOKEN
        ).strip()
        raw_timeout = (values.get("STEEL_EXECUTOR_TIMEOUT") or "").strip()
        try:
            timeout = float(raw_timeout) if raw_timeout else DEFAULT_STEEL_TIMEOUT
        except ValueError as exc:
            raise ConfigurationError(
                f"STEEL_EXECUTOR_TIMEOUT={raw_timeout!r} is not a number"
            ) from exc
        if not executor_url:
            raise ConfigurationError("STEEL_EXECUTOR_URL must not be empty")
        if not token:
            raise ConfigurationError("STEEL_EXECUTOR_TOKEN must not be empty")
        return BrowserSettings(
            backend=backend,
            executor_url=executor_url,
            executor_token=token,
            timeout=max(1.0, timeout),
        )

    cloud_values = dict(values)
    cloud_values["FIRECRAWL_MODE"] = "cloud"
    browser_api_url = (values.get("FIRECRAWL_BROWSER_API_URL") or "").strip()
    if browser_api_url:
        cloud_values["FIRECRAWL_API_URL"] = browser_api_url
    try:
        scrape_mode = firecrawl_provider.normalise_mode(values.get("FIRECRAWL_MODE"))
        if not browser_api_url and scrape_mode == firecrawl_provider.MODE_LOCAL:
            cloud_values.pop("FIRECRAWL_API_URL", None)
        firecrawl = firecrawl_provider.resolve_settings(cloud_values)
    except firecrawl_provider.ConfigurationError as exc:
        raise ConfigurationError(str(exc)) from exc
    return BrowserSettings(backend=backend, firecrawl=firecrawl)


def open_browser_session(
    settings: BrowserSettings | None = None,
    sid: str | None = None,
) -> BrowserBackend:
    settings = settings or resolve_settings()
    if settings.is_local:
        firecrawl_provider.ensure_local_bypasses_proxy(settings.executor_url)
        try:
            return SteelBrowser.open(
                settings.executor_url,
                settings.executor_token,
                sid,
                request_timeout=settings.timeout,
            )
        except SteelBrowserError as exc:
            raise ConfigurationError(
                f"could not open local Steel browser at {settings.executor_url}: {exc}"
            ) from exc
    if settings.firecrawl is None:
        raise ConfigurationError("Firecrawl Cloud browser settings are missing")
    try:
        _app, browser = firecrawl_provider.open_browser_session(settings.firecrawl, sid)
    except firecrawl_provider.ConfigurationError as exc:
        raise ConfigurationError(str(exc)) from exc
    return browser


def load_session_id(settings: BrowserSettings) -> str | None:
    return cloud_browser.load_session_id(settings.session_id_file)


def save_session_id(settings: BrowserSettings, sid: str) -> None:
    cloud_browser.save_session_id(sid, settings.session_id_file)
