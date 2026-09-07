"""Task: send one chat message and print the assistant reply.

Uses a logged-in cloud browser session (resumes data/chat_session/session_id.txt
or --resume). Writes the raw NDJSON stream and a parsed summary to
data/chat_session/last_stream.ndjson / last_reply.json.

Usage:
    python src/cuhk_shenzhen_web2api/scripts/send_message.py "你好" \
        [--model claude-haiku-4-5] [--pool "Students Pool"] [--session SID]
"""

from __future__ import annotations

import argparse
import json
import sys

from firecrawl import Firecrawl

from cuhk_shenzhen_web2api import cloud_browser, env, login
from cuhk_shenzhen_web2api.chat_client import ChatClient, parse_chat_stream
from cuhk_shenzhen_web2api.paths import CHAT_DATA_DIR, SESSION_ID_FILE


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
    parser.add_argument("--resume", default=None, help="Resume Firecrawl browser sid")
    parser.add_argument(
        "--no-tools",
        action="store_true",
        dest="no_tools",
        help="Set params.tool_proxy=false",
    )
    args = parser.parse_args()

    config = env.load_env()
    api_key = env.firecrawl_api_key(config)
    if not api_key:
        print("FIRECRAWL_API_KEY missing in .env", file=sys.stderr)
        sys.exit(1)

    app = Firecrawl(api_key=api_key)
    sid = args.resume or cloud_browser.load_session_id(SESSION_ID_FILE)
    if not sid:
        print("no browser session — run scripts/login.py first", file=sys.stderr)
        sys.exit(1)
    cb = cloud_browser.resume_session(app, sid)
    print("session   ", sid, flush=True)

    final = login.ensure_on_chat(
        cb, env.chat_username(config), env.chat_password(config)
    )
    if "/chat" not in (final or ""):
        print("not on /chat/:", final, file=sys.stderr)
        sys.exit(1)

    client = ChatClient(cb, quota_pool=args.quota_pool)
    params = {"tool_proxy": False} if args.no_tools else None
    reply = client.send_stream(
        args.message,
        approach_id=args.approach_id,
        quota_pool=args.quota_pool,
        chat_session_id=args.session,
        parent_idx=args.parent,
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
