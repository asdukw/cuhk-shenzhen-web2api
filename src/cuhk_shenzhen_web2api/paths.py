"""Repository-relative paths used across the package."""

from __future__ import annotations

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
BASE_DIR = PACKAGE_DIR.parents[1]

DATA_DIR = BASE_DIR / "data"
CHAT_DATA_DIR = DATA_DIR / "chat_session"
BUNDLES_DIR = CHAT_DATA_DIR / "bundles"

SESSION_ID_FILE = CHAT_DATA_DIR / "session_id.txt"
STEEL_SESSION_ID_FILE = CHAT_DATA_DIR / "steel_session_id.txt"
COOKIES_FILE = CHAT_DATA_DIR / "cookies.json"

CHAT_URL = "https://ai.cuhk.edu.cn/chat/"
