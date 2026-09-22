r"""Live end-to-end checks for the local CUHK-Shenzhen web2api server.

The checks are deliberately kept outside the offline CI suite: they require a
running server, a valid browser session, and consume the upstream chat quota.

Examples (PowerShell)::

    .venv\Scripts\python.exe scripts\e2e_test.py --scenario qa
    .venv\Scripts\python.exe scripts\e2e_test.py --scenario conversation
    .venv\Scripts\python.exe scripts\e2e_test.py --scenario tools
    .venv\Scripts\python.exe scripts\e2e_test.py --scenario all

The tools scenario verifies the HTTP registration/call contract with a
``pwsh`` tool. The current ``POST /tools`` endpoint intentionally installs a
placeholder handler, so use ``--require-real-pwsh`` only after the server is
configured with a real pwsh-backed handler.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

import requests

DEFAULT_BASE_URL = "http://127.0.0.1:8765"
DEFAULT_TIMEOUT = 300.0
DEFAULT_MODEL = "campus-affairs-qa"
DEFAULT_PWSH_COMMAND = "Write-Output 'web2api-e2e-pwsh'"


class E2EFailure(AssertionError):
    """Raised when an end-to-end assertion fails."""


@dataclass
class ConversationTurn:
    """Identifiers returned by one completed chat turn."""

    chat_session_id: str
    approach_msg_idx: int
    text: str


class E2EClient:
    """Small HTTP client with assertions shared by all scenarios."""

    def __init__(self, base_url: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.http = requests.Session()

    def request_json(
        self, method: str, path: str, *, expected: int = 200, **kwargs: Any
    ) -> dict[str, Any] | list[Any]:
        response = self.http.request(
            method,
            f"{self.base_url}{path}",
            timeout=self.timeout,
            **kwargs,
        )
        if response.status_code != expected:
            body = response.text[:500]
            raise E2EFailure(
                f"{method} {path} returned HTTP {response.status_code}, "
                f"expected {expected}: {body}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise E2EFailure(f"{method} {path} did not return JSON") from exc

    def health(self) -> dict[str, Any]:
        data = self.request_json("GET", "/health")
        if not isinstance(data, dict):
            raise E2EFailure("/health returned a non-object JSON value")
        if not data.get("session_id"):
            raise E2EFailure("/health did not report an authenticated session_id")
        return data

    def model(self, requested: str | None) -> str:
        data = self.request_json("GET", "/model")
        if not isinstance(data, dict):
            raise E2EFailure("/model returned a non-object JSON value")
        available = data.get("available") or []
        current = requested or data.get("current")
        if not current:
            raise E2EFailure("server did not report a default model")
        if requested and available and requested not in available:
            # Some campus-provided approaches (for example
            # ``campus-affairs-qa``) are intentionally hidden from the model
            # selector but remain callable by explicit approach_id.
            print(f"  using explicitly requested model not in catalog: {requested}")
        return str(current)

    def chat(
        self,
        message: str,
        *,
        model: str,
        chat_session_id: str | None = None,
        parent_idx: int = -1,
    ) -> ConversationTurn:
        started = time.monotonic()
        data = self.request_json(
            "POST",
            "/chat",
            json={
                "message": message,
                "approach_id": model,
                "chat_session_id": chat_session_id,
                "parent_idx": parent_idx,
                "stream": False,
            },
        )
        if not isinstance(data, dict):
            raise E2EFailure("/chat returned a non-object JSON value")
        session = data.get("chat_session_id")
        approach_idx = data.get("approach_msg_idx")
        text = data.get("text")
        if not isinstance(session, str) or not session:
            raise E2EFailure("chat reply did not contain chat_session_id")
        if not isinstance(approach_idx, int):
            raise E2EFailure("chat reply did not contain integer approach_msg_idx")
        if not isinstance(text, str) or not text.strip():
            raise E2EFailure("chat reply contained empty text")
        print(
            f"  turn completed in {time.monotonic() - started:.1f}s; "
            f"session={session[:16]}..., parent={approach_idx}, "
            f"text={text[:100]!r}"
        )
        return ConversationTurn(session, approach_idx, text)


def run_qa(client: E2EClient, model: str, message: str) -> None:
    """Exercise one ordinary non-streaming question/answer exchange."""
    print("\n=== E2E 1/3: ordinary Q&A ===")
    turn = client.chat(message, model=model)
    if turn.approach_msg_idx < 0:
        raise E2EFailure("ordinary Q&A returned an invalid approach_msg_idx")


def run_conversation(client: E2EClient, model: str) -> None:
    """Exercise at least three turns using the previous approach index."""
    print("\n=== E2E 2/3: three-turn conversation ===")
    prompts = (
        "请简短介绍深圳校区学生事务助手能提供哪些帮助，并记住测试暗号：web2api-e2e-42。",
        "我上一轮让你记住的测试暗号是什么？只回答暗号。",
        "结合上一轮的内容，用一句话确认你记得这个暗号。",
    )
    previous: ConversationTurn | None = None
    for number, prompt in enumerate(prompts, start=1):
        turn = client.chat(
            prompt,
            model=model,
            chat_session_id=previous.chat_session_id if previous else None,
            parent_idx=previous.approach_msg_idx if previous else -1,
        )
        if previous is not None and turn.chat_session_id != previous.chat_session_id:
            raise E2EFailure(
                f"turn {number} started a new session instead of continuing "
                f"{previous.chat_session_id}"
            )
        if previous is not None and turn.approach_msg_idx <= previous.approach_msg_idx:
            raise E2EFailure(
                f"turn {number} did not advance approach_msg_idx: "
                f"{previous.approach_msg_idx} -> {turn.approach_msg_idx}"
            )
        previous = turn
    print("  conversation continuity: same session across 3 turns")


def _local_pwsh(command: str) -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if not executable:
        raise E2EFailure("pwsh/powershell is required for the tools scenario")
    result = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise E2EFailure(
            f"local PowerShell command failed ({result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip()


def run_tools(
    client: E2EClient,
    *,
    command: str,
    require_real_pwsh: bool,
    verify_local_pwsh: bool,
) -> None:
    """Exercise tool registration, dispatch, and execution-log endpoints."""
    print("\n=== E2E 3/3: tool call (pwsh contract) ===")
    expected_output = _local_pwsh(command) if verify_local_pwsh else ""
    if expected_output:
        print(f"  local pwsh output: {expected_output!r}")

    definitions = client.request_json("GET", "/tools")
    if not isinstance(definitions, list):
        raise E2EFailure("/tools returned a non-array JSON value")
    names = {item.get("name") for item in definitions if isinstance(item, dict)}
    if "pwsh" not in names:
        registration = client.request_json(
            "POST",
            "/tools",
            json={
                "name": "pwsh",
                "description": "Execute a PowerShell command for e2e testing",
                "parameters": {
                    "command": {
                        "type": "string",
                        "description": "PowerShell command",
                        "required": True,
                    }
                },
            },
        )
        if (
            not isinstance(registration, dict)
            or registration.get("status") != "registered"
        ):
            raise E2EFailure(f"pwsh registration failed: {registration}")

    result = client.request_json(
        "POST",
        "/tools/call",
        json={"tool_name": "pwsh", "arguments": {"command": command}},
    )
    if not isinstance(result, dict) or result.get("status") != "success":
        raise E2EFailure(f"pwsh tool call failed: {result}")
    content = str(result.get("content", ""))
    if not content:
        raise E2EFailure("pwsh tool call returned empty content")

    # The current HTTP registration route intentionally returns a placeholder;
    # make that limitation visible instead of pretending it ran PowerShell.
    is_placeholder = "executed with arguments" in content
    if is_placeholder:
        print("  server tool result is the documented placeholder handler")
        if require_real_pwsh:
            raise E2EFailure(
                "server did not execute pwsh; POST /tools is still using its "
                "placeholder handler"
            )
    elif expected_output and expected_output not in content:
        raise E2EFailure(
            f"server pwsh result did not contain expected output {expected_output!r}: "
            f"{content!r}"
        )

    log = client.request_json("GET", "/tools/log")
    if not isinstance(log, list) or not any(
        isinstance(entry, dict) and entry.get("tool_name") == "pwsh" for entry in log
    ):
        raise E2EFailure("/tools/log did not record the pwsh call")
    print("  tool registration, dispatch, and execution log: ok")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--scenario",
        choices=("qa", "conversation", "tools", "all"),
        default="all",
        help="Scenario to run (default: all)",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Model (default: {DEFAULT_MODEL}; override for another approach)",
    )
    parser.add_argument(
        "--message",
        default="请用一句话说明深圳校区学生事务助手能提供什么帮助。",
        help="Prompt for the ordinary Q&A scenario",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--pwsh-command", default=DEFAULT_PWSH_COMMAND)
    parser.add_argument(
        "--no-local-pwsh",
        action="store_true",
        help="Do not execute the deterministic command locally before calling the API",
    )
    parser.add_argument(
        "--require-real-pwsh",
        action="store_true",
        help="Fail if the server returns its documented placeholder tool result",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    client = E2EClient(args.base_url, args.timeout)
    try:
        print(f"Server: {client.base_url}")
        health = client.health()
        browser = health.get("browser") or {}
        backend = (
            browser.get("backend", "unknown")
            if isinstance(browser, dict)
            else "unknown"
        )
        print(
            f"Authenticated session: {str(health['session_id'])[:16]}...; "
            f"browser={backend}"
        )
        model = client.model(args.model)
        print(f"Model: {model}")

        if args.scenario in {"qa", "all"}:
            run_qa(client, model, args.message)
        if args.scenario in {"conversation", "all"}:
            run_conversation(client, model)
        if args.scenario in {"tools", "all"}:
            run_tools(
                client,
                command=args.pwsh_command,
                require_real_pwsh=args.require_real_pwsh,
                verify_local_pwsh=not args.no_local_pwsh,
            )
    except requests.exceptions.ConnectionError:
        print(
            "ERROR: server is not running; start it with "
            ".venv\\Scripts\\python.exe src\\cuhk_shenzhen_web2api\\scripts\\server.py",
            file=sys.stderr,
        )
        return 2
    except (E2EFailure, requests.RequestException) as exc:
        print(f"E2E FAILED: {exc}", file=sys.stderr)
        return 1

    print("\nE2E PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
