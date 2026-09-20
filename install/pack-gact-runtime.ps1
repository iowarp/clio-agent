<#
.SYNOPSIS
  Compress the portable CLIO runtime into the two resources used by the
  Windows desktop installer.

.DESCRIPTION
  The previous NSIS bundle listed every Python file separately, making install
  and uninstall take many minutes. This script creates one Zstandard-compressed
  tar archive plus a small integrity manifest. The desktop verifies and expands
  it atomically on first launch.
#>

[CmdletBinding()]
param(
  [Parameter()]
  [string]$Runtime = (Join-Path $PSScriptRoot '..\external\gact-tui\desktop\src-tauri\gact-runtime'),

  [Parameter()]
  [string]$Archive = (Join-Path $PSScriptRoot '..\external\gact-tui\desktop\src-tauri\gact-runtime.tar.zst'),

  [Parameter()]
  [string]$Manifest = (Join-Path $PSScriptRoot '..\external\gact-tui\desktop\src-tauri\gact-runtime.pack.json')
)

$ErrorActionPreference = 'Stop'
$Runtime = [System.IO.Path]::GetFullPath($Runtime)
$Archive = [System.IO.Path]::GetFullPath($Archive)
$Manifest = [System.IO.Path]::GetFullPath($Manifest)

if (-not (Test-Path -LiteralPath (Join-Path $Runtime 'runtime.json') -PathType Leaf)) {
  throw "pack-gact-runtime: $Runtime does not contain runtime.json"
}

$tar = Get-Command tar.exe -ErrorAction SilentlyContinue
if (-not $tar) {
  throw "pack-gact-runtime: Windows tar.exe with Zstandard support is required"
}

$runtimeParent = Split-Path -Parent $Runtime
$runtimeLeaf = Split-Path -Leaf $Runtime
$archiveParent = Split-Path -Parent $Archive
New-Item -ItemType Directory -Path $archiveParent -Force | Out-Null
Remove-Item -LiteralPath $Archive -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $Manifest -Force -ErrorAction SilentlyContinue

$watch = [System.Diagnostics.Stopwatch]::StartNew()
Write-Host "[pack-gact-runtime] compressing $Runtime"
& $tar.Source --zstd -cf $Archive -C $runtimeParent $runtimeLeaf
if ($LASTEXITCODE -ne 0) {
  throw "pack-gact-runtime: tar.exe exited $LASTEXITCODE"
}
$watch.Stop()

$files = @(Get-ChildItem -LiteralPath $Runtime -Recurse -File -Force)
$runtimeBytes = [long](($files | Measure-Object -Property Length -Sum).Sum)
$archiveInfo = Get-Item -LiteralPath $Archive
$sha256 = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
$pack = [ordered]@{
  schema = 1
  archive = [System.IO.Path]::GetFileName($Archive)
  sha256 = $sha256
  runtime_files = $files.Count
  runtime_bytes = $runtimeBytes
  archive_bytes = $archiveInfo.Length
}
[System.IO.File]::WriteAllText(
  $Manifest,
  (($pack | ConvertTo-Json -Compress) + "`n"),
  [System.Text.UTF8Encoding]::new($false)
)

Write-Host (
  '[pack-gact-runtime] OK - {0:N1} MiB -> {1:N1} MiB in {2:N1}s; SHA-256 {3}' -f `
    ($runtimeBytes / 1MB), ($archiveInfo.Length / 1MB), $watch.Elapsed.TotalSeconds, $sha256
)
