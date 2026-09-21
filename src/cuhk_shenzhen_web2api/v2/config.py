"""Configuration for the opt-in runtime; never reads legacy login credentials."""

import ipaddress
import json
import os
from dataclasses import dataclass
from pathlib import Path

from ..paths import BASE_DIR


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    api_key: str
    backend: str = "httpx"
    host: str = "127.0.0.1"
    port: int = 8767
    concurrency: int = 1
    interval: float = 20.0
    model: str = ""
    auth_dir: Path | None = None
    firecrawl_session_id: str = ""
    firecrawl_api_key: str = ""
    stream_mode: str = "buffered"
    stream_validation: Path | None = None
    auth_root: Path = BASE_DIR / ".web2api-v2"

    @property
    def streaming_verified(self) -> bool:
        if not self.stream_validation:
            return False
        try:
            report = json.loads(self.stream_validation.read_text(encoding="utf-8"))
            return (
                report.get("stream_protocol") == "browser-ndjson-v1"
                and report.get("stream_verified") is True
            )
        except (OSError, ValueError):
            return False

    def __post_init__(self):
        if not self.api_key.strip():
            raise ValueError("WEB2API_V2_API_KEY is required")
        if self.backend not in {"httpx", "playwright", "firecrawl"}:
            raise ValueError("Invalid backend")
        address = ipaddress.ip_address(self.host)
        if not (
            address.is_loopback or address in ipaddress.ip_network("100.64.0.0/10")
        ):
            raise ValueError("Bind only to loopback or your Tailscale IPv4 address")
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be 1..65535")
        if not 1 <= self.concurrency <= 4 or not 0 <= self.interval <= 300:
            raise ValueError("concurrency must be 1..4; interval must be 0..300")
        if self.stream_mode not in {"buffered", "incremental"}:
            raise ValueError("stream_mode must be buffered or incremental")
        if (
            self.backend != "httpx"
            and self.concurrency != 1
            and not (
                self.backend == "playwright"
                and self.stream_mode == "incremental"
                and self.streaming_verified
            )
        ):
            raise ValueError("Browser backends require concurrency=1")
        root = self.data_dir.resolve()
        legacy = (BASE_DIR / "data").resolve()
        if root == legacy or legacy in root.parents:
            raise ValueError("Use an isolated directory outside legacy data/")

    @classmethod
    def load(cls):
        values: dict[str, str] = {}
        path = BASE_DIR / ".env.v2"
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    values[key.strip()] = value.strip().strip("\"'")
        values.update(
            {k: v for k, v in os.environ.items() if k.startswith("WEB2API_V2_")}
        )
        return cls(
            data_dir=Path(
                values.get(
                    "WEB2API_V2_DATA_DIR", str(BASE_DIR / ".web2api-v2" / "runtime")
                )
            ),
            api_key=values.get("WEB2API_V2_API_KEY", ""),
            backend=values.get("WEB2API_V2_BACKEND", "httpx"),
            host=values.get("WEB2API_V2_HOST", "127.0.0.1"),
            port=int(values.get("WEB2API_V2_PORT", "8767")),
            concurrency=int(values.get("WEB2API_V2_CONCURRENCY", "1")),
            interval=float(values.get("WEB2API_V2_INTERVAL", "20")),
            model=values.get("WEB2API_V2_MODEL", ""),
            auth_dir=Path(values["WEB2API_V2_AUTH_DIR"])
            if values.get("WEB2API_V2_AUTH_DIR")
            else None,
            firecrawl_session_id=values.get("WEB2API_V2_FIRECRAWL_SESSION_ID", ""),
            firecrawl_api_key=values.get("WEB2API_V2_FIRECRAWL_API_KEY", ""),
            stream_mode=values.get("WEB2API_V2_STREAM_MODE", "buffered"),
            stream_validation=Path(values["WEB2API_V2_STREAM_VALIDATION"])
            if values.get("WEB2API_V2_STREAM_VALIDATION")
            else None,
            auth_root=Path(
                values.get("WEB2API_V2_AUTH_ROOT", str(BASE_DIR / ".web2api-v2"))
            ),
        )
