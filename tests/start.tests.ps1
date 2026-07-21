$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $root 'start.ps1'
if (-not (Test-Path -LiteralPath $launcher)) {
    throw 'start.ps1 is missing'
}

$tokens = $null
$parseErrors = $null
[void][System.Management.Automation.Language.Parser]::ParseFile(
    $launcher,
    [ref]$tokens,
    [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) {
    throw ($parseErrors | Out-String)
}

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $launcher -NoBrowser
if ($LASTEXITCODE -ne 0) {
    throw "launcher exited with code $LASTEXITCODE"
}

$status = Invoke-RestMethod -Uri 'http://127.0.0.1:8300/status' -TimeoutSec 5
if ($status.env -ne 'testnet') {
    throw "expected testnet, got $($status.env)"
}

$frontend = Invoke-WebRequest -Uri 'http://127.0.0.1:5173/' -UseBasicParsing -TimeoutSec 5
if ($frontend.StatusCode -ne 200) {
    throw "frontend returned HTTP $($frontend.StatusCode)"
}

Write-Host 'Launcher integration test passed.' -ForegroundColor Green
