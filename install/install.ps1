# CLIO installer (Windows / PowerShell).
#
# Default: pulls clio-agent from PyPI and downloads a prebuilt
# CLIO-branded clio-tui.exe from clio-agent's GitHub Releases. No `git` or `go` required;
# you only need `uv` or `pip`.
#
# Source-build mode (opt-in for tracking unreleased work): set
# $env:CLIO_REF=<branch> and/or $env:GACT_REF=<branch> to
# clone-and-build the selected component instead. Source mode for
# clio-agent needs `git` + `uv`; source mode for gact-tui needs
# `git` + `go` 1.26+.
#
# Honors environment overrides:
#   CLIO_PREFIX        install root           (default: $HOME\AppData\Local\clio)
#   CLIO_BIN_DIR       launcher location      (default: ...\WindowsApps)
#   CLIO_VERSION       pin clio-agent         (default: latest from PyPI)
#   GACT_VERSION       legacy override for TUI release tag (default: match CLIO)
#   CLIO_INSTALLER_REF pin launcher scripts   (default: v<installed clio-agent>)
#   CLIO_REF           clio-agent branch      (default: release mode)
#   GACT_REF           gact-tui branch        (default: release mode)
#   CLIO_GIT_PROTOCOL  https | ssh            (default: https; only used
#                                              in source-build mode)

$ErrorActionPreference = 'Stop'

function Say  ($m) { Write-Host "==> $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "!! $m"  -ForegroundColor Yellow }
function Die  ($m) { Write-Host "xx $m"  -ForegroundColor Red; exit 1 }
function Have ($cmd) { return $null -ne (Get-Command $cmd -ErrorAction SilentlyContinue) }
function RunNative($cmd, [string[]]$cmdArgs) {
    & $cmd @cmdArgs
    if ($LASTEXITCODE -ne 0) {
        Die "$cmd failed with exit code $LASTEXITCODE"
    }
}
function LongPath($path) {
    $full = [System.IO.Path]::GetFullPath($path)
    if ($full.StartsWith('\\?\')) { return $full }
    if ($full.StartsWith('\\')) { return "\\?\UNC\$($full.Substring(2))" }
    return "\\?\$full"
}
function RemoveTree($path) {
    Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $path) {
        try {
            [System.IO.Directory]::Delete((LongPath $path), $true)
        } catch {
            # Fall through to the explicit check below so users see a hard failure.
        }
    }
    if (Test-Path -LiteralPath $path) {
        Die "failed to remove existing path: $path"
    }
}

# ---------- defaults ---------------------------------------------------
$Prefix      = if ($env:CLIO_PREFIX)       { $env:CLIO_PREFIX }      else { Join-Path $HOME 'AppData\Local\clio' }
$BinDir      = if ($env:CLIO_BIN_DIR)      { $env:CLIO_BIN_DIR }     else { Join-Path $HOME 'AppData\Local\Microsoft\WindowsApps' }
$ClioVersion = $env:CLIO_VERSION
$GactVersion = if ($env:GACT_VERSION)      { $env:GACT_VERSION }     else { 'latest' }
$ClioInstallerRef = $env:CLIO_INSTALLER_REF
$ClioRef     = $env:CLIO_REF
$GactRef     = $env:GACT_REF
$Protocol    = if ($env:CLIO_GIT_PROTOCOL) { $env:CLIO_GIT_PROTOCOL } else { 'https' }

switch ($Protocol) {
    'https' {
        $ClioRepo = 'https://github.com/iowarp/clio-agent.git'
        $GactRepo = 'https://github.com/iowarp/gact-tui.git'
    }
    'ssh' {
        $ClioRepo = 'git@github.com:iowarp/clio-agent.git'
        $GactRepo = 'git@github.com:iowarp/gact-tui.git'
    }
    default { Die "CLIO_GIT_PROTOCOL must be 'https' or 'ssh' (got: $Protocol)" }
}

# ---------- prerequisite checks ---------------------------------------
$PyInstall = $null
if     (Have uv)   { $PyInstall = 'uv' }
elseif (Have pip)  { $PyInstall = 'pip' }
elseif (Have pip3) { $PyInstall = 'pip3' }
else {
    Die "need uv or pip to install clio-agent. install uv with: irm https://astral.sh/uv/install.ps1 | iex"
}

if ($ClioRef) {
    if (-not (Have git)) { Die "git required when CLIO_REF is set (source-build mode)" }
    if (-not (Have uv))  { Die "uv required to build clio-agent from source" }
    if (-not (Have bash)) { Die "bash required when CLIO_REF is set so the CLIO TUI build script can run" }
    if (-not (Have go))  { Die "go (>= 1.26) required when CLIO_REF is set so the CLIO TUI can be built" }
}
if ($GactRef) {
    if (-not (Have git)) { Die "git required when GACT_REF is set (source-build mode)" }
    if (-not (Have go))  { Die "go (>= 1.26) required to build gact from source" }
    if (-not $ClioRef) { Die "GACT_REF source-build mode now requires CLIO_REF so CLIO branding scripts are available" }
}

New-Item -ItemType Directory -Force -Path $Prefix, $BinDir | Out-Null

# ---------- install clio-agent ----------------------------------------
$Venv = Join-Path $Prefix 'clio-agent\.venv'

if ($ClioRef) {
    Say "Cloning clio-agent at $ClioRef (source-build mode)"
    RemoveTree (Join-Path $Prefix 'clio-agent')
    RunNative git @('clone', '--quiet', '--recurse-submodules', '--shallow-submodules', '--branch', $ClioRef, '--depth', '1', $ClioRepo, (Join-Path $Prefix 'clio-agent'))
    Say "Installing clio-agent deps (uv sync)"
    Push-Location (Join-Path $Prefix 'clio-agent')
    RunNative uv @('sync')
    Pop-Location
} else {
    $pkgSpec = if ($ClioVersion) { "clio-agent==$ClioVersion" } else { 'clio-agent' }
    Say "Installing $pkgSpec from PyPI"
    RemoveTree (Join-Path $Prefix 'clio-agent')
    New-Item -ItemType Directory -Force -Path (Join-Path $Prefix 'clio-agent') | Out-Null
    if ($PyInstall -eq 'uv') {
        RunNative uv @('venv', '--python', '>=3.12', $Venv)
        RunNative uv @('pip', 'install', '--quiet', '--python', (Join-Path $Venv 'Scripts\python.exe'), $pkgSpec, 'dspy==3.3.0b1')
    } else {
        RunNative python @('-m', 'venv', $Venv)
        $PipExe = Join-Path $Venv "Scripts\$PyInstall.exe"
        RunNative $PipExe @('install', '--quiet', '--upgrade', 'pip')
        RunNative $PipExe @('install', '--quiet', $pkgSpec)
    }
}

$InstalledClioVersion = $null
$VenvPython = Join-Path $Venv 'Scripts\python.exe'
if (Test-Path $VenvPython) {
    try {
        $InstalledClioVersion = (& $VenvPython -c "from importlib.metadata import version; print(version('clio-agent'))").Trim()
    } catch {
        $InstalledClioVersion = $null
    }
}

# ---------- provision clio-kit MCP tool launcher ----------------------
# Marketplace packs launch their MCP servers via the installed `clio-kit
# mcp-server <name>` launcher. Provision it as a uv tool so the launcher is on
# PATH before first spawn. Installed-tool launchers (not `uvx clio-kit@...`)
# avoid the concurrent cold-cache ephemeral-env race and `uv cache prune`
# deleting envs under running servers (astral-sh/uv#11694). `uv tool install`
# is idempotent (re-run is a no-op / version pin). Needs uv; without it the
# packs' tools simply stay unprovisioned until uv is installed.
if (Have uv) {
    Say "Provisioning clio-kit MCP tool launcher (uv tool install clio-kit==2.2.3)"
    & uv tool install clio-kit==2.2.3
    if ($LASTEXITCODE -ne 0) {
        Warn "clio-kit provisioning failed; marketplace pack tools will be unavailable until 'uv tool install clio-kit==2.2.3' succeeds and the dir from 'uv tool dir --bin' is on PATH"
    }
} else {
    Warn "uv not found - skipping clio-kit MCP launcher provisioning; install uv, then run: uv tool install clio-kit==2.2.3"
}

# ---------- install gact ----------------------------------------------
$GactExe = Join-Path $Prefix 'gact.exe'

if ($ClioRef -and -not $GactRef) {
    Say "Building CLIO-branded TUI from clio-agent submodule"
    Push-Location (Join-Path $Prefix 'clio-agent')
    RunNative bash @('./scripts/build_clio_tui.sh', $GactExe)
    Pop-Location
} elseif ($GactRef) {
    Say "Cloning gact-tui at $GactRef (source-build mode)"
    $gactSrc = Join-Path $Prefix 'gact-tui'
    RemoveTree $gactSrc
    RunNative git @('clone', '--quiet', '--branch', $GactRef, '--depth', '1', $GactRepo, $gactSrc)
    Say "Building CLIO-branded TUI"
    Push-Location (Join-Path $Prefix 'clio-agent')
    $env:GACT_TUI_ROOT = $gactSrc
    RunNative bash @('./scripts/build_clio_tui.sh', $GactExe)
    Remove-Item Env:\GACT_TUI_ROOT -ErrorAction SilentlyContinue
    Pop-Location
} else {
    $tag = $GactVersion
    if ($tag -eq 'latest') {
        if ($ClioVersion) {
            $tag = "v$ClioVersion"
        } else {
            Say "Resolving latest clio-agent release"
            $rel = Invoke-RestMethod -UseBasicParsing -Uri 'https://api.github.com/repos/iowarp/clio-agent/releases/latest'
            $tag = $rel.tag_name
            if (-not $tag) { Die "couldn't resolve clio-agent latest release tag" }
        }
    }
    # Pick the native TUI for the host CPU. Windows on ARM can run the x64
    # binary via emulation, but ship the native arm64 build when on ARM.
    $cpu = "$env:PROCESSOR_ARCHITECTURE $env:PROCESSOR_ARCHITEW6432"
    if ($cpu -match 'ARM64') { $asset = 'clio-tui-windows-arm64.exe' }
    else                     { $asset = 'clio-tui-windows-amd64.exe' }
    $url   = "https://github.com/iowarp/clio-agent/releases/download/$tag/$asset"
    Say "Downloading $asset from clio-agent $tag"
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $GactExe

    # Web UI bundle (powers `clio --web`): optional, best-effort. The release
    # ships clio-web-<version>.zip containing a clio-web-<version>/ dist dir;
    # unpack it into $Prefix\clio-agent\web, which the launcher serves
    # same-origin. Mirrors install.sh.
    $webver = $tag -replace '^v', ''
    $webUrl = "https://github.com/iowarp/clio-agent/releases/download/$tag/clio-web-$webver.zip"
    $webZip = Join-Path $Prefix 'clio-web.zip'
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $webUrl -OutFile $webZip -ErrorAction Stop
        $webTmp = Join-Path $Prefix '_webtmp'
        $webDir = Join-Path $Prefix 'clio-agent\web'
        RemoveTree $webTmp
        RemoveTree $webDir
        Expand-Archive -LiteralPath $webZip -DestinationPath $webTmp -Force
        New-Item -ItemType Directory -Force -Path $webDir | Out-Null
        $webInner = Join-Path $webTmp "clio-web-$webver"
        if (Test-Path $webInner) {
            Copy-Item (Join-Path $webInner '*') $webDir -Recurse -Force
        } else {
            Copy-Item (Join-Path $webTmp '*') $webDir -Recurse -Force
        }
        Remove-Item $webZip -Force -ErrorAction SilentlyContinue
        Remove-Item $webTmp -Recurse -Force -ErrorAction SilentlyContinue
        Say "Web UI bundle installed (run: clio --web)"
    } catch {
        Remove-Item $webZip -Force -ErrorAction SilentlyContinue
        Warn "web UI bundle unavailable for $tag - 'clio --web' disabled until reinstalled with it"
    }
}

# ---------- launcher + uninstaller ------------------------------------
$launcherRef = if ($ClioRef) {
    $ClioRef
} elseif ($ClioInstallerRef) {
    $ClioInstallerRef
} elseif ($InstalledClioVersion) {
    "v$InstalledClioVersion"
} else {
    'main'
}
$Raw         = "https://raw.githubusercontent.com/iowarp/clio-agent/$launcherRef/install"

$Launcher = Join-Path $BinDir 'clio.cmd'
Say "Installing launcher: $Launcher"
if ($ClioRef) {
    Copy-Item (Join-Path $Prefix 'clio-agent\install\clio.ps1') (Join-Path $BinDir 'clio.ps1') -Force
    Copy-Item (Join-Path $Prefix 'clio-agent\install\clio.cmd') $Launcher -Force
} else {
    Invoke-WebRequest -UseBasicParsing -Uri "$Raw/clio.ps1" -OutFile (Join-Path $BinDir 'clio.ps1')
    Invoke-WebRequest -UseBasicParsing -Uri "$Raw/clio.cmd" -OutFile $Launcher
}

Say "Installing uninstaller: $Prefix\uninstall.ps1"
if ($ClioRef) {
    Copy-Item (Join-Path $Prefix 'clio-agent\install\uninstall.ps1') (Join-Path $Prefix 'uninstall.ps1') -Force
} else {
    Invoke-WebRequest -UseBasicParsing -Uri "$Raw/uninstall.ps1" -OutFile (Join-Path $Prefix 'uninstall.ps1')
}

# ---------- finishing notes -------------------------------------------
Say "Done."
$clioSrc = if ($ClioRef) { "source: $ClioRef" } else { "PyPI: $(if ($ClioVersion) { $ClioVersion } else { 'latest' })" }
$gactSrc = if ($GactRef) { "source: $GactRef" } else { "release: $tag" }
Write-Host ""
Write-Host "Installed to:   $Prefix"
Write-Host "Launcher:       $Launcher"
Write-Host "clio-agent:     $clioSrc"
Write-Host "gact:           $gactSrc"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Make sure $BinDir is on your PATH (it usually is on Win 10/11)."
Write-Host "  2. Run:   clio"
Write-Host "  3. Manage the server: clio status | clio restart | clio logs"
Write-Host "  4. Tab-completion:    clio completion powershell >> `$PROFILE"
Write-Host "  5. Uninstall:         clio uninstall   (add -Purge to drop config)"
