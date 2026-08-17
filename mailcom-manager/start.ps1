param(
    [int]$Port = 3211,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$workspaceRoot = Split-Path -Parent $root
$python = Join-Path $workspaceRoot "register_env\Scripts\python.exe"
$healthUrl = "http://127.0.0.1:$Port/api/health"
$homeUrl = "http://127.0.0.1:$Port/"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python environment not found. Run app\setup.ps1 first: $python"
}

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $listener) {
    $localProxy = Get-NetTCPConnection -LocalPort 7897 -State Listen -ErrorAction SilentlyContinue
    if ($localProxy -and -not $env:MAILCOM_IMAP_PROXY) {
        $env:MAILCOM_IMAP_PROXY = "socks5://127.0.0.1:7897"
    }
    $stdout = Join-Path $root "data\server.stdout.log"
    $stderr = Join-Path $root "data\server.stderr.log"
    $process = Start-Process `
        -FilePath $python `
        -ArgumentList "-m", "uvicorn", "manager.app:app", "--host", "127.0.0.1", "--port", "$Port" `
        -WorkingDirectory $root `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -PassThru
    $process.Id | Set-Content -LiteralPath (Join-Path $root "data\server.pid") -Encoding ascii
}

$ready = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Milliseconds 500
    try {
        $response = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
        if ($response.status -eq "ok") {
            $ready = $true
            break
        }
    } catch {}
}

if (-not $ready) {
    Get-Content -LiteralPath (Join-Path $root "data\server.stderr.log") -Tail 60 -ErrorAction SilentlyContinue
    throw "MailCom Manager did not become healthy"
}

Write-Output "MailCom Manager is ready: $homeUrl"
if (-not $NoBrowser) {
    Start-Process $homeUrl
}
