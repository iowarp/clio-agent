#requires -Version 5.1
<#
.SYNOPSIS
  Build the PORTABLE embedded clio-agent runtime for the bundled CLIO
  Desktop installer variant (Windows). macOS/Linux: build-gact-runtime.sh.

.DESCRIPTION
  Replaces gact-tui's retired build-clio-runtime.ps1 (#909): that script
  built a `uv venv --relocatable`, whose python.exe is a shim loading the
  BUILD HOST's base interpreter from pyvenv.cfg `home` -- so the shipped
  runtime could never start on a fresh machine. This script instead ships
  the interpreter itself: uv's python-build-standalone distribution is
  copied INTO the runtime and the clio-agent wheel is installed directly
  into it (no venv, no pyvenv.cfg, no build-host paths on the exec path).

  The runtime self-describes via a generic manifest (<out>\runtime.json,
  iowarp/gact-tui#311) so the desktop launcher needs zero knowledge of
  what's inside:
    {"schema": 1, "exec": ["python/python.exe", "-m", "clio_agent.gact", "--no-agent"]}

  Console-script exes are DELETED after install: they embed absolute
  build paths and break on relocation -- `-m clio_agent.gact` is the only
  supported entry. The build proves portability on the real object: the
  finished tree is copied to a temp location and booted from there.

.PARAMETER Out
  Output directory for the runtime tree (required). clio-bundles.yml
  passes the gact-tui submodule's src-tauri\gact-runtime.

.PARAMETER Ref
  clio-agent git ref to install. Defaults to $env:CLIO_REF or "develop".

.PARAMETER Source
  Local clio-agent checkout to install from instead of the git ref (CI
  passes its own workspace so the runtime is built from EXACTLY the
  released tree). Defaults to $env:CLIO_AGENT_SOURCE.

.PARAMETER PythonVersion
  Python minor version. Defaults to $env:CLIO_RUNTIME_PYTHON or "3.12".
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory)][string] $Out,
  [string] $Ref = $(if ($env:CLIO_REF) { $env:CLIO_REF } else { 'develop' }),
  [string] $Source = $env:CLIO_AGENT_SOURCE,
  [string] $PythonVersion = $(if ($env:CLIO_RUNTIME_PYTHON) { $env:CLIO_RUNTIME_PYTHON } else { '3.12' })
)

$ErrorActionPreference = 'Stop'

function Get-DirSizeMB([string] $path) {
  if (-not (Test-Path $path)) { return 0 }
  $sum = (Get-ChildItem -LiteralPath $path -Recurse -File -ErrorAction SilentlyContinue |
            Measure-Object -Property Length -Sum).Sum
  if (-not $sum) { return 0 }
  return [math]::Round($sum / 1MB, 1)
}

# Invoke-Native: stream output, gate purely on exit code (uv writes
# progress to stderr, which PS 5.1 would otherwise promote to an error).
function Invoke-Native {
  param([Parameter(Mandatory)][string] $Exe, [string[]] $Args)
  $prev = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    & $Exe @Args 2>&1 | ForEach-Object { "$_" }
  } finally {
    $ErrorActionPreference = $prev
  }
  if ($LASTEXITCODE -ne 0) {
    throw "$Exe $($Args -join ' ') failed (exit $LASTEXITCODE)"
  }
}

$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
  Write-Error "build-gact-runtime: 'uv' is required but not found on PATH."
  exit 1
}
Write-Host "[build-gact-runtime] uv: $($uv.Source) ($((& $uv.Source --version) 2>$null))"

# Always rebuild from clean so a stale tree can't leak into the bundle.
if (Test-Path $Out) {
  Write-Host "[build-gact-runtime] removing existing $Out before rebuild"
  Remove-Item -LiteralPath $Out -Recurse -Force
}
New-Item -ItemType Directory -Path $Out -Force | Out-Null
$Out = (Resolve-Path $Out).Path

# --- 1. interpreter: python-build-standalone, copied INTO the runtime ---
$staging = Join-Path $Out '.uv-python-staging'
Write-Host "[build-gact-runtime] installing standalone CPython $PythonVersion"
Invoke-Native -Exe $uv.Source -Args @('python', 'install', $PythonVersion, '--install-dir', $staging)
# The staging dir holds the real versioned dist plus a bare-minor alias
# (cpython-3.12-... junction -> cpython-3.12.13-...). Copy the real one.
$dist = Get-ChildItem -LiteralPath $staging -Directory |
  Where-Object { $_.Name -match ('^cpython-' + [regex]::Escape($PythonVersion) + '\.\d') } |
  Select-Object -First 1
if (-not $dist) { throw "build-gact-runtime: no cpython dist under $staging" }
Copy-Item -LiteralPath $dist.FullName -Destination (Join-Path $Out 'python') -Recurse
Remove-Item -LiteralPath $staging -Recurse -Force

# Our copy is a private distribution now, not uv's managed install --
# drop the PEP 668 marker so `uv pip install --python` targets it.
Get-ChildItem -LiteralPath (Join-Path $Out 'python') -Recurse -Depth 2 -Filter 'EXTERNALLY-MANAGED' -ErrorAction SilentlyContinue |
  ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }

$pyBinRel = 'python/python.exe'
$pyBin = Join-Path $Out 'python\python.exe'
if (-not (Test-Path $pyBin)) { throw "build-gact-runtime: $pyBinRel missing in dist" }

# --- 2. install clio-agent + the portable science launcher -------------
if ($Source) {
  if (-not (Test-Path (Join-Path $Source 'pyproject.toml'))) {
    throw "build-gact-runtime: Source=$Source is not a clio-agent checkout"
  }
  $spec = (Resolve-Path $Source).Path
} else {
  $spec = "clio-agent @ git+https://github.com/iowarp/clio-agent.git@$Ref"
}
$clioKitSpec = 'clio-kit==2.10.6'
Write-Host "[build-gact-runtime] installing: $spec + $clioKitSpec"
Invoke-Native -Exe $uv.Source -Args @(
    'pip', 'install', '--python', $pyBin, $spec, $clioKitSpec, 'globus-sdk>=3.0.0',
    'dspy==3.3.0b1', 'fastmcp==4.0.0b5',
    'fastmcp-slim==4.0.0b5', 'fastmcp-tasks==4.0.0b5'
)

# Web Search is the recommended installer-selected service. Install its
# source-locked adapter into the main relocatable runtime now, rather than
# making the first desktop connection build a 100-package environment.
$webMcpProject = Join-Path $Out 'python\clio-kit-mcp-servers\web'
if (-not (Test-Path (Join-Path $webMcpProject 'pyproject.toml'))) {
  throw "build-gact-runtime: bundled CLIO Web Search MCP project missing at $webMcpProject"
}
Write-Host "[build-gact-runtime] installing bundled CLIO Web Search adapter"
Invoke-Native -Exe $uv.Source -Args @(
    'pip', 'install', '--python', $pyBin, $webMcpProject,
    'fastmcp==4.0.0b5', 'fastmcp-slim==4.0.0b5', 'fastmcp-tasks==4.0.0b5'
)

# clio-kit materializes each locked MCP server with uv on first use. Ship uv
# beside the relocatable runtime instead of requiring a fresh desktop user to
# install developer tooling or discover PATH configuration.
$runtimeBin = Join-Path $Out 'bin'
New-Item -ItemType Directory -Path $runtimeBin -Force | Out-Null
Copy-Item -LiteralPath $uv.Source -Destination (Join-Path $runtimeBin 'uv.exe') -Force
Copy-Item -LiteralPath $uv.Source -Destination (Join-Path $runtimeBin 'uvx.exe') -Force

$sizeBefore = Get-DirSizeMB $Out
Write-Host "[build-gact-runtime] size before prune: $sizeBefore MB"

# --- 3. prune -----------------------------------------------------------
$pyRoot = Join-Path $Out 'python'
Get-ChildItem -LiteralPath $pyRoot -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
  ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
Get-ChildItem -LiteralPath $pyRoot -Recurse -File -Filter '*.pyc' -ErrorAction SilentlyContinue |
  ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }

$sitePkgs = Join-Path $pyRoot 'Lib\site-packages'
if (Test-Path $sitePkgs) {
  # in-package tests/ trees in vendored deps (clio_agent ships none)
  Get-ChildItem -LiteralPath $sitePkgs -Directory -ErrorAction SilentlyContinue | ForEach-Object {
    foreach ($t in @('tests', 'test')) {
      $td = Join-Path $_.FullName $t
      if (Test-Path $td) { Remove-Item -LiteralPath $td -Recurse -Force -ErrorAction SilentlyContinue }
    }
  }
  # *.dist-info/RECORD is KEPT: the desktop upgrades this runtime in place
  # with `uv pip install`, and without RECORD uv cannot uninstall the old
  # version -- stale modules and duplicate metadata survive the upgrade.

  # Installer-hostile filenames (NSIS aborts on parens/brackets -- the
  # litellm benchmark-data lesson from the 0.7.0 gact-tui release).
  $benchDir = Join-Path $sitePkgs 'litellm\proxy\guardrails\guardrail_hooks\litellm_content_filter\guardrail_benchmarks'
  if (Test-Path -LiteralPath $benchDir) {
    Remove-Item -LiteralPath $benchDir -Recurse -Force -ErrorAction SilentlyContinue
  }
  Get-ChildItem -LiteralPath $pyRoot -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '[()\[\]]' } |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }
  $offenders = @(Get-ChildItem -LiteralPath $pyRoot -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '[()\[\]]' })
  if ($offenders.Count -gt 0) {
    $offenders | ForEach-Object { Write-Host "  installer-hostile: $($_.FullName)" }
    throw "build-gact-runtime: $($offenders.Count) installer-hostile filenames remain after prune"
  }
}

# Console-script exes embed the absolute build path -- relocation traps.
# Delete them all; `-m clio_agent.gact` is the only supported entry.
$scriptsDir = Join-Path $pyRoot 'Scripts'
if (Test-Path $scriptsDir) {
  Get-ChildItem -LiteralPath $scriptsDir -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notmatch '^python' } |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }
}
# distlib/setuptools launcher stubs under site-packages (t64.exe, w64-arm.exe,
# ...) are dead weight because console scripts are deleted and ``-m`` is the
# GACT entry. Keep real packaged runtimes: the Codex SDK provider executable
# and iowarp-core's native daemon/tools. Removing clio_run.exe leaves imports
# healthy but silently forces ARC to degrade when the first agent is built.
if (Test-Path $sitePkgs) {
  $codexCli = Join-Path $sitePkgs 'codex_cli_bin\bin\codex.exe'
  $iowarpCoreBin = Join-Path $sitePkgs 'iowarp_core\bin'
  Get-ChildItem -LiteralPath $sitePkgs -Recurse -File -Filter '*.exe' -ErrorAction SilentlyContinue |
    Where-Object {
      $_.FullName -ne $codexCli -and
      -not $_.FullName.StartsWith($iowarpCoreBin, [System.StringComparison]::OrdinalIgnoreCase)
    } |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }
  if (-not (Test-Path -LiteralPath $codexCli)) {
    throw 'build-gact-runtime: packaged Codex provider executable is missing'
  }
  $clioCoreLauncher = Join-Path $iowarpCoreBin 'clio_run.exe'
  if (-not (Test-Path -LiteralPath $clioCoreLauncher)) {
    throw 'build-gact-runtime: packaged clio-core launcher is missing'
  }
}

# Prepare the real startup import graph in the release image, not on the
# user's first launch. Compiling the entire distribution is both wasteful and
# invalid: CPython ships non-imported Tcl demo files with syntax errors, while
# some optional provider paths exceed Windows' legacy path limit.
Write-Host "[build-gact-runtime] compiling portable startup bytecode"
$precompiler = Join-Path $Source 'install/precompile_runtime.py'
Invoke-Native -Exe $pyBin -Args @($precompiler, '--python-root', $pyRoot)
$compiled = @(Get-ChildItem -LiteralPath $pyRoot -Recurse -File -Filter '*.pyc' -ErrorAction SilentlyContinue).Count
if ($compiled -eq 0) {
  throw 'build-gact-runtime: bytecode preparation produced no .pyc files'
}
Write-Host "[build-gact-runtime] prepared $compiled bytecode files"

$sizeAfter = Get-DirSizeMB $Out
Write-Host "[build-gact-runtime] size after prune:  $sizeAfter MB (was $sizeBefore MB)"

# --- 4. generic runtime manifest ----------------------------------------
$manifest = @{ schema = 1; exec = @($pyBinRel, '-m', 'clio_agent.gact', '--no-agent') } |
  ConvertTo-Json -Compress
[System.IO.File]::WriteAllText((Join-Path $Out 'runtime.json'), $manifest + "`n")
Write-Host "[build-gact-runtime] manifest: $manifest"

# --- 5. portability proof on the real object ----------------------------
# A venv would leave a pyvenv.cfg pinning the build host's interpreter;
# assert the failure mode is structurally absent, then boot the runtime
# FROM A RELOCATED COPY -- the invariant the old script never proved.
$venvCfg = @(Get-ChildItem -LiteralPath $Out -Recurse -Filter 'pyvenv.cfg' -ErrorAction SilentlyContinue)
if ($venvCfg.Count -gt 0) {
  throw "build-gact-runtime: pyvenv.cfg found -- runtime is venv-shaped, not portable"
}
$reloc = Join-Path ([System.IO.Path]::GetTempPath()) ("gact-runtime-relocated-" + [System.IO.Path]::GetRandomFileName())
Copy-Item -LiteralPath $Out -Destination $reloc -Recurse
$relocPy = Join-Path $reloc 'python\python.exe'
Write-Host "[build-gact-runtime] sanity (relocated): $relocPy -m clio_agent.gact --help"
Invoke-Native -Exe $relocPy -Args @('-m', 'clio_agent.gact', '--help') | Out-Null
Invoke-Native -Exe $relocPy -Args @('-c', 'from clio_kit import cli; cli()', '--help') | Out-Null
$relocUv = Join-Path $reloc 'bin\uv.exe'
Invoke-Native -Exe $relocUv -Args @('--version') | Out-Null
# Imports and /v1/capabilities do not initialize ARC under --no-agent. Prove
# the relocated image can launch clio-core and select the intended tiered
# backend instead of degrading to LocalFS after installation.
$relocCoreLauncher = Join-Path $reloc 'python\Lib\site-packages\iowarp_core\bin\clio_run.exe'
if (-not (Test-Path -LiteralPath $relocCoreLauncher)) {
  throw 'build-gact-runtime: relocated clio-core launcher is missing'
}
$previousUserDir = $env:CLIO_USER_DIR
$previousFileCapacity = $env:CLIO_ARC_CTE_FILE_CAPACITY
# The smoke proves the RELOCATED IMAGE can init clio-core; where its user
# state lands is irrelevant to that, so keep it OFF the temp volume holding
# the relocated copy (the build just wrote two ~1 GB trees there) and beside
# $Out instead -- outside $Out so it can never reach the bundle. The file
# tier is deliberately tiny because ARC's preflight demands
# capacity + a 1 GiB free-space reserve on that volume, and a gigabyte-scale
# request on a build volume buys nothing this gate is trying to prove: any
# shortfall surfaces as a LOUD degrade to LocalFS indistinguishable from the
# prune casualty the gate exists to catch.
$smokeUser = (Join-Path ([System.IO.Path]::GetDirectoryName($Out)) 'gact-runtime-arc-smoke')
$previousRuntimeStateDir = $env:CLIO_RUNTIME_STATE_DIR
Remove-Item -LiteralPath $smokeUser -Recurse -Force -ErrorAction SilentlyContinue
try {
  $env:CLIO_USER_DIR = $smokeUser
  $env:CLIO_ARC_CTE_FILE_CAPACITY = '64MB'
  # Hermetic by explicit selection, not by degrade: the spawn lock, pidfile,
  # client registry and daemon log default to the host-global ~/.clio, which
  # on a build machine means sharing a daemon (and a log) with whatever else
  # runs there. Point them at the smoke's own directory so this proves the
  # RELOCATED IMAGE, and so the daemon log lands where the failure path looks.
  $env:CLIO_RUNTIME_STATE_DIR = (Join-Path $smokeUser 'runtime-state')
  New-Item -ItemType Directory -Path $smokeUser -Force | Out-Null
  New-Item -ItemType Directory -Path $env:CLIO_RUNTIME_STATE_DIR -Force | Out-Null
  # Diagnostic only: never fail the build because free space could not be read.
  $smokeFree = 'unknown'
  try {
    $smokeFree = "$([math]::Round((New-Object System.IO.DriveInfo((Get-Item $smokeUser).Root)).AvailableFreeSpace / 1GB, 2)) GB"
  } catch { }
  Write-Host "[build-gact-runtime] sanity (relocated ARC): initialize clio-core store (user dir $smokeUser, free $smokeFree)"
  # NOT swallowed: on a degrade the helper prints the typed reason, the stack
  # from a wrapper-free re-run, and the daemon log. That output IS the gate's
  # diagnostic value, and piping it away once already cost a release cycle.
  Invoke-Native -Exe $relocPy -Args @((Join-Path $Source 'install/arc_smoke.py'))
} finally {
  Remove-Item -LiteralPath $smokeUser -Recurse -Force -ErrorAction SilentlyContinue
  if ($null -eq $previousRuntimeStateDir) {
    Remove-Item Env:CLIO_RUNTIME_STATE_DIR -ErrorAction SilentlyContinue
  } else { $env:CLIO_RUNTIME_STATE_DIR = $previousRuntimeStateDir }
  if ($null -eq $previousUserDir) { Remove-Item Env:CLIO_USER_DIR -ErrorAction SilentlyContinue }
  else { $env:CLIO_USER_DIR = $previousUserDir }
  if ($null -eq $previousFileCapacity) {
    Remove-Item Env:CLIO_ARC_CTE_FILE_CAPACITY -ErrorAction SilentlyContinue
  } else { $env:CLIO_ARC_CTE_FILE_CAPACITY = $previousFileCapacity }
}
# --help only proves imports; BOOT the relocated copy and poll the API --
# the only automated proof a prune casualty or loader problem would fail.
$port = Get-Random -Minimum 24000 -Maximum 44000
Write-Host "[build-gact-runtime] sanity (relocated boot): /v1/capabilities on :$port"
$srv = Start-Process -FilePath $relocPy -PassThru -WindowStyle Hidden `
  -ArgumentList @('-m', 'clio_agent.gact', '--no-agent', '--host', '127.0.0.1', '--port', "$port")
$bootWatch = [System.Diagnostics.Stopwatch]::StartNew()
$bootOk = $false
foreach ($i in 1..30) {
  try {
    $resp = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri "http://127.0.0.1:$port/v1/capabilities"
    if ($resp.StatusCode -eq 200) { $bootOk = $true; break }
  } catch {}
  Start-Sleep -Seconds 1
}
$bootWatch.Stop()
Stop-Process -Id $srv.Id -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $reloc -Recurse -Force -ErrorAction SilentlyContinue
if (-not $bootOk) {
  throw "build-gact-runtime: relocated runtime failed to serve /v1/capabilities within 30 seconds"
}
Write-Host ("[build-gact-runtime] relocated cold boot ready in {0:N2}s" -f $bootWatch.Elapsed.TotalSeconds)

Write-Host "[build-gact-runtime] OK - portable runtime ready at $Out ($sizeAfter MB)"
