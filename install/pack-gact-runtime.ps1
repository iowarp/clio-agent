<#
.SYNOPSIS
  Compress the portable CLIO runtime into the archive and manifest used by the
  Windows desktop installer.

.DESCRIPTION
  The previous NSIS bundle listed every Python file separately, making install
  and uninstall take many minutes. This script creates one Zstandard-compressed
  tar archive (level 19, long window; install/compress_runtime.py) plus a small
  integrity manifest. With -AssetName and -AssetUrl the setup carries only the
  manifest: the archive is a release asset that install.ps1 downloads while the
  setup unpacks it, verifying its hash in the same pass (gact-tui#739).
#>

[CmdletBinding()]
param(
  [Parameter()]
  [string]$Runtime = (Join-Path $PSScriptRoot '..\external\gact-tui\desktop\src-tauri\gact-runtime'),

  [Parameter()]
  [string]$Archive = (Join-Path $PSScriptRoot '..\external\gact-tui\desktop\src-tauri\gact-runtime.tar.zst'),

  [Parameter()]
  [string]$Manifest = (Join-Path $PSScriptRoot '..\external\gact-tui\desktop\src-tauri\gact-runtime.pack.json'),

  # A setup that does not carry the archive: its release asset name and URL.
  # install.ps1 downloads the asset (install plan) while the setup unpacks it;
  # a setup run on its own fetches the URL.
  [Parameter()]
  [string]$AssetName = '',

  [Parameter()]
  [string]$AssetUrl = ''
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
  throw "pack-gact-runtime: Windows tar.exe is required"
}

$runtimeParent = Split-Path -Parent $Runtime
$runtimeLeaf = Split-Path -Leaf $Runtime
$archiveParent = Split-Path -Parent $Archive
New-Item -ItemType Directory -Path $archiveParent -Force | Out-Null
Remove-Item -LiteralPath $Archive -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $Manifest -Force -ErrorAction SilentlyContinue

$watch = [System.Diagnostics.Stopwatch]::StartNew()
Write-Host "[pack-gact-runtime] archiving $Runtime"
# tar.exe writes the archive; the runtime's own Python compresses it at level 19 with a
# long window (install/compress_runtime.py): tar's default level 3 left the download
# about 140 MB larger.
$plainTar = "$Archive.tar"
Remove-Item -LiteralPath $plainTar -Force -ErrorAction SilentlyContinue
$python = Join-Path $Runtime 'python\python.exe'
# Members are dealt through the archive (install/interleave_runtime.py) so install.ps1's
# unpackers each write their part while it downloads instead of after the last byte.
$memberList = "$Archive.members"
& $python -I -B (Join-Path $PSScriptRoot 'interleave_runtime.py') --runtime $Runtime --out $memberList
if ($LASTEXITCODE -ne 0) {
  throw "pack-gact-runtime: interleave_runtime.py exited $LASTEXITCODE"
}
& $tar.Source -cf $plainTar -C $runtimeParent -n -T $memberList
if ($LASTEXITCODE -ne 0) {
  throw "pack-gact-runtime: tar.exe exited $LASTEXITCODE"
}
Remove-Item -LiteralPath $memberList -Force
& $python -I -B (Join-Path $PSScriptRoot 'compress_runtime.py') --tar $plainTar --out $Archive
if ($LASTEXITCODE -ne 0) {
  throw "pack-gact-runtime: compress_runtime.py exited $LASTEXITCODE"
}
Remove-Item -LiteralPath $plainTar -Force
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
if ($AssetName) { $pack.asset = $AssetName }
if ($AssetUrl) { $pack.url = $AssetUrl }
[System.IO.File]::WriteAllText(
  $Manifest,
  (($pack | ConvertTo-Json -Compress) + "`n"),
  [System.Text.UTF8Encoding]::new($false)
)

Write-Host (
  '[pack-gact-runtime] OK - {0:N1} MiB -> {1:N1} MiB in {2:N1}s; SHA-256 {3}' -f `
    ($runtimeBytes / 1MB), ($archiveInfo.Length / 1MB), $watch.Elapsed.TotalSeconds, $sha256
)
