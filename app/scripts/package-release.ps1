param(
  [string]$Version = '1.0.0',
  [switch]$SkipChecks
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$appRoot = Split-Path -Parent $PSScriptRoot
$workspaceRoot = Split-Path -Parent $appRoot
$releaseName = "AutoRegister-v$Version"
$artifactRoot = Join-Path $workspaceRoot 'artifacts'
$stageRoot = Join-Path $artifactRoot $releaseName
$stageApp = Join-Path $stageRoot 'app'
$zipPath = Join-Path $artifactRoot "$releaseName.zip"
$zipHashPath = "$zipPath.sha256"

if (-not $SkipChecks) {
  Push-Location $appRoot
  try {
    & npm.cmd run type-check
    if ($LASTEXITCODE -ne 0) { throw "Type check failed with exit code $LASTEXITCODE" }
    & npm.cmd test -- --run
    if ($LASTEXITCODE -ne 0) { throw "Frontend tests failed with exit code $LASTEXITCODE" }
    & npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw "Frontend build failed with exit code $LASTEXITCODE" }
  } finally {
    Pop-Location
  }
}

if (-not (Test-Path -LiteralPath (Join-Path $appRoot 'dist\index.html'))) {
  throw 'Frontend build output is missing. Run without -SkipChecks to build it.'
}

New-Item -ItemType Directory -Force -Path $artifactRoot | Out-Null
if (Test-Path -LiteralPath $stageRoot) {
  Remove-Item -LiteralPath $stageRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $stageApp | Out-Null

$directories = @('backend', 'dist', 'mongodb', 'public', 'scripts', 'src', 'tests', '.vscode')
foreach ($directory in $directories) {
  $source = Join-Path $appRoot $directory
  if (Test-Path -LiteralPath $source) {
    Copy-Item -LiteralPath $source -Destination $stageApp -Recurse -Force
  }
}

$files = @(
  '.env.example', '.gitignore', 'env.d.ts', 'index.html', 'package-lock.json',
  'package.json', 'README.md', 'requirements-dev.txt', 'requirements.txt',
  'setup.ps1', 'tsconfig.app.json', 'tsconfig.json', 'tsconfig.node.json',
  'vite.config.ts', 'vitest.config.ts'
)
foreach ($file in $files) {
  Copy-Item -LiteralPath (Join-Path $appRoot $file) -Destination $stageApp -Force
}

Get-ChildItem -LiteralPath $stageRoot -Recurse -Directory -Force |
  Where-Object { $_.Name -in @('__pycache__', '.pytest_cache') } |
  Sort-Object FullName -Descending |
  Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $stageRoot -Recurse -File -Force |
  Where-Object { $_.Name -match '\.(pyc|pyo|log)$' -or $_.Name -eq '.env' } |
  Remove-Item -Force

Copy-Item -LiteralPath (Join-Path $appRoot 'README.md') -Destination (Join-Path $stageRoot 'README.md') -Force

$manifestPath = Join-Path $stageRoot 'RELEASE_SHA256SUMS.txt'
$manifestLines = Get-ChildItem -LiteralPath $stageRoot -Recurse -File |
  Where-Object { $_.FullName -ne $manifestPath } |
  Sort-Object FullName |
  ForEach-Object {
    $relativePath = $_.FullName.Substring($stageRoot.Length + 1).Replace('\', '/')
    $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $relativePath"
  }
[IO.File]::WriteAllLines($manifestPath, $manifestLines, [Text.UTF8Encoding]::new($false))

if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
if (Test-Path -LiteralPath $zipHashPath) { Remove-Item -LiteralPath $zipHashPath -Force }
Compress-Archive -LiteralPath $stageRoot -DestinationPath $zipPath -CompressionLevel Optimal

$zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
[IO.File]::WriteAllText($zipHashPath, "$zipHash  $releaseName.zip`n", [Text.UTF8Encoding]::new($false))

Write-Host "Release: $zipPath"
Write-Host "SHA256: $zipHash"
