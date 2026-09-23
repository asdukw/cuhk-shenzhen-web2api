"""Configure and persist the local Steel browser session."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from . import env
from .paths import STEEL_SESSION_ID_FILE
from .steel_browser import SteelBrowser, SteelBrowserError

BACKEND_STEEL_LOCAL = "steel-local"
DEFAULT_STEEL_EXECUTOR_URL = "http://127.0.0.1:3003"
DEFAULT_STEEL_EXECUTOR_TOKEN = "steel-local-only"
DEFAULT_STEEL_TIMEOUT = 130.0


class ConfigurationError(RuntimeError):
    """The local browser cannot be used as configured."""


@dataclass(frozen=True)
class BrowserSettings:
    executor_url: str = DEFAULT_STEEL_EXECUTOR_URL
    executor_token: str = DEFAULT_STEEL_EXECUTOR_TOKEN
    timeout: float = DEFAULT_STEEL_TIMEOUT

    @property
    def backend(self) -> str:
        return BACKEND_STEEL_LOCAL

    @property
    def session_id_file(self) -> Path:
        return STEEL_SESSION_ID_FILE

    def describe(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "executor_url": self.executor_url,
            "executor_token": "set" if self.executor_token else "unset",
            "timeout": self.timeout,
        }


def resolve_settings(env_values: dict[str, str] | None = None) -> BrowserSettings:
    values = env.load_env() if env_values is None else env_values
    backend = (values.get("BROWSER_BACKEND") or BACKEND_STEEL_LOCAL).strip().lower()
    if backend not in ("steel-local", "steel", "local"):
        raise ConfigurationError("BROWSER_BACKEND must be steel-local")
    executor_url = (
        (values.get("STEEL_EXECUTOR_URL") or DEFAULT_STEEL_EXECUTOR_URL)
        .strip()
        .rstrip("/")
    )
    token = (values.get("STEEL_EXECUTOR_TOKEN") or DEFAULT_STEEL_EXECUTOR_TOKEN).strip()
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
        executor_url=executor_url,
        executor_token=token,
        timeout=max(1.0, timeout),
    )


def _bypass_proxy(url: str) -> None:
    host = urlsplit(url).hostname
    if not host:
        raise ConfigurationError(f"invalid STEEL_EXECUTOR_URL: {url!r}")
    for key in ("NO_PROXY", "no_proxy"):
        hosts = [
            part.strip() for part in os.environ.get(key, "").split(",") if part.strip()
        ]
        for local_host in (host, "127.0.0.1", "localhost"):
            if local_host not in hosts:
                hosts.append(local_host)
        os.environ[key] = ",".join(hosts)


def open_browser_session(
    settings: BrowserSettings | None = None,
    sid: str | None = None,
) -> SteelBrowser:
    settings = settings or resolve_settings()
    _bypass_proxy(settings.executor_url)
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


def load_session_id(settings: BrowserSettings) -> str | None:
    try:
        return settings.session_id_file.read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


def save_session_id(settings: BrowserSettings, sid: str) -> None:
    settings.session_id_file.parent.mkdir(parents=True, exist_ok=True)
    settings.session_id_file.write_text(sid, encoding="utf-8")
