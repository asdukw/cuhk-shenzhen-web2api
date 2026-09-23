"""Task: send one chat message and print the assistant reply.

Uses a logged-in browser session (resumes the backend-specific session ID or
``--resume``). Writes the raw NDJSON stream and a parsed summary to
``data/chat_session/last_stream.ndjson`` / ``last_reply.json``.

The local Steel browser settings come from .env; see `browser_provider`.

Usage:
    python src/cuhk_shenzhen_web2api/scripts/send_message.py "你好" \
        [--model claude-haiku-4-5] [--pool "Students Pool"] [--session SID] \
        [--continue] [--image x.png --file doc.pdf]
"""

from __future__ import annotations

import argparse
import json
import sys

from cuhk_shenzhen_web2api import browser_provider, env, login
from cuhk_shenzhen_web2api.chat_client import ChatClient
from cuhk_shenzhen_web2api.paths import CHAT_DATA_DIR


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("message", help="User message")
    parser.add_argument("--model", default="claude-haiku-4-5", dest="approach_id")
    parser.add_argument("--pool", default="Students Pool", dest="quota_pool")
    parser.add_argument(
        "--session",
        default=None,
        help="Existing chat_session_id to continue, or 'new' (omit to start fresh)",
    )
    parser.add_argument(
        "--parent",
        type=int,
        default=-1,
        help="parent_idx (previous turn's end message)",
    )
    parser.add_argument(
        "--continue",
        action="store_true",
        dest="continue_turn",
        help="Auto-continue the last conversation (reads last_reply.json)",
    )
    parser.add_argument(
        "--image",
        action="append",
        default=[],
        metavar="PATH",
        help="Local image to upload and attach (repeatable)",
    )
    parser.add_argument(
        "--file",
        action="append",
        default=[],
        metavar="PATH",
        help="Local file to upload and attach (repeatable)",
    )
    parser.add_argument("--resume", default=None, help="Resume browser session id")
    parser.add_argument(
        "--no-tools",
        action="store_true",
        dest="no_tools",
        help="Set params.tool_proxy=false",
    )
    parser.add_argument(
        "--tool-proxy",
        action="store_true",
        dest="tool_proxy",
        help="Enable tool proxy (params.tool_proxy=true)",
    )
    parser.add_argument(
        "--register-tool",
        nargs=3,
        metavar=("NAME", "DESCRIPTION", "ENDPOINT"),
        help="Register a tool: name description endpoint",
    )
    args = parser.parse_args()

    config = env.load_env()
    try:
        settings = browser_provider.resolve_settings(config)
    except browser_provider.ConfigurationError as exc:
        print(f"browser backend unusable: {exc}", file=sys.stderr)
        sys.exit(1)
    sid = args.resume or browser_provider.load_session_id(settings)
    if not sid:
        print("no browser session — run scripts/login.py first", file=sys.stderr)
        sys.exit(1)

    try:
        print(f"backend   {settings.describe()}", flush=True)
        cb = browser_provider.open_browser_session(settings, sid)
    except browser_provider.ConfigurationError as exc:
        print(f"browser backend unusable: {exc}", file=sys.stderr)
        sys.exit(1)
    print("session   ", cb.sid, flush=True)

    final = login.ensure_on_chat(
        cb, env.chat_username(config), env.chat_password(config)
    )
    if "/chat" not in (final or ""):
        print("not on /chat/:", final, file=sys.stderr)
        sys.exit(1)

    client = ChatClient(cb, quota_pool=args.quota_pool)

    # Build params dict
    params: dict | None = None
    if args.no_tools:
        params = {"tool_proxy": False}
    elif args.tool_proxy:
        params = {"tool_proxy": True}

    # Register tool if requested
    if args.register_tool:
        name, description, endpoint = args.register_tool
        from cuhk_shenzhen_web2api.tool_proxy import register_tool

        def make_tool_handler(endpoint_url: str):
            def handler(arguments: dict) -> str:
                import requests

                response = requests.post(endpoint_url, json=arguments)
                response.raise_for_status()
                return response.text

            return handler

        register_tool(
            name=name,
            description=description,
            func=make_tool_handler(endpoint),
        )
        print(f"Registered tool: {name}", flush=True)

    last_reply: dict = {}
    last_file = CHAT_DATA_DIR / "last_reply.json"
    if args.continue_turn and last_file.exists():
        last_reply = json.loads(last_file.read_text(encoding="utf-8"))

    if (
        args.continue_turn
        and last_reply.get("chat_session_id")
        and last_reply.get("approach_msg_idx") is not None
    ):
        print(
            f"continue session {last_reply['chat_session_id']} "
            f"parent={last_reply['approach_msg_idx']}",
            flush=True,
        )
    elif args.continue_turn:
        print(
            "--continue requested but no usable last_reply.json — starting fresh",
            file=sys.stderr,
        )

    if args.image or args.file:
        reply = client.send_with_files(
            args.message,
            approach_id=args.approach_id,
            image_paths=tuple(args.image),
            file_paths=tuple(args.file),
            quota_pool=args.quota_pool,
            chat_session_id=args.session or last_reply.get("chat_session_id"),
            parent_idx=args.parent
            if args.parent >= 0
            else last_reply.get("approach_msg_idx", -1),
            params=params,
        )
    else:
        reply = client.send_stream(
            args.message,
            approach_id=args.approach_id,
            quota_pool=args.quota_pool,
            chat_session_id=args.session or last_reply.get("chat_session_id"),
            parent_idx=args.parent
            if args.parent >= 0
            else last_reply.get("approach_msg_idx", -1),
            params=params,
        )

    CHAT_DATA_DIR.mkdir(parents=True, exist_ok=True)
    (CHAT_DATA_DIR / "last_stream.ndjson").write_text(
        "\n".join(json.dumps(ev, ensure_ascii=False) for ev in reply.lines),
        encoding="utf-8",
    )
    summary = {
        "chat_session_id": reply.chat_session_id,
        "user_msg_idx": reply.user_msg_idx,
        "approach_msg_idx": reply.approach_msg_idx,
        "title": reply.title,
        "status": reply.status,
        "ctx_token_cnt": reply.ctx_token_cnt,
        "tools": reply.tools,
        "text": reply.text,
    }
    (CHAT_DATA_DIR / "last_reply.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("\n=== reply ===")
    print(f"chat_session_id: {reply.chat_session_id}")
    print(f"status: {reply.status} | title: {reply.title}")
    if reply.tools:
        print("tools:", [t.get("tool_name") for t in reply.tools])
    print("---")
    print(reply.text)
    print("\nsaved -> last_stream.ndjson, last_reply.json")


if __name__ == "__main__":
    main()
