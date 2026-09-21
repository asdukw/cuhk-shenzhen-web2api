$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$listeners = @(Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue)
foreach ($listener in $listeners) {
    $process = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
    if ($process) {
        Stop-Process -Id $process.Id -Force
        $process.WaitForExit(5000)
    }
}

& $PSScriptRoot\start_server.ps1
