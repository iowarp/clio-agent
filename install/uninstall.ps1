# CLIO uninstaller (Windows / PowerShell).
#
# Undoes install.ps1: stops the server, removes the launcher, and
# removes known application payloads. Pass -Purge to also remove Agent
# configuration. Data, state, and other products are retained.
#
#   Flags:
#     -Yes     skip the confirmation prompt (non-interactive)
#     -Purge   also remove Agent configuration (including stored credentials)
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

if ($env:CLIO_PREFIX)  { $Prefix = $env:CLIO_PREFIX } else { $Prefix = Join-Path $(if ($env:CLIO_AGENT_DATA_DIR) { $env:CLIO_AGENT_DATA_DIR } elseif ($env:CLIO_AGENT_HOME) { Join-Path $env:CLIO_AGENT_HOME 'data' } elseif ($env:CLIO_USER_DIR) { Join-Path $env:CLIO_USER_DIR 'data' } else { Join-Path $env:LOCALAPPDATA 'clio-agent\data' }) 'app' }
# Continue a pre-namespace installation until it is explicitly migrated.
$LegacyPrefix = Join-Path $env:LOCALAPPDATA 'clio'
if (-not $env:CLIO_PREFIX -and -not $env:CLIO_AGENT_HOME -and -not $env:CLIO_AGENT_DATA_DIR -and -not $env:CLIO_USER_DIR -and -not (Test-Path -LiteralPath (Join-Path $Prefix 'clio-agent\.venv')) -and (Test-Path -LiteralPath (Join-Path $LegacyPrefix 'clio-agent\.venv'))) { $Prefix = $LegacyPrefix }
if ($env:CLIO_PORT)    { $Port   = [int]$env:CLIO_PORT } else { $Port = 17800 }
if ($env:CLIO_BIN_DIR) { $BinDir = $env:CLIO_BIN_DIR } else { $BinDir = Join-Path $HOME 'AppData\Local\Microsoft\WindowsApps' }

$ClioState   = $Prefix
$ClioConfig  = Join-Path $HOME '.config\clio-agent'
$AgentPython = Join-Path $Prefix 'clio-agent\.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $AgentPython) {
    $ResolvedState = & $AgentPython -c 'from clio_agent.paths import user_state_dir; print(user_state_dir())' 2>$null
    if ($LASTEXITCODE -eq 0 -and $ResolvedState) { $ClioState = $ResolvedState.Trim() }
    $ResolvedConfig = & $AgentPython -c 'from clio_agent.paths import user_config_dir; print(user_config_dir())' 2>$null
    if ($LASTEXITCODE -eq 0 -and $ResolvedConfig) { $ClioConfig = $ResolvedConfig.Trim() }
}
$PidFile = Join-Path $ClioState 'clio-server.pid'
if (-not (Test-Path -LiteralPath $PidFile)) { $PidFile = Join-Path $Prefix 'clio-server.pid' }
$LauncherCmd = Join-Path $BinDir 'clio.cmd'
$LauncherPs1 = Join-Path $BinDir 'clio.ps1'

if ($Purge) {
    $ResolvedConfig = [IO.Path]::GetFullPath($ClioConfig).TrimEnd('\', '/')
    if ($ResolvedConfig -notmatch '[\\/]clio-agent([\\/]config)?$') {
        throw "Refusing to purge a custom config root: $ResolvedConfig. Remove its contents explicitly after review."
    }
}

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
Write-Host "  application payloads in: $Prefix (unknown files retained)"
Write-Host "  launcher:        $LauncherCmd"
Write-Host "  launcher:        $LauncherPs1"
if ($Purge) {
    Write-Host "  clio config:     $ClioConfig  (will be removed)"
} else {
    Write-Host "  clio config:     $ClioConfig  (kept; pass -Purge to remove)"
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
    Say "Removing installed application payloads in $Prefix"
    $ResolvedPrefix = [IO.Path]::GetFullPath($Prefix)
    foreach ($relative in @('clio-agent\.venv', 'clio-agent\web', 'gact.exe', 'uninstall.ps1')) {
        $OwnedPath = [IO.Path]::GetFullPath((Join-Path $ResolvedPrefix $relative))
        if (-not $OwnedPath.StartsWith($ResolvedPrefix + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid application path' }
        Remove-Item -LiteralPath $OwnedPath -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path $Prefix) {
        Warn "Retained user data and unknown files in $Prefix."
    }
}
if ($Purge) {
    if (Test-Path -LiteralPath $ResolvedConfig) { Say "Removing $ResolvedConfig"; Remove-Item -LiteralPath $ResolvedConfig -Recurse -Force }
    # gact owns its own configuration; retain it.
}

Say "CLIO uninstalled."
if (-not $Purge -and (Test-Path $ClioConfig)) {
    Write-Host "  Agent config kept ($ClioConfig) - re-run with -Purge to remove"
}
exit 0
