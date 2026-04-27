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
$ClioRef   = if ($env:CLIO_REF)     { $env:CLIO_REF }     else { 'v0.3.1' }
$GactRef   = if ($env:GACT_REF)     { $env:GACT_REF }     else { 'v0.2.1' }
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

# ---------- launcher ---------------------------------------------------
$Launcher = Join-Path $BinDir 'clio.cmd'
Say "Writing launcher: $Launcher"
@"
@echo off
setlocal
if "%CLIO_PORT%"=="" set CLIO_PORT=17800
if "%CLIO_PREFIX%"=="" set CLIO_PREFIX=$Prefix

curl -sf -m 1 http://127.0.0.1:%CLIO_PORT%/v1/health >NUL 2>&1
if errorlevel 1 (
  echo Starting CLIO server on :%CLIO_PORT%
  start "clio-server" /B "%CLIO_PREFIX%\clio-agent\.venv\Scripts\clio-agent-gact.exe" --port %CLIO_PORT% > "%CLIO_PREFIX%\clio-server.log" 2>&1
  timeout /t 4 /nobreak >NUL
)

set GACT_BACKEND=http://127.0.0.1:%CLIO_PORT%
"%CLIO_PREFIX%\gact.exe" --no-intro %*
"@ | Set-Content -Path $Launcher -Encoding ASCII

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
Write-Host "  3. Mid-session swap: Ctrl+S -> Settings -> Model -> Change provider..."
