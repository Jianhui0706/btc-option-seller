[CmdletBinding()]
param(
    [switch]$NoBrowser
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectDisplayName = [Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String('QlRDIOacn+adg+WNluaWueezu+e7nw==')
)
$projectReadyMessage = [Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String('QlRDIOacn+adg+WNluaWueezu+e7n+W3suWwsee7quOAgg==')
)

$projectRoot = $PSScriptRoot
$backendDir = Join-Path $projectRoot 'web\backend'
$frontendDir = Join-Path $projectRoot 'web\frontend'
$runtimeDir = Join-Path $projectRoot '.runtime'
$statePath = Join-Path $runtimeDir 'launcher-state.json'
$backendUri = 'http://127.0.0.1:8300/status'
$frontendUri = 'http://127.0.0.1:5173/'
$startedProcesses = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()

function Write-Step {
    param([string]$Message)
    Write-Host "[$projectDisplayName] $Message" -ForegroundColor Cyan
}

function Get-ListenerPid {
    param([int]$Port)

    $connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $connection) {
        return $null
    }
    return [int]$connection.OwningProcess
}

function Get-ProcessCommandLine {
    param([int]$ProcessId)

    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return ''
    }
    return [string]$process.CommandLine
}

function Test-BackendProcess {
    param([int]$ProcessId)

    $commandLine = Get-ProcessCommandLine -ProcessId $ProcessId
    if ($commandLine -notmatch '(?i)(^|[\\/\s])server\.py([\s"'']|$)') {
        return $false
    }

    try {
        $status = Invoke-RestMethod -Uri $backendUri -TimeoutSec 2
        return ($status.env -eq 'testnet')
    }
    catch {
        return $false
    }
}

function Test-FrontendProcess {
    param([int]$ProcessId)

    $commandLine = Get-ProcessCommandLine -ProcessId $ProcessId
    return (
        $commandLine -match '(?i)vite' -and
        $commandLine.IndexOf($frontendDir, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    )
}

function Stop-ProjectListener {
    param(
        [int]$Port,
        [ValidateSet('backend', 'frontend')]
        [string]$Service
    )

    $ownerPid = Get-ListenerPid -Port $Port
    if ($null -eq $ownerPid) {
        return
    }

    $isProjectProcess = if ($Service -eq 'backend') {
        Test-BackendProcess -ProcessId $ownerPid
    }
    else {
        Test-FrontendProcess -ProcessId $ownerPid
    }

    if (-not $isProjectProcess) {
        $commandLine = Get-ProcessCommandLine -ProcessId $ownerPid
        throw "Port $Port is occupied by an unknown process (PID $ownerPid): $commandLine"
    }

    Write-Step "Stopping previous $Service process (PID $ownerPid)..."
    Stop-Process -Id $ownerPid -Force -ErrorAction Stop
}

function Wait-PortReleased {
    param(
        [int]$Port,
        [int]$TimeoutSeconds = 10
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        if ($null -eq (Get-ListenerPid -Port $Port)) {
            return
        }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)

    throw "Port $Port was not released within $TimeoutSeconds seconds."
}

function Wait-RestJson {
    param(
        [string]$Uri,
        [int]$TimeoutSeconds = 30
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        try {
            return Invoke-RestMethod -Uri $Uri -TimeoutSec 2
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    } while ((Get-Date) -lt $deadline)

    throw "Health check timed out: $Uri"
}

function Wait-WebSuccess {
    param(
        [string]$Uri,
        [int]$TimeoutSeconds = 30
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        try {
            $response = Invoke-WebRequest -Uri $Uri -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 400) {
                return
            }
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    } while ((Get-Date) -lt $deadline)

    throw "Health check timed out: $Uri"
}

function Stop-NewProcesses {
    foreach ($process in $startedProcesses) {
        if (-not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
    }

    foreach ($service in @(
        @{ Port = 8300; Name = 'backend' },
        @{ Port = 5173; Name = 'frontend' }
    )) {
        $ownerPid = Get-ListenerPid -Port $service.Port
        if ($null -eq $ownerPid) {
            continue
        }
        $owned = if ($service.Name -eq 'backend') {
            Test-BackendProcess -ProcessId $ownerPid
        }
        else {
            Test-FrontendProcess -ProcessId $ownerPid
        }
        if ($owned) {
            Stop-Process -Id $ownerPid -Force -ErrorAction SilentlyContinue
        }
    }
}

function Show-LogTail {
    param([string]$Path)

    if (Test-Path -LiteralPath $Path) {
        Write-Host "--- $Path ---" -ForegroundColor Yellow
        Get-Content -LiteralPath $Path -Tail 30 -ErrorAction SilentlyContinue
    }
}

$backendStdout = $null
$backendStderr = $null
$frontendStdout = $null
$frontendStderr = $null

try {
    if (-not (Test-Path -LiteralPath (Join-Path $backendDir 'server.py'))) {
        throw "Backend entry point not found: $backendDir\server.py"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $frontendDir 'package.json'))) {
        throw "Frontend package.json not found: $frontendDir\package.json"
    }

    $pythonExe = (& python -c 'import sys; print(sys.executable)' 2>$null | Select-Object -Last 1).Trim()
    if (-not $pythonExe -or -not (Test-Path -LiteralPath $pythonExe)) {
        throw 'Python executable could not be resolved.'
    }
    $npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if ($null -eq $npmCommand) {
        throw 'npm.cmd was not found in PATH.'
    }

    New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $backendDir 'logs') | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $frontendDir 'logs') | Out-Null

    Write-Step 'Stopping previous project services...'
    Stop-ProjectListener -Port 8300 -Service backend
    Stop-ProjectListener -Port 5173 -Service frontend
    Wait-PortReleased -Port 8300
    Wait-PortReleased -Port 5173

    $timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backendStdout = Join-Path $backendDir "logs\backend-$timestamp.stdout.log"
    $backendStderr = Join-Path $backendDir "logs\backend-$timestamp.stderr.log"
    $frontendStdout = Join-Path $frontendDir "logs\frontend-$timestamp.stdout.log"
    $frontendStderr = Join-Path $frontendDir "logs\frontend-$timestamp.stderr.log"

    Write-Step 'Starting backend with: python server.py'
    $backendProcess = Start-Process -FilePath $pythonExe -ArgumentList @('server.py') `
        -WorkingDirectory $backendDir -WindowStyle Hidden `
        -RedirectStandardOutput $backendStdout -RedirectStandardError $backendStderr -PassThru
    $startedProcesses.Add($backendProcess)

    Write-Step 'Starting frontend with: npm run dev'
    $frontendProcess = Start-Process -FilePath $npmCommand.Source `
        -ArgumentList @('run', 'dev', '--', '--host', '127.0.0.1', '--port', '5173', '--strictPort') `
        -WorkingDirectory $frontendDir -WindowStyle Hidden `
        -RedirectStandardOutput $frontendStdout -RedirectStandardError $frontendStderr -PassThru
    $startedProcesses.Add($frontendProcess)

    Write-Step 'Checking backend /status...'
    $status = Wait-RestJson -Uri $backendUri -TimeoutSeconds 40
    if ($status.env -ne 'testnet') {
        throw "Backend environment is '$($status.env)', expected 'testnet'."
    }

    Write-Step 'Checking frontend...'
    Wait-WebSuccess -Uri $frontendUri -TimeoutSeconds 40

    $backendPid = Get-ListenerPid -Port 8300
    $frontendPid = Get-ListenerPid -Port 5173
    $state = [ordered]@{
        project_root = $projectRoot
        started_at = (Get-Date).ToUniversalTime().ToString('o')
        backend_pid = $backendPid
        frontend_pid = $frontendPid
        backend_log = $backendStderr
        frontend_log = $frontendStdout
    }
    $state | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8

    $activeTradeCount = @($status.active_trades).Count
    Write-Host ''
    Write-Host $projectReadyMessage -ForegroundColor Green
    Write-Host "  Backend : $backendUri (PID $backendPid)"
    Write-Host "  Frontend: $frontendUri (PID $frontendPid)"
    Write-Host "  Env     : $($status.env)"
    Write-Host "  Trades  : $activeTradeCount active"

    if (-not $NoBrowser) {
        Write-Step 'Opening browser...'
        Start-Process $frontendUri
    }
}
catch {
    Write-Host ''
    Write-Host "Startup failed: $($_.Exception.Message)" -ForegroundColor Red
    Stop-NewProcesses
    Show-LogTail -Path $backendStderr
    Show-LogTail -Path $backendStdout
    Show-LogTail -Path $frontendStderr
    Show-LogTail -Path $frontendStdout
    exit 1
}
