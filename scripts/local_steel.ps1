[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("up", "down", "restart", "status", "verify", "logs")]
    [string]$Action = "status",

    [ValidateRange(5, 300)]
    [int]$TimeoutSeconds = 120
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$deployRoot = Join-Path $projectRoot "deploy\steel"
$composeFile = Join-Path $deployRoot "compose.yaml"
$localEnvFile = Join-Path $deployRoot ".env"
$composeArguments = @("compose", "--file", $composeFile)

if (Test-Path -LiteralPath $localEnvFile) {
    $composeArguments += @("--env-file", $localEnvFile)
}

function Invoke-Compose {
    param([string[]]$Arguments)

    & docker @composeArguments @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose failed with exit code $LASTEXITCODE"
    }
}

function Get-Setting {
    param([string]$Name, [string]$Default)

    $processValue = [Environment]::GetEnvironmentVariable($Name)
    if ($processValue) {
        return $processValue
    }
    if (Test-Path -LiteralPath $localEnvFile) {
        foreach ($line in Get-Content -LiteralPath $localEnvFile) {
            if ($line -match "^\s*$Name\s*=\s*(.*?)\s*$") {
                return $Matches[1].Trim('"').Trim("'")
            }
        }
    }
    return $Default
}

function Wait-Steel {
    $port = Get-Setting "STEEL_EXECUTOR_PORT" "3003"
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $uri = "http://127.0.0.1:$port/health"

    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $response = Invoke-RestMethod -Uri $uri -Method Get -TimeoutSec 3
            if ($response.status -eq "ok") {
                return
            }
        }
        catch {
            Start-Sleep -Seconds 1
        }
    }
    throw "local Steel executor did not become ready at $uri"
}

function Invoke-Executor {
    param([string]$Path, [hashtable]$Body)

    $port = Get-Setting "STEEL_EXECUTOR_PORT" "3003"
    $token = Get-Setting "STEEL_EXECUTOR_TOKEN" "steel-local-only"
    $headers = @{Authorization = "Bearer $token"}
    $json = $Body | ConvertTo-Json -Compress
    return Invoke-RestMethod -Uri "http://127.0.0.1:$port$Path" -Method Post `
        -Headers $headers -ContentType "application/json" -Body $json `
        -TimeoutSec $TimeoutSeconds
}

function Test-Steel {
    $opened = Invoke-Executor "/session/open" @{}
    $code = @'
var __steel_verify_page = await context.newPage();
await __steel_verify_page.goto("https://example.com", {waitUntil: "domcontentloaded"});
var __steel_verify_result = JSON.stringify({url: await __steel_verify_page.url(), title: await __steel_verify_page.title()});
await __steel_verify_page.close();
__steel_verify_result
'@
    $executed = Invoke-Executor "/execute" @{
        sessionId = $opened.sessionId
        code = $code
        timeout = 60
    }
    if ($executed.error) {
        throw "Steel browser verification failed: $($executed.error)"
    }
    $page = $executed.result | ConvertFrom-Json
    if ($page.url -ne "https://example.com/" -or $page.title -ne "Example Domain") {
        throw "Steel browser returned an unexpected page result"
    }
    [pscustomobject]@{
        ExecutorUrl = "http://127.0.0.1:$(Get-Setting 'STEEL_EXECUTOR_PORT' '3003')"
        SessionId = $opened.sessionId
        Url = $page.url
        Title = $page.title
    }
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "docker was not found in PATH"
}

switch ($Action) {
    "up" {
        Invoke-Compose -Arguments @("up", "-d", "--build", "--remove-orphans")
        Wait-Steel
        Test-Steel
    }
    "down" {
        Invoke-Compose -Arguments @("down")
    }
    "restart" {
        Invoke-Compose -Arguments @("down")
        Invoke-Compose -Arguments @("up", "-d", "--build", "--remove-orphans")
        Wait-Steel
        Test-Steel
    }
    "status" {
        Invoke-Compose -Arguments @("ps")
    }
    "verify" {
        Wait-Steel
        Test-Steel
    }
    "logs" {
        Invoke-Compose -Arguments @("logs", "--tail", "200", "steel", "executor")
    }
}
