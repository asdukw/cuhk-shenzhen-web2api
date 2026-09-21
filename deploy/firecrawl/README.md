# Local Firecrawl scrape API

This Compose stack runs the open-source Firecrawl scrape API entirely on the
local machine. It is adapted from upstream revision
`95c8ab18f524d1aa813cca2a6dc8bd39191504ec`. The API and Playwright images are
pinned by digest. The helper builds the small NuQ PostgreSQL image directly
from that pinned upstream revision when it is not already present locally.
The Playwright container runs its prebuilt JavaScript directly, avoiding a
Corepack/pnpm download during normal container startup.

Only `127.0.0.1:3002` is published. Authentication is disabled because the API
is loopback-only. Do not change the port binding to `0.0.0.0` without adding
authentication and transport security.

NuQ PostgreSQL data is stored in a named Docker volume, so ordinary `down` and
`up` operations preserve it. `docker compose down -v` is the explicit reset.

The stack supports `/v2/scrape`; it deliberately does not configure
`BROWSER_SERVICE_URL`. Firecrawl's standalone `/v2/browser` API is therefore
unavailable until this project gains its own local browser backend.

Use the repository helper from PowerShell:

```powershell
.\scripts\local_firecrawl.ps1 up
.\scripts\local_firecrawl.ps1 verify
.\scripts\local_firecrawl.ps1 status
.\scripts\local_firecrawl.ps1 logs
.\scripts\local_firecrawl.ps1 down
```

To override defaults, copy `.env.example` to `.env` in this directory. The
local `.env` file is ignored by the repository's global `*.env` rule.

Configure the Python client with:

```env
FIRECRAWL_MODE=local
FIRECRAWL_API_URL=http://127.0.0.1:3002
FIRECRAWL_API_KEY=
```

This configuration currently enables local scraping only. Login, chat, and
probe workflows still require a browser-session backend.
