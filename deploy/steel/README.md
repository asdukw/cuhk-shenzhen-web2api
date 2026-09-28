# Local Steel browser backend

This stack runs Steel Browser and the repository-owned executor directly with
Node.js. Docker is not required.

## Requirements

- Node.js 22 or newer
- npm
- Google Chrome, Chromium, or Edge with a compatible executable path
- Python 3.12 and `uv` for the gateway

The Steel source is cloned into `.steel/steel-browser` by default and pinned to
a known commit. The checkout and its `node_modules` are gitignored.

## Setup

From the repository root:

```powershell
.\scripts\local_steel.ps1 setup
```

If dependencies download slowly, set the local proxy before running setup:

```powershell
$env:HTTP_PROXY="http://127.0.0.1:7897"
$env:HTTPS_PROXY="http://127.0.0.1:7897"
$env:NO_PROXY="localhost,127.0.0.1,::1"
.\scripts\local_steel.ps1 setup
```

## Run

```powershell
.\scripts\local_steel.ps1 up
.\scripts\local_steel.ps1 status
.\scripts\local_steel.ps1 verify
.\scripts\local_steel.ps1 logs
.\scripts\local_steel.ps1 down
```

`up` starts these loopback-only services:

- Steel API and session viewer: `http://127.0.0.1:3000`
- Steel CDP: `ws://127.0.0.1:9223`
- authenticated executor: `http://127.0.0.1:3003`

The gateway can start/stop the same Node processes automatically when you run
`uv run gateway`. It only stops processes that it started itself.

## Configuration

```dotenv
BROWSER_BACKEND=steel-local
STEEL_EXECUTOR_URL=http://127.0.0.1:3003
STEEL_EXECUTOR_TOKEN=steel-local-only
# STEEL_SOURCE_DIR=.steel/steel-browser
# STEEL_REVISION=your-pinned-commit
# STEEL_API_PORT=3000
# STEEL_CDP_PORT=9223
# STEEL_HEADLESS=true
# CHROME_EXECUTABLE_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe
# CHROME_USER_DATA_DIR=data/steel/chrome-profile
```

The executor accepts arbitrary Node JavaScript and is protected by a bearer
token. Keep it bound to loopback and do not expose it to a shared network.
