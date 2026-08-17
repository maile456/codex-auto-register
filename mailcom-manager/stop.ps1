param([int]$Port = 3211)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
foreach ($listener in $listeners) {
    Stop-Process -Id $listener.OwningProcess -Force -ErrorAction SilentlyContinue
}
$pidFile = Join-Path $root "data\server.pid"
if (Test-Path $pidFile) {
    $savedPid = Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue
    if ($savedPid -and (Get-Process -Id $savedPid -ErrorAction SilentlyContinue)) {
        Stop-Process -Id $savedPid -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $pidFile -Force
}
Write-Output "MailCom Manager stopped"
