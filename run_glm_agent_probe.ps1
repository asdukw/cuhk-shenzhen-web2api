$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$probeId = [guid]::NewGuid().ToString('N')
$newAuthDirectory = Join-Path $PSScriptRoot ('.web2api-v2\auth-agent-' + $probeId)
$outputRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\outputs'))
$newReportDirectory = Join-Path $outputRoot ('glm-agent-probe-' + $probeId)
$previousBrowserPath = $env:PLAYWRIGHT_BROWSERS_PATH
$previousAuthDirectory = $env:WEB2API_V2_AUTH_DIR
try {
    # Process-only variables: never modify .env.v2, Kilo or system settings.
    $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PSScriptRoot '.playwright-browsers'
    & '.\.venv\Scripts\python.exe' '-m' 'cuhk_shenzhen_web2api.v2.diagnose' '--backend' 'playwright' '--data-dir' $newAuthDirectory
    if ($LASTEXITCODE -ne 0) { throw 'Manual login check did not pass; no GLM requests sent.' }
    $env:WEB2API_V2_AUTH_DIR = $newAuthDirectory
    & '.\.venv\Scripts\python.exe' '-m' 'cuhk_shenzhen_web2api.v2.agent_probe' '--allow-service-offline' '--output-dir' $newReportDirectory
    Write-Host "Probe report: $newReportDirectory"
    Write-Host 'See report.json for the outcome; process completion does not imply protocol success.'
} finally {
    $env:PLAYWRIGHT_BROWSERS_PATH = $previousBrowserPath
    $env:WEB2API_V2_AUTH_DIR = $previousAuthDirectory
}
