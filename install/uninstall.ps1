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

# Remove-ClioTree PATH: take a tree out of use by renaming it, and delete it after the
# uninstall in one hidden process (Start-ClioTreeRemoval). Deleting an installed
# runtime's tens of thousands of files under real-time scanning takes 30-40 s even with
# cmd's rd. A tree that cannot be renamed (a file in use) is deleted now with rd, which
# is about 2.5x faster than Remove-Item -Recurse, removes read-only files and does not
# follow junctions. Whatever rd leaves goes to Remove-Item, so a failure still surfaces
# unless -Soft.
$script:Doomed = New-Object System.Collections.Generic.List[string]
function LongPath([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    if ($full.StartsWith('\\?\')) { return $full }
    if ($full.StartsWith('\\')) { return "\\?\UNC\$($full.Substring(2))" }
    return "\\?\$full"
}
function Remove-ClioTree([string]$Path, [switch]$Soft) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    if (Test-Path -LiteralPath $Path -PathType Container) {
        $doomed = "$Path.removing-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
        try { [IO.Directory]::Move($Path, $doomed); $script:Doomed.Add($doomed); return } catch { }
        & (Join-Path $env:SystemRoot 'System32\cmd.exe') /d /c "rd /s /q `"$(LongPath $Path)`" 2>nul"
    }
    if (Test-Path -LiteralPath $Path) {
        if ($Soft) { Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue }
        else { Remove-Item -LiteralPath $Path -Recurse -Force }
    }
}
# Start-ClioTreeRemoval: delete the renamed trees in one hidden process. A tree renamed
# inside another one moved with it, so only trees still at their names are passed. A
# clio-agent folder left holding nothing but these trees goes after them.
function Start-ClioTreeRemoval {
    $trees = @($script:Doomed | Where-Object { Test-Path -LiteralPath $_ })
    if (-not $trees.Count) { return }
    $steps = @($trees | ForEach-Object { "rd /s /q `"$(LongPath $_)`"" })
    $parents = @($trees | ForEach-Object { Split-Path -Parent $_ } | Sort-Object -Unique)
    foreach ($parent in $parents) {
        if ((Split-Path -Leaf $parent) -ne 'clio-agent') { continue }
        $kept = @(Get-ChildItem -LiteralPath $parent -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -notmatch '\.removing-[0-9a-f]{8}$' })
        if (-not $kept.Count) { $steps += "rd `"$(LongPath $parent)`"" }
    }
    $deleter = Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\cmd.exe') -ArgumentList '/d', '/c', ("(" + ($steps -join ' & ') + ") 2>nul") -WindowStyle Hidden -PassThru
    Say "Deleting the removed folders' files in the background (pid $($deleter.Id))"
}

# Every process in the trees rooted at ROOTS, from one process snapshot. A child must
# start after its parent, so a reused parent PID does not adopt unrelated processes.
function Get-ClioProcessTree([int[]]$Roots, $Snapshot) {
    $byPid = @{}
    foreach ($p in $Snapshot) { $byPid[[int]$p.ProcessId] = $p }
    $tree = @{}
    $queue = New-Object System.Collections.Generic.Queue[int]
    foreach ($r in $Roots) { if ($r -and -not $tree.ContainsKey($r)) { $tree[$r] = $true; $queue.Enqueue($r) } }
    while ($queue.Count -gt 0) {
        $parent = $queue.Dequeue()
        $parentStart = if ($byPid.ContainsKey($parent)) { $byPid[$parent].CreationDate } else { $null }
        foreach ($p in $Snapshot) {
            $id = [int]$p.ProcessId
            if ([int]$p.ParentProcessId -ne $parent -or $tree.ContainsKey($id)) { continue }
            if ($parentStart -and $p.CreationDate -and $p.CreationDate -lt $parentStart) { continue }
            $tree[$id] = $true
            $queue.Enqueue($id)
        }
    }
    return @($tree.Keys)
}

# clio-core on Windows backs its shared memory with files in %TEMP%\clio_<user>: about
# 3 GB per runtime (port-keyed chi_* segments) plus per-process clio_<pid>_* and
# clio-<pid>-shm-* files. A runtime that is killed rather than stopped leaves them all.
# Remove only those of processes this uninstall stopped, or of this computer's recorded
# CLIO runtime once it is gone. A file still mapped by a live process cannot be deleted.
function Remove-CoreSegments([int[]]$Gone) {
    $goneSet = @{}
    foreach ($id in $Gone) { if ($id) { $goneSet[[int]$id] = $true } }
    $stateDirs = @()
    if ($env:CLIO_RUNTIME_STATE_DIR) { $stateDirs += $env:CLIO_RUNTIME_STATE_DIR }
    $stateDirs += @(Get-ChildItem -Path (Join-Path $HostState 'core-hosts\*') -Directory -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
    foreach ($dir in $stateDirs) {
        $first = "$(Get-Content -LiteralPath (Join-Path $dir 'clio-runtime.pid') -ErrorAction SilentlyContinue | Select-Object -First 1)".Trim().Split()[0]
        $recorded = 0
        if ([int]::TryParse("$first", [ref]$recorded) -and $recorded -and -not (Get-Process -Id $recorded -ErrorAction SilentlyContinue)) { $goneSet[$recorded] = $true }
    }
    if ($goneSet.Count -eq 0) { return }
    $removed = 0
    $bytes = [long]0
    foreach ($segDir in @(Get-ChildItem -Path (Join-Path ([IO.Path]::GetTempPath()) 'clio_*') -Directory -ErrorAction SilentlyContinue)) {
        $doomed = @()
        foreach ($record in @(Get-ChildItem -LiteralPath $segDir.FullName -Filter 'chi_runtime_pid_*' -File -ErrorAction SilentlyContinue)) {
            if ($record.Name -notmatch '^chi_runtime_pid_(.+)_(\d+)$') { continue }
            $owner = $Matches[1]; $port = $Matches[2]
            $runtimePid = 0
            if (-not [int]::TryParse("$(Get-Content -LiteralPath $record.FullName -ErrorAction SilentlyContinue | Select-Object -First 1)".Trim(), [ref]$runtimePid)) { continue }
            if (-not $goneSet.ContainsKey($runtimePid)) { continue }
            $doomed += @(Get-ChildItem -LiteralPath $segDir.FullName -File -ErrorAction SilentlyContinue | Where-Object {
                $_.Name -match ('^chi_[a-z_]+_' + [regex]::Escape($owner) + '_' + $port + '$') -or $_.Name -eq "clio_$port.ipc"
            })
        }
        $doomed += @(Get-ChildItem -LiteralPath $segDir.FullName -File -ErrorAction SilentlyContinue | Where-Object {
            $_.Name -match '^clio(?:_rambdev)?[-_](\d+)[-_]' -and $goneSet.ContainsKey([int]$Matches[1])
        })
        foreach ($f in $doomed) {
            $size = $f.Length
            Remove-Item -LiteralPath $f.FullName -Force -ErrorAction SilentlyContinue
            if (-not (Test-Path -LiteralPath $f.FullName)) { $removed++; $bytes += $size }
        }
    }
    if ($removed) { Say ("Removed {0} clio-core memory files ({1:N1} GB) from {2}" -f $removed, ($bytes / 1GB), [IO.Path]::GetTempPath()) }
}

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
# A recorded PID can be reused and the port can belong to another program (a dev
# server, another CLIO install), so only a process of this installation is stopped:
# it, or an ancestor started before it, runs from or names this install's prefix.
$Snapshot = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
$PrefixKey = [IO.Path]::GetFullPath($Prefix).TrimEnd('\', '/').ToLowerInvariant() + '\'
function Test-InstallProcess([int]$Id) {
    $byPid = @{}
    foreach ($p in $Snapshot) { $byPid[[int]$p.ProcessId] = $p }
    $proc = $byPid[$Id]
    for ($depth = 0; $proc -and $depth -lt 8; $depth++) {
        foreach ($text in @($proc.ExecutablePath, $proc.CommandLine)) {
            if ($text -and $text.ToLowerInvariant().Replace('/', '\').Contains($PrefixKey)) { return $true }
        }
        $parent = $byPid[[int]$proc.ParentProcessId]
        if (-not $parent -or ($proc.CreationDate -and $parent.CreationDate -and $parent.CreationDate -gt $proc.CreationDate)) { break }
        $proc = $parent
    }
    return $false
}
function Get-ProcessLabel([int]$Id) {
    $proc = $Snapshot | Where-Object { [int]$_.ProcessId -eq $Id } | Select-Object -First 1
    if ($proc -and $proc.ExecutablePath) { return "$($proc.Name), $($proc.ExecutablePath)" }
    if ($proc) { return $proc.Name }
    return 'unknown'
}
$serverPid = $null
if (Test-Path $PidFile) {
    $p = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($p) {
        $p = $p.Trim()
        if ($p -and (Get-Process -Id $p -ErrorAction SilentlyContinue)) {
            if (Test-InstallProcess ([int]$p)) { $serverPid = [int]$p }
            else { Warn "The recorded server pid $p is now another program ($(Get-ProcessLabel ([int]$p))); leaving it running" }
        }
    }
}
if (-not $serverPid) {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($conn) {
        $owner = [int]$conn.OwningProcess
        if (Test-InstallProcess $owner) { $serverPid = $owner }
        else { Warn "Port $Port is held by pid $owner ($(Get-ProcessLabel $owner)), which is not part of this installation; leaving it running" }
    }
}
# Sweep any leftover server processes started from this prefix (zombies
# that never registered a pidfile - exactly the state install.ps1 hit).
$escaped = [regex]::Escape($Prefix)
$leftovers = @($Snapshot | Where-Object { $_.CommandLine -and $_.CommandLine -match 'clio-agent(-gact)?' -and $_.CommandLine -match $escaped } | ForEach-Object { [int]$_.ProcessId })
$Stopped = @(Get-ClioProcessTree (@($serverPid) + $leftovers) $Snapshot)
if ($serverPid) {
    Say "Stopping CLIO server (pid $serverPid)"
    & cmd /d /c "taskkill /PID $serverPid /T /F >nul 2>nul"
    Start-Sleep -Milliseconds 500
} else {
    Say "No running CLIO server found"
}
foreach ($leftover in $leftovers) {
    if (-not (Get-Process -Id $leftover -ErrorAction SilentlyContinue)) { continue }
    Say "Killing leftover process (pid $leftover)"
    & cmd /d /c "taskkill /PID $leftover /T /F >nul 2>nul"
}
for ($i = 0; $i -lt 20; $i++) {
    if (-not @($Stopped | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })) { break }
    Start-Sleep -Milliseconds 250
}
Remove-CoreSegments $Stopped

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
        Remove-ClioTree $OwnedPath -Soft
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
    if (Test-Path -LiteralPath $ResolvedConfig) { Say "Removing $ResolvedConfig"; Remove-ClioTree $ResolvedConfig }
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
                Remove-ClioTree $dir
                $parent = Split-Path -Parent $dir
                if ((Split-Path -Leaf $parent) -eq 'clio-agent' -and -not (Get-ChildItem -LiteralPath $parent -Force -ErrorAction SilentlyContinue)) { Remove-Item -LiteralPath $parent -Force -ErrorAction SilentlyContinue }
            }
        }
    }
}
if ($PurgeCaches -and (Test-Path -LiteralPath $ClioCache)) {
    Say "Removing $ClioCache"
    Remove-ClioTree $ClioCache
}
Start-ClioTreeRemoval

Say "CLIO uninstalled."
if (-not $Purge -and (Test-Path $ClioConfig)) {
    Write-Host "  Agent config kept ($ClioConfig) - re-run with -Purge to remove"
}
if (-not $PurgeCaches -and (Test-Path -LiteralPath $ClioCache)) {
    Write-Host "  Agent caches kept ($ClioCache) - re-run with -PurgeCaches to remove"
}
exit 0
