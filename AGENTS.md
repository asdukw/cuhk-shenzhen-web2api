# AGENTS.md

Programmatic client for CUHK-Shenzhen's AI chat (`https://ai.cuhk.edu.cn/chat/`): a Firecrawl cloud browser drives the full SSO chain, then calls a reverse-engineered chat API. The packaged entrypoint (`cuhk_shenzhen_web2api:main`) is a placeholder — the real runnables are the scripts. There is no README; this file is the primary doc.

## Run / verify
- Windows + PowerShell. Always call the venv interpreter explicitly — bare `python`/`py` resolves to the active venv anyway, and there is no `pip` module inside it:
  `.venv\Scripts\python.exe <script> 2>&1`
- Scripts in `scripts/` are thin one-task runners; all shared logic lives in the package modules (`chat_client.py`, `cloud_browser.py`, `login.py`, `env.py`, `paths.py`, `server.py`). Add logic to the package, not to scripts.
- Offline tests live in `tests/` (stdlib `unittest`, no pytest; `tests` is a namespace dir so `-m unittest discover` fails — list modules explicitly):
  `.venv\Scripts\python.exe -m unittest tests.test_v2_store tests.test_v2_engine tests.test_v2_app tests.test_v2_transport tests.test_v2_optimization tests.test_v2_agent_probe tests.test_kilo_bridge tests.test_v2_browser_integration`
  The browser-integration module launches local Chromium against a loopback server only (no campus network); it needs the `kilo`/`local` extra and a Chromium install.
- Ruff + pyright are in the venv `[dependency-groups] dev`. Run locally:
  `uv run ruff check src` · `uv run ruff format --check src` · `uv run pyright` · `uv run python -m compileall -q src\cuhk_shenzhen_web2api`
- CI (`.github/workflows/ci.yml`, GitHub Actions, check name `check`) installs `--extra kilo --group dev`, then runs compileall, ruff check, ruff format --check, pyright, and the offline unittest suites above (installing Chromium for the loopback browser test). Note the repo's git remote is GitLab, so this GitHub workflow only runs if the repo is also pushed to GitHub.
- Never commit `.env`; it is gitignored, secret, and was already purge-removed from Git history. Human is `1XXXXXXX` (SDS, XXXX XXXXX).

## Real work happens live
These scripts talk to the production site through Firecrawl's cloud browser and mutate `data/` (gitignored). They need a live session and consume the rate-limited API — do not run them casually.

### Entrypoint scripts
- `scripts/login.py` — create/resume a cloud browser session, run SSO login, persist session id + cookies to `data/chat_session/`. Run this first to establish a session.
- `scripts/send_message.py "text"` — send one chat message, print the assistant reply. Flags: `--model`, `--pool "Students Pool"`, `--session <id>`, `--parent <idx>`, `--no-tools`, `--continue`, `--image PATH`, `--file PATH`.
- `scripts/probe.py` — recon: auth/config reads, model catalog, frontend-bundle API-surface scan + bundle download (cached under `data/chat_session/`).
- `scripts/scrape_chat.py` — generic Firecrawl page scrape; the only script that takes a raw cookie (`--cookie`) instead of doing SSO.
- `scripts/server.py` — local FastAPI server (`uvicorn`, 127.0.0.1:8765). Endpoints:
  - `GET /health` — browser session id, URL, auth user.
  - `POST /chat` — send message (JSON body: `message`, `approach_id`, `quota_pool`, `chat_session_id`, `parent_idx`, `tool_proxy`). Slash commands handled: `/model`, `/model <name>`, `/help`.
  - `POST /response` — unified response endpoint (JSON body: `message`, `stream`, ...). When `stream=false` (default), returns complete reply as JSON. When `stream=true`, returns SSE events.
  - `GET /sessions[?limit]` — recent conversations with titles.
  - `GET /sessions/{id}` — full conversation (messages array).
  - `GET /model` — current default model + available models.
  - `POST /model` — switch default model (`{"approach_id": "..."}`).
  - `GET /tools` — list all registered tools.
  - `POST /tools` — register a new tool.
  - `DELETE /tools/{tool_name}` — unregister a tool.
  - `POST /tools/call` — execute a tool call.
  - `GET /tools/log` — get tool execution log.
  - `DELETE /tools/log` — clear tool execution log.

## Firecrawl `browser_execute` sandbox quirks (JS emitted by `cloud_browser.py`)
- Only the **last expression's value** is returned (`console.log` is dropped); scripts must end with the value, e.g. leaves `JSON.stringify(...)` as the final expression.
- Top-level `const`/`let`/`function` persist across calls in the same session → "already declared" errors on redeclare. Use `var` or bare assignment and never redeclare a name.
- `window`/`document` do **not** exist at the Node level — browser-DOM code must run through `page.evaluate( async () => {...} )`.
- Free tier is ~3 steps/min: `CloudBrowser.js()` sleeps 3.5s between calls and retries on rate-limit. Prefer the `CloudBrowser`/`ChatClient` helpers over hand-rolled `browser_execute` loops.
- Regex literals containing `/` in paths must escape them as `\/`.

## Chat API protocol (`POST /chat/`)
- Response is `application/x-ndjson+json`, one JSON event per line: `start` / `hb` / `msg` / `end`.
- `msg` item types: `text` → accumulate `content` across events for the reply; `tool` → tool calls (e.g. `web_search`).
- `end` carries `status` (`finished`/`aborted`/`killed`/`error`), `title`, `ctx_token_cnt`; `start` carries `chat_session_id`, `user_msg_idx`, `approach_msg_idx`.
- Continue a conversation by reusing the `chat_session_id` **and** setting `parent_idx` to the *assistant* (approach) message index from `start.approach_msg_idx`. Pointing `parent_idx` at a *user* message returns server error "新消息的 parent_idx 必须指向 approach 消息".
- CSRF: read the `csrftoken` cookie and send it as the `X-CSRFToken` header on every POST (`ChatClient` does this).

## ChatClient additions (item 3)
- `iter_ndjson(text)` / `assemble_reply(events)` — incremental NDJSON stream decoder (generator + fold).
- `continue_stream(content, approach_id, last: ChatReply, **kw)` — auto-continue from a previous reply (uses `chat_session_id` + `approach_msg_idx`).
- `list_sessions(need=30, time_offset, count_offset, includes_pinned)` → `list[dict]` via `POST /getNextHistoryMeta/`.
- `history_item(chat_session_id)` → `dict` via `POST /getHistoryItem/` (full messages array with `role` `self_idx` `parent_idx` `items`).
- `upload_path(path, media=False)` → `media_id` via `POST /uploadMedia/` (images) or `POST /uploadFile/` (files). Local file bytes pushed through browser context as Blob.
- `send_with_files(content, approach_id, image_paths, file_paths, **kw)` — upload then send with `image_ids`/`file_ids`.

## Discovered endpoints (item 3)
- `POST /getNextHistoryMeta/` — `{need, time_offset, count_offset, includes_pinned}` → session list.
- `POST /getHistoryItem/` — `{chat_session_id}` → full conversation.
- `POST /uploadFile/` — raw file bytes → `media_id`.
- `POST /uploadMedia/` — raw image/video bytes → `media_id`.
