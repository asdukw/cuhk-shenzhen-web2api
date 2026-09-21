[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("up", "down", "restart", "status", "verify", "logs")]
    [string]$Action = "status",

    [ValidateRange(5, 300)]
    [int]$TimeoutSeconds = 90
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$deployRoot = Join-Path $projectRoot "deploy\firecrawl"
$composeFile = Join-Path $deployRoot "compose.yaml"
$localEnvFile = Join-Path $deployRoot ".env"
$composeArguments = @("compose", "--file", $composeFile)
$nuqImage = "firecrawl-nuq-postgres:latest"
$nuqBuildContext = "https://github.com/firecrawl/firecrawl.git#95c8ab18f524d1aa813cca2a6dc8bd39191504ec:apps/nuq-postgres"

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

function Get-FirecrawlPort {
    if ($env:FIRECRAWL_PORT) {
        return [int]$env:FIRECRAWL_PORT
    }
    if (Test-Path -LiteralPath $localEnvFile) {
        foreach ($line in Get-Content -LiteralPath $localEnvFile) {
            if ($line -match '^\s*FIRECRAWL_PORT\s*=\s*(\d+)\s*$') {
                return [int]$Matches[1]
            }
        }
    }
    return 3002
}

function Wait-Firecrawl {
    $port = Get-FirecrawlPort
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $uri = "http://127.0.0.1:$port/"

    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri $uri -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -eq 200) {
                return
            }
        }
        catch {
            Start-Sleep -Seconds 1
        }
    }
    throw "local Firecrawl API did not become ready at $uri"
}

function Ensure-NuqImage {
    & docker image inspect $nuqImage *> $null
    if ($LASTEXITCODE -eq 0) {
        return
    }

    Write-Host "Building $nuqImage from pinned Firecrawl source..."
    & docker build --tag $nuqImage $nuqBuildContext
    if ($LASTEXITCODE -ne 0) {
        throw "failed to build $nuqImage"
    }
}

function Test-Firecrawl {
    $port = Get-FirecrawlPort
    $baseUrl = "http://127.0.0.1:$port"
    $root = Invoke-RestMethod -Uri "$baseUrl/" -Method Get -TimeoutSec 10
    $payload = @{url = "https://example.com"; formats = @("markdown")} |
        ConvertTo-Json -Compress
    $scrape = Invoke-RestMethod -Uri "$baseUrl/v2/scrape" -Method Post `
        -ContentType "application/json" -Body $payload -TimeoutSec 90

    $markdown = $scrape.data.markdown
    if (-not $scrape.success -or [string]::IsNullOrWhiteSpace($markdown)) {
        throw "local Firecrawl scrape verification failed"
    }

    [pscustomobject]@{
        ApiUrl = $baseUrl
        ApiMessage = $root.message
        ScrapeSuccess = $scrape.success
        MarkdownLength = $markdown.Length
        Title = $scrape.data.metadata.title
        BrowserBackend = "not implemented"
    }
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "docker was not found in PATH"
}

switch ($Action) {
    "up" {
        Ensure-NuqImage
        Invoke-Compose -Arguments @("up", "-d", "--remove-orphans")
        Wait-Firecrawl
        Test-Firecrawl
    }
    "down" {
        Invoke-Compose -Arguments @("down")
    }
    "restart" {
        Invoke-Compose -Arguments @("down")
        Ensure-NuqImage
        Invoke-Compose -Arguments @("up", "-d", "--remove-orphans")
        Wait-Firecrawl
        Test-Firecrawl
    }
    "status" {
        Invoke-Compose -Arguments @("ps")
    }
    "verify" {
        Wait-Firecrawl
        Test-Firecrawl
    }
    "logs" {
        Invoke-Compose -Arguments @(
            "logs", "--tail", "200", "api", "playwright-service"
        )
    }
}
