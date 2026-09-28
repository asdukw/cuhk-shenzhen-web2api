"""Load configuration from .env and environment variables."""

from __future__ import annotations

import os

from .paths import BASE_DIR

ENV_FILE = BASE_DIR / ".env"

# Keys that matter to this project (in .env or the process environment).
KNOWN_KEYS = (
    "BROWSER_BACKEND",
    "STEEL_EXECUTOR_URL",
    "STEEL_EXECUTOR_TOKEN",
    "STEEL_EXECUTOR_TIMEOUT",
    "STEEL_SOURCE_DIR",
    "STEEL_REVISION",
    "STEEL_API_PORT",
    "STEEL_CDP_PORT",
    "STEEL_HEADLESS",
    "CHROME_EXECUTABLE_PATH",
    "CHROME_USER_DATA_DIR",
    "CHAT_USERNAME",
    "CHAT_PASSWORD",
    "USERNAME",
    "PASSWORD",
)

# USERNAME/PASSWORD are common OS variables (especially on Windows), so they
# are read from .env only. Use CHAT_USERNAME/CHAT_PASSWORD for process overrides.
_FILE_ONLY_KEYS = frozenset({"USERNAME", "PASSWORD"})


def load_env() -> dict[str, str]:
    """Merge .env values (project root) with process-environment overrides."""
    env: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    # Process environment values override project-specific .env keys.
    # Generic USERNAME/PASSWORD are excluded because Windows always defines
    # USERNAME for the current desktop account.
    for key in KNOWN_KEYS:
        if key in _FILE_ONLY_KEYS:
            continue
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def chat_username(env: dict[str, str] | None = None) -> str:
    env = env or load_env()
    return env.get("CHAT_USERNAME") or env.get("USERNAME", "")


def chat_password(env: dict[str, str] | None = None) -> str:
    env = env or load_env()
    return env.get("CHAT_PASSWORD") or env.get("PASSWORD", "")
