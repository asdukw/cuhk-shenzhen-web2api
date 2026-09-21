param(
    [Parameter(Mandatory=$true)][string]$Model,
    [ValidateSet(1,2,4)][int]$Concurrency = 1,
    [ValidateSet(20,10,5)][int]$Interval = 20,
    [ValidateRange(1,12)][int]$Count = 12,
    [string]$Previous = ''
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
# User-run only. Does not start/restart services or change configuration.
$reportDirectory = Join-Path '.web2api-v2' ('benchmark-' + [guid]::NewGuid().ToString('N'))
$benchmarkArgs = @('-m', 'cuhk_shenzhen_web2api.v2.benchmark', '--model', $Model,
    '--stage', "$Concurrency", '--interval', "$Interval", '--count', "$Count",
    '--output-dir', $reportDirectory)
if ($Previous) { $benchmarkArgs += @('--previous', $Previous) }
& '.\.venv\Scripts\python.exe' @benchmarkArgs
$resultCode = $LASTEXITCODE
Write-Host "Report directory: $reportDirectory"
exit $resultCode
