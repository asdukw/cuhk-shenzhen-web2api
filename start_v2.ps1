$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PSScriptRoot '.playwright-browsers'
& '.\.venv\Scripts\python.exe' '-m' 'cuhk_shenzhen_web2api.v2'
exit $LASTEXITCODE
