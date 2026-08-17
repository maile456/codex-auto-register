param([switch]$Elevated)

$ErrorActionPreference = 'Stop'
$serviceName = 'AutoRegisterMongoDB'
$installer = Join-Path $PSScriptRoot 'install-mongodb-service.ps1'
$service = Get-Service $serviceName -ErrorAction SilentlyContinue

function Test-MongoPort {
  $client = [Net.Sockets.TcpClient]::new()
  try {
    $task = $client.ConnectAsync('127.0.0.1', 27017)
    return $task.Wait(500) -and $client.Connected
  } catch {
    return $false
  } finally {
    $client.Dispose()
  }
}

if (-not $service) {
  & $installer
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  $service = Get-Service $serviceName -ErrorAction Stop
}

if ($service.Status -ne 'Running') {
  try {
    Start-Service $serviceName
  } catch {
    if ($Elevated) { throw }
    $arguments = @(
      '-NoProfile', '-ExecutionPolicy', 'Bypass',
      '-File', ('"{0}"' -f $PSCommandPath), '-Elevated'
    )
    $process = Start-Process powershell.exe -Verb RunAs -ArgumentList $arguments -Wait -PassThru
    exit $process.ExitCode
  }
  $service.WaitForStatus('Running', [TimeSpan]::FromSeconds(20))
}

for ($attempt = 0; $attempt -lt 40; $attempt++) {
  if (Test-MongoPort) {
    Write-Host "$serviceName is running and listening on 127.0.0.1:27017"
    exit 0
  }
  Start-Sleep -Milliseconds 500
}

throw "$serviceName did not listen on 127.0.0.1:27017 within 20 seconds"
