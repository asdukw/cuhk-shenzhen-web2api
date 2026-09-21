"""Common interface implemented by browser-session backends."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class BrowserBackend(Protocol):
    """The browser operations used by login, chat, and probe workflows."""

    sid: str

    def js(self, code: str, timeout: int = 120, retries: int = 6) -> str: ...

    def js_json(
        self, code: str, timeout: int = 120, retries: int = 6
    ) -> dict | list | None: ...

    def url(self) -> str: ...

    def is_alive(self) -> bool: ...

    def cookies(self) -> list[dict]: ...

    def save_cookies(self, path: Path) -> Path: ...

    def page_scan(self) -> dict: ...

    def close(self) -> None: ...
