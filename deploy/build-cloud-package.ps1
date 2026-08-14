[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $projectRoot 'web\backend'
$frontendDir = Join-Path $projectRoot 'web\frontend'
$envPath = Join-Path $backendDir '.env'
$stagingRoot = Join-Path $projectRoot '.cloud-package'
$packageName = 'btc-option-seller-cloud'
$packageRoot = Join-Path $stagingRoot $packageName
$outputDir = Join-Path $projectRoot 'dist'
$archivePath = Join-Path $outputDir "$packageName-43-160-202-151.tar.gz"

function Get-EnvValue {
    param([string]$Name)
    $line = Get-Content -LiteralPath $envPath -Encoding UTF8 |
        Where-Object { $_ -like "$Name=*" } |
        Select-Object -First 1
    if (-not $line) { return '' }
    return $line.Substring($Name.Length + 1).Trim()
}

if (-not (Test-Path -LiteralPath $envPath)) {
    throw "Missing local secret file: $envPath"
}
if ((Get-EnvValue 'DERIBIT_ENV') -ne 'testnet') {
    throw 'Cloud package is locked to DERIBIT_ENV=testnet.'
}
foreach ($key in @('DERIBIT_TESTNET_CLIENT_ID', 'DERIBIT_TESTNET_CLIENT_SECRET')) {
    if ([string]::IsNullOrWhiteSpace((Get-EnvValue $key))) {
        throw "$key is not configured."
    }
}
$webhookPassphrase = Get-EnvValue 'WEBHOOK_PASSPHRASE'
$legacyWebhookSecret = Get-EnvValue 'WEBHOOK_SECRET'
if ([string]::IsNullOrWhiteSpace($webhookPassphrase) -and
    [string]::IsNullOrWhiteSpace($legacyWebhookSecret)) {
    throw 'WEBHOOK_PASSPHRASE or WEBHOOK_SECRET must be configured.'
}

& npm.cmd run build --prefix $frontendDir
if ($LASTEXITCODE -ne 0) {
    throw "Frontend build failed with exit code $LASTEXITCODE."
}

$resolvedStaging = [IO.Path]::GetFullPath($stagingRoot)
$resolvedProject = [IO.Path]::GetFullPath($projectRoot)
if (-not $resolvedStaging.StartsWith($resolvedProject, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Refusing to clean a staging path outside the project.'
}
if (Test-Path -LiteralPath $stagingRoot) {
    Remove-Item -LiteralPath $stagingRoot -Recurse -Force
}

New-Item -ItemType Directory -Force -Path (Join-Path $packageRoot 'web\backend') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $packageRoot 'deploy\cloud') | Out-Null
New-Item -ItemType Directory -Force -Path $outputDir | Out-Null

foreach ($directory in @('config', 'scripts', 'src', 'static')) {
    Copy-Item -LiteralPath (Join-Path $backendDir $directory) -Destination (Join-Path $packageRoot 'web\backend') -Recurse
}
foreach ($file in @('server.py', 'requirements.txt')) {
    Copy-Item -LiteralPath (Join-Path $backendDir $file) -Destination (Join-Path $packageRoot 'web\backend')
}
$envText = [IO.File]::ReadAllText($envPath, [Text.Encoding]::UTF8)
$normalizedEnv = $envText.Replace("`r`n", "`n").Replace("`r", "`n")
$utf8NoBom = [Text.UTF8Encoding]::new($false)
[IO.File]::WriteAllText((Join-Path $packageRoot 'web\backend\.env'), $normalizedEnv, $utf8NoBom)
Get-ChildItem -LiteralPath (Join-Path $projectRoot 'deploy\cloud') | Copy-Item -Destination (Join-Path $packageRoot 'deploy\cloud') -Recurse

Get-ChildItem -LiteralPath $packageRoot -Directory -Recurse -Force |
    Where-Object { $_.Name -eq '__pycache__' } |
    Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $packageRoot -File -Recurse -Force |
    Where-Object { $_.Extension -in @('.pyc', '.pyo') } |
    Remove-Item -Force

if (Test-Path -LiteralPath $archivePath) {
    Remove-Item -LiteralPath $archivePath -Force
}
& tar.exe -czf $archivePath -C $stagingRoot $packageName
if ($LASTEXITCODE -ne 0) {
    throw "Archive creation failed with exit code $LASTEXITCODE."
}
Remove-Item -LiteralPath $stagingRoot -Recurse -Force

Write-Host "Cloud package ready: $archivePath" -ForegroundColor Green
