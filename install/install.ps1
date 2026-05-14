# CLIO + GACT TUI installer (Windows / PowerShell).
#
# One-liner:
#   irm https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.ps1 | iex
#
# Honors environment overrides:
#   $env:CLIO_PREFIX  install root (default: $HOME\AppData\Local\clio)
#   $env:CLIO_REF     clio-agent git ref/tag (default: v0.3.1)
#   $env:GACT_REF     gact-tui git ref/tag    (default: v0.2.1)
#   $env:CLIO_BIN_DIR launcher dir (default: $HOME\AppData\Local\Microsoft\WindowsApps)

$ErrorActionPreference = 'Stop'

function Say($msg)  { Write-Host "==> $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "!! $msg"  -ForegroundColor Yellow }
function Die($msg)  { Write-Host "xx $msg"  -ForegroundColor Red; exit 1 }

# ---------- defaults ---------------------------------------------------
$Prefix    = if ($env:CLIO_PREFIX)  { $env:CLIO_PREFIX }  else { Join-Path $HOME 'AppData\Local\clio' }
$BinDir    = if ($env:CLIO_BIN_DIR) { $env:CLIO_BIN_DIR } else { Join-Path $HOME 'AppData\Local\Microsoft\WindowsApps' }
# Default to the main branches so a bare one-liner tracks the latest
# released state. GactRef stays on 'clio' until iowarp/gact-tui#12
# (clio -> main) merges, after which it can move to 'main' too.
$ClioRef   = if ($env:CLIO_REF)     { $env:CLIO_REF }     else { 'main' }
$GactRef   = if ($env:GACT_REF)     { $env:GACT_REF }     else { 'clio' }
# Default to HTTPS for the one-liner UX, but allow override to SSH for
# users who only have SSH access (and for the period while the repos
# are still private — anonymous HTTPS returns 404). Set
#   $env:CLIO_GIT_PROTOCOL = 'ssh'
# to switch both URLs to git@github.com:.../...git form.
$Protocol = if ($env:CLIO_GIT_PROTOCOL) { $env:CLIO_GIT_PROTOCOL } else { 'https' }
switch ($Protocol) {
    'https' {
        $ClioRepo = 'https://github.com/iowarp/clio-agent.git'
        $GactRepo = 'https://github.com/JaimeCernuda/gact-tui.git'
    }
    'ssh' {
        $ClioRepo = 'git@github.com:iowarp/clio-agent.git'
        $GactRepo = 'git@github.com:JaimeCernuda/gact-tui.git'
    }
    default { Die "CLIO_GIT_PROTOCOL must be 'https' or 'ssh' (got: $Protocol)" }
}

# ---------- prerequisite checks ---------------------------------------
function Have($cmd) {
    return $null -ne (Get-Command $cmd -ErrorAction SilentlyContinue)
}

$missing = @()
if (-not (Have git)) { $missing += 'git'           }
if (-not (Have uv))  { $missing += 'uv'            }
if (-not (Have go))  { $missing += 'go (>=1.26)'   }

if ($missing.Count -gt 0) {
    Warn "Missing prerequisites: $($missing -join ', ')"
    Write-Host ""
    Write-Host "Install them via winget, then re-run:" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  winget install Git.Git"
    Write-Host "  winget install astral-sh.uv"
    Write-Host "  winget install GoLang.Go"
    Write-Host ""
    Write-Host "Then:" -ForegroundColor Yellow
    Write-Host "  irm https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.ps1 | iex"
    exit 1
}

Say "Detected: $(go version)"
Say "uv: $(uv --version)"

# ---------- clone + build ---------------------------------------------
New-Item -ItemType Directory -Force -Path $Prefix, $BinDir | Out-Null
Set-Location $Prefix

function CloneOrUpdate($repo, $dir, $ref) {
    if (Test-Path "$dir/.git") {
        Say "Updating $dir -> $ref"
        git -C $dir fetch --tags --quiet
        git -C $dir checkout --quiet $ref
    } else {
        Say "Cloning $dir -> $ref"
        git clone --quiet --branch $ref --depth 1 $repo $dir
    }
}

CloneOrUpdate $ClioRepo 'clio-agent' $ClioRef
CloneOrUpdate $GactRepo 'gact-tui'   $GactRef

Say "Installing CLIO Python deps (uv sync --extra api)"
Push-Location "$Prefix/clio-agent"
uv sync --extra api
Pop-Location

Say "Building gact TUI"
Push-Location "$Prefix/gact-tui/tui"
go build -o "$Prefix/gact.exe" .
Pop-Location

# ---------- launcher + uninstaller -------------------------------------
# The launcher is a real CLI (clio.ps1) checked into the clio-agent
# repo under install/. We copy it from the freshly-cloned checkout
# rather than generating it inline, so there is one source of truth and
# `clio start|stop|restart|status|logs|doctor|report` ship with it.
$InstallSrc = Join-Path $Prefix 'clio-agent\install'
$Launcher   = Join-Path $BinDir 'clio.cmd'

Say "Installing launcher: $Launcher"
Copy-Item -Path (Join-Path $InstallSrc 'clio.ps1') -Destination (Join-Path $BinDir 'clio.ps1') -Force
Copy-Item -Path (Join-Path $InstallSrc 'clio.cmd') -Destination $Launcher -Force

Say "Installing uninstaller: $Prefix\uninstall.ps1"
Copy-Item -Path (Join-Path $InstallSrc 'uninstall.ps1') -Destination (Join-Path $Prefix 'uninstall.ps1') -Force

Say "Done."
Write-Host ""
Write-Host "Installed to:   $Prefix"
Write-Host "Launcher:       $Launcher"
Write-Host "clio-agent ref: $ClioRef"
Write-Host "gact-tui  ref:  $GactRef"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Make sure $BinDir is on your PATH (it usually is on Win 10/11)."
Write-Host "  2. Run:   clio"
Write-Host "     The TUI pops the LM-provider modal on first connect."
Write-Host "  3. Manage the server: clio status | clio restart | clio logs"
Write-Host "  4. Tab-completion:    clio completion powershell >> `$PROFILE"
Write-Host "  5. Uninstall:         clio uninstall   (add -Purge to drop config)"
