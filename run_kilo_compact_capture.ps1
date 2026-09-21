$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

# Read-only capture proxy for one real Kilo /compact request. It does NOT touch
# .env.v2, Kilo config, auth dirs or the running 8768 service; it only forwards
# to them and writes a desensitized structural report to a NEW output dir.
$probeId = [guid]::NewGuid().ToString('N')
$outputRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\outputs'))
$captureDirectory = Join-Path $outputRoot ('kilo-compact-capture-' + $probeId)

try {
    Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8768/health' -TimeoutSec 5 | Out-Null
} catch {
    throw 'Bridge on 127.0.0.1:8768 is not answering /health. Start it first; the proxy only forwards.'
}

Write-Host "Capture report directory: $captureDirectory"
Write-Host 'Steps: in Kilo set the custom provider Base URL to http://127.0.0.1:8769/v1,'
Write-Host '       run /compact once, watch this console, then Ctrl+C and restore the Base URL.'
Write-Host 'Content is never recorded (roles/byte-lengths/tool names only); Authorization is not logged.'

& '.\.venv\Scripts\python.exe' -m 'cuhk_shenzhen_web2api.kilo_bridge.capture_proxy' `
    '--listen' '127.0.0.1:8769' '--upstream' '127.0.0.1:8768' `
    '--output-dir' $captureDirectory

Write-Host "Done. Shape log: $captureDirectory\requests.jsonl  summary: $captureDirectory\summary.json"
