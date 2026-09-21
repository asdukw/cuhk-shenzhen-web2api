# Local Steel browser backend

This stack runs [Steel Browser](https://github.com/steel-dev/steel-browser)
and a small repository-owned executor that preserves the JavaScript contract
used by `CloudBrowser`: a persistent `page` handle, top-level `await`, the last
expression as the result, and persistent top-level `var` bindings.

The services bind only to loopback:

- Steel API and session viewer: `http://127.0.0.1:3000`
- Steel CDP proxy: `ws://127.0.0.1:9223`
- authenticated executor: `http://127.0.0.1:3003`

Start and verify the backend from PowerShell:

```powershell
.\scripts\local_steel.ps1 up
```

The verification opens `https://example.com` in a temporary tab and closes the
tab afterward, so it does not navigate an existing authenticated chat page.

Use it together with the local Firecrawl scrape stack:

```dotenv
FIRECRAWL_MODE=local
BROWSER_BACKEND=steel-local
STEEL_EXECUTOR_URL=http://127.0.0.1:3003
STEEL_EXECUTOR_TOKEN=steel-local-only
```

For a shared or less trusted machine, override `STEEL_EXECUTOR_TOKEN` in both
the project `.env` and `deploy/steel/.env`. The executor intentionally accepts
arbitrary Node code, so it must not be exposed beyond loopback.

Other lifecycle commands:

```powershell
.\scripts\local_steel.ps1 verify
.\scripts\local_steel.ps1 status
.\scripts\local_steel.ps1 logs
.\scripts\local_steel.ps1 down
```

The named `steel-profile` volume preserves the Chromium profile across
container restarts. `data/chat_session/steel_session_id.txt` identifies the
currently active Steel session and is separate from the Firecrawl Cloud ID.

`STEEL_IMAGE` can override the pinned official image when validating a locally
built copy of the same Steel release. If unset, Compose uses the official image
by immutable digest.
