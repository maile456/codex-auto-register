param([switch]$Elevated)

$ErrorActionPreference = 'Stop'

function Test-Administrator {
  $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
  $principal = [Security.Principal.WindowsPrincipal]::new($identity)
  return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

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

if (-not (Test-Administrator)) {
  $arguments = @(
    '-NoProfile',
    '-ExecutionPolicy', 'Bypass',
    '-File', ('"{0}"' -f $PSCommandPath),
    '-Elevated'
  )
  $process = Start-Process powershell.exe -Verb RunAs -ArgumentList $arguments -Wait -PassThru
  exit $process.ExitCode
}

$workspace = Split-Path -Parent $PSScriptRoot
$root = Split-Path -Parent $workspace
$config = Join-Path $workspace 'mongodb\mongod.yml'
$dataRoot = Join-Path $root 'data\mongodb'
$serviceName = 'AutoRegisterMongoDB'
$installerName = 'mongodb-windows-x86_64-8.0.28-signed.msi'
$installerAtRoot = Join-Path $root $installerName
$expectedHash = '4DBA821FBE63E380F80A21785BADE811744A276C17D01E51C8C5BBBCD9C682FB'
$downloadUrl = "https://fastdl.mongodb.org/windows/$installerName"
$installLog = Join-Path $root 'mongodb-install.log'

New-Item -ItemType Directory -Force (Join-Path $dataRoot 'db') | Out-Null
$dbPath = (Join-Path $dataRoot 'db').Replace("'", "''")
$logPath = (Join-Path $dataRoot 'mongod.log').Replace("'", "''")
$configText = @"
storage:
  dbPath: '$dbPath'

systemLog:
  destination: file
  logAppend: true
  path: '$logPath'

net:
  bindIp: 127.0.0.1
  port: 27017

processManagement:
  windowsService:
    serviceName: AutoRegisterMongoDB
    displayName: AutoRegister MongoDB
    description: Local-only MongoDB service for AutoRegister development
"@
[IO.File]::WriteAllText($config, $configText, [Text.UTF8Encoding]::new($false))

$service = Get-Service $serviceName -ErrorAction SilentlyContinue
if ($service) {
  $serviceInfo = Get-CimInstance Win32_Service -Filter "Name='$serviceName'"
  if ($serviceInfo.PathName -notlike "*--config $config*") {
    throw "$serviceName exists with an unexpected config: $($serviceInfo.PathName)"
  }
  if ($serviceInfo.StartMode -ne 'Auto') {
    Set-Service $serviceName -StartupType Automatic
  }
  if ($service.Status -ne 'Running') {
    Start-Service $serviceName
    $service.WaitForStatus('Running', [TimeSpan]::FromSeconds(20))
  }
  Write-Host "$serviceName is installed and running"
  exit 0
}

$listener = Get-NetTCPConnection -LocalPort 27017 -State Listen -ErrorAction SilentlyContinue
if ($listener) {
  $process = Get-Process -Id $listener.OwningProcess -ErrorAction Stop
  $portableRoot = Join-Path $root 'mongodb-portable'
  if (-not $process.Path.StartsWith($portableRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Port 27017 is owned by a non-AutoRegister process: $($process.Path)"
  }
  Stop-Process -Id $process.Id -Force
  $process.WaitForExit(15000)
}

$binary = 'C:\Program Files\MongoDB\Server\8.0\bin\mongod.exe'
$temporaryInstaller = $false
if (-not (Test-Path -LiteralPath $binary)) {
  $installer = $installerAtRoot
  if (-not (Test-Path -LiteralPath $installer)) {
    $installer = Join-Path $env:TEMP $installerName
    Invoke-WebRequest -UseBasicParsing -Uri $downloadUrl -OutFile $installer
    $temporaryInstaller = $true
  }

  try {
    $hash = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash
    if ($hash -ne $expectedHash) {
      throw "MongoDB installer SHA-256 mismatch: $hash"
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $installer
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'MongoDB, Inc\.') {
      throw 'MongoDB installer signature is invalid or the signer does not match'
    }

    $arguments = @(
      '/i', ('"{0}"' -f $installer),
      '/qn', '/norestart',
      'ADDLOCAL=ServerNoService',
      'SHOULD_INSTALL_COMPASS=0',
      '/l*v', ('"{0}"' -f $installLog)
    )
    $install = Start-Process msiexec.exe -ArgumentList $arguments -Wait -PassThru
    if ($install.ExitCode -notin 0, 3010) {
      throw "MongoDB MSI installation failed with exit code $($install.ExitCode)"
    }
  } finally {
    if ($temporaryInstaller) {
      Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
    }
  }
}

if (-not (Test-Path -LiteralPath $binary)) {
  throw "mongod.exe was not found after installation: $binary"
}

& $binary --config $config --install
if ($LASTEXITCODE -ne 0) {
  throw "Failed to register $serviceName service; exit code $LASTEXITCODE"
}

$service = Get-Service $serviceName -ErrorAction Stop
Start-Service $serviceName
$service.WaitForStatus('Running', [TimeSpan]::FromSeconds(20))

for ($attempt = 0; $attempt -lt 40; $attempt++) {
  if (Test-MongoPort) {
    Write-Host "$serviceName is installed and listening on 127.0.0.1:27017"
    exit 0
  }
  Start-Sleep -Milliseconds 500
}

throw "$serviceName started but did not listen on 127.0.0.1:27017 within 20 seconds"
