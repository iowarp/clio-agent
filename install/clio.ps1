# CLIO launcher + server lifecycle manager (Windows / PowerShell).
#
# `clio` with no arguments boots the clio-agent server (if not already
# up) and attaches the gact TUI. The subcommands mirror `gact agent *`:
# they let you manage the backing server without hunting PIDs.
#
#   clio                 ensure server is up, then attach the TUI
#   clio start           start the server (no TUI)
#   clio stop            stop the server
#   clio restart         stop then start
#   clio status | ps     PID / port / health
#   clio logs [N]        tail the server + gact stderr logs
#   clio doctor          check prerequisites and install layout
#   clio report          print a diagnostics bundle for GitHub issues
#   clio completion SH   print shell completion (powershell)
#   clio uninstall [...] run the uninstaller
#   clio help            this text
#
# Environment overrides:
#   CLIO_PREFIX   install root      (default: $HOME\AppData\Local\clio)
#   CLIO_PORT     server port       (default: 17800)
#   CLIO_BIN_DIR  launcher location (default: ...\WindowsApps)
param(
    [Parameter(Position = 0)]
    [string]$Command = "",
    [Parameter(ValueFromRemainingArguments = $true)]
    $Rest
)

$ErrorActionPreference = 'Stop'

# ---------- resolved paths --------------------------------------------
if ($env:CLIO_PREFIX) { $Prefix = $env:CLIO_PREFIX } else { $Prefix = Join-Path $HOME 'AppData\Local\clio' }
if ($env:CLIO_PORT)   { $Port   = [int]$env:CLIO_PORT } else { $Port = 17800 }
if ($env:CLIO_BIN_DIR){ $BinDir = $env:CLIO_BIN_DIR } else { $BinDir = Join-Path $HOME 'AppData\Local\Microsoft\WindowsApps' }

$PidFile   = Join-Path $Prefix 'clio-server.pid'
$ServerLog = Join-Path $Prefix 'clio-server.log'
$ServerErr = Join-Path $Prefix 'clio-server.err.log'
$GactLog   = Join-Path $Prefix 'gact-stderr.log'
$ServerBin = Join-Path $Prefix 'clio-agent\.venv\Scripts\clio-agent-gact.exe'
$GactBin   = Join-Path $Prefix 'gact.exe'

function Say  ($m) { Write-Host "==> $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "!! $m"  -ForegroundColor Yellow }
function Err  ($m) { Write-Host "xx $m"  -ForegroundColor Red }

# Test-Health returns $true when the server answers /v1/health.
# CLIO returns 503 for a reachable server whose dependencies still need
# user configuration, such as first-run LM provider selection. Treat that
# as "server is up" so the TUI can surface and resolve the configuration.
function Test-Health {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/v1/health" -UseBasicParsing -TimeoutSec 1
        return ($r.StatusCode -eq 200 -or $r.StatusCode -eq 503)
    } catch {
        $status = $_.Exception.Response.StatusCode.value__
        return ($status -eq 503)
    }
}

# Get-ServerPid returns a live server PID (validated pidfile, or the
# listener on the port) or $null.
function Get-ServerPid {
    if (Test-Path $PidFile) {
        $p = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($p) {
            $p = $p.Trim()
            if ($p -and (Get-Process -Id $p -ErrorAction SilentlyContinue)) {
                return [int]$p
            }
        }
    }
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($conn) { return [int]$conn.OwningProcess }
    return $null
}

function Start-Server {
    if (Test-Health) {
        Say "CLIO server already running on :$Port"
        return $true
    }
    # A pidfile with no healthy server is a stale/zombie launch - drop it.
    Remove-Item $PidFile -ErrorAction SilentlyContinue
    if (-not (Test-Path $ServerBin)) {
        Err "server binary not found: $ServerBin"
        Err "is CLIO installed? run the installer or set CLIO_PREFIX."
        return $false
    }
    Say "Starting CLIO server on :$Port (log: $ServerLog)"
    # Launch through a hidden cmd wrapper that does its own '>' / '2>'
    # file redirection. Start-Process -RedirectStandard* would set
    # bInheritHandles=TRUE and leak the caller's stdout pipe into the
    # detached server, so a scripted/piped `clio start` would hang
    # forever waiting on a pipe the server keeps open. cmd-level
    # redirection avoids that: the server inherits only the log files.
    # The doubled outer quotes are the classic `cmd /c "..."` form so
    # cmd strips exactly one pair and parses the inner quotes itself.
    $inner = '""{0}" --port {1} > "{2}" 2> "{3}""' -f $ServerBin, $Port, $ServerLog, $ServerErr
    $proc = Start-Process -FilePath $env:ComSpec `
        -ArgumentList '/c', $inner `
        -WorkingDirectory (Join-Path $Prefix 'clio-agent') `
        -WindowStyle Hidden -PassThru
    Set-Content -Path $PidFile -Value $proc.Id -Encoding ascii
    for ($i = 0; $i -lt 30; $i++) {
        if (Test-Health) { Say "  healthy (pid $($proc.Id))"; return $true }
        Start-Sleep -Milliseconds 500
    }
    Err "server did not become healthy in ~15s - check $ServerLog / $ServerErr"
    return $false
}

function Stop-Server {
    $p = Get-ServerPid
    if (-not $p) {
        Say "no CLIO server running"
        Remove-Item $PidFile -ErrorAction SilentlyContinue
        return
    }
    Say "Stopping CLIO server (pid $p)"
    # taskkill /T kills the whole tree - clio-agent-gact spawns python
    # children, and a bare Stop-Process would orphan them (the zombie
    # state that holds the log file open and blocks the next start).
    & taskkill /PID $p /T /F | Out-Null
    for ($i = 0; $i -lt 20; $i++) {
        if (-not (Get-Process -Id $p -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 250
    }
    Remove-Item $PidFile -ErrorAction SilentlyContinue
    Say "  stopped"
}

function Get-Status {
    $p = Get-ServerPid
    if (Test-Health) { $h = "healthy" } else { $h = "not responding" }
    Write-Host "prefix:  $Prefix"
    Write-Host "port:    $Port"
    if ($p) { Write-Host "pid:     $p" } else { Write-Host "pid:     (none)" }
    Write-Host "health:  $h"
    Write-Host "server:  $ServerLog"
    Write-Host "stderr:  $ServerErr"
    Write-Host "gact:    $GactLog"
}

function Show-Logs ($n) {
    if (-not $n) { $n = 40 }
    $files = @($ServerLog, $ServerErr, $GactLog) | Where-Object { Test-Path $_ }
    if ($files.Count -eq 0) {
        Warn "no logs yet (server hasn't run, or different CLIO_PREFIX)"
        return
    }
    Get-Content -Path $files -Tail $n -Wait
}

function Invoke-Doctor {
    $bad = 0
    Write-Host "CLIO doctor"
    Write-Host "  prefix:       $Prefix"
    if (Test-Path $ServerBin) { Write-Host "  server bin:   ok" } else { Write-Host "  server bin:   MISSING"; $bad = 1 }
    if (Test-Path $GactBin)   { Write-Host "  gact bin:     ok" } else { Write-Host "  gact bin:     MISSING"; $bad = 1 }
    if (Test-Health) { Write-Host "  port ${Port}:    server healthy" } else { Write-Host "  port ${Port}:    free / no server" }
    $launcher = Join-Path $BinDir 'clio.cmd'
    if (Test-Path $launcher) { Write-Host "  launcher:     $launcher" } else { Write-Host "  launcher:     not in $BinDir" }
    return $bad
}

# Invoke-Report prints a diagnostics bundle suitable for pasting into a
# GitHub issue: versions, environment, health, and the tail of every
# log. Goes to stdout so it can be piped or redirected.
function Invoke-Report {
    Write-Host "==== CLIO bug report ===="
    Write-Host "date:       $(Get-Date -Format o)"
    Write-Host "os:         $([System.Environment]::OSVersion.VersionString)"
    Write-Host "powershell: $($PSVersionTable.PSVersion)"
    Write-Host "prefix:     $Prefix"
    Write-Host "port:       $Port"
    if (Test-Path $GactBin) {
        Write-Host "gact:       $((& $GactBin version) -join ' ')"
    } else {
        Write-Host "gact:       (binary missing)"
    }
    $pyproj = Join-Path $Prefix 'clio-agent\pyproject.toml'
    if (Test-Path $pyproj) {
        $verLine = (Get-Content $pyproj | Where-Object { $_ -match '^version\s*=' } | Select-Object -First 1)
        Write-Host "clio-agent: $verLine"
    }
    if (Test-Health) { Write-Host "health:     healthy" } else { Write-Host "health:     not responding" }
    foreach ($lf in @($ServerLog, $ServerErr, $GactLog)) {
        Write-Host ""
        Write-Host "---- $lf (last 40 lines) ----"
        if (Test-Path $lf) { Get-Content $lf -Tail 40 } else { Write-Host "(none)" }
    }
    Write-Host ""
    Write-Host "==== end report ===="
}

function Write-Completion ($shell) {
    switch ($shell) {
        'powershell' {
@'
Register-ArgumentCompleter -CommandName clio -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)
    @('start','stop','restart','status','ps','logs','doctor','report','completion','uninstall','attach','help') |
        Where-Object { $_ -like "$wordToComplete*" } |
        ForEach-Object { [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_) }
}
'@
        }
        default {
            Err "usage: clio completion powershell"
            return
        }
    }
}

# Show-Usage prints the leading comment block as the help text. Reads
# until the first non-comment line so it stays correct as the header
# grows.
function Show-Usage {
    foreach ($line in (Get-Content $PSCommandPath | Select-Object -Skip 1)) {
        if ($line -notmatch '^#') { break }
        $line -replace '^# ?', ''
    }
}

# ---------- dispatch --------------------------------------------------
if (-not $Rest) { $Rest = @() }

switch ($Command) {
    { $_ -eq "" -or $_ -eq "attach" } {
        if (-not (Start-Server)) { exit 1 }
        $env:GACT_BACKEND = "http://127.0.0.1:$Port"
        # Run gact attached to this console (stdout stays on the
        # terminal so the TUI renders) but capture its stderr to a log
        # so Go panics survive for bug reports instead of scrolling away.
        $spArgs = @{
            FilePath              = $GactBin
            NoNewWindow           = $true
            Wait                  = $true
            RedirectStandardError = $GactLog
            PassThru              = $true
        }
        if ($Rest.Count -gt 0) { $spArgs['ArgumentList'] = $Rest }
        $proc = Start-Process @spArgs
        exit $proc.ExitCode
    }
    "start"   { if (Start-Server) { exit 0 } else { exit 1 } }
    "stop"    { Stop-Server; exit 0 }
    "restart" { Stop-Server; if (Start-Server) { exit 0 } else { exit 1 } }
    { $_ -eq "status" -or $_ -eq "ps" } { Get-Status; exit 0 }
    "logs"    { Show-Logs $Rest[0]; exit 0 }
    "doctor"  { exit (Invoke-Doctor) }
    "report"  { Invoke-Report; exit 0 }
    "completion" { Write-Completion $Rest[0]; exit 0 }
    "uninstall" {
        $un = Join-Path $Prefix 'uninstall.ps1'
        if (-not (Test-Path $un)) { $un = Join-Path $Prefix 'clio-agent\install\uninstall.ps1' }
        if (Test-Path $un) {
            & powershell -NoProfile -ExecutionPolicy Bypass -File $un @Rest
            exit $LASTEXITCODE
        }
        Err "uninstaller not found (looked in $Prefix and the clio-agent checkout)"
        exit 1
    }
    { $_ -eq "help" -or $_ -eq "-h" -or $_ -eq "--help" } { Show-Usage; exit 0 }
    default {
        Err "unknown command: $Command"
        Show-Usage
        exit 2
    }
}
