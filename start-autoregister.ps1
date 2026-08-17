param(
    [switch] $SkipMailCom
)

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$App = Join-Path $Root 'app'
$Python = Join-Path $Root 'register_env\Scripts\python.exe'
$settingsPath = Join-Path $Root 'data\settings.json'
$RuntimeLogs = Join-Path $Root 'data\runtime'
$MailComStart = Join-Path $Root 'mailcom-manager\start.ps1'
New-Item -ItemType Directory -Force -Path $RuntimeLogs | Out-Null
$Roxy = $env:ROXY_BROWSER_PATH
if (-not $Roxy -and (Test-Path -LiteralPath $settingsPath)) {
    try {
        $Roxy = (Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json).browserExecutablePath
    } catch {
        Write-Host '  settings.json could not be read; RoxyBrowser will not be auto-started.' -ForegroundColor Yellow
    }
}

function Test-LocalPort([int] $Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync('127.0.0.1', $Port)
        if (-not $task.Wait(1000)) { return $false }
        return $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Start-Component([string] $Name, [scriptblock] $Action) {
    Write-Host "[$Name] checking..." -ForegroundColor Cyan
    & $Action
}

if (-not (Test-Path $Python)) {
    throw "Python environment not found. Run app\setup.ps1 first: $Python"
}

Start-Component 'RoxyBrowser' {
    if (Get-Process -Name 'RoxyBrowser' -ErrorAction SilentlyContinue) {
        Write-Host '  already running' -ForegroundColor Green
    } elseif ($Roxy -and (Test-Path -LiteralPath $Roxy)) {
        Start-Process -FilePath $Roxy -WindowStyle Normal
        Write-Host '  started; API may need a few seconds to become ready' -ForegroundColor Green
    } else {
        Write-Host '  path is not configured; start RoxyBrowser manually or set ROXY_BROWSER_PATH' -ForegroundColor Yellow
    }
}

Start-Component 'MongoDB' {
    if (Test-LocalPort 27017) {
        Write-Host '  already running' -ForegroundColor Green
    } else {
        & (Join-Path $App 'scripts\start-mongodb.ps1')
        if ($LASTEXITCODE -ne 0) { throw "MongoDB startup failed with exit code $LASTEXITCODE" }
    }
}

Start-Component 'Backend' {
    if (Test-LocalPort 8000) {
        Write-Host '  already running' -ForegroundColor Green
    } else {
        $previousLogEnqueue = $env:OPLL_LOG_ENQUEUE
        $env:OPLL_LOG_ENQUEUE = 'false'
        try {
            Start-Process -FilePath $Python -ArgumentList '-m', 'backend' -WorkingDirectory $App -WindowStyle Hidden `
                -RedirectStandardOutput (Join-Path $RuntimeLogs 'autoregister-backend.out.log') `
                -RedirectStandardError (Join-Path $RuntimeLogs 'autoregister-backend.err.log')
        } finally {
            if ($null -eq $previousLogEnqueue) {
                Remove-Item Env:\OPLL_LOG_ENQUEUE -ErrorAction SilentlyContinue
            } else {
                $env:OPLL_LOG_ENQUEUE = $previousLogEnqueue
            }
        }
        Write-Host '  started' -ForegroundColor Green
    }
}

Start-Component 'Frontend' {
    if (Test-LocalPort 5173) {
        Write-Host '  already running' -ForegroundColor Green
    } else {
        Start-Process -FilePath 'npm.cmd' `
            -ArgumentList 'run', 'dev', '--', '--configLoader', 'native', '--host', '127.0.0.1' `
            -WorkingDirectory $App -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $RuntimeLogs 'autoregister-frontend.out.log') `
            -RedirectStandardError (Join-Path $RuntimeLogs 'autoregister-frontend.err.log')
        Write-Host '  started' -ForegroundColor Green
    }
}

if (-not $SkipMailCom) {
    Start-Component 'MailCom' {
        if (Test-LocalPort 3211) {
            Write-Host '  already running' -ForegroundColor Green
        } elseif (Test-Path -LiteralPath $MailComStart) {
            & $MailComStart -NoBrowser
        } else {
            Write-Host '  manager is not installed; non-MailCom mailboxes remain available' -ForegroundColor Yellow
        }
    }
}

Start-Sleep -Seconds 3
Write-Host ''
Write-Host 'AutoRegister started:' -ForegroundColor Green
Write-Host '  Page: http://127.0.0.1:5173/launch'
Write-Host '  Backend: http://127.0.0.1:8000'
Write-Host '  Roxy API: http://127.0.0.1:50000 (enable it in Roxy first)'
Write-Host '  Proxy bridge: starts on demand at http://127.0.0.1:18796'
if (-not $SkipMailCom) {
    Write-Host '  MailCom: http://127.0.0.1:3211'
}

try {
    $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/health' -TimeoutSec 5
    Write-Host "  Backend status: $($health.status)" -ForegroundColor Green
} catch {
    Write-Host '  Backend is still starting; refresh in a few seconds.' -ForegroundColor Yellow
}

try {
    $null = Invoke-WebRequest -Uri 'http://127.0.0.1:5173/launch' -UseBasicParsing -TimeoutSec 5
    Write-Host '  Frontend status: ready' -ForegroundColor Green
} catch {
    Write-Host "  Frontend is still starting; check $RuntimeLogs\autoregister-frontend.err.log" -ForegroundColor Yellow
}

if (-not $SkipMailCom) {
    try {
        $mailHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:3211/api/health' -TimeoutSec 5
        Write-Host "  MailCom status: $($mailHealth.status)" -ForegroundColor Green
    } catch {
        Write-Host '  MailCom is still starting; registration can use other mailbox sources.' -ForegroundColor Yellow
    }
}
