# AGENTS.md

## Project overview

This repository is a Python 3.12 client and local HTTP bridge for CUHK-Shenzhen's AI chat at `https://ai.cuhk.edu.cn/chat/`. A selectable browser backend completes the ADFS/aTrust SSO flow and keeps the authenticated, IP-bound browser session alive. `ChatClient` then calls the reverse-engineered chat API from the browser page context so the service receives the right cookies and CSRF token.

The scrape backend is selectable: Firecrawl Cloud (default) or the repository-managed local Firecrawl stack. Browser sessions independently use Firecrawl Cloud or the repository-managed local Steel stack. See "Firecrawl backends" and "Browser backends" below.

The project uses a `src` layout, `uv` for dependency management, `uv_build` as the build backend, FastAPI for the local server, and Ruff/Pyright for static checks. `README.md` is currently empty, so this file is the primary contributor guide.

The installed console entry point, `cuhk-shenzhen-web2api`, currently calls the placeholder `main()` in `src/cuhk_shenzhen_web2api/__init__.py`. Do not use it for real workflows.

## Repository map

- `src/cuhk_shenzhen_web2api/chat_client.py`: chat protocol, NDJSON decoding, conversation history, abort/recover, and uploads.
- `src/cuhk_shenzhen_web2api/cloud_browser.py`: rate-limited Firecrawl Cloud browser-session adapter.
- `src/cuhk_shenzhen_web2api/browser_backend.py`: structural interface shared by browser adapters.
- `src/cuhk_shenzhen_web2api/browser_provider.py`: resolves Firecrawl Cloud vs local Steel browser settings and owns backend-aware session IDs.
- `src/cuhk_shenzhen_web2api/steel_browser.py`: HTTP adapter for the local Steel executor.
- `src/cuhk_shenzhen_web2api/firecrawl_provider.py`: resolves the Firecrawl backend (cloud vs local) and builds the SDK client. The only place that reads `FIRECRAWL_*` configuration.
- `src/cuhk_shenzhen_web2api/login.py`: SSO navigation and authentication flow.
- `src/cuhk_shenzhen_web2api/env.py`: `.env` and process-environment loading.
- `src/cuhk_shenzhen_web2api/paths.py`: repository and `data/` paths.
- `src/cuhk_shenzhen_web2api/server.py`: FastAPI application and compatibility endpoints.
- `src/cuhk_shenzhen_web2api/tool_proxy.py`: tool registration, execution, and logging.
- `src/cuhk_shenzhen_web2api/scripts/`: thin command-line runners for login, chat, probing, scraping, and serving.
- `deploy/firecrawl/`: repository-managed, loopback-only Firecrawl scrape stack adapted from the pinned open-source upstream implementation.
- `deploy/steel/`: repository-managed, loopback-only Steel browser plus the Node JavaScript executor.
- `scripts/`: local manual test and server helper scripts; this is not where shared application logic belongs.
- `docs/tool_proxy.md`: tool-proxy API and extension details.
- `data/`: gitignored live-session state, cookies, replies, scans, and downloaded bundles.

Keep shared behavior in package modules. Command-line scripts should only parse arguments, call package APIs, and format output. Never construct a `Firecrawl(...)` client directly outside `firecrawl_provider`.

## Setup

The supported development environment is Windows PowerShell.

If a dependency download, image pull, or other network operation fails or
stalls, retry it through the local HTTP proxy on port 7897. For host-side
commands use `HTTP_PROXY=http://127.0.0.1:7897` and
`HTTPS_PROXY=http://127.0.0.1:7897`; for traffic originating inside Docker use
`http://host.docker.internal:7897`. Keep loopback service traffic out of the
proxy with `NO_PROXY`/`no_proxy` as described below. Do not commit proxy
credentials if the proxy configuration later requires authentication.

1. Install `uv` and Python 3.12.
2. Install the locked project and development dependencies:

   ```powershell
   uv sync
   ```

3. Create a local configuration file from `.env.example` and fill in the values without committing it:

   ```powershell
   Copy-Item .env.example .env
   ```

Required live credentials are either `USERNAME`/`PASSWORD` or `CHAT_USERNAME`/`CHAT_PASSWORD`, plus the Firecrawl backend settings described below. `CHAT_*` names take precedence when both aliases are present. `CHAT_COOKIE` is used only by cookie-based scraping paths.

Always invoke repository scripts through the virtual environment interpreter when documenting or reproducing a command:

```powershell
.venv\Scripts\python.exe <script> 2>&1
```

Bare `python` and `py` may resolve to the active environment, but do not rely on that. The virtual environment does not provide a usable `pip`; use `uv sync` to change installed dependencies and update both `pyproject.toml` and `uv.lock` when adding a dependency.

## Firecrawl backends

`firecrawl_provider.resolve_settings()` is the single source of truth for scrape settings. It reads `.env` (via `env.load_env()`) and returns a validated `FirecrawlSettings`; `build_client()` turns that into the SDK client. The local Firecrawl backend supports scrape calls only.

| Variable | Meaning |
| --- | --- |
| `FIRECRAWL_MODE` | `cloud` (default) or `local`. `self-hosted`, `selfhosted`, `docker`, `offline` normalise to `local`; `hosted`, `remote`, `api` normalise to `cloud`. Any other value raises `ConfigurationError`. |
| `FIRECRAWL_API_URL` | Base URL override. Defaults to `https://api.firecrawl.dev` (cloud) or `http://127.0.0.1:3002` (local, the docker-compose publish). |
| `FIRECRAWL_API_KEY` | Required in cloud mode, optional in local mode. It is always passed to the SDK explicitly — including as `""` — so the SDK cannot silently pick up a stray process-environment key and send it to a local instance. |
| `FIRECRAWL_RATE_SLEEP` | Seconds slept before each browser call. Default 3.5 (cloud free tier is ~3 req/min) or 0.5 (local). |
| `FIRECRAWL_MAX_TTL` | Browser-session TTL ceiling. Defaults to 3600 and is capped there because the v2 API rejects larger values on both backends. |
| `FIRECRAWL_TIMEOUT` | HTTP timeout in seconds. Unset means the SDK default. |

Cloud and local scrape backends speak the same Firecrawl v2 protocol.

### Repository-managed local scrape stack

`deploy/firecrawl/compose.yaml` runs Firecrawl API, its scrape-only Playwright service, Redis, RabbitMQ, and NuQ PostgreSQL. The API and Playwright images are pinned by digest; the helper builds the NuQ image from pinned upstream source when it is missing. Only `127.0.0.1:3002` is published.

Use the PowerShell helper rather than invoking an ad-hoc Compose checkout:

```powershell
.\scripts\local_firecrawl.ps1 up
.\scripts\local_firecrawl.ps1 verify
.\scripts\local_firecrawl.ps1 status
.\scripts\local_firecrawl.ps1 logs
.\scripts\local_firecrawl.ps1 down
```

`verify` calls the real local `/v2/scrape` endpoint against `https://example.com`; it does not contact CUHK. Configure the Python client with `FIRECRAWL_MODE=local`, `FIRECRAWL_API_URL=http://127.0.0.1:3002`, and an empty `FIRECRAWL_API_KEY`.

## Browser backends

`browser_provider.resolve_settings()` independently selects browser sessions. When `BROWSER_BACKEND` is unset, it follows `FIRECRAWL_MODE`: cloud selects `firecrawl-cloud`, local selects `steel-local`.

| Variable | Meaning |
| --- | --- |
| `BROWSER_BACKEND` | `firecrawl-cloud` or `steel-local`; aliases `firecrawl`, `cloud`, `steel`, and `local` are accepted. |
| `FIRECRAWL_BROWSER_API_URL` | Optional Firecrawl Cloud browser URL when scrape calls use a different Firecrawl backend. |
| `STEEL_EXECUTOR_URL` | Local executor URL, default `http://127.0.0.1:3003`. |
| `STEEL_EXECUTOR_TOKEN` | Shared token for the executor, default `steel-local-only`; override it on shared machines. |
| `STEEL_EXECUTOR_TIMEOUT` | Executor HTTP timeout in seconds, default 130. |

Browser session IDs are backend-aware: Firecrawl Cloud uses `data/chat_session/session_id.txt`; Steel uses `data/chat_session/steel_session_id.txt`. Never copy an ID between backends. The aTrust session is bound to the browser's egress IP, so keep cookies paired with their original browser backend.

### Repository-managed local Steel stack

`deploy/steel/compose.yaml` runs the pinned Steel Browser API and a small Node executor. The executor uses Playwright over Steel's CDP WebSocket and preserves the existing JavaScript contract: a persistent `page` handle, top-level `await`, last-expression results, and persistent top-level `var` bindings. It executes arbitrary code by design, so its port is loopback-only and token-protected.

```powershell
.\scripts\local_steel.ps1 up
.\scripts\local_steel.ps1 verify
.\scripts\local_steel.ps1 status
.\scripts\local_steel.ps1 logs
.\scripts\local_steel.ps1 down
```

`verify` opens `https://example.com` in a temporary tab and closes it, without navigating an existing authenticated page. The named Docker volume preserves the Chromium profile across restarts.

### The self-hosted Firecrawl browser-service caveat

**The local Firecrawl stack intentionally does not serve the browser-session API.** Firecrawl's open-source API answers HTTP 503 `Browser feature is not configured (BROWSER_SERVICE_URL is missing).` whenever `BROWSER_SERVICE_URL` is unset, and its open-source `playwright-service` implements scrape requests rather than persistent sessions. Use `BROWSER_BACKEND=steel-local` for fully local browser workflows.

Consequences for local mode:

- `scrape_chat.py` works, because `/v2/scrape` is part of the stock stack.
- `login.py`, `send_message.py`, `probe.py`, and `server.py` use `browser_provider` and therefore work with local Steel.
- Directly calling `firecrawl_provider.open_browser_session()` against stock local Firecrawl still raises an actionable `ConfigurationError`.

Do not paper over this by falling back to the cloud automatically — a silent fallback would hide a misconfiguration and burn cloud quota.

### Local mode and the system HTTP proxy

`build_client()` calls `ensure_local_bypasses_proxy()` for local backends. The SDK issues bare `requests.post` calls, so it honours `HTTP_PROXY`/`HTTPS_PROXY` from the environment; on a machine running a system proxy (Clash, a corporate MITM) loopback traffic would be sent to the proxy and fail with `502 upstream connect failed`. The helper appends the backend host plus `127.0.0.1` and `localhost` to `NO_PROXY`/`no_proxy`, idempotently and without dropping existing entries. Cloud mode is untouched, since it needs the proxy.

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

`scripts/test_firecrawl_provider.py` is the offline smoke runner for backend selection. It makes no network calls: it asserts mode resolution and aliases, mode-specific defaults, every `ConfigurationError` path, that `describe()` never leaks the key, that local clients send no `Authorization` header, that the proxy bypass is idempotent and preserves existing `NO_PROXY` entries, and that the upstream 503 is classified while unrelated errors are not. Run it after touching `firecrawl_provider.py` or `cloud_browser.py`:

```powershell
.venv\Scripts\python.exe scripts\test_firecrawl_provider.py 2>&1
```

`scripts/test_browser_provider.py` is the offline smoke runner for Firecrawl Cloud vs local Steel selection, backend-specific session files, redaction, and Steel result/error adaptation:

```powershell
.venv\Scripts\python.exe scripts\test_browser_provider.py 2>&1
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

- `GET /health`: Firecrawl session ID, browser URL, authenticated user, and the resolved Firecrawl backend (`mode`, `api_url`, whether a key is set).
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
