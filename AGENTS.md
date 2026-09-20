# AGENTS.md

## Project overview

This repository is a Python 3.12 client and local HTTP bridge for CUHK-Shenzhen's AI chat at `https://ai.cuhk.edu.cn/chat/`. A Firecrawl cloud browser completes the ADFS/aTrust SSO flow and keeps the authenticated, IP-bound browser session alive. `ChatClient` then calls the reverse-engineered chat API from the browser page context so the service receives the right cookies and CSRF token.

The project uses a `src` layout, `uv` for dependency management, `uv_build` as the build backend, FastAPI for the local server, and Ruff/Pyright for static checks. `README.md` is currently empty, so this file is the primary contributor guide.

The installed console entry point, `cuhk-shenzhen-web2api`, currently calls the placeholder `main()` in `src/cuhk_shenzhen_web2api/__init__.py`. Do not use it for real workflows.

## Repository map

- `src/cuhk_shenzhen_web2api/chat_client.py`: chat protocol, NDJSON decoding, conversation history, abort/recover, and uploads.
- `src/cuhk_shenzhen_web2api/cloud_browser.py`: rate-limited Firecrawl `browser_execute` wrapper and browser session persistence.
- `src/cuhk_shenzhen_web2api/login.py`: SSO navigation and authentication flow.
- `src/cuhk_shenzhen_web2api/env.py`: `.env` and process-environment loading.
- `src/cuhk_shenzhen_web2api/paths.py`: repository and `data/` paths.
- `src/cuhk_shenzhen_web2api/server.py`: FastAPI application and compatibility endpoints.
- `src/cuhk_shenzhen_web2api/tool_proxy.py`: tool registration, execution, and logging.
- `src/cuhk_shenzhen_web2api/scripts/`: thin command-line runners for login, chat, probing, scraping, and serving.
- `scripts/`: local manual test and server helper scripts; this is not where shared application logic belongs.
- `docs/tool_proxy.md`: tool-proxy API and extension details.
- `data/`: gitignored live-session state, cookies, replies, scans, and downloaded bundles.

Keep shared behavior in package modules. Command-line scripts should only parse arguments, call package APIs, and format output.

## Setup

The supported development environment is Windows PowerShell.

1. Install `uv` and Python 3.12.
2. Install the locked project and development dependencies:

   ```powershell
   uv sync
   ```

3. Create a local configuration file from `.env.example` and fill in the values without committing it:

   ```powershell
   Copy-Item .env.example .env
   ```

Required live credentials are `FIRECRAWL_API_KEY` plus either `USERNAME`/`PASSWORD` or `CHAT_USERNAME`/`CHAT_PASSWORD`. `CHAT_*` names take precedence when both aliases are present. `CHAT_COOKIE` is used only by cookie-based scraping paths.

Always invoke repository scripts through the virtual environment interpreter when documenting or reproducing a command:

```powershell
.venv\Scripts\python.exe <script> 2>&1
```

Bare `python` and `py` may resolve to the active environment, but do not rely on that. The virtual environment does not provide a usable `pip`; use `uv sync` to change installed dependencies and update both `pyproject.toml` and `uv.lock` when adding a dependency.

## Safe verification

There is no pytest/unittest suite. The CI job named `check` runs these four offline checks on pushes to `master` and on pull requests:

```powershell
uv run ruff check src
uv run ruff format --check src
uv run pyright
uv run python -m compileall -q src\cuhk_shenzhen_web2api
```

Run all four before finishing a code change. For a quick syntax-only sanity check, use:

```powershell
.venv\Scripts\python.exe -m compileall -q src\cuhk_shenzhen_web2api
```

`scripts/test_tool_proxy.py` is a manual, offline smoke runner, not a discovered test suite:

```powershell
.venv\Scripts\python.exe scripts\test_tool_proxy.py 2>&1
```

Do not treat `scripts/e2e_test.py` as a routine test. It expects a running authenticated server, calls production chat endpoints, changes the selected model, sends messages, and consumes the rate-limited Firecrawl service.

## Live workflows

Live commands contact production and mutate gitignored files under `data/`. Do not run them merely to validate an unrelated change. They require valid credentials, consume a rate-limited API, and can create chat history or upload user files.

Establish or resume a login first:

```powershell
.venv\Scripts\python.exe src\cuhk_shenzhen_web2api\scripts\login.py 2>&1
```

This persists the Firecrawl session ID and cookies in `data/chat_session/`. Useful live runners are:

```powershell
# Send one message and save the raw/parsed response.
.venv\Scripts\python.exe src\cuhk_shenzhen_web2api\scripts\send_message.py "text" 2>&1

# Inspect auth/config and scan/download frontend bundles.
.venv\Scripts\python.exe src\cuhk_shenzhen_web2api\scripts\probe.py 2>&1

# Scrape a page directly; this is the runner that accepts --cookie.
.venv\Scripts\python.exe src\cuhk_shenzhen_web2api\scripts\scrape_chat.py 2>&1

# Start the local API server on 127.0.0.1:8765.
.venv\Scripts\python.exe src\cuhk_shenzhen_web2api\scripts\server.py 2>&1
```

`send_message.py` supports `--model`, `--pool`, `--session`, `--parent`, `--continue`, `--no-tools`, `--tool-proxy`, repeated `--image`/`--file`, and tool registration. Consult `--help` for the current interface instead of duplicating argument parsing in another script.

The alternative `scripts/_run_server.py` adds request logging and explicit signal handling. Once the server is authenticated and running, `scripts/e2e_test.py` exercises its endpoints. Both are manual helpers.

## Local server API

`src/cuhk_shenzhen_web2api/server.py` owns the FastAPI application. Its main routes are:

- `GET /health`: Firecrawl session ID, browser URL, and authenticated user.
- `GET /model` and `POST /model`: inspect or change the default approach/model.
- `POST /chat`: complete JSON reply; handles `/model`, `/model <name>`, and `/help` commands.
- `POST /chat/stream`: streaming chat endpoint.
- `POST /response`: unified endpoint; `stream=false` returns JSON and `stream=true` returns SSE.
- `POST /v1/responses`: OpenAI Responses-compatible input, JSON output, or SSE output.
- `GET /v1/models`: OpenAI-compatible model listing.
- `GET /sessions` and `GET /sessions/{session_id}`: recent sessions and complete history.
- `GET /tools`, `POST /tools`, and `DELETE /tools/{tool_name}`: tool registry management.
- `POST /tools/call`: execute a registered tool.
- `GET /tools/log` and `DELETE /tools/log`: inspect or clear execution history.

Read the Pydantic request models and route implementation before changing payload compatibility. Keep the native `/chat` behavior and the OpenAI-compatible `/v1` behavior consistent where they share a code path.

## Chat protocol invariants

The upstream `POST /chat/` response uses `application/x-ndjson+json`, with one JSON event per line:

- `start` includes `chat_session_id`, `user_msg_idx`, and `approach_msg_idx`.
- `hb` is a heartbeat.
- `msg` contains items. Concatenate `text` item content in arrival order and preserve `tool` items.
- `end` includes `status`, `title`, `ctx_token_cnt`, and possible truncation metadata.

Use `iter_ndjson()` and `assemble_reply()` rather than adding another parser. Continue a conversation by reusing `chat_session_id` and setting `parent_idx` to the prior assistant/approach index from `start.approach_msg_idx`. A user-message index is invalid and produces `新消息的 parent_idx 必须指向 approach 消息`.

Every upstream POST must read the `csrftoken` cookie and send it in `X-CSRFToken`. Prefer `ChatClient` helpers, which already enforce this behavior.

Relevant upstream endpoints are:

- `POST /chat/`, `/chat/abort/`, and `/chat/recover/` for conversation lifecycle.
- `POST /getNextHistoryMeta/` with `{need, time_offset, count_offset, includes_pinned}` for session summaries.
- `POST /getHistoryItem/` with `{chat_session_id}` for full message history.
- `POST /uploadFile/` and `POST /uploadMedia/` for attachments; both return `media_id`.

`ChatClient.upload_path()` sends local bytes through the browser context as a Blob. `send_with_files()` uploads first, then passes `image_ids` and `file_ids` to the chat request.

## Firecrawl sandbox constraints

JavaScript emitted through `CloudBrowser.js()` runs in a persistent Node sandbox with a `page` handle:

- Only the last expression's value is returned; `console.log()` output is discarded. End snippets with the value to return, often `JSON.stringify(...)`.
- Top-level `const`, `let`, and `function` bindings persist across calls and fail when redeclared. Use `var` or assignment and choose collision-resistant temporary names.
- `window` and `document` do not exist at Node scope. Browser DOM and fetch work must run inside `page.evaluate(...)`.
- Slash characters in regex literals for paths must be escaped as `\/`.
- Firecrawl browser session TTL is capped at 3600 seconds by the service.
- The free tier is heavily rate-limited. `CloudBrowser.js()` sleeps before calls and retries rate-limit failures; use it instead of direct `browser_execute` loops and combine browser work when practical.

The authenticated session is bound to the cloud browser environment. Do not move upstream requests to local `requests`/`httpx` calls unless the authentication model has deliberately changed.

## Code style and change rules

- Follow the existing typed Python style: `from __future__ import annotations`, modern built-in generics, small focused helpers, and type annotations at package boundaries.
- Let Ruff define formatting and lint behavior. Do not add unrelated formatting churn.
- Keep Pyright clean for `src/cuhk_shenzhen_web2api`; that is the configured type-check scope.
- Add concise comments only for protocol discoveries, sandbox workarounds, or behavior that is not evident from the code.
- Keep reverse-engineered endpoint constants and protocol handling centralized in `chat_client.py`.
- Keep HTTP schemas and compatibility translation in `server.py`; keep tool execution mechanics in `tool_proxy.py`.
- Preserve existing public request fields and response shapes unless a breaking change is explicitly intended.
- There is no documented deployment pipeline beyond package building and the CI checks. Do not invent release or deployment commands.

## Security and repository hygiene

- Never commit `.env`, cookies, credentials, Firecrawl session IDs, raw production responses, uploaded files, or anything under `data/`.
- Do not print credential values or full cookies while debugging. Redact session IDs and user data in shared logs.
- Treat downloaded bundles and production chat history as potentially sensitive, even though `data/` is gitignored.
- Confirm `git status --short` before finishing so generated artifacts and secrets have not become tracked.
- Do not casually run live login, probe, chat, upload, server E2E, or scraping flows. State clearly when live verification was not performed.
