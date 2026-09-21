"""Load configuration from .env and environment variables."""

from __future__ import annotations

import os

from .paths import BASE_DIR

ENV_FILE = BASE_DIR / ".env"

# Keys that matter to this project (in .env or the process environment).
KNOWN_KEYS = (
    "FIRECRAWL_API_KEY",
    "WEB2API_API_KEY",
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


def web2api_api_key(env: dict[str, str] | None = None) -> str:
    env = env or load_env()
    return env.get("WEB2API_API_KEY", "").strip()
