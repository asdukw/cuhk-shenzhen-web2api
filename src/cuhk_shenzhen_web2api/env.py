"""Load configuration from .env and environment variables."""

from __future__ import annotations

import os

from .paths import BASE_DIR

ENV_FILE = BASE_DIR / ".env"

# Keys that matter to this project (in .env or the process environment).
# The FIRECRAWL_* block selects which Firecrawl backend is used; see
# `firecrawl_provider` for how they are interpreted.
KNOWN_KEYS = (
    "FIRECRAWL_MODE",
    "FIRECRAWL_API_URL",
    "FIRECRAWL_API_KEY",
    "FIRECRAWL_RATE_SLEEP",
    "FIRECRAWL_MAX_TTL",
    "FIRECRAWL_TIMEOUT",
    "FIRECRAWL_BROWSER_API_URL",
    "BROWSER_BACKEND",
    "STEEL_EXECUTOR_URL",
    "STEEL_EXECUTOR_TOKEN",
    "STEEL_EXECUTOR_TIMEOUT",
    "CHAT_USERNAME",
    "CHAT_PASSWORD",
    "CHAT_COOKIE",
    "USERNAME",
    "PASSWORD",
)


def load_env() -> dict[str, str]:
    """Merge .env values (project root) with process-environment overrides."""
    env: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    for key in KNOWN_KEYS:
        if key not in env and os.environ.get(key):
            env[key] = os.environ[key]
    return env


def chat_username(env: dict[str, str] | None = None) -> str:
    env = env or load_env()
    return env.get("CHAT_USERNAME") or env.get("USERNAME", "")


def chat_password(env: dict[str, str] | None = None) -> str:
    env = env or load_env()
    return env.get("CHAT_PASSWORD") or env.get("PASSWORD", "")


def firecrawl_api_key(env: dict[str, str] | None = None) -> str:
    env = env or load_env()
    return env.get("FIRECRAWL_API_KEY", "").strip()


def firecrawl_mode(env: dict[str, str] | None = None) -> str:
    """Raw `FIRECRAWL_MODE` value; empty when unset (callers default to cloud).

    Use `firecrawl_provider.resolve_settings` for the validated, normalised
    value together with the rest of the backend configuration.
    """
    env = env or load_env()
    return env.get("FIRECRAWL_MODE", "").strip()
