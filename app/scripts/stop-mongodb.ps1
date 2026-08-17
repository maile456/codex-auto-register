param([switch]$Elevated)

$ErrorActionPreference = 'Stop'
$serviceName = 'AutoRegisterMongoDB'
$service = Get-Service $serviceName -ErrorAction SilentlyContinue

if (-not $service) {
  Write-Host "$serviceName is not installed"
  exit 0
}
if ($service.Status -eq 'Stopped') {
  Write-Host "$serviceName is already stopped"
  exit 0
}

try {
  Stop-Service $serviceName
} catch {
  if ($Elevated) { throw }
  $arguments = @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass',
    '-File', ('"{0}"' -f $PSCommandPath), '-Elevated'
  )
  $process = Start-Process powershell.exe -Verb RunAs -ArgumentList $arguments -Wait -PassThru
  exit $process.ExitCode
}
$service.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(20))
Write-Host "$serviceName stopped"
