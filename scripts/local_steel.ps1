[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("setup", "up", "down", "restart", "status", "verify", "logs")]
    [string]$Action = "status"
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python venv not found at $python; run uv sync first"
}

& $python -m cuhk_shenzhen_web2api.scripts.steel_node $Action
if ($LASTEXITCODE -ne 0) {
    throw "steel-node $Action failed with exit code $LASTEXITCODE"
}
