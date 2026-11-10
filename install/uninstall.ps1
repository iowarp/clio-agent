# CLIO uninstaller (Windows / PowerShell).
#
# Undoes install.ps1: stops the server, removes the launcher, and
# removes known application payloads. Pass -Purge to also remove Agent
# configuration. Data, state, caches, and other products are retained unless
# their scope is named, as in uninstall.sh.
#
#   Flags:
#     -Yes          skip the confirmation prompt (non-interactive)
#     -Purge        also remove Agent configuration (including stored credentials)
#     -PurgeCaches  also remove the Agent cache root (document runtime, LibreOffice,
#                   tool caches; default %LOCALAPPDATA%\clio-agent\cache). It is
#                   rebuilt on the next launch.
#     -PurgeData    also remove the Agent data and state roots: conversations,
#                   workspaces, memory, the clio-core store, service environments
#                   and a source install's clone. This cannot be undone. Kept while
#                   a clio-core daemon is running.
#
#   Remove everything:  -Purge -PurgeCaches -PurgeData
#   CLIO Desktop is removed from Settings > Apps (its own uninstaller).
#
#   Environment overrides (must match the install):
#     CLIO_PREFIX   install root      (default: $HOME\AppData\Local\clio)
#     CLIO_PORT     server port       (default: gact.port in config.yaml, else 17800)
#     CLIO_BIN_DIR  launcher location (default: ...\WindowsApps)
#
# Self-relaunch: the uninstaller copies itself to a temp file and
# re-execs from there (-Relaunched) so it can delete its own original
# location (the clio-agent checkout lives inside CLIO_PREFIX).
param(
    [switch]$Yes,
    [switch]$Purge,
    [switch]$PurgeCaches,
    [switch]$PurgeData,
    [switch]$Relaunched
)

$ErrorActionPreference = 'Stop'

if ($env:CLIO_PREFIX)  { $Prefix = $env:CLIO_PREFIX } else { $Prefix = Join-Path $(if ($env:CLIO_AGENT_DATA_DIR) { $env:CLIO_AGENT_DATA_DIR } elseif ($env:CLIO_AGENT_HOME) { Join-Path $env:CLIO_AGENT_HOME 'data' } elseif ($env:CLIO_USER_DIR) { Join-Path $env:CLIO_USER_DIR 'data' } else { Join-Path $env:LOCALAPPDATA 'clio-agent\data' }) 'app' }
# Continue a pre-namespace installation until it is explicitly migrated.
$LegacyPrefix = Join-Path $env:LOCALAPPDATA 'clio'
if (-not $env:CLIO_PREFIX -and -not $env:CLIO_AGENT_HOME -and -not $env:CLIO_AGENT_DATA_DIR -and -not $env:CLIO_USER_DIR -and -not (Test-Path -LiteralPath (Join-Path $Prefix 'clio-agent\.venv')) -and (Test-Path -LiteralPath (Join-Path $LegacyPrefix 'clio-agent\.venv'))) { $Prefix = $LegacyPrefix }
if ($env:CLIO_PORT)    { $Port   = [int]$env:CLIO_PORT } else { $Port = 17800 }
if ($env:CLIO_BIN_DIR) { $BinDir = $env:CLIO_BIN_DIR } else { $BinDir = Join-Path $HOME 'AppData\Local\Microsoft\WindowsApps' }

# The native Agent roots, as clio_agent.paths resolves them without an installed Agent.
function Get-AgentRoot([string]$Role) {
    $direct = [Environment]::GetEnvironmentVariable("CLIO_AGENT_$($Role.ToUpper())_DIR")
    if ($direct) { return $direct }
    if ($env:CLIO_AGENT_HOME) { return Join-Path $env:CLIO_AGENT_HOME $Role }
    if ($env:CLIO_USER_DIR) { if ($Role -eq 'config') { return $env:CLIO_USER_DIR }; return Join-Path $env:CLIO_USER_DIR $Role }
    if ($Role -eq 'config') { return Join-Path $env:APPDATA 'clio-agent\config' }
    return Join-Path $env:LOCALAPPDATA "clio-agent\$Role"
}
$ClioState   = $Prefix
$ClioConfig  = Get-AgentRoot 'config'
$ClioCache   = Get-AgentRoot 'cache'
$ClioData    = Get-AgentRoot 'data'
$DataState   = Get-AgentRoot 'state'
$HostState   = Join-Path $env:LOCALAPPDATA 'clio-agent\state'
$AgentPython = Join-Path $Prefix 'clio-agent\.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $AgentPython) {
    $ResolvedState = & $AgentPython -c 'from clio_agent.paths import user_state_dir; print(user_state_dir())' 2>$null
    if ($LASTEXITCODE -eq 0 -and $ResolvedState) { $ClioState = $ResolvedState.Trim() }
    $ResolvedConfig = & $AgentPython -c 'from clio_agent.paths import user_config_dir; print(user_config_dir())' 2>$null
    if ($LASTEXITCODE -eq 0 -and $ResolvedConfig) { $ClioConfig = $ResolvedConfig.Trim() }
    if ($PurgeCaches -or $PurgeData) {
        $Resolved = & $AgentPython -c 'from clio_agent.paths import host_state_dir, user_cache_dir, user_data_dir, user_state_dir; print(user_cache_dir()); print(user_data_dir()); print(user_state_dir()); print(host_state_dir())' 2>$null
        if ($LASTEXITCODE -eq 0 -and @($Resolved).Count -eq 4) {
            $ClioCache, $ClioData, $DataState, $HostState = @($Resolved | ForEach-Object { $_.Trim() })
        }
    }
    # Windows PowerShell 5.1 turns native stderr into a terminating error under 'Stop'.
    $SavedPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $ResolvedPort = & $AgentPython -m clio_agent.server_port 2>$null } finally { $ErrorActionPreference = $SavedPreference }
    $ParsedPort = 0
    if ($LASTEXITCODE -eq 0 -and [int]::TryParse("$ResolvedPort".Trim(), [ref]$ParsedPort)) { $Port = $ParsedPort }
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
if ($PurgeCaches) {
    $ClioCache = [IO.Path]::GetFullPath($ClioCache).TrimEnd('\', '/')
    if ($ClioCache -notmatch '[\\/]clio-agent([\\/]cache)?$') {
        throw "Refusing to purge a custom cache root: $ClioCache. Remove its contents explicitly after review."
    }
}
$PurgeDataDirs = @()
if ($PurgeData) {
    foreach ($dir in @($ClioData, $DataState)) {
        $full = [IO.Path]::GetFullPath($dir).TrimEnd('\', '/')
        if ($full -notmatch '[\\/]clio-agent([\\/](data|state))?$') {
            throw "Refusing to purge a custom data or state root: $full. Remove it explicitly after review."
        }
        if ($PurgeDataDirs -notcontains $full) { $PurgeDataDirs += $full }
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
    if ($PurgeCaches) { $argList += '-PurgeCaches' }
    if ($PurgeData) { $argList += '-PurgeData' }
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
if ($PurgeCaches) {
    Write-Host "  clio caches:     $ClioCache  (-PurgeCaches)"
} else {
    Write-Host "  clio caches:     $ClioCache  (kept; pass -PurgeCaches to remove)"
}
if ($PurgeData) {
    foreach ($dir in $PurgeDataDirs) { Write-Host "  clio data:       $dir  (-PurgeData: conversations, workspaces, memory)" }
} else {
    Write-Host "  clio data:       $ClioData  (kept; pass -PurgeData to remove)"
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
# A live clio-core daemon still has the store open; its pid is recorded per host.
function Get-CoreDaemonPid {
    foreach ($pidFile in @(Get-ChildItem -Path (Join-Path $HostState 'core-hosts\*\clio-runtime.pid') -File -ErrorAction SilentlyContinue)) {
        $first = "$(Get-Content -LiteralPath $pidFile.FullName -ErrorAction SilentlyContinue | Select-Object -First 1)".Trim().Split()[0]
        $corePid = 0
        if ([int]::TryParse("$first", [ref]$corePid) -and (Get-Process -Id $corePid -ErrorAction SilentlyContinue)) { return $corePid }
    }
    return $null
}
if ($Purge) {
    if (Test-Path -LiteralPath $ResolvedConfig) { Say "Removing $ResolvedConfig"; Remove-Item -LiteralPath $ResolvedConfig -Recurse -Force }
    # gact owns its own configuration; retain it.
}
if ($PurgeData) {
    $corePid = Get-CoreDaemonPid
    if ($corePid) {
        Warn "The clio-core daemon (pid $corePid) is running; kept $($PurgeDataDirs -join ', ')."
    } else {
        foreach ($dir in $PurgeDataDirs) {
            if (Test-Path -LiteralPath $dir) {
                Say "Removing $dir"
                Remove-Item -LiteralPath $dir -Recurse -Force
                $parent = Split-Path -Parent $dir
                if ((Split-Path -Leaf $parent) -eq 'clio-agent' -and -not (Get-ChildItem -LiteralPath $parent -Force -ErrorAction SilentlyContinue)) { Remove-Item -LiteralPath $parent -Force -ErrorAction SilentlyContinue }
            }
        }
    }
}
if ($PurgeCaches -and (Test-Path -LiteralPath $ClioCache)) {
    Say "Removing $ClioCache"
    Remove-Item -LiteralPath $ClioCache -Recurse -Force
}

Say "CLIO uninstalled."
if (-not $Purge -and (Test-Path $ClioConfig)) {
    Write-Host "  Agent config kept ($ClioConfig) - re-run with -Purge to remove"
}
if (-not $PurgeCaches -and (Test-Path -LiteralPath $ClioCache)) {
    Write-Host "  Agent caches kept ($ClioCache) - re-run with -PurgeCaches to remove"
}
exit 0
