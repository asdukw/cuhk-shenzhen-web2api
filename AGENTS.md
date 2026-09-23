# AGENTS.md

## Project overview

This Python 3.12 project bridges CUHK-Shenzhen's AI chat at `https://ai.cuhk.edu.cn/chat/` to a local HTTP API. The repository-managed local Steel browser completes ADFS/aTrust SSO and keeps the authenticated, IP-bound browser session alive. `ChatClient` calls the chat API inside that page context so cookies and CSRF tokens match. Only the local Steel browser is supported.

The project uses a `src` layout, `uv`, `uv_build`, FastAPI, Ruff, and Pyright. The installed `cuhk-shenzhen-web2api` console entry point still calls a placeholder; use the package scripts instead.

## Repository map

- `src/cuhk_shenzhen_web2api/chat_client.py`: chat protocol, NDJSON, history, abort/recover, uploads.
- `src/cuhk_shenzhen_web2api/browser_backend.py`: browser operations protocol.
- `src/cuhk_shenzhen_web2api/browser_provider.py`: local Steel configuration and session-ID persistence.
- `src/cuhk_shenzhen_web2api/steel_browser.py`: HTTP adapter for the local Steel executor.
- `src/cuhk_shenzhen_web2api/login.py`: SSO navigation and authentication.
- `src/cuhk_shenzhen_web2api/env.py`: `.env` and process environment.
- `src/cuhk_shenzhen_web2api/paths.py`: repository and `data/` paths.
- `src/cuhk_shenzhen_web2api/server.py`: FastAPI and compatibility endpoints.
- `src/cuhk_shenzhen_web2api/tool_proxy.py`: tool registration and execution.
- `src/cuhk_shenzhen_web2api/scripts/`: thin runners for login, chat, probing, serving.
- `deploy/steel/`: loopback-only Steel browser and Node JavaScript executor.
- `scripts/`: manual tests and service helpers; shared logic belongs in the package.
- `docs/tool_proxy.md`: tool-proxy API details.
- `data/`: gitignored sessions, cookies, replies, scans, downloaded bundles.

## Setup and local backend

Use Windows PowerShell. Install `uv` and Python 3.12, then run `uv sync`. Copy `.env.example` to `.env` and fill in `USERNAME`/`PASSWORD` or `CHAT_USERNAME`/`CHAT_PASSWORD` (the `CHAT_*` aliases take precedence). No hosted browser API key is needed. Only `BROWSER_BACKEND=steel-local` (or `steel`/`local`) is accepted; unset also defaults to Steel. An old cloud backend selection fails explicitly.

| Variable | Meaning |
| --- | --- |
| `STEEL_EXECUTOR_URL` | Local executor URL, default `http://127.0.0.1:3003`. |
| `STEEL_EXECUTOR_TOKEN` | Shared executor token, default `steel-local-only`; override on shared machines in both `.env` files. |
| `STEEL_EXECUTOR_TIMEOUT` | Executor HTTP timeout in seconds, default 130. |

Start and verify the repository-managed stack:

```powershell
.\scripts\local_steel.ps1 up
.\scripts\local_steel.ps1 verify
.\scripts\local_steel.ps1 status
.\scripts\local_steel.ps1 logs
.\scripts\local_steel.ps1 down
```

`verify` opens `https://example.com` in a temporary tab without disturbing an authenticated page. The executor accepts arbitrary JavaScript, so it is token-protected and loopback-only. Its named Docker volume preserves the Chromium profile. Session IDs live at `data/chat_session/steel_session_id.txt` and cannot be reused from another backend. The aTrust session is tied to the browser's egress IP.

If a dependency download or image pull stalls, retry via `HTTP_PROXY=http://127.0.0.1:7897` and `HTTPS_PROXY=http://127.0.0.1:7897` on the host, or `http://host.docker.internal:7897` inside Docker. Keep local service traffic out of the proxy via `NO_PROXY`/`no_proxy`; `browser_provider` adds the executor host without dropping existing entries. Never commit proxy credentials.

Always invoke repository scripts with `.venv\Scripts\python.exe <script> 2>&1`. Bare `python` and `py` may resolve incorrectly; use `uv sync` for dependencies, updating both `pyproject.toml` and `uv.lock`.

## Safe verification

There is no discovered test suite. Run all four CI checks before finishing code changes:

```powershell
uv run ruff check src
uv run ruff format --check src
uv run pyright
uv run python -m compileall -q src\cuhk_shenzhen_web2api
```

Run offline smoke scripts as relevant:

```powershell
.venv\Scripts\python.exe scripts\test_browser_provider.py 2>&1
.venv\Scripts\python.exe scripts\test_tool_proxy.py 2>&1
```

Do not use `scripts/e2e_test.py` as a routine test: it needs an authenticated server, changes the model, sends messages, and consumes production service quota.

## Live workflows

Live commands contact CUHK and mutate gitignored `data/`. Do not run them just to validate unrelated changes. Login/resume, send a message, inspect the page, or serve HTTP with:

```powershell
.venv\Scripts\python.exe src\cuhk_shenzhen_web2api\scripts\login.py 2>&1
.venv\Scripts\python.exe src\cuhk_shenzhen_web2api\scripts\send_message.py "text" 2>&1
.venv\Scripts\python.exe src\cuhk_shenzhen_web2api\scripts\probe.py 2>&1
.venv\Scripts\python.exe src\cuhk_shenzhen_web2api\scripts\server.py 2>&1
```

`send_message.py` supports `--model`, `--pool`, `--session`, `--parent`, `--continue`, `--no-tools`, `--tool-proxy`, repeated `--image`/`--file`, and tool registration. Consult `--help` for current options. The server listens on `127.0.0.1:8765` and initializes the browser on first request. `scripts/_run_server.py` adds request logging and signal handling.

## Local server API

`GET /health` reports the browser session ID, URL, user, and local Steel settings (token redacted). `GET/POST /model` selects the default model; `POST /chat` and `/chat/stream` provide JSON and streaming chat; `POST /response` handles JSON or SSE. `POST /v1/responses` and `GET /v1/models` provide OpenAI-compatible shapes. `GET /sessions` and `/sessions/{session_id}` return history. `/tools`, `/tools/call`, and `/tools/log` manage the tool proxy. Read Pydantic schemas before changing payload compatibility; keep shared `/chat` and `/v1` behavior aligned.

## Chat protocol invariants

The upstream `POST /chat/` returns NDJSON events: `start` includes `chat_session_id`, `user_msg_idx`, and `approach_msg_idx`; `hb` is a heartbeat; `msg` contains ordered `text` and `tool` items; `end` includes status, title, context-token count, and possible truncation metadata. Use `iter_ndjson()` and `assemble_reply()` rather than adding another parser. For continuation, reuse the session ID and set `parent_idx` to the previous assistant `approach_msg_idx` (never the user-message index).

Every upstream POST needs the `csrftoken` cookie in `X-CSRFToken`; use `ChatClient` helpers. Upstream endpoints include `/chat/`, `/chat/abort/`, `/chat/recover/`, `/getNextHistoryMeta/`, `/getHistoryItem/`, `/uploadFile/`, and `/uploadMedia/`. `ChatClient.upload_path()` sends bytes as a Blob through the browser context; `send_with_files()` uploads then passes `image_ids` and `file_ids`.

## Steel JavaScript contract

The executor maintains a persistent Node context with `page`. Only the last expression is returned; `console.log()` is discarded. End snippets with a value, often `JSON.stringify(...)`. Top-level `const`, `let`, and `function` bindings persist and fail on redeclaration; use `var` or assignment and collision-resistant names. `window`, `document`, and browser `fetch` belong inside `page.evaluate(...)`. Escape slashes in path regex literals as `\/`. Keep authenticated requests in the browser page context, not local `requests`/`httpx` calls.

## Style and security

Follow typed Python conventions, `from __future__ import annotations`, modern built-in generics, focused helpers, Ruff formatting, and Pyright. Centralize protocol details in `chat_client.py`, HTTP schemas in `server.py`, and tool execution in `tool_proxy.py`. Avoid unrelated changes or breaking request/response shapes. Never commit `.env`, credentials, cookies, session IDs, raw production replies, uploaded files, or `data/`. Redact session IDs and user data in shared logs. Confirm `git status --short` before finishing and state when live verification was not performed.
