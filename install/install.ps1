# CLIO installer (Windows / PowerShell).
#
# Default: pulls clio-agent from PyPI and downloads a prebuilt
# gact.exe from gact-tui's GitHub Releases. No `git` or `go` required;
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
#   GACT_VERSION       pin gact release tag   (default: latest)
#   CLIO_REF           clio-agent branch      (default: release mode)
#   GACT_REF           gact-tui branch        (default: release mode)
#   CLIO_GIT_PROTOCOL  https | ssh            (default: https; only used
#                                              in source-build mode)

$ErrorActionPreference = 'Stop'

function Say  ($m) { Write-Host "==> $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "!! $m"  -ForegroundColor Yellow }
function Die  ($m) { Write-Host "xx $m"  -ForegroundColor Red; exit 1 }
function Have ($cmd) { return $null -ne (Get-Command $cmd -ErrorAction SilentlyContinue) }

# ---------- defaults ---------------------------------------------------
$Prefix      = if ($env:CLIO_PREFIX)       { $env:CLIO_PREFIX }      else { Join-Path $HOME 'AppData\Local\clio' }
$BinDir      = if ($env:CLIO_BIN_DIR)      { $env:CLIO_BIN_DIR }     else { Join-Path $HOME 'AppData\Local\Microsoft\WindowsApps' }
$ClioVersion = $env:CLIO_VERSION
$GactVersion = if ($env:GACT_VERSION)      { $env:GACT_VERSION }     else { 'latest' }
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
}
if ($GactRef) {
    if (-not (Have git)) { Die "git required when GACT_REF is set (source-build mode)" }
    if (-not (Have go))  { Die "go (>= 1.26) required to build gact from source" }
}

New-Item -ItemType Directory -Force -Path $Prefix, $BinDir | Out-Null

# ---------- install clio-agent ----------------------------------------
$Venv = Join-Path $Prefix 'clio-agent\.venv'

if ($ClioRef) {
    Say "Cloning clio-agent at $ClioRef (source-build mode)"
    Remove-Item -Recurse -Force (Join-Path $Prefix 'clio-agent') -ErrorAction SilentlyContinue
    git clone --quiet --branch $ClioRef --depth 1 $ClioRepo (Join-Path $Prefix 'clio-agent')
    Say "Installing clio-agent deps (uv sync --extra api)"
    Push-Location (Join-Path $Prefix 'clio-agent')
    uv sync --extra api
    Pop-Location
} else {
    $pkgSpec = if ($ClioVersion) { "clio-agent[api]==$ClioVersion" } else { 'clio-agent[api]' }
    Say "Installing $pkgSpec from PyPI"
    Remove-Item -Recurse -Force (Join-Path $Prefix 'clio-agent') -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path (Join-Path $Prefix 'clio-agent') | Out-Null
    if ($PyInstall -eq 'uv') {
        uv venv --python ">=3.12" $Venv | Out-Null
        uv pip install --quiet --python (Join-Path $Venv 'Scripts\python.exe') $pkgSpec
    } else {
        python -m venv $Venv
        & (Join-Path $Venv "Scripts\$PyInstall.exe") install --quiet --upgrade pip
        & (Join-Path $Venv "Scripts\$PyInstall.exe") install --quiet $pkgSpec
    }
}

# ---------- install gact ----------------------------------------------
$GactExe = Join-Path $Prefix 'gact.exe'

if ($GactRef) {
    Say "Cloning gact-tui at $GactRef (source-build mode)"
    $gactSrc = Join-Path $Prefix 'gact-tui'
    Remove-Item -Recurse -Force $gactSrc -ErrorAction SilentlyContinue
    git clone --quiet --branch $GactRef --depth 1 $GactRepo $gactSrc
    Say "Building gact"
    Push-Location (Join-Path $gactSrc 'tui')
    go build -o $GactExe .
    Pop-Location
} else {
    $tag = $GactVersion
    if ($tag -eq 'latest') {
        Say "Resolving latest gact-tui release"
        $rel = Invoke-RestMethod -UseBasicParsing -Uri 'https://api.github.com/repos/iowarp/gact-tui/releases/latest'
        $tag = $rel.tag_name
        if (-not $tag) { Die "couldn't resolve gact-tui latest release tag" }
    }
    $asset = 'gact-windows-amd64.exe'
    $url   = "https://github.com/iowarp/gact-tui/releases/download/$tag/$asset"
    Say "Downloading $asset from gact-tui $tag"
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $GactExe
}

# ---------- launcher + uninstaller ------------------------------------
$launcherRef = if ($ClioRef) { $ClioRef } else { 'main' }
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
