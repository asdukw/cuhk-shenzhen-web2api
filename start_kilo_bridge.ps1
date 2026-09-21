param(
    [Parameter(Mandatory=$true)][string]$AuthDirectory,
    [switch]$AcknowledgeUpstreamTools
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not $AcknowledgeUpstreamTools) {
    throw 'Campus built-in web tools cannot be reliably disabled. Use non-sensitive test data. Pass -AcknowledgeUpstreamTools after reviewing this risk.'
}
# User-started foreground service. Does not terminate/restart other processes.
& '.\.venv\Scripts\python.exe' '-m' 'cuhk_shenzhen_web2api.kilo_bridge' '--auth-dir' $AuthDirectory '--acknowledge-upstream-tools'
exit $LASTEXITCODE
