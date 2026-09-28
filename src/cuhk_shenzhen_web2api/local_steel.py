"""Manage the local Steel API and executor as native Node processes."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from . import env as env_module
from .browser_provider import BrowserSettings, resolve_settings
from .paths import BASE_DIR, DATA_DIR

DEFAULT_STEEL_REPO = "https://github.com/steel-dev/steel-browser.git"
DEFAULT_STEEL_REVISION = "dacea7e217ccded0d3886b8771a0ae52d254552c"
DEFAULT_STEEL_API_PORT = 3000
DEFAULT_STEEL_CDP_PORT = 9223
EXECUTOR_DIR = BASE_DIR / "deploy" / "steel" / "executor"
LOG_DIR = DATA_DIR / "steel"
PID_FILE = LOG_DIR / "pids.json"


@dataclass
class NodeStack:
    api: subprocess.Popen | None = None
    executor: subprocess.Popen | None = None


def _setting(name: str, default: str) -> str:
    return env_module.load_env().get(name, default).strip()


def _source_dir() -> Path:
    configured = _setting("STEEL_SOURCE_DIR", "")
    if not configured:
        return BASE_DIR / ".steel" / "steel-browser"
    source = Path(configured).expanduser()
    return source if source.is_absolute() else BASE_DIR / source


def _revision() -> str:
    return _setting("STEEL_REVISION", DEFAULT_STEEL_REVISION)


def _api_port() -> int:
    return int(_setting("STEEL_API_PORT", str(DEFAULT_STEEL_API_PORT)))


def _cdp_port() -> int:
    return int(_setting("STEEL_CDP_PORT", str(DEFAULT_STEEL_CDP_PORT)))


def _npm() -> str:
    names = ("npm.cmd", "npm") if os.name == "nt" else ("npm",)
    for name in names:
        resolved = shutil.which(name)
        if resolved:
            return resolved
    raise RuntimeError("npm was not found in PATH")


def _node() -> str:
    resolved = shutil.which("node")
    if not resolved:
        raise RuntimeError("node was not found in PATH")
    return resolved


def _chrome_executable() -> str | None:
    configured = _setting("CHROME_EXECUTABLE_PATH", "")
    if configured:
        return configured
    if os.name == "nt":
        candidates = (
            Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
            / "Google/Chrome/Application/chrome.exe",
            Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
        )
    else:
        candidates = (
            Path("/usr/bin/google-chrome"),
            Path("/usr/bin/chromium"),
        )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def _local_env() -> dict[str, str]:
    env = os.environ.copy()
    hosts = [
        part.strip() for part in env.get("NO_PROXY", "").split(",") if part.strip()
    ]
    for host in ("127.0.0.1", "localhost"):
        if host not in hosts:
            hosts.append(host)
    env["NO_PROXY"] = ",".join(hosts)
    env["no_proxy"] = env["NO_PROXY"]
    return env


def _run(command: list[str], *, cwd: Path | None = None) -> None:
    try:
        subprocess.run(command, cwd=cwd, check=True, text=True, capture_output=True)
    except FileNotFoundError as exc:
        raise RuntimeError(f"command not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise RuntimeError(f"{' '.join(command)} failed: {detail}") from exc


def _commit_exists(source: Path, revision: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "--verify", f"{revision}^{{commit}}"],
        check=False,
        text=True,
        capture_output=True,
    )
    return result.returncode == 0


def ensure_steel_source(*, install: bool = False) -> Path:
    """Clone the pinned Steel source and optionally install Node dependencies."""
    source = _source_dir()
    if not source.exists():
        source.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", DEFAULT_STEEL_REPO, str(source)])
    if not (source / ".git").exists():
        raise RuntimeError(f"STEEL_SOURCE_DIR is not a git checkout: {source}")

    revision = _revision()
    if revision and not _commit_exists(source, revision):
        _run(["git", "-C", str(source), "fetch", "--depth", "1", "origin", revision])
    if revision:
        _run(["git", "-C", str(source), "checkout", "--detach", revision])

    if install or not (source / "node_modules").exists():
        npm = _npm()
        _run([npm, "ci", "--include=dev"], cwd=source)
        _run(
            [
                npm,
                "install-scripts",
                "approve",
                "duckdb",
                "classic-level",
                "esbuild",
                "@swc/core",
            ],
            cwd=source,
        )
        _run(
            [
                npm,
                "rebuild",
                "duckdb",
                "classic-level",
                "esbuild",
                "@swc/core",
                "--foreground-scripts",
            ],
            cwd=source,
        )
    if install or not (EXECUTOR_DIR / "node_modules").exists():
        _run([_npm(), "ci", "--omit=dev", "--ignore-scripts"], cwd=EXECUTOR_DIR)
    return source


def _ready(url: str, timeout: float = 2.0) -> bool:
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(Request(url), timeout=timeout) as response:
            payload = json.load(response)
    except (OSError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("status") == "ok"


def _wait_ready(url: str, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _ready(url):
            return
        time.sleep(1)
    raise RuntimeError(f"local Steel service did not become ready at {url}")


def _spawn(
    command: list[str], *, cwd: Path, name: str, env: dict[str, str]
) -> subprocess.Popen:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{name}.log"
    log = log_path.open("ab", buffering=0)
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
        start_new_session=os.name != "nt",
    )
    return process


def _start_api(source: Path) -> subprocess.Popen:
    env = _local_env()
    profile = Path(
        _setting("CHROME_USER_DATA_DIR", str(DATA_DIR / "steel" / "chrome-profile"))
    ).expanduser()
    if not profile.is_absolute():
        profile = BASE_DIR / profile
    env.update(
        {
            "NODE_ENV": "development",
            "HOST": "127.0.0.1",
            "PORT": str(_api_port()),
            "CDP_REDIRECT_PORT": str(_cdp_port()),
            "CDP_DOMAIN": f"localhost:{_cdp_port()}",
            "CHROME_HEADLESS": _setting("STEEL_HEADLESS", "true"),
            "CHROME_USER_DATA_DIR": str(profile),
        }
    )
    chrome = _chrome_executable()
    if chrome:
        env["CHROME_EXECUTABLE_PATH"] = chrome
    return _spawn(
        [_npm(), "run", "dev", "-w", "api"], cwd=source, name="steel-api", env=env
    )


def _start_executor(settings: BrowserSettings) -> subprocess.Popen:
    env = _local_env()
    port = urlsplit(settings.executor_url).port or 3003
    env.update(
        {
            "PORT": str(port),
            "STEEL_API_URL": f"http://127.0.0.1:{_api_port()}",
            "STEEL_CDP_HOST": "127.0.0.1",
            "STEEL_EXECUTOR_TOKEN": settings.executor_token,
        }
    )
    return _spawn([_node(), "server.mjs"], cwd=EXECUTOR_DIR, name="executor", env=env)


def start_node_stack(settings: BrowserSettings | None = None) -> NodeStack:
    """Start missing Steel/executor processes and return their handles."""
    settings = settings or resolve_settings()
    source = ensure_steel_source(install=False)
    api_url = f"http://127.0.0.1:{_api_port()}/v1/health"
    executor_url = f"{settings.executor_url.rstrip('/')}/health"

    stack = NodeStack()
    try:
        if not _ready(api_url):
            stack.api = _start_api(source)
            _wait_ready(api_url)
        if not _ready(executor_url):
            stack.executor = _start_executor(settings)
            _wait_ready(executor_url)
    except Exception:
        stop_node_stack(stack)
        raise
    return stack


def _terminate_tree(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            capture_output=True,
        )
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)


def stop_node_stack(stack: NodeStack) -> None:
    _terminate_tree(stack.executor)
    _terminate_tree(stack.api)


def _write_pid_file(stack: NodeStack) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    payload = _read_pid_file()
    if stack.api:
        payload["api"] = stack.api.pid
    if stack.executor:
        payload["executor"] = stack.executor.pid
    PID_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _read_pid_file() -> dict[str, int | None]:
    try:
        payload = json.loads(PID_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"api": None, "executor": None}
    return {
        "api": payload.get("api") if isinstance(payload.get("api"), int) else None,
        "executor": payload.get("executor")
        if isinstance(payload.get("executor"), int)
        else None,
    }


def _process_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def status() -> dict[str, object]:
    settings = resolve_settings()
    pids = _read_pid_file()
    return {
        "api_pid": pids["api"],
        "api_alive": _process_alive(pids["api"]),
        "api_ready": _ready(f"http://127.0.0.1:{_api_port()}/v1/health"),
        "executor_pid": pids["executor"],
        "executor_alive": _process_alive(pids["executor"]),
        "executor_ready": _ready(f"{settings.executor_url.rstrip('/')}/health"),
    }


def start_detached(settings: BrowserSettings | None = None) -> NodeStack:
    stack = start_node_stack(settings)
    _write_pid_file(stack)
    return stack


def stop_detached() -> None:
    pids = _read_pid_file()
    for pid in (pids["executor"], pids["api"]):
        if pid:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    check=False,
                    capture_output=True,
                )
            else:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        pass


def log_paths() -> tuple[Path, Path]:
    return LOG_DIR / "steel-api.log", LOG_DIR / "executor.log"


@contextmanager
def managed_local_steel() -> Iterator[None]:
    """Start missing local Node services and stop only services started here."""
    stack = start_node_stack()
    try:
        yield
    finally:
        stop_node_stack(stack)


if __name__ == "__main__":
    print(json.dumps(status(), indent=2))
