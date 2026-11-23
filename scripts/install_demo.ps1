[CmdletBinding()]
param(
    [ValidatePattern('^[a-z0-9][a-z0-9_-]*$')]
    [string]$ProjectName = "anzma-demo",
    [string]$EnvironmentFile = ".env",
    [ValidateRange(1, 65535)]
    [int]$ReadyTimeoutSeconds = 600,
    [ValidateRange(1, 65535)]
    [int]$ModelTimeoutSeconds = 1800
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$envPath = if ([System.IO.Path]::IsPathRooted($EnvironmentFile)) {
    [System.IO.Path]::GetFullPath($EnvironmentFile)
} else {
    [System.IO.Path]::GetFullPath((Join-Path $repoRoot $EnvironmentFile))
}
$envDirectory = Split-Path -Parent $envPath
$envExample = Join-Path $repoRoot "deploy\examples\.env.example"
$composeFile = Join-Path $repoRoot "deploy\compose\docker-compose.yml"
$apiPort = 8000

function New-RandomHexSecret {
    param([int]$ByteCount = 32)

    $bytes = New-Object byte[] $ByteCount
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    }
    finally {
        $rng.Dispose()
    }
    return (($bytes | ForEach-Object { $_.ToString("x2") }) -join "")
}

function Read-EnvValues {
    param([string[]]$Lines)

    $values = @{}
    foreach ($line in $Lines) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            $values[$Matches[1]] = $Matches[2]
        }
    }
    return $values
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker CLI was not found. Install Docker Desktop, open it, then rerun this script."
}

& docker info *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Docker is not running or the current user cannot access it. Start Docker Desktop and rerun."
}

if (-not (Test-Path -LiteralPath $envPath)) {
    if (-not (Test-Path -LiteralPath $envExample)) {
        throw "Environment template is missing: $envExample"
    }
    if (-not (Test-Path -LiteralPath $envDirectory)) {
        New-Item -ItemType Directory -Path $envDirectory -Force | Out-Null
    }
    Copy-Item -LiteralPath $envExample -Destination $envPath
    Write-Host "Created .env from the example configuration."
}

$lines = @(Get-Content -LiteralPath $envPath)
$envValues = Read-EnvValues -Lines $lines
if (-not $envValues.ContainsKey("PGPASSWORD") -or [string]::IsNullOrWhiteSpace($envValues["PGPASSWORD"])) {
    $updated = $false
    for ($index = 0; $index -lt $lines.Count; $index++) {
        if ($lines[$index] -match '^PGPASSWORD=') {
            $lines[$index] = "PGPASSWORD=$(New-RandomHexSecret)"
            $updated = $true
            break
        }
    }
    if (-not $updated) { $lines += "PGPASSWORD=$(New-RandomHexSecret)" }
}
if (-not $envValues.ContainsKey("ANZMA_API_TOKEN") -or [string]::IsNullOrWhiteSpace($envValues["ANZMA_API_TOKEN"])) {
    $updated = $false
    for ($index = 0; $index -lt $lines.Count; $index++) {
        if ($lines[$index] -match '^ANZMA_API_TOKEN=') {
            $lines[$index] = "ANZMA_API_TOKEN=$(New-RandomHexSecret)"
            $updated = $true
            break
        }
    }
    if (-not $updated) { $lines += "ANZMA_API_TOKEN=$(New-RandomHexSecret)" }
}

$envValues = Read-EnvValues -Lines $lines
if ($envValues["ANZMA_AUTH_DISABLED"] -match '^(?i:true|1|yes)$') {
    throw "ANZMA_AUTH_DISABLED is enabled in .env. The demo installer requires dashboard authentication."
}
if ($envValues["API_HOST_PORT"] -match '^\d+$') {
    $apiPort = [int]$envValues["API_HOST_PORT"]
}

$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
$envText = ($lines -join [Environment]::NewLine) + [Environment]::NewLine
[System.IO.File]::WriteAllText($envPath, $envText, $utf8NoBom)

$composeArgs = @(
    "compose", "--env-file", $envPath, "-p", $ProjectName,
    "--profile", "freshness", "-f", $composeFile
)
Write-Host "Building and starting Anzma demo services ($ProjectName)..."
& docker @composeArgs up -d --build
if ($LASTEXITCODE -ne 0) {
    throw "Docker Compose failed to build or start the demo. Review the Compose output above."
}

$readyUrl = "http://127.0.0.1:$apiPort/ready"
$readyDeadline = (Get-Date).AddSeconds($ReadyTimeoutSeconds)
$ready = $false
Write-Host "Waiting for the API and database readiness check..."
while ((Get-Date) -lt $readyDeadline) {
    try {
        $response = Invoke-RestMethod -Uri $readyUrl -TimeoutSec 5
        if ($response.status -eq "ready" -and $response.database -eq "up") {
            $ready = $true
            break
        }
    }
    catch {
        Start-Sleep -Seconds 5
    }
}
if (-not $ready) {
    throw "The API did not become ready at $readyUrl within $ReadyTimeoutSeconds seconds. Inspect logs with the same Compose project name."
}

$modelDeadline = (Get-Date).AddSeconds($ModelTimeoutSeconds)
$modelReady = $false
Write-Host "Waiting for the configured Ollama model to finish provisioning..."
while ((Get-Date) -lt $modelDeadline) {
    $containerId = (& docker @composeArgs ps -a -q ollama-init 2>$null | Select-Object -First 1)
    if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($containerId)) {
        $state = (& docker inspect --format '{{.State.Status}}|{{.State.ExitCode}}' $containerId 2>$null | Select-Object -First 1)
        if ($LASTEXITCODE -eq 0 -and $state -match '^exited\|0$') {
            $modelReady = $true
            break
        }
        if ($state -match '^exited\|') {
            & docker @composeArgs logs --no-color ollama-init
            throw "Ollama model provisioning failed. Review the logs above and the OLLAMA_MODEL setting in .env."
        }
    }
    Start-Sleep -Seconds 5
}
if (-not $modelReady) {
    throw "The configured Ollama model did not finish provisioning within $ModelTimeoutSeconds seconds. Inspect ollama-init logs; existing volumes were preserved."
}

Write-Host ""
Write-Host "Anzma demo is ready at http://localhost:$apiPort"
Write-Host "API readiness: $readyUrl"
Write-Host "Sign in with the ANZMA_API_TOKEN stored in the local .env file. Do not share or commit that file."
Write-Host "Demo data is synthetic; no live building source is connected by this installer."
Write-Host "Compose project: $ProjectName (use this same name for later management commands)."
