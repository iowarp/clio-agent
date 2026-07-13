# CLIO uninstaller (Windows / PowerShell).
#
# Undoes install.ps1: stops the server, removes the launcher, and
# deletes the install prefix. Pass -Purge to also remove CLIO's user
# state + config (~\.config\clio-agent and ~\.config\gact).
#
#   Flags:
#     -Yes     skip the confirmation prompt (non-interactive)
#     -Purge   also remove ~\.config\clio-agent (sessions, workspaces,
#              blueprints, ARC) AND ~\.config\gact (TUI config/themes)
#
#   Environment overrides (must match the install):
#     CLIO_PREFIX   install root      (default: $HOME\AppData\Local\clio)
#     CLIO_PORT     server port       (default: 17800)
#     CLIO_BIN_DIR  launcher location (default: ...\WindowsApps)
#
# Self-relaunch: the uninstaller copies itself to a temp file and
# re-execs from there (-Relaunched) so it can delete its own original
# location (the clio-agent checkout lives inside CLIO_PREFIX).
param(
    [switch]$Yes,
    [switch]$Purge,
    [switch]$Relaunched
)

$ErrorActionPreference = 'Stop'

if ($env:CLIO_PREFIX)  { $Prefix = $env:CLIO_PREFIX } else { $Prefix = Join-Path $HOME 'AppData\Local\clio' }
if ($env:CLIO_PORT)    { $Port   = [int]$env:CLIO_PORT } else { $Port = 17800 }
if ($env:CLIO_BIN_DIR) { $BinDir = $env:CLIO_BIN_DIR } else { $BinDir = Join-Path $HOME 'AppData\Local\Microsoft\WindowsApps' }

$PidFile     = Join-Path $Prefix 'clio-server.pid'
$ClioConfig  = Join-Path $HOME '.config\clio-agent'
$GactConfig  = Join-Path $HOME '.config\gact'
$LauncherCmd = Join-Path $BinDir 'clio.cmd'
$LauncherPs1 = Join-Path $BinDir 'clio.ps1'

function Say  ($m) { Write-Host "==> $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "!! $m"  -ForegroundColor Yellow }

# ---- self-relaunch from temp so we can delete our own location ------
if (-not $Relaunched) {
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("clio-uninstall-" + ([guid]::NewGuid().ToString('N')) + ".ps1")
    Copy-Item -Path $PSCommandPath -Destination $tmp -Force
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $tmp, '-Relaunched')
    if ($Yes)   { $argList += '-Yes' }
    if ($Purge) { $argList += '-Purge' }
    & powershell @argList
    $code = $LASTEXITCODE
    Remove-Item $tmp -ErrorAction SilentlyContinue
    exit $code
}

# ---- plan ------------------------------------------------------------
Write-Host ""
Write-Host "CLIO uninstall - the following will be removed:"
Write-Host "  install prefix:  $Prefix"
Write-Host "  launcher:        $LauncherCmd"
Write-Host "  launcher:        $LauncherPs1"
if ($Purge) {
    Write-Host "  clio config:     $ClioConfig  (will be removed)"
    Write-Host "  gact config:     $GactConfig  (will be removed)"
} else {
    Write-Host "  clio config:     $ClioConfig  (kept; pass -Purge to remove)"
    Write-Host "  gact config:     $GactConfig  (kept; pass -Purge to remove)"
}
Write-Host ""

if (-not $Yes) {
    $ans = Read-Host "Proceed? [y/N]"
    if ($ans -ne 'y' -and $ans -ne 'Y') { Warn "aborted"; exit 1 }
}

# ---- stop the server -------------------------------------------------
$serverPid = $null
if (Test-Path $PidFile) {
    $p = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($p) {
        $p = $p.Trim()
        if ($p -and (Get-Process -Id $p -ErrorAction SilentlyContinue)) { $serverPid = [int]$p }
    }
}
if (-not $serverPid) {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($conn) { $serverPid = [int]$conn.OwningProcess }
}
if ($serverPid) {
    Say "Stopping CLIO server (pid $serverPid)"
    & taskkill /PID $serverPid /T /F | Out-Null
    Start-Sleep -Milliseconds 500
} else {
    Say "No running CLIO server found"
}

# Sweep any leftover server processes started from this prefix (zombies
# that never registered a pidfile - exactly the state install.ps1 hit).
$escaped = [regex]::Escape($Prefix)
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -match 'clio-agent(-gact)?' -and $_.CommandLine -match $escaped } |
    ForEach-Object {
        Say "Killing leftover process (pid $($_.ProcessId))"
        & taskkill /PID $_.ProcessId /T /F | Out-Null
    }

# ---- remove files ----------------------------------------------------
foreach ($f in @($LauncherCmd, $LauncherPs1)) {
    if (Test-Path $f) { Say "Removing $f"; Remove-Item $f -Force -ErrorAction SilentlyContinue }
}
if (Test-Path $Prefix) {
    Say "Removing $Prefix"
    Remove-Item $Prefix -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path $Prefix) {
        Warn "could not fully remove $Prefix - a file may still be locked; retry after closing CLIO/gact."
    }
}
if ($Purge) {
    if (Test-Path $ClioConfig) { Say "Removing $ClioConfig"; Remove-Item $ClioConfig -Recurse -Force -ErrorAction SilentlyContinue }
    if (Test-Path $GactConfig) { Say "Removing $GactConfig"; Remove-Item $GactConfig -Recurse -Force -ErrorAction SilentlyContinue }
}

Say "CLIO uninstalled."
if (-not $Purge -and ((Test-Path $ClioConfig) -or (Test-Path $GactConfig))) {
    Write-Host "  config kept ($ClioConfig, $GactConfig) - re-run with -Purge to remove"
}
exit 0
