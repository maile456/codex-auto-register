$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root 'register_env\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
  & python -m venv (Join-Path $root 'register_env')
}

& $python -m pip install --upgrade pip
& $python -m pip install -r requirements.txt -r requirements-dev.txt
& npm.cmd ci
& (Join-Path $PSScriptRoot 'scripts\install-mongodb-service.ps1')

Write-Host 'Setup complete. Run AutoRegister: Full stack in VS Code.'
