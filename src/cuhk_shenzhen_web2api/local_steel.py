"""Manage the repository-owned Steel Compose stack for the gateway."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.request import ProxyHandler, Request, build_opener

from .browser_provider import resolve_settings
from .paths import BASE_DIR

COMPOSE_FILE = BASE_DIR / "deploy" / "steel" / "compose.yaml"
COMPOSE_ENV_FILE = COMPOSE_FILE.parent / ".env"
SERVICES = frozenset({"steel", "executor"})


def _compose(*args: str) -> subprocess.CompletedProcess[str]:
    command = ["docker", "compose", "--file", str(COMPOSE_FILE)]
    if COMPOSE_ENV_FILE.exists():
        command.extend(("--env-file", str(COMPOSE_ENV_FILE)))
    command.extend(args)
    try:
        return subprocess.run(command, check=True, text=True, capture_output=True)
    except FileNotFoundError as exc:
        raise RuntimeError("Docker was not found in PATH") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise RuntimeError(f"docker compose {' '.join(args)} failed: {detail}") from exc


def _running_services() -> set[str]:
    return set(_compose("ps", "--status", "running", "--services").stdout.split())


def _wait_ready(timeout: float = 120) -> None:
    url = f"{resolve_settings().executor_url}/health"
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with opener.open(Request(url), timeout=3) as response:
                health = json.load(response)
                if isinstance(health, dict) and health.get("status") == "ok":
                    return
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError(f"local Steel executor did not become ready at {url}")


@contextmanager
def managed_local_steel() -> Iterator[None]:
    """Start missing services and stop only services started by this gateway."""
    running = _running_services()
    started = SERVICES - running
    try:
        if started:
            print("Starting local Steel...", flush=True)
            compose_args = ("up", "-d", "--build")
            if running:
                compose_args += ("--no-recreate",)
            else:
                compose_args += ("--remove-orphans",)
            _compose(*compose_args)
        _wait_ready()
        yield
    finally:
        if started:
            print("Stopping local Steel started by gateway...", flush=True)
            try:
                if running:
                    _compose("stop", *sorted(started))
                else:
                    _compose("down")
            except RuntimeError as exc:
                print(f"Steel shutdown failed: {exc}", file=sys.stderr)
