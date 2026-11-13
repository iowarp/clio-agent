# CLIO installer (Windows / PowerShell).
#
#   install.ps1                  the latest stable release
#   install.ps1 --dev            the latest release overall, pre-releases included
#   install.ps1 --source         source at `main`, with what main pins (submodules, uv.lock)
#   install.ps1 --dev --source   source at `develop`
#   install.ps1 -Desktop         the desktop app, bundled with its agent (x64 setup;
#                                runs under emulation on ARM); -Dev/-Version pick the release
#   install.ps1 -Desktop -Progress json
#                                the same, reporting each install step (resolve, download,
#                                verify, install) and a summary as JSON lines on stdout
#   install.ps1 -Desktop -Unattended
#                                the same, running the setup silently with its defaults
#   install.ps1 -ResetOldData    move data CLIO 0.9.5.1 or older wrote aside without asking
#                                (this release cannot open it; it is kept, never deleted)
#   install.ps1 -EnableLongPaths turn on Windows long path support without asking first
#                                (one Windows admin prompt for that setting only);
#                                -SkipLongPaths never asks. Unset, an interactive run
#                                asks once when they are off; other runs do not ask
#
# -Source/-Dev/-Version/-Ref are the same options. From a URL:
#   & ([scriptblock]::Create((irm <url>/install.ps1))) --dev
# --source run from inside a clio-agent checkout installs that checkout in place; run
# from a URL it clones. A release that publishes the desktop's prebuilt runtime archive
# (x64) installs from it, unpacked while it downloads; any other release is installed
# from PyPI, constrained by that release tag's own uv.lock, on the Python that release
# declares (.python-version; 3.13 before). CLIO_INSTALL_FROM=pypi chooses PyPI.
#
# Pins: --version <pypi version> (release), --ref <git ref> (source). Older spellings
# still work: $env:CLIO_VERSION is --version, $env:CLIO_REF is --source --ref.
#
# Honors environment overrides:
#   CLIO_PREFIX        install root           (default: <agent data>\app)
#   CLIO_BIN_DIR       launcher location      (default: ...\WindowsApps)
#   CLIO_INSTALLER_REF pin launcher scripts   (default: the installed release's tag)
#   CLIO_KIT_PACKAGE   local candidate wheel/base spec (default: released 2.12.5)
#   CLIO_GIT_PROTOCOL  https | ssh            (default: https; source mode only)
#   CLIO_INSTALL_PROGRESS human | json        (default: human; -Desktop only)
#   CLIO_LONG_PATHS    ask | enable | skip    (default: ask; see -EnableLongPaths)

# `& ([scriptblock]::Create((irm <url>))) <options>` (above; CLIO Desktop runs it so)
# runs this text in a child scope, where $script: names the caller's scope: every
# $script: variable read empty, the runtime archive's plan among them, so the install
# fell back to PyPI. The same text is then run dot-sourced in a module of its own, whose
# script scope is its top level; output, options and exit codes pass through unchanged.
$ClioScopeProbe = $true
if (-not (Test-Path -LiteralPath variable:script:ClioScopeProbe)) {
    . (New-Module -Name clio-install -ScriptBlock { }) ([scriptblock]::Create($MyInvocation.MyCommand.ScriptBlock.ToString())) @args
    return
}

$ErrorActionPreference = 'Stop'
# CLIO_PORT names the server's API port, but clio-core reads the same name as its RPC
# port. The installer starts no server, and its migrate step would otherwise put the
# clio-core daemon and client on the API port and stall until the daemon-start limit.
Remove-Item Env:CLIO_PORT -ErrorAction SilentlyContinue
# Windows PowerShell 5.1 redraws Invoke-WebRequest's progress bar for every chunk, which
# makes a large download many times slower than the network (the desktop setup took
# 10-15 minutes). Large files go through Save-ClioDownload, which shows curl's own meter.
$ProgressPreference = 'SilentlyContinue'

# -Progress json keeps stdout for its events, so messages go to stderr instead.
$ProgressJson = $false
function Write-ClioLine([string]$text, [string]$color) {
    if ($script:ProgressJson) { [Console]::Error.WriteLine($text) } else { Write-Host $text -ForegroundColor $color }
}
function Say  ($m) { Write-ClioLine "==> $m" Green }
function Warn ($m) { Write-ClioLine "!! $m" Yellow }
# The legacy TUI is frozen and unsupported; no TUI payload is installed.
$OfficeProc = $null
$OfficeLog = $null
$OfficeErr = $null
$BytecodeProc = $null
$BytecodeLog = $null
$BytecodeErr = $null
function Stop-BytecodePrepare {
    if ($script:BytecodeProc -and -not $script:BytecodeProc.HasExited) {
        $current = Get-Process -Id $script:BytecodeProc.Id -ErrorAction SilentlyContinue
        if ($current -and $current.StartTime -eq $script:BytecodeProc.StartTime) {
            & taskkill /T /F /PID $script:BytecodeProc.Id *> $null
            $script:BytecodeProc.WaitForExit()
        }
    }
    $script:BytecodeProc = $null
}
function Stop-OfficePrepare {
    if ($script:OfficeProc -and -not $script:OfficeProc.HasExited) {
        $current = Get-Process -Id $script:OfficeProc.Id -ErrorAction SilentlyContinue
        if ($current -and $current.StartTime -eq $script:OfficeProc.StartTime) {
            & taskkill /T /F /PID $script:OfficeProc.Id *> $null
            $script:OfficeProc.WaitForExit()
        }
    }
    $script:OfficeProc = $null
    Remove-ClioOfficeTreeFetch
    $fetch = if ($script:PrefetchJob) { $script:PrefetchJob.Process }
    if ($fetch -and -not $fetch.HasExited) {
        & taskkill /T /F /PID $fetch.Id *> $null
        $fetch.WaitForExit()
    }
    $script:PrefetchJob = $null
}
# The source install's LibreOffice download (Start-ClioOfficeTreeFetch), once the Office
# worker no longer reads it.
function Remove-ClioOfficeTreeFetch {
    if (-not $script:OfficeTreeScratch) { return }
    $fetch = $script:OfficeTreeFetch
    if ($fetch -and -not $fetch.HasExited) { & taskkill.exe /T /F /PID $fetch.Id 2>&1 | Out-Null }
    $script:ClioSideFetches = @($script:ClioSideFetches | Where-Object { $_ -ne $fetch })
    $script:OfficeTreeFetch = $null
    if ($env:CLIO_INSTALL_DOWNLOADS -and $env:CLIO_INSTALL_DOWNLOADS.StartsWith($script:OfficeTreeScratch)) {
        Remove-Item Env:CLIO_INSTALL_DOWNLOADS -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $script:OfficeTreeScratch -Recurse -Force -ErrorAction SilentlyContinue
    $script:OfficeTreeScratch = $null
}
# Run as a file (-File, CI), the installer exits with its status. Run from a URL
# (`irm ... | iex`, or a script block) it has no file, and `exit` would close the
# user's PowerShell window and the error with it: it throws or returns instead.
function Exit-Installer ([int]$code) {
    if ($PSCommandPath) { exit $code }
    if ($code -ne 0) { throw "CLIO installer stopped (exit $code)" }
}
function Die  ($m) { Stop-BytecodePrepare; Stop-OfficePrepare; Write-ClioLine "xx $m" Red; Complete-ClioRun $false $m; Exit-Installer 1 }
# DieWith CODE MESSAGE: Die, keeping the failed command's exit status.
function DieWith ([int]$code, $m) { Stop-BytecodePrepare; Stop-OfficePrepare; Write-ClioLine "xx $m" Red; Complete-ClioRun $false $m; Exit-Installer $code }

# The desktop install's shared steps (install.sh has the same): resolve, download, verify,
# install. Each step's time is reported; -Progress json writes one JSON object per line
# on stdout ({"event":"step",...} and a final {"event":"summary",...}). The NSIS setup
# prepares the bundled agent itself, inside the install step.
$RunClock = $null
$StepClock = $null
$StepName = ''
$StepTimes = [ordered]@{}
$RunBytes = 0
function Format-ClioSeconds([double]$s) { return ([math]::Round($s, 1)).ToString('0.0', [Globalization.CultureInfo]::InvariantCulture) }
function Write-ClioEvent($record) {
    if ($script:ProgressJson) { [Console]::Out.WriteLine(($record | ConvertTo-Json -Compress -Depth 4)) }
}
function Start-ClioStep([string]$name) {
    if ($script:StepName) {
        $seconds = Format-ClioSeconds $script:StepClock.Elapsed.TotalSeconds
        $script:StepTimes[$script:StepName] = [double]$seconds
        Write-ClioEvent ([ordered]@{ event = 'step'; step = $script:StepName; state = 'end'; seconds = [double]$seconds })
    }
    $script:StepName = $name
    $script:StepClock = [Diagnostics.Stopwatch]::StartNew()
    if (-not $script:RunClock) { $script:RunClock = [Diagnostics.Stopwatch]::StartNew() }
    if ($name) { Write-ClioEvent ([ordered]@{ event = 'step'; step = $name; state = 'begin' }) }
}
function Complete-ClioRun([bool]$ok, [string]$err = '') {
    if (-not $script:RunClock) { return }
    $failed = $script:StepName
    if ($ok) { Start-ClioStep '' } else { $script:StepName = '' }
    $total = [double](Format-ClioSeconds $script:RunClock.Elapsed.TotalSeconds)
    $script:RunClock = $null
    if ($ok) {
        Write-ClioEvent ([ordered]@{ event = 'summary'; ok = $true; seconds = $script:StepTimes; total = $total; bytes = $script:RunBytes })
        $parts = @($script:StepTimes.GetEnumerator() | ForEach-Object { "$($_.Key) $(Format-ClioSeconds $_.Value) s" })
        Say "Done in $(Format-ClioSeconds $total) s ($($parts -join ', '))."
    } else {
        Write-ClioEvent ([ordered]@{ event = 'summary'; ok = $false; step = $failed; error = $err; seconds = $script:StepTimes; total = $total; bytes = $script:RunBytes })
    }
}
# Show the setup's prepare progress that arrived after byte OFFSET; returns the new offset.
# The writer keeps the file open, so it is read with shared access, whole lines only.
function Show-ClioPrepareLog([string]$path, [int64]$offset) {
    if (-not (Test-Path -LiteralPath $path)) { return $offset }
    $text = ''
    try {
        $stream = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]'ReadWrite, Delete')
        try {
            if ($stream.Length -le $offset) { return $offset }
            $null = $stream.Seek($offset, [IO.SeekOrigin]::Begin)
            $text = (New-Object IO.StreamReader($stream, (New-Object Text.UTF8Encoding($false)))).ReadToEnd()
        } finally { $stream.Dispose() }
    } catch { return $offset }
    $end = $text.LastIndexOf([char]10)
    if ($end -lt 0) { return $offset }
    $complete = $text.Substring(0, $end + 1)
    if ($script:StepName -ne 'prepare') { Start-ClioStep 'prepare' }
    foreach ($line in $complete.Split([char]10)) {
        $line = $line.TrimEnd([char]13)
        if ($line) { Write-ClioLine "    $line" Gray }
    }
    return $offset + [Text.Encoding]::UTF8.GetByteCount($complete)
}
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
# Remove-ClioTreeLater PATH: delete a tree nothing uses any more in a hidden process.
# A replaced environment holds tens of thousands of files, and deleting them under
# real-time scanning takes about half a minute the install need not wait for. The tree
# is renamed first, so a later install never meets a half-deleted one under its name.
function Remove-ClioTreeLater([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $doomed = "$Path.removing-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
    try { [IO.Directory]::Move($Path, $doomed) } catch { RemoveTree $Path; return }
    $deleter = Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\cmd.exe') -ArgumentList '/d', '/c', "rd /s /q `"$(LongPath $doomed)`"" -WindowStyle Hidden -PassThru
    # Idle priority also lowers its disk priority, so the install keeps the disk.
    try { $deleter.PriorityClass = [Diagnostics.ProcessPriorityClass]::Idle } catch { }
}

# ---------- defaults ---------------------------------------------------
$Prefix      = if ($env:CLIO_PREFIX)       { $env:CLIO_PREFIX }      else { Join-Path $(if ($env:CLIO_AGENT_DATA_DIR) { $env:CLIO_AGENT_DATA_DIR } elseif ($env:CLIO_AGENT_HOME) { Join-Path $env:CLIO_AGENT_HOME 'data' } elseif ($env:CLIO_USER_DIR) { Join-Path $env:CLIO_USER_DIR 'data' } else { Join-Path $env:LOCALAPPDATA 'clio-agent\data' }) 'app' }
# Continue a pre-namespace installation until it is explicitly migrated.
$LegacyPrefix = Join-Path $env:LOCALAPPDATA 'clio'
if (-not $env:CLIO_PREFIX -and -not $env:CLIO_AGENT_HOME -and -not $env:CLIO_AGENT_DATA_DIR -and -not $env:CLIO_USER_DIR -and -not (Test-Path -LiteralPath (Join-Path $Prefix 'clio-agent\.venv')) -and (Test-Path -LiteralPath (Join-Path $LegacyPrefix 'clio-agent\.venv'))) { $Prefix = $LegacyPrefix }
$BinDir      = if ($env:CLIO_BIN_DIR)      { $env:CLIO_BIN_DIR }     else { Join-Path $HOME 'AppData\Local\Microsoft\WindowsApps' }
function Get-ClioReleaseTag([string]$Version) {
    $Version = $Version -replace '^v', ''
    if ($Version -match '^([0-9]+\.[0-9]+\.[0-9]+)b([0-9]+)\.post([0-9]+)$') {
        return "v$($Matches[1])-beta.$($Matches[2]).$($Matches[3])"
    }
    if ($Version -match '^([0-9]+\.[0-9]+\.[0-9]+(?:\.[0-9]+)?)b([0-9]+)$') {
        return "v$($Matches[1])-beta.$($Matches[2])"
    }
    # Only betas have their own tag spelling; alphas and release candidates are
    # tagged as their package version (0.10.0a1 is v0.10.0a1).
    if ($Version -match '^[0-9]+\.[0-9]+\.[0-9]+(?:\.[0-9]+)?(?:-beta\.[0-9]+(?:\.[0-9]+)?|(?:a|rc)[0-9]+)?$') {
        return "v$Version"
    }
    throw "No GitHub release tag for package version: $Version"
}

# Save-ClioDownload URI FILE: a large download (the web UI; the desktop's without curl.exe).
# curl.exe ships with Windows 10 1803 and later; it retries, resumes a cut transfer and
# shows its own progress. Without it, Invoke-WebRequest (progress bar off, above).
function Save-ClioDownload([string]$Uri, [string]$OutFile) {
    # Resume only within this download: a file left by an earlier run may be another version.
    Remove-Item -LiteralPath $OutFile -Force -ErrorAction SilentlyContinue
    $curl = Get-Command curl.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($curl) {
        & $curl.Source --fail --location --retry 5 --retry-delay 2 --retry-connrefused --continue-at - --output $OutFile $Uri
        if ($LASTEXITCODE -ne 0) { throw "download failed (curl.exe exit $LASTEXITCODE): $Uri" }
    } else {
        Say 'curl.exe not found; downloading with Invoke-WebRequest'
        Invoke-WebRequest -UseBasicParsing -Uri $Uri -OutFile $OutFile
    }
}

# Save-ClioDownloads ITEMS EXPECTED [UNTIL]: the desktop's downloads (the setup and the
# release plan's files), all at once. Each is one curl.exe, which retries and resumes as
# Save-ClioDownload does; the bytes are hashed as they land, so no second pass reads the
# file again. A line every 5 s (2 s as JSON) reports the progress. Each item (.Url, .File)
# gets .Ok (curl succeeded), .Sha (lowercase hex of what was written) and .Landed; a plan
# file (.Final set) then goes through Complete-ClioPrefetch. With UNTIL (one of ITEMS) it
# returns as soon as that item has landed: the others keep downloading, carried on by
# Step-ClioDownloads and ended by Stop-ClioDownloads.
$ClioTransfers = @()
$ClioBuffer = New-Object byte[] 1048576
$ClioHold = $null
# A plan file of RANGED_MIN bytes or more arrives over CONNECTIONS connections: one
# connection to GitHub's release storage carries about 10 MB/s, well under most links.
# Each curl.exe fetches every CONNECTIONS-th SEGMENT-sized range over one connection
# (--next), and the ranges are joined in order into NAME.part as they complete, so the
# setup reading that file sees it grow at the link's rate. The first LEAD bytes come over
# one connection while the setup downloads; the others start once it has landed.
$ClioRangedMin = 128MB
$ClioConnections = 6
$ClioSegment = 8MB
$ClioLead = 32MB
function Save-ClioDownloads($Items, [int64]$Expected, $Until = $null) {
    foreach ($item in $Items) {
        Remove-Item -LiteralPath $item.File -Force -ErrorAction SilentlyContinue
        $item | Add-Member -Force -NotePropertyName Ok -NotePropertyValue $false
        $item | Add-Member -Force -NotePropertyName Sha -NotePropertyValue ''
        $item | Add-Member -Force -NotePropertyName Landed -NotePropertyValue $false
    }
    $curl = Get-Command curl.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $curl) {
        foreach ($item in $Items) {
            try { Save-ClioDownload $item.Url $item.File; $item.Sha = Get-ClioSha256 $item.File; $item.Ok = $true } catch { Warn "$_" }
            $item.Landed = $true
            if ($item.Final) { Complete-ClioPrefetch $item }
        }
        return
    }
    $script:ClioHold = $Until
    $script:ClioTransfers = @(foreach ($item in $Items) {
        if ($item.Bytes -ge $script:ClioRangedMin -and $item.Ranges) {
            New-ClioRangedTransfer $item $curl.Source
            continue
        }
        $arguments = "--fail --location --silent --show-error --retry 5 --retry-delay 2 --retry-connrefused --continue-at - --output `"$($item.File)`" `"$($item.Url)`""
        $process = Start-Process -FilePath $curl.Source -ArgumentList $arguments -NoNewWindow -PassThru
        # Windows PowerShell reports no exit code for a process whose handle was never read.
        $null = $process.Handle
        [pscustomobject]@{ Item = $item; Process = $process; Hash = [Security.Cryptography.SHA256]::Create(); Stream = $null; Bytes = [int64]0; Done = $false; Ranged = $false }
    })
    $interval = if ($script:ProgressJson) { 2 } else { 5 }
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $next = $interval
    $last = [int64]0
    while ($true) {
        $running = Step-ClioDownloads
        if (-not $running) { break }
        if ($Until -and $Until.Landed) { break }
        if ($clock.Elapsed.TotalSeconds -ge $next) {
            $bytes = [int64](($script:ClioTransfers | Measure-Object -Property Bytes -Sum).Sum)
            Write-ClioEvent ([ordered]@{ event = 'progress'; step = 'download'; bytes = $bytes; total = $Expected })
            $of = if ($Expected -gt 0) { " of $([math]::Round($Expected / 1e6))" } else { '' }
            $rate = ([math]::Round(($bytes - $last) / $interval / 1e6, 1)).ToString('0.0', [Globalization.CultureInfo]::InvariantCulture)
            Say "Downloaded $([math]::Round($bytes / 1e6))$of MB ($rate MB/s)"
            $last = $bytes
            $next += $interval
        }
        Start-Sleep -Milliseconds 200
    }
    $script:RunBytes = [int64](($script:ClioTransfers | Measure-Object -Property Bytes -Sum).Sum)
}

# Step-ClioDownloads: hash what each running download wrote since the last call and finish
# those whose curl.exe exited. $true while any still runs.
function Step-ClioDownloads {
    $active = $false
    foreach ($t in $script:ClioTransfers) {
        if ($t.Done) { continue }
        if ($t.Ranged) {
            if (Step-ClioRangedTransfer $t) { $active = $true }
            continue
        }
        # Read the exit first: what curl wrote before it exited is then all on disk.
        $exited = $t.Process.HasExited
        if (-not $t.Stream -and (Test-Path -LiteralPath $t.Item.File)) {
            try { $t.Stream = [IO.File]::Open($t.Item.File, 'Open', 'Read', 'ReadWrite, Delete') } catch { }
        }
        if ($t.Stream) {
            while (($n = $t.Stream.Read($script:ClioBuffer, 0, $script:ClioBuffer.Length)) -gt 0) {
                $null = $t.Hash.TransformBlock($script:ClioBuffer, 0, $n, $null, 0)
                $t.Bytes += $n
            }
        }
        if (-not $exited) { $active = $true; continue }
        $t.Process.WaitForExit()
        $null = $t.Hash.TransformFinalBlock($script:ClioBuffer, 0, 0)
        $t.Item.Sha = (($t.Hash.Hash | ForEach-Object { $_.ToString('x2') }) -join '')
        $t.Item.Ok = $t.Process.ExitCode -eq 0
        if ($t.Stream) { $t.Stream.Dispose() }
        $t.Hash.Dispose()
        $t.Done = $true
        $t.Item.Landed = $true
        if ($t.Item.Final) { Complete-ClioPrefetch $t.Item }
    }
    return $active
}

# Stop-ClioDownloads: end the downloads still running (the setup no longer needs them, or
# the run failed) and count what all of them fetched.
function Stop-ClioDownloads {
    if (-not $script:ClioTransfers.Count) { return }
    foreach ($t in $script:ClioTransfers) {
        if ($t.Done) { continue }
        if ($t.Ranged) {
            foreach ($worker in $t.Workers) { try { $worker.Kill(); $worker.WaitForExit() } catch { } }
            $t.Out.Dispose()
        } else {
            try { $t.Process.Kill(); $t.Process.WaitForExit() } catch { }
        }
        if ($t.Stream) { $t.Stream.Dispose() }
        $t.Hash.Dispose()
        $t.Done = $true
    }
    $script:RunBytes = [int64](($script:ClioTransfers | Measure-Object -Property Bytes -Sum).Sum)
    $script:ClioTransfers = @()
}

# New-ClioRangedTransfer ITEM CURL: ITEM's download as ranges (see $ClioRangedMin), its
# lead range started. ITEM.Ranges is a scratch directory for the ranges in flight.
function New-ClioRangedTransfer($item, [string]$curl) {
    New-Item -ItemType Directory -Force -Path $item.Ranges | Out-Null
    $ranges = New-Object System.Collections.Generic.List[object]
    $start = [int64]0
    while ($start -lt $item.Bytes) {
        $size = if ($ranges.Count) { $script:ClioSegment } else { $script:ClioLead }
        $length = [math]::Min([int64]$size, $item.Bytes - $start)
        $ranges.Add([pscustomobject]@{ Start = $start; Length = $length; File = (Join-Path $item.Ranges "$($item.Name).$($ranges.Count)") })
        $start += $length
    }
    # The setup reads NAME.part while it grows and moves it once it is complete.
    $out = New-Object IO.FileStream($item.File, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]'ReadWrite, Delete')
    $t = [pscustomobject]@{
        Item = $item; Process = $null; Hash = [Security.Cryptography.SHA256]::Create(); Stream = $null; Bytes = [int64]0; Done = $false
        Ranged = $true; Curl = $curl; Ranges = $ranges; Workers = @(); Next = 0; Taken = [int64]0; Out = $out
    }
    Start-ClioRangeWorker $t 0
    $t
}

# Start-ClioRangeWorker TRANSFER N: one curl.exe for ranges N, N+CONNECTIONS, ... in turn.
function Start-ClioRangeWorker($t, [int]$worker) {
    $lines = New-Object System.Collections.Generic.List[string]
    for ($i = $worker; $i -lt $t.Ranges.Count; $i += $script:ClioConnections) {
        $r = $t.Ranges[$i]
        if ($lines.Count) { $lines.Add('next') }
        $lines.Add("url = `"$($t.Item.Url)`"")
        $lines.Add("range = $($r.Start)-$($r.Start + $r.Length - 1)")
        # A config file's quoted strings take backslash escapes.
        $lines.Add("output = `"$($r.File.Replace('\', '\\'))`"")
        foreach ($option in 'fail', 'location', 'silent', 'show-error', 'retry-connrefused', 'retry = 5', 'retry-delay = 2') { $lines.Add($option) }
    }
    $config = Join-Path $t.Item.Ranges "$($t.Item.Name).curl$worker"
    [IO.File]::WriteAllLines($config, $lines)
    $process = Start-Process -FilePath $t.Curl -ArgumentList "--config `"$config`"" -NoNewWindow -PassThru
    $null = $process.Handle
    $t.Workers += $process
}

# Step-ClioRangedTransfer TRANSFER: start the other connections once the setup has landed,
# append the completed ranges to NAME.part in order and hash them. $true while it runs.
function Step-ClioRangedTransfer($t) {
    $wanted = [math]::Min($script:ClioConnections, $t.Ranges.Count)
    if ($t.Workers.Count -lt $wanted -and (-not $script:ClioHold -or $script:ClioHold.Landed)) {
        for ($w = $t.Workers.Count; $w -lt $wanted; $w++) { Start-ClioRangeWorker $t $w }
    }
    # Read the exits first: what curl wrote before it exited is then all on disk.
    $exited = $t.Workers.Count -eq $wanted -and -not @($t.Workers | Where-Object { -not $_.HasExited }).Count
    while ($t.Next -lt $t.Ranges.Count) {
        $r = $t.Ranges[$t.Next]
        if (-not $t.Stream) {
            if (-not (Test-Path -LiteralPath $r.File)) { break }
            try { $t.Stream = [IO.File]::Open($r.File, 'Open', 'Read', 'ReadWrite, Delete') } catch { break }
        }
        # A retried range is written again from its start with the same bytes; reading on
        # from here waits until it has grown past this point again.
        $n = $t.Stream.Read($script:ClioBuffer, 0, [int][math]::Min($r.Length - $t.Taken, $script:ClioBuffer.Length))
        if ($n -le 0) { break }
        $t.Out.Write($script:ClioBuffer, 0, $n)
        $null = $t.Hash.TransformBlock($script:ClioBuffer, 0, $n, $null, 0)
        $t.Bytes += $n
        $t.Taken += $n
        if ($t.Taken -eq $r.Length) {
            $t.Stream.Dispose()
            $t.Stream = $null
            $t.Taken = 0
            $t.Next++
            Remove-Item -LiteralPath $r.File -Force -ErrorAction SilentlyContinue
        }
    }
    $t.Out.Flush()
    $complete = $t.Next -ge $t.Ranges.Count
    if (-not $complete -and -not $exited) { return $true }
    # Complete, or every connection has ended with a range missing.
    $t.Out.Dispose()
    if ($t.Stream) { $t.Stream.Dispose() }
    $null = $t.Hash.TransformFinalBlock($script:ClioBuffer, 0, 0)
    $t.Item.Sha = (($t.Hash.Hash | ForEach-Object { $_.ToString('x2') }) -join '')
    $t.Item.Ok = $complete
    $t.Hash.Dispose()
    foreach ($worker in $t.Workers) { if (-not $worker.HasExited) { try { $worker.Kill() } catch { } } }
    $t.Done = $true
    $t.Item.Landed = $true
    if ($t.Item.Final) { Complete-ClioPrefetch $t.Item }
    return $false
}

# Complete-ClioPrefetch ITEM: a plan file is NAME.part while it downloads and NAME once its
# bytes match the plan, which is what the setup's prepare waits on (office.py
# installer_download, as install.sh's prefetch). A failure only means the setup fetches it.
function Complete-ClioPrefetch($item) {
    if ($item.Ok -and $item.Sha -eq $item.Expected) {
        try { Move-Item -LiteralPath $item.File -Destination $item.Final -Force; return } catch { }
    }
    $item.Ok = $false
    Remove-Item -LiteralPath $item.File -Force -ErrorAction SilentlyContinue
    Warn "could not fetch $($item.Name) ahead of time; the setup downloads it while preparing"
}

# Start-ClioSideFetch ITEM: a plan file the install does not wait for (the browser
# archives), fetched by its own curl.exe from the start of the runtime download. It is
# NAME.part while it downloads and NAME once complete, renamed by the same cmd.exe, so no
# step of this script has to tend it. The browser step waits on that and keeps only bytes
# matching the plan's pin (driver.py installer_archive); a failed fetch leaves neither
# file, and that step downloads the archive itself.
$ClioSideFetches = @()
function Start-ClioSideFetch($item) {
    $curl = Get-Command curl.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $curl) { return }
    # The browser step finds NAME.part even before the first bytes arrive.
    [IO.File]::WriteAllBytes($item.File, [byte[]]@())
    $fetch = "`"$($curl.Source)`" --fail --location --silent --show-error --retry 5 --retry-delay 2 --retry-connrefused --output `"$($item.File)`" `"$($item.Url)`""
    $command = "$fetch && move /y `"$($item.File)`" `"$($item.Final)`" >nul || del /f /q `"$($item.File)`""
    try {
        $process = Start-Process -FilePath $env:ComSpec -ArgumentList "/d /s /c `"$command`"" -NoNewWindow -PassThru
    } catch {
        Remove-Item -LiteralPath $item.File -Force -ErrorAction SilentlyContinue
        return
    }
    $null = $process.Handle
    $script:ClioSideFetches += $process
}

# Stop-ClioSideFetches: end the side fetches still running (with their curl.exe).
function Stop-ClioSideFetches {
    foreach ($process in $script:ClioSideFetches) {
        if (-not $process.HasExited) { & taskkill.exe /T /F /PID $process.Id 2>&1 | Out-Null }
    }
    $script:ClioSideFetches = @()
}

# Get-ClioManifestSha MANIFEST NAME: NAME's SHA-256 in a release checksum manifest, or
# $null. GitHub replaces spaces in asset names with dots; older checksum manifests keep the
# original name. Accept sha256sum's text and binary markers.
function Get-ClioManifestSha([string]$Manifest, [string]$Name) {
    $entries = @($Manifest -split '\r?\n' | Where-Object {
        ($_ -replace 'CLIO Desktop', 'CLIO.Desktop') -match ('^([a-fA-F0-9]{64}) [ *]' + [regex]::Escape($Name) + '$')
    })
    if ($entries.Count -ne 1) { return $null }
    return $entries[0].Substring(0, 64).ToLowerInvariant()
}

# Read-ClioPlan FILE: the release install plan's well-formed lines ("kind sha256 bytes
# name url", scripts/release_install_plan.py), the files the first launch would download.
function Read-ClioPlan([string]$Path) {
    @(foreach ($line in [IO.File]::ReadAllLines($Path)) {
        $f = @($line -split ' ')
        if ($f.Count -ne 5 -or $f[1] -cnotmatch '^[0-9a-f]{64}$' -or $f[2] -notmatch '^[0-9]+$' -or
            $f[3] -notmatch '^[A-Za-z0-9][A-Za-z0-9._+-]*$' -or $f[4] -notmatch '^https://') { continue }
        [pscustomobject]@{ Name = $f[3]; Sha = $f[1]; Bytes = [int64]$f[2]; Url = $f[4]; Kind = $f[0] }
    })
}

# Get-ClioSha256 FILE: the file's SHA-256 as lowercase hex. Windows PowerShell 5.1 started
# from pwsh inherits pwsh's PSModulePath and cannot load Get-FileHash's module, so fall back
# to .NET when the cmdlet is missing or fails.
function Get-ClioSha256([string]$Path) {
    if (Get-Command Get-FileHash -ErrorAction SilentlyContinue) {
        try { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() } catch { }
    }
    $sha = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($Path)
    try {
        return (($sha.ComputeHash($stream) | ForEach-Object { $_.ToString('x2') }) -join '')
    } finally {
        $stream.Dispose()
        $sha.Dispose()
    }
}

# ConvertFrom-ClioReleaseTag TAG: the package version a release tag names, and how it
# sorts; $null for a tag that is not a CLIO release. v0.9.5.1 is stable; v0.9.5-beta.8(.1)
# is 0.9.5b8(.post1); v0.10.0a1 and v0.10.0rc1 are themselves.
function ConvertFrom-ClioReleaseTag([string]$Tag) {
    if ($Tag -notmatch '^v([0-9]+\.[0-9]+\.[0-9]+(?:\.[0-9]+)?)(?:-beta\.([0-9]+)(?:\.([0-9]+))?|(a|rc)([0-9]+))?$') { return $null }
    $base = [version]$Matches[1]
    if ($Matches[2]) {
        $text = "$($Matches[1])b$($Matches[2])" + $(if ($Matches[3]) { ".post$($Matches[3])" } else { '' })
        $pre = @(2, [int]$Matches[2], [int]('0' + $Matches[3]))
    } elseif ($Matches[4]) {
        $text = "$($Matches[1])$($Matches[4])$($Matches[5])"
        $pre = @($(if ($Matches[4] -eq 'a') { 1 } else { 3 }), [int]$Matches[5], 0)
    } else {
        $text = $Matches[1]
        $pre = @(4, 0, 0)
    }
    $key = '{0:D6}.{1:D6}.{2:D6}.{3:D6}-{4}.{5:D6}.{6:D6}' -f $base.Major, $base.Minor, $base.Build, [Math]::Max($base.Revision, 0), $pre[0], $pre[1], $pre[2]
    return [pscustomobject]@{ Tag = $Tag; Version = $text; Stable = ($pre[0] -eq 4); Key = $key }
}

# Get-ClioReleaseList: each published GitHub Release's tag_name, draft and published_at.
# Invoke-RestMethod asks for the list uncompressed (~7 MB of JSON) and turns every field
# of every asset into an object, about 5 s; compressed it is ~0.7 MB, and only these
# fields are read. Each field is matched as a key (quotes inside JSON strings are
# escaped); a list they do not account for is read with Invoke-RestMethod instead.
function Get-ClioReleaseList {
    $uri = 'https://api.github.com/repos/iowarp/clio-agent/releases?per_page=100'
    $curl = Get-Command curl.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($curl) {
        $file = Join-Path ([IO.Path]::GetTempPath()) ("clio-releases-" + [guid]::NewGuid() + '.json')
        try {
            & $curl.Source --proto '=https' --tlsv1.2 --fail --silent --show-error --location --compressed --retry 3 --output $file $uri
            if ($LASTEXITCODE -eq 0) {
                $text = [IO.File]::ReadAllText($file)
                $fields = [regex]::Matches($text, '"tag_name":\s*"([^"\\]*)"[\s\S]*?"draft":\s*(true|false)[\s\S]*?"published_at":\s*(?:"([^"\\]*)"|null)')
                if ($fields.Count -gt 0 -and $fields.Count -eq [regex]::Matches($text, '"tag_name":').Count) {
                    return @(foreach ($m in $fields) {
                        [pscustomobject]@{ tag_name = $m.Groups[1].Value; draft = ($m.Groups[2].Value -eq 'true'); published_at = $m.Groups[3].Value }
                    })
                }
            }
        } catch {
        } finally {
            Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
        }
    }
    return Invoke-RestMethod -UseBasicParsing -Uri $uri
}

# Resolve-ClioRelease DEV: the release to install from the published GitHub Releases
# (PyPI can hold strays that never became a release). Stable: the highest stable
# version. -Dev: the most recently published release, whatever its version says (an
# old alpha can sort above the newest beta). Drafts are never listed.
function Resolve-ClioRelease([bool]$Dev) {
    $releases = Get-ClioReleaseList
    $found = @(foreach ($r in $releases) {
        if ($r.draft) { continue }
        $v = ConvertFrom-ClioReleaseTag ([string]$r.tag_name)
        if (-not $v -or (-not $Dev -and -not $v.Stable)) { continue }
        $v | Add-Member -NotePropertyName Published -NotePropertyValue ([string]$r.published_at) -PassThru
    })
    if ($found.Count -eq 0) { return $null }
    if ($Dev) { return ($found | Sort-Object Published, Key -Descending | Select-Object -First 1) }
    return ($found | Sort-Object Key -Descending | Select-Object -First 1)
}

$OptSource = $false
# Provider SDKs laid down with the managed packages, so a first turn never installs one.
# claude_code's SDK is otherwise installed by the first Claude Code turn.
$ProviderSupport = if ($null -ne $env:CLIO_INSTALL_PROVIDER_SUPPORT) { $env:CLIO_INSTALL_PROVIDER_SUPPORT } else { 'claude_code' }
$OptDev = $false
$OptVersion = ''
$OptRef = ''
$OptDesktop = $false
$OptDownloadOnly = ''
$OptResetOldData = $false
$OptUnattended = $false
$OptLongPaths = if ($env:CLIO_LONG_PATHS) { $env:CLIO_LONG_PATHS.Trim().ToLower() } else { 'ask' }
$OptProgress =  if ($env:CLIO_INSTALL_PROGRESS) { $env:CLIO_INSTALL_PROGRESS } else { 'human' }
for ($i = 0; $i -lt $args.Count; $i++) {
    switch -Regex ([string]$args[$i]) {
        '^(--source|-Source)$' { $OptSource = $true; continue }
        '^(--dev|-Dev)$' { $OptDev = $true; continue }
        '^(--version|-Version)$' { $i++; $OptVersion = [string]$args[$i]; continue }
        '^--version=' { $OptVersion = ([string]$args[$i]).Substring(10); continue }
        '^(--ref|-Ref)$' { $i++; $OptRef = [string]$args[$i]; continue }
        '^--ref=' { $OptRef = ([string]$args[$i]).Substring(6); continue }
        '^(--desktop|-Desktop)$' { $OptDesktop = $true; continue }
        '^(--download-only|-DownloadOnly)$' { $i++; $OptDownloadOnly = [string]$args[$i]; continue }
        '^(--reset-old-data|-ResetOldData)$' { $OptResetOldData = $true; continue }
        '^(--unattended|-Unattended)$' { $OptUnattended = $true; continue }
        '^(--enable-long-paths|-EnableLongPaths)$' { $OptLongPaths = 'enable'; continue }
        '^(--skip-long-paths|-SkipLongPaths)$' { $OptLongPaths = 'skip'; continue }
        '^(--progress|-Progress)$' { $i++; $OptProgress = [string]$args[$i]; continue }
        '^--progress=' { $OptProgress = ([string]$args[$i]).Substring(11); continue }
        default { Write-Host "clio installer: unknown option '$($args[$i])'" -ForegroundColor Red; Exit-Installer 2 }
    }
}
if (-not $OptVersion -and $env:CLIO_VERSION) { $OptVersion = $env:CLIO_VERSION }
if ($env:CLIO_REF -and -not $OptRef) { $OptSource = $true; $OptRef = $env:CLIO_REF }
if ($OptSource -and $OptVersion) { Die '--version pins a release; use --ref <tag or commit> with --source' }
if (-not $OptSource -and $OptRef) { Die '--ref pins source; add --source' }
if ($OptProgress -notin @('human', 'json')) { Die "--progress takes human or json, not '$OptProgress'" }
$ProgressJson = $OptProgress -eq 'json'
if ($OptLongPaths -notin @('ask', 'enable', 'skip')) { Die "CLIO_LONG_PATHS takes ask, enable or skip, not '$OptLongPaths'" }

function Test-CanAsk {
    try {
        return [Environment]::UserInteractive -and -not [Console]::IsInputRedirected -and -not [Console]::IsOutputRedirected
    } catch {
        return $false
    }
}
function Read-YesNo([string]$Question, [string]$Default) {
    $answer = Read-Host $Question
    if (-not $answer) { $answer = $Default }
    return @('y', 'yes') -contains $answer.Trim().ToLower()
}

# ---------- Windows long paths (asked before any download) ------------
# Without them a path stops at 260 characters, which CLIO's deepest files can pass.
# The setting is machine-wide, so turning it on takes an admin prompt: only that one
# registry write runs elevated, and the rest of the install does not. Every outcome
# lets the install go on. -EnableLongPaths (CLIO_LONG_PATHS=enable) asks Windows
# without asking here first, for scripted installs; -SkipLongPaths (=skip) never asks.
$ClioFileSystemKey = 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem'
function Test-ClioLongPaths {
    try { return (Get-ItemProperty -LiteralPath $ClioFileSystemKey -Name LongPathsEnabled -ErrorAction Stop).LongPathsEnabled -eq 1 } catch { return $false }
}
function Enable-ClioLongPaths {
    $write = "Set-ItemProperty -LiteralPath '$ClioFileSystemKey' -Name LongPathsEnabled -Value 1 -Type DWord"
    try {
        $admin = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -Verb RunAs -WindowStyle Hidden -Wait -PassThru `
            -ArgumentList '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', $write
        $why = "the admin prompt ended with exit $($admin.ExitCode)"
    } catch {
        $why = 'the admin prompt was declined or could not open'
    }
    if (Test-ClioLongPaths) {
        Say 'Long path support is now on.'
    } else {
        Warn "Long path support is still off ($why). CLIO will still work; keep its folders short."
    }
}
if (-not (Test-ClioLongPaths)) {
    if ($OptLongPaths -eq 'enable') {
        Say 'Long paths are off on this PC: turning them on (-EnableLongPaths opens a Windows admin prompt).'
        Enable-ClioLongPaths
    } elseif ($OptLongPaths -eq 'skip') {
        Say 'Long paths are off on this PC; not asking, as requested. CLIO will still work.'
    } elseif ($OptUnattended -or $ProgressJson -or -not (Test-CanAsk)) {
        Say 'Long paths are off on this PC; no console to ask on, so they stay off (-EnableLongPaths turns them on). CLIO will still work.'
    } elseif (Read-YesNo ('Long paths are off on this PC. CLIO can run into problems with deep folders. ' +
            'Turn on long path support? This opens a Windows admin prompt for this one setting. [Y/n]') 'y') {
        Enable-ClioLongPaths
    } else {
        Say 'Long path support stays off. CLIO will still work; -EnableLongPaths turns it on later.'
    }
}

# ---------- desktop app (-Desktop) ------------------------------------
# The released desktop setup carries its own agent, so -Desktop needs no Python, uv or
# git. The steps are install.sh --desktop's: resolve (the release, its checksum manifest
# and its install plan), download (the setup and the plan's files at once, hashed as they
# arrive), verify, install. The setup itself prepares the agent and its tools (Office,
# pnpm) before it exits, from the plan's files when their bytes match its pins
# (CLIO_INSTALL_DOWNLOADS); its timed lines in CLIO_INSTALL_LOG become the prepare step.
# -DownloadOnly DIR stops after the verified download; -Unattended runs the setup
# silently (/S) with its defaults instead of showing its pages.
function Install-ClioDesktop {
    if ($OptSource) { Die '-Desktop installs a released app; -Source builds the agent only' }
    if (-not [Environment]::Is64BitOperatingSystem) { Die 'CLIO Desktop requires 64-bit Windows.' }
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $repo = 'https://github.com/iowarp/clio-agent'
    # A throw (a failed download or lookup) ends the run with a summary, as Die does.
    try {
        Install-ClioDesktopRelease $repo
    } catch {
        Complete-ClioRun $false $_.Exception.Message
        throw
    }
}

function Install-ClioDesktopRelease([string]$repo) {
    $triple = 'x86_64-pc-windows-msvc'
    Start-ClioStep 'resolve'
    if ($OptVersion) {
        $tag = Get-ClioReleaseTag $OptVersion
    } else {
        $tag = (Resolve-ClioRelease $OptDev).Tag
    }
    if (-not $tag) { Die 'Could not find a CLIO release on GitHub.' }
    $version = $tag -replace '^v', ''
    $asset = "CLIO.Desktop_${version}_x64-setup-bundled.exe"
    $scratch = [IO.Path]::GetFullPath((Join-Path ([IO.Path]::GetTempPath()) ("clio-desktop-" + [Guid]::NewGuid().ToString('N'))))
    New-Item -ItemType Directory -Path $scratch | Out-Null
    try {
        $sums = Join-Path $scratch 'checksums'
        try { Invoke-WebRequest -UseBasicParsing "$repo/releases/download/$tag/SHA256SUMS.$triple.bundled.txt" -OutFile $sums }
        catch { Die "Could not download the release checksums (SHA256SUMS.$triple.bundled.txt)" }
        $manifest = [IO.File]::ReadAllText($sums)
        $expected = Get-ClioManifestSha $manifest $asset
        if (-not $expected) { Die 'Release checksum missing or ambiguous; nothing installed' }
        $url = "$repo/releases/download/$tag/$asset"
        $downloads = Join-Path $scratch 'downloads'
        New-Item -ItemType Directory -Path $downloads | Out-Null
        $items = @([pscustomobject]@{ Name = $asset; Url = $url; File = (Join-Path $scratch $asset); Final = $null; Expected = $expected })
        $total = [int64]0
        try { $total = [int64]@((Invoke-WebRequest -UseBasicParsing -Method Head $url).Headers['Content-Length'])[0] } catch { }
        # The plan is authenticated by the manifest; a release without one (or a plan that
        # cannot be read) only means the setup downloads those files while it prepares.
        $planName = "install-plan.$triple.bundled.txt"
        $planSha = Get-ClioManifestSha $manifest $planName
        # A plan whose app waits for a download still in progress lets the setup start as
        # soon as it has landed, its files arriving while it installs and prepares.
        $planWaits = $false
        if (-not $OptDownloadOnly -and $planSha) {
            $planFile = Join-Path $scratch 'plan'
            try {
                Invoke-WebRequest -UseBasicParsing "$repo/releases/download/$tag/$planName" -OutFile $planFile
                if ((Get-ClioSha256 $planFile) -ne $planSha) { throw 'checksum mismatch' }
                $plan = @(Read-ClioPlan $planFile)
                $planWaits = [bool](@([IO.File]::ReadAllLines($planFile)) -cmatch '^capability waits-for-partial-downloads( |$)')
                foreach ($entry in $plan) {
                    $final = Join-Path $downloads $entry.Name
                    $items += [pscustomobject]@{ Name = $entry.Name; Url = $entry.Url; File = "$final.part"; Final = $final; Expected = $entry.Sha; Bytes = $entry.Bytes; Ranges = (Join-Path $scratch 'ranges') }
                    $total += $entry.Bytes
                }
                if ($plan.Count) { Say "Fetching what the first launch needs at the same time: $(($plan | ForEach-Object { $_.Name }) -join ', ')" }
            } catch {
                Warn "could not read the release's install plan; the setup downloads what it needs while preparing"
            }
        }
        Start-ClioStep 'download'
        Say "Downloading CLIO Desktop $tag ($asset)"
        Save-ClioDownloads $items $total $(if ($planWaits) { $items[0] } else { $null })
        Start-ClioStep 'verify'
        $installer = $items[0]
        if (-not $installer.Ok) { Die "Could not download $asset from release $tag" }
        # The bytes were hashed as they arrived. Should curl have rewritten part of the
        # file (a retry that could not resume), read it again before refusing it.
        if ($installer.Sha -ne $expected -and (Get-ClioSha256 $installer.File) -ne $expected) {
            Die 'Download checksum mismatch; nothing installed'
        }
        # Plan files that landed intact, or are still arriving for a setup that waits.
        $prefetched = @($items | Select-Object -Skip 1 | Where-Object { $_.Ok -or -not $_.Landed }).Count
        if ($OptDownloadOnly) {
            New-Item -ItemType Directory -Force -Path $OptDownloadOnly | Out-Null
            $verified = Join-Path $OptDownloadOnly $asset
            Copy-Item -LiteralPath $installer.File -Destination $verified
            Say "Verified download: $verified"
            Complete-ClioRun $true
            return
        }
        Start-ClioStep 'install'
        $setupArgs = @()
        if ($OptUnattended) {
            Say 'Installing CLIO Desktop and preparing its agent (unattended)...'
            $setupArgs = @('/S')
        } else {
            # The installer's own pages ask for the model provider and infrastructure.
            Say 'Opening the CLIO Desktop installer...'
        }
        # The setup's --prepare-runtime uses these files instead of downloading them again.
        $previousDownloads = $env:CLIO_INSTALL_DOWNLOADS
        if ($prefetched) { $env:CLIO_INSTALL_DOWNLOADS = $downloads }
        # It also appends its timed progress to CLIO_INSTALL_LOG. NSIS /S shows nothing,
        # so the file is followed here and timed as the 'prepare' step, as install.sh does.
        $previousLog = $env:CLIO_INSTALL_LOG
        $prepareLog = Join-Path $scratch 'prepare.log'
        $env:CLIO_INSTALL_LOG = $prepareLog
        try {
            if ($setupArgs.Count) { $setup = Start-Process -FilePath $installer.File -ArgumentList $setupArgs -PassThru }
            else { $setup = Start-Process -FilePath $installer.File -PassThru }
            $null = $setup.Handle  # keeps ExitCode readable once the setup exits
            $shown = [int64]0
            do {
                $exited = $setup.WaitForExit(250)
                $null = Step-ClioDownloads
                $shown = Show-ClioPrepareLog $prepareLog $shown
            } until ($exited)
        } finally {
            $env:CLIO_INSTALL_DOWNLOADS = $previousDownloads
            $env:CLIO_INSTALL_LOG = $previousLog
        }
        # The setup has prepared; a plan file still downloading is no longer used.
        Stop-ClioDownloads
        if ($setup.ExitCode -ne 0) { Die "CLIO Desktop setup did not finish (exit $($setup.ExitCode))" }
        Say 'CLIO Desktop installed. Open it from the Start menu.'
        Complete-ClioRun $true
    } finally {
        Stop-ClioDownloads
        Remove-Item -LiteralPath $scratch -Recurse -Force -ErrorAction SilentlyContinue
    }
}
if ($OptDesktop) {
    Install-ClioDesktop
    Exit-Installer 0
    return
}
if ($OptDownloadOnly) { Die '-DownloadOnly goes with -Desktop' }
if ($OptUnattended) { Die '-Unattended goes with -Desktop' }

# Without Windows long paths a file path stops at 259 characters (WinError 206). CLIO's
# deepest files sit 198 characters below its home (the document runtime's bundled npm),
# 192 below its cache, 172 below its data folder and 165 below its install folder, so a
# longer root fails part-way through the install or a later turn. These limits keep 4
# characters in reserve.
if (-not (Test-ClioLongPaths)) {
    $homeRoot = if ($env:CLIO_AGENT_HOME) { $env:CLIO_AGENT_HOME } elseif ($env:CLIO_USER_DIR) { $env:CLIO_USER_DIR } else { Join-Path $env:LOCALAPPDATA 'clio-agent' }
    $roots = @(
        @{ Name = 'CLIO home'; Path = $homeRoot; Max = 56; Variable = 'CLIO_AGENT_HOME' },
        @{ Name = 'CLIO cache folder'; Path = $env:CLIO_AGENT_CACHE_DIR; Max = 62; Variable = 'CLIO_AGENT_CACHE_DIR' },
        @{ Name = 'CLIO data folder'; Path = $env:CLIO_AGENT_DATA_DIR; Max = 82; Variable = 'CLIO_AGENT_DATA_DIR' },
        @{ Name = 'CLIO install folder'; Path = $env:CLIO_PREFIX; Max = 90; Variable = 'CLIO_PREFIX' }
    )
    foreach ($root in $roots) {
        if (-not $root.Path) { continue }
        $full = [IO.Path]::GetFullPath($root.Path).TrimEnd('\')
        if ($full.Length -le $root.Max) { continue }
        Die ("The $($root.Name) $full is $($full.Length) characters, and long paths are off on this computer: " +
            "CLIO's files in it would pass Windows' 260-character path limit and the install would fail part-way. " +
            "Choose a folder of at most $($root.Max) characters with $($root.Variable) (for example C:\clio), " +
            "or turn long paths on as an administrator " +
            "(Set-ItemProperty HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem LongPathsEnabled 1) and run the installer again.")
    }
}
$ClioInstallerRef = $env:CLIO_INSTALLER_REF
$ClioKitPackage = $env:CLIO_KIT_PACKAGE
$Protocol    = if ($env:CLIO_GIT_PROTOCOL) { $env:CLIO_GIT_PROTOCOL } else { 'https' }

switch ($Protocol) {
    'https' {
        $ClioRepo = 'https://github.com/iowarp/clio-agent.git'
    }
    'ssh' {
        $ClioRepo = 'git@github.com:iowarp/clio-agent.git'
    }
    default { Die "CLIO_GIT_PROTOCOL must be 'https' or 'ssh' (got: $Protocol)" }
}

# ---------- prerequisite checks ---------------------------------------
# uv installs Python, the environment and the locked dependencies in every mode. A
# release's prebuilt runtime carries its own uv, so that install takes it from there.
New-Item -ItemType Directory -Force -Path $Prefix, $BinDir | Out-Null
function Initialize-ClioUv([string]$From = '') {
    if (Have uv) { return }
    $bundled = if ($From) { Join-Path $From 'uv.exe' } else { '' }
    if ($bundled -and (Test-Path -LiteralPath $bundled)) {
        Say "Installing uv into $BinDir (from the CLIO runtime)"
        foreach ($exe in 'uv.exe', 'uvx.exe') {
            $source = Join-Path $From $exe
            if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination (Join-Path $BinDir $exe) -Force }
        }
    } else {
        Say "Installing uv into $BinDir"
        $env:UV_INSTALL_DIR = $BinDir
        $env:UV_NO_MODIFY_PATH = '1'
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    }
    $env:PATH = "$BinDir;$env:PATH"
    if (-not (Have uv)) { Die 'could not install uv; install it (https://docs.astral.sh/uv/) and run this installer again' }
}
# A source install needs uv only once its clone is in: uv's installer runs in its own
# process while git clones, and Complete-ClioUvInstall joins it before the first uv use.
function Start-ClioUvInstall {
    if (Have uv) { return }
    Say "Installing uv into $BinDir beside the clone"
    $env:UV_INSTALL_DIR = $BinDir
    $env:UV_NO_MODIFY_PATH = '1'
    $stem = Join-Path ([IO.Path]::GetTempPath()) ("clio-uv-" + [Guid]::NewGuid().ToString('N'))
    $script:UvInstallLogs = @("$stem.log", "$stem.err.log")
    $shell = (Get-Process -Id $PID).Path
    $script:UvInstall = Start-Process -FilePath $shell -WindowStyle Hidden -PassThru `
        -ArgumentList '-NoProfile -ExecutionPolicy Bypass -Command "Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression"' `
        -RedirectStandardOutput $script:UvInstallLogs[0] -RedirectStandardError $script:UvInstallLogs[1]
    $null = $script:UvInstall.Handle
}
function Complete-ClioUvInstall {
    $proc = $script:UvInstall
    if (-not $proc) { return }
    $script:UvInstall = $null
    $proc.WaitForExit()
    $env:PATH = "$BinDir;$env:PATH"
    if (-not (Have uv)) {
        Get-Content -LiteralPath $script:UvInstallLogs -Tail 40 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host $_ }
        Die 'could not install uv; install it (https://docs.astral.sh/uv/) and run this installer again'
    }
    Remove-Item -LiteralPath $script:UvInstallLogs -Force -ErrorAction SilentlyContinue
}
if ($OptSource) { Start-ClioUvInstall }
# uv otherwise prefers an installed Microsoft Store Python over downloading one. A
# packaged (MSIX) interpreter cannot run inside protected execution: MXC refuses to
# launch packaged apps. Use uv's own Pythons unless the operator chose otherwise.
if (-not $env:UV_MANAGED_PYTHON) { $env:UV_MANAGED_PYTHON = '1' }
if ($OptSource -and -not (Have git)) { Die 'git is required for --source' }
# The checkout this script runs from, when it runs from one (not from a URL).
$Checkout = $null
if ($PSScriptRoot) {
    $candidate = Split-Path -Parent $PSScriptRoot
    $pyproject = Join-Path $candidate 'pyproject.toml'
    if ((Test-Path -LiteralPath $pyproject) -and (Select-String -LiteralPath $pyproject -Pattern '^name = "clio-agent"' -Quiet) -and (Test-Path -LiteralPath (Join-Path $candidate '.git'))) {
        $Checkout = $candidate
    }
}

# The clone's interpreter is the longest part of the managed-tool worker's setup: fetched
# in the clone's place it lands before the dependency and Office archives unpack, while the
# CPU is still free, instead of competing with them. This process reads the ref's
# .python-version, waits for uv, and installs it; uv's lock on its Python directory makes
# the worker's and the dependency step's installs wait for it and reuse it.
$PythonPrefetch = $null
function Start-ClioPythonPrefetch {
    if (-not $OptSource -or ($Checkout -and -not $OptRef)) { return }
    $ref = if ($OptRef) { $OptRef } elseif ($OptDev) { 'develop' } else { 'main' }
    if ($ref -notmatch '^[A-Za-z0-9._/-]+$') { return }
    $uv = if ($script:UvInstall) { Join-Path $BinDir 'uv.exe' } else { (Get-Command uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source }
    if (-not $uv) { return }
    $wait = if ($script:UvInstall) { $script:UvInstall.Id } else { 0 }
    $body = @"
`$ProgressPreference = 'SilentlyContinue'
`$v = ''
try { `$v = ([string](Invoke-RestMethod -UseBasicParsing -TimeoutSec 20 -Uri 'https://raw.githubusercontent.com/iowarp/clio-agent/$ref/.python-version')).Trim() } catch { }
if (`$v -match '^3\.\d+(\.\d+)?$') {
    if ($wait) { Wait-Process -Id $wait -ErrorAction SilentlyContinue }
    if (Test-Path -LiteralPath '$uv') { & '$uv' python install --quiet `$v }
}
"@
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($body))
    $shell = (Get-Process -Id $PID).Path
    $script:PythonPrefetch = Start-Process -FilePath $shell -WindowStyle Hidden -PassThru `
        -ArgumentList "-NoProfile -ExecutionPolicy Bypass -EncodedCommand $encoded"
    $null = $script:PythonPrefetch.Handle
}
function Complete-ClioPythonPrefetch {
    if ($script:PythonPrefetch) { $script:PythonPrefetch.WaitForExit(); $script:PythonPrefetch = $null }
}
Start-ClioPythonPrefetch

# A ref's Python: its .python-version, or 3.13 for releases from before that file.
function PythonOf([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        $value = (Get-Content -LiteralPath $Path -Raw).Trim()
        if ($value) { return $value }
    }
    return '3.13'
}

# ---------- the source clone: upgrade or repair on a re-run -----------
# The clone mode installs into $Prefix\clio-agent. Running the installer again upgrades
# or repairs that clone in place; it never removes anything a person could have changed.
# Mirrors install.sh (source_clone_reusable / update_source_clone).
$ClioRepoUrls = @(
    'https://github.com/iowarp/clio-agent', 'https://github.com/iowarp/clio-agent.git',
    'git@github.com:iowarp/clio-agent', 'git@github.com:iowarp/clio-agent.git'
)

# Whether $Path holds this installer's clone to update ($true), or nothing ($false:
# absent, or what an earlier install left that holds no work: a partial clone an
# interrupted install left, a link to a checkout, a release install's environment, all
# removed here). Anything else is refused with its cause and the fix.
function Test-SourceCloneReusable([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if (-not $item) { return $false }
    if ($item.LinkType) {
        Say "Replacing the link to the checkout an in-place install used ($($item.Target)); the checkout is not touched"
        [System.IO.Directory]::Delete($Path, $false)  # the junction only, never its target
        return $false
    }
    if (-not (Test-Path -LiteralPath (Join-Path $Path '.git'))) {
        $others = @(Get-ChildItem -LiteralPath $Path -Force | Where-Object { $_.Name -notin @('.venv', '.venv.previous', 'web') })
        if ($others.Count -gt 0) {
            Die "Source reinstall refused: '$Path' exists but is not a git clone (it holds '$($others[0].Name)'). Move it aside, or choose another CLIO_PREFIX, then run this installer again."
        }
        Say "Replacing the release install's environment in '$Path' (your data lives elsewhere)"
        RemoveTree $Path
        return $false
    }
    $origin = "$(& git -C $Path config --get remote.origin.url 2>$null)".Trim()
    if (-not $origin) {
        Die "Source reinstall refused: '$Path' is a git repository with no 'origin', not a CLIO clone this installer made. Move it aside, or choose another CLIO_PREFIX, then run this installer again."
    }
    if ($origin -notin $ClioRepoUrls) {
        Die "Source reinstall refused: '$Path' is a clone of $origin, not of $ClioRepo. Move it aside, or choose another CLIO_PREFIX, then run this installer again."
    }
    # An interrupted clone has no checked-out commit (or no index yet): nothing of yours
    # is in it, so it is cloned again.
    & git -C $Path rev-parse -q --verify 'HEAD^{commit}' *> $null
    $hasCommit = $LASTEXITCODE -eq 0
    $gitDir = "$(& git -C $Path rev-parse --absolute-git-dir 2>$null)".Trim()
    if (-not $hasCommit -or -not $gitDir -or -not (Test-Path -LiteralPath (Join-Path $gitDir 'index'))) {
        Warn "'$Path' is a clone an interrupted install left unfinished; cloning it again"
        RemoveTree $Path
        return $false
    }
    return $true
}

# Bring this installer's clone at $Path to $Ref: fetch it, check it out detached, update
# the submodules. Local changes to tracked files (submodules included) refuse it.
function Update-SourceClone([string]$Path, [string]$Ref) {
    $status = @(& git -C $Path status --porcelain=v2 --untracked-files=no)
    if ($LASTEXITCODE -ne 0) { Die "could not read the state of the clone '$Path' (git's message is above)" }
    # A submodule merely at another commit, nothing changed inside it, is what an
    # interrupted clone or submodule update leaves, and the update below resets it: it is
    # not a change. (Its commits, if any were made there, stay in that submodule.)
    $dirty = @($status | Where-Object {
        $f = "$_" -split ' '
        ($f[0] -in @('1', '2', 'u')) -and -not ($f[0] -eq '1' -and $f[1] -eq '.M' -and $f[2] -cmatch '^SC\.[.U]$')
    })
    if ($dirty.Count -gt 0) {
        Die ("Source reinstall refused: the clone '$Path' has local changes to tracked files:`n" + ($dirty -join "`n") + "`nCommit them on a branch or stash them (git -C '$Path' stash), or move the clone aside, then run this installer again.")
    }
    $previous = "$(& git -C $Path rev-parse --short HEAD)".Trim()
    Say "Updating the clio-agent clone in '$Path' to $Ref (was $previous)"
    & git -C $Path fetch -q --depth 1 origin $Ref
    if ($LASTEXITCODE -ne 0) { Die "could not fetch '$Ref' into '$Path' (git's message is above): check that the branch, tag or commit exists and that this machine reaches GitHub, then run this installer again" }
    & git -C $Path checkout -q --detach FETCH_HEAD
    if ($LASTEXITCODE -ne 0) { Die "could not check out '$Ref' in '$Path' (git's message is above; untracked files in the way are never removed: move them, then run this installer again)" }
    Update-SourceSubmodules $Path
    if ($LASTEXITCODE -ne 0) { Die "could not update the submodules of '$Path' to '$Ref' (git's message is above); run this installer again" }
}

# The clone holds what the agent runs: the root files (pyproject, uv.lock), src,
# install and the submodule mount points, sparse and blobless. The site, docs and tests
# were 11,600 files and ~400 MB of the clone; 'git sparse-checkout disable' brings them.
$SourceSparseDirs = @('src', 'install', 'external')
# Of the submodules, the agent reads only the marketplace (plugins/service.py); gact-tui
# is the desktop's source, used by bundle builds.
$SourceSubmodules = @('external/clio-agent-marketplace')

# Update-SourceSubmodules PATH: init/update the submodules the agent reads. A ref whose
# .gitmodules has none of them gets all of its submodules, as before.
function Update-SourceSubmodules([string]$Path) {
    $declared = @(& git -C $Path config -f .gitmodules --get-regexp '\.path$' 2>$null | ForEach-Object { ("$_" -split ' ', 2)[1] })
    $wanted = @($declared | Where-Object { $_ -in $SourceSubmodules })
    $gitArgs = @('-C', $Path, 'submodule', 'update', '--init', '--recursive', '--depth', '1', '--') + $wanted
    & git @gitArgs
}

# Managed tools need only the locked bootstrap packages, so their preparation
# overlaps the agent's much larger sync without racing the main virtualenv.
function Start-OfficePrepare {
    if ($script:OfficeProc) { return }
    Complete-ClioUvInstall
    $worker = Join-Path $Src 'install/prepare-office.ps1'
    if (-not (Test-Path -LiteralPath $worker)) { return }  # older source ref
    Start-ClioOfficeTreeFetch
    $stem = Join-Path ([IO.Path]::GetTempPath()) ("clio-office-" + [Guid]::NewGuid().ToString('N'))
    $script:OfficeLog = "$stem.log"
    $script:OfficeErr = "$stem.err.log"
    $shell = (Get-Process -Id $PID).Path
    $workerArgs = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -SourceRoot "{1}" -PythonVersion "{2}"' -f $worker, $Src, $py
    Say "Preparing managed tools beside the agent dependencies (log: $($script:OfficeLog))"
    $script:OfficeProc = Start-Process -FilePath $shell -ArgumentList $workerArgs `
        -WorkingDirectory $Src -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $script:OfficeLog -RedirectStandardError $script:OfficeErr
    $null = $script:OfficeProc.Handle
}

function Complete-OfficePrepare {
    if (-not $script:OfficeProc) { return }
    $proc = $script:OfficeProc
    $proc.WaitForExit()
    $code = $proc.ExitCode
    $seconds = [Math]::Round(($proc.ExitTime - $proc.StartTime).TotalSeconds, 1)
    $script:OfficeProc = $null
    Remove-ClioOfficeTreeFetch
    if ($code -ne 0) {
        Get-Content -LiteralPath $script:OfficeLog, $script:OfficeErr -Tail 80 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host $_ }
        DieWith $code "Managed-tool preparation failed (logs: $($script:OfficeLog), $($script:OfficeErr)); installation was not marked ready."
    }
    # office_bootstrap's last line names each sub-job's seconds; the rest is the worker's setup.
    $detail = ''
    $done = Get-Content -LiteralPath $script:OfficeLog -ErrorAction SilentlyContinue |
        Where-Object { $_ -like '{"status": "packages_prepared"*' } | Select-Object -Last 1
    if ($done) {
        try {
            $report = $done | ConvertFrom-Json
            $parts = @("setup $([Math]::Round($seconds - $report.seconds, 1)) s")
            if ($report.timings) { foreach ($job in $report.timings.PSObject.Properties) { $parts += "$($job.Name) $($job.Value) s" } }
            $detail = " ($($parts -join ', '))"
        } catch { $detail = '' }
    }
    Say "Managed-tool preparation completed in $seconds s$detail; the managed-package step checks its files"
    $script:OfficePrepared = $true
    Remove-Item -LiteralPath $script:OfficeLog, $script:OfficeErr -Force -ErrorAction SilentlyContinue
}

function Start-BytecodePrepare {
    $precompiler = Join-Path $Src 'install/precompile_runtime.py'
    if (-not (Test-Path -LiteralPath $precompiler)) { return }  # older source ref
    $stem = Join-Path ([IO.Path]::GetTempPath()) ("clio-bytecode-" + [Guid]::NewGuid().ToString('N'))
    $script:BytecodeLog = "$stem.log"
    $script:BytecodeErr = "$stem.err.log"
    $compileArgs = '-B "{0}" --python-root "{1}" --source-root "{2}"' -f $precompiler, $Venv, (Join-Path $Src 'src')
    Say "Compiling installed bytecode beside final setup (log: $($script:BytecodeLog))"
    $script:BytecodeProc = Start-Process -FilePath $VenvPython -ArgumentList $compileArgs `
        -WorkingDirectory $Src -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $script:BytecodeLog -RedirectStandardError $script:BytecodeErr
    $null = $script:BytecodeProc.Handle
}

function Complete-BytecodePrepare {
    if (-not $script:BytecodeProc) { return }
    $proc = $script:BytecodeProc
    $proc.WaitForExit()
    $code = $proc.ExitCode
    $seconds = [Math]::Round(($proc.ExitTime - $proc.StartTime).TotalSeconds, 1)
    $script:BytecodeProc = $null
    if ($code -ne 0) {
        Get-Content -LiteralPath $script:BytecodeLog, $script:BytecodeErr -Tail 80 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host $_ }
        DieWith $code "Bytecode preparation failed (logs: $($script:BytecodeLog), $($script:BytecodeErr)); installation was not marked ready."
    }
    Say "Installed bytecode prepared in $seconds s"
    Remove-Item -LiteralPath $script:BytecodeLog, $script:BytecodeErr -Force -ErrorAction SilentlyContinue
}

# ---------- a release's prebuilt runtime -------------------------------
# A release that publishes the desktop's runtime archive (gact-runtime-*.tar.zst, named
# with its SHA-256 by the bundled install plan) installs from it instead of PyPI: one
# download, unpacked while it arrives, holding the agent, its Python and every locked
# dependency laid out and compiled, with Office and the document environment the managed
# packages adopt. The runtime is unpacked into the environment directory and the
# environment is a virtualenv over it, so the launchers, upgrades, data check and
# uninstall are the PyPI install's. A release without the archive installs from PyPI;
# CLIO_INSTALL_FROM=pypi chooses PyPI.
$ClioRuntimeTriple = 'x86_64-pc-windows-msvc'
$ClioUnpackers = @()
$ClioUnpacked = [int64]0
function Get-ClioRuntimePlan([string]$Tag, [string]$Scratch) {
    if ($env:CLIO_INSTALL_FROM -eq 'pypi') { return $null }
    if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64' -and $env:PROCESSOR_ARCHITEW6432 -ne 'AMD64') { return $null }
    $tar = Join-Path $env:SystemRoot 'System32\tar.exe'
    if (-not (Test-Path -LiteralPath $tar)) { return $null }
    $plan = @(Get-ClioReleasePlan $Tag $Scratch 'installing from PyPI')
    if (@($plan | Where-Object { $_.Kind -eq 'runtime' -and $_.Name -like 'gact-runtime-*.tar.zst' }).Count -ne 1) { return $null }
    return $plan
}

# Get-ClioReleasePlan TAG SCRATCH FALLBACK [NAME]: release TAG's plan file NAME (its bundled
# install plan for this target), read only when it matches the release's SHA256SUMS.
function Get-ClioReleasePlan([string]$Tag, [string]$Scratch, [string]$Fallback, [string]$Name = "install-plan.$($script:ClioRuntimeTriple).bundled.txt") {
    $base = "https://github.com/iowarp/clio-agent/releases/download/$Tag"
    $sums = Join-Path $Scratch 'checksums'
    $planFile = Join-Path $Scratch 'plan'
    try { Invoke-WebRequest -UseBasicParsing "$base/SHA256SUMS.$($script:ClioRuntimeTriple).bundled.txt" -OutFile $sums } catch { return }
    $planSha = Get-ClioManifestSha ([IO.File]::ReadAllText($sums)) $Name
    if (-not $planSha) { return }
    try { Invoke-WebRequest -UseBasicParsing "$base/$Name" -OutFile $planFile } catch { return }
    if ((Get-ClioSha256 $planFile) -ne $planSha) {
        Warn "release $Tag's install plan does not match its checksum; $Fallback"
        return
    }
    Read-ClioPlan $planFile
}

# Start-ClioOfficeTreeFetch: a source install's LibreOffice as the newest release lays it
# (scripts/build_office_tree.py): about 200 MB that the Office worker unpacks while it
# downloads, instead of TDF's 373 MB MSI, whose CAB expands one file at a time. Its line
# is a plan file of its own (office-plan.TRIPLE.txt), not in the bundled install plan,
# whose every line the desktop and runtime installs fetch. The SHA-256 is written beside
# the download; office.py takes the tree only when its name is this checkout's layout
# and its bytes match. Otherwise the worker lays Office from the MSI.
$OfficeTreeScratch = $null
$OfficeTreeFetch = $null
function Start-ClioOfficeTreeFetch {
    if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64' -and $env:PROCESSOR_ARCHITEW6432 -ne 'AMD64') { return }
    try {
        $release = Resolve-ClioRelease $true
        if (-not $release) { return }
        $scratch = Join-Path ([IO.Path]::GetTempPath()) ("clio-office-tree-" + [guid]::NewGuid())
        New-Item -ItemType Directory -Force -Path $scratch | Out-Null
        $script:OfficeTreeScratch = $scratch
        $plan = @(Get-ClioReleasePlan $release.Tag $scratch "laying LibreOffice from TDF's MSI" "office-plan.$($script:ClioRuntimeTriple).txt")
        $entry = @($plan | Where-Object { $_.Kind -eq 'office' -and $_.Name -like 'office-*-windows-x64.tar.zst' }) | Select-Object -First 1
        if (-not $entry) { return }
        $downloads = Join-Path $scratch 'downloads'
        New-Item -ItemType Directory -Force -Path $downloads | Out-Null
        $final = Join-Path $downloads $entry.Name
        [IO.File]::WriteAllText("$final.sha256", $entry.Sha)
        $running = @($script:ClioSideFetches).Count
        Start-ClioSideFetch ([pscustomobject]@{ Name = $entry.Name; Url = $entry.Url; File = "$final.part"; Final = $final })
        if (@($script:ClioSideFetches).Count -eq $running) { return }  # no curl.exe
        $script:OfficeTreeFetch = $script:ClioSideFetches[-1]
        $env:CLIO_INSTALL_DOWNLOADS = $downloads
        Say "Fetching LibreOffice as release $($release.Tag) lays it ($($entry.Name), $([math]::Round($entry.Bytes / 1e6)) MB)"
    } catch {
        Warn "could not fetch the release's LibreOffice ($_); laying it from TDF's MSI"
    }
}

# Start-ClioUnpackers DIR ITEM [PARTS]: tar.exe processes that unpack ITEM's archive into DIR while
# it downloads, one per PARTS filter (the runtime archive's parts by default). Defender scans each new file as it is closed, so one process leaves the
# disk idle between files; each process writes its own part of the tree and skips the
# rest, which costs only decompression. Each is fed by its own thread reading the growing
# download, so a process held up by its files never holds up the others.
$ClioFeeders = @()
$ClioFeed = $null
$ClioFeederScript = {
    param([string]$File, [string]$Final, $Out, $Feed)
    $buffer = New-Object byte[] 1048576
    $in = $null
    $read = [int64]0
    try {
        while (-not $Feed.Abort) {
            if (-not $in) {
                foreach ($path in $File, $Final) {
                    if ($in -or -not [IO.File]::Exists($path)) { continue }
                    try { $in = [IO.File]::Open($path, 'Open', 'Read', 'ReadWrite, Delete') } catch { }
                }
                if (-not $in) { Start-Sleep -Milliseconds 50; continue }
            }
            $n = $in.Read($buffer, 0, $buffer.Length)
            if ($n -gt 0) { $Out.Write($buffer, 0, $n); $read += $n; continue }
            if ($Feed.Length -ge 0 -and $read -ge $Feed.Length) { break }
            Start-Sleep -Milliseconds 50
        }
    } finally {
        if ($in) { $in.Dispose() }
        try { $Out.Close() } catch { }
    }
    $read
}
function Start-ClioUnpackers([string]$Dir, $Item, [string[]]$Parts = $null) {
    $parts = $Parts
    if (-not $parts) {
        $site = 'gact-runtime/python/Lib/site-packages'
        $documents = @('gact-runtime/documents/office', 'gact-runtime/documents/node_modules', 'gact-runtime/documents/office-profile.zip')
        $parts = @(
            (@($documents | ForEach-Object { "--include `"$_`"" }) -join ' '),
            "--include `"$site/[_a-b]*`"",
            "--include `"$site/[c-k]*`"",
            "--include `"$site/[l-z]*`"",
            ((@($documents | ForEach-Object { "--exclude `"$_`"" }) + "--exclude `"$site/[_a-z]*`"") -join ' ')
        )
    }
    $script:ClioUnpackers = @(foreach ($part in $parts) {
        $info = New-Object Diagnostics.ProcessStartInfo (Join-Path $env:SystemRoot 'System32\tar.exe')
        $info.Arguments = "-x -f - --strip-components 1 -C `"$Dir`" $part"
        $info.UseShellExecute = $false
        $info.RedirectStandardInput = $true
        [Diagnostics.Process]::Start($info)
    })
    $script:ClioFeed = [hashtable]::Synchronized(@{ Length = [int64]-1; Abort = $false })
    $script:ClioFeeders = @(foreach ($unpacker in $script:ClioUnpackers) {
        $shell = [PowerShell]::Create()
        $null = $shell.AddScript($script:ClioFeederScript).AddArgument($Item.File).AddArgument($Item.Final).AddArgument($unpacker.StandardInput.BaseStream).AddArgument($script:ClioFeed)
        [pscustomobject]@{ Shell = $shell; Handle = $shell.BeginInvoke() }
    })
}

# Stop-ClioUnpackers LENGTH: the archive is complete at LENGTH bytes (feed it to the end),
# or -1 (the download failed: stop now). Sets $ClioUnpacked to the fewest bytes any
# unpacker was fed.
function Stop-ClioUnpackers([int64]$Length) {
    if ($Length -ge 0) { $script:ClioFeed.Length = $Length } else { $script:ClioFeed.Abort = $true }
    $fed = @(foreach ($feeder in $script:ClioFeeders) {
        try { [int64](@($feeder.Shell.EndInvoke($feeder.Handle))[-1]) } catch { [int64]-1 }
        $feeder.Shell.Dispose()
    })
    $script:ClioUnpacked = if ($fed.Count) { [int64](($fed | Measure-Object -Minimum).Minimum) } else { [int64]-1 }
    foreach ($unpacker in $script:ClioUnpackers) {
        if (-not $unpacker.WaitForExit(120000)) { try { $unpacker.Kill() } catch { } }
    }
}

# Install-ClioSourceDeps PYTHON: a fresh source clone's locked dependencies as the newest
# release built them (scripts/build_source_deps.py): one archive of the virtualenv, with
# its bytecode, unpacked into $Venv while it downloads, instead of uv fetching, laying and
# compiling ~200 wheels file by file. Its line is a plan file of its own
# (deps-plan.TRIPLE.txt). It is taken only when its name carries the SHA-256 of this
# clone's uv.lock and PYTHON's minor version; the virtualenv is then pointed at this
# machine's PYTHON, and `uv sync --locked` installs the project over it as over any
# environment, so the result is the environment uv sync makes. Any failure removes $Venv
# and uv sync installs everything. CLIO_SOURCE_DEPS_ARCHIVE names a local archive instead
# (a build's own, before a release publishes it).
function Install-ClioSourceDeps([string]$Python) {
    if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64' -and $env:PROCESSOR_ARCHITEW6432 -ne 'AMD64') { return }
    $tar = Join-Path $env:SystemRoot 'System32\tar.exe'
    $lock = Join-Path $Src 'uv.lock'
    if ((Test-Path -LiteralPath $Venv) -or -not (Test-Path -LiteralPath $tar) -or -not (Test-Path -LiteralPath $lock)) { return }
    if ($Python -notmatch '^3\.(\d+)') { return }
    $minor = $Matches[1]
    $name = "clio-deps-$((Get-ClioSha256 $lock).Substring(0, 16))-py3$minor-windows-x64.tar.zst"
    $scratch = Join-Path ([IO.Path]::GetTempPath()) ("clio-deps-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Force -Path $scratch | Out-Null
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $failed = ''
    try {
        $local = $env:CLIO_SOURCE_DEPS_ARCHIVE
        if ($local) {
            if ((Split-Path -Leaf $local) -ne $name -or -not (Test-Path -LiteralPath $local -PathType Leaf)) {
                Say "CLIO_SOURCE_DEPS_ARCHIVE is not $name; uv installs the dependencies"
                return
            }
            $item = [pscustomobject]@{ Name = $name; File = $local; Final = $local; Bytes = (Get-Item -LiteralPath $local).Length }
        } else {
            $release = Resolve-ClioRelease $true
            if (-not $release) { return }
            $plan = @(Get-ClioReleasePlan $release.Tag $scratch 'uv installs the dependencies' "deps-plan.$($script:ClioRuntimeTriple).txt")
            $entry = @($plan | Where-Object { $_.Kind -eq 'deps' -and $_.Name -eq $name }) | Select-Object -First 1
            if (-not $entry) {
                Say "Release $($release.Tag) has no dependencies prebuilt for this clone's uv.lock; uv installs them"
                return
            }
            $final = Join-Path $scratch $entry.Name
            $item = [pscustomobject]@{ Name = $entry.Name; Url = $entry.Url; File = "$final.part"; Final = $final; Expected = $entry.Sha; Bytes = $entry.Bytes; Ranges = (Join-Path $scratch 'ranges') }
        }
        Complete-ClioUvInstall
        # uv fetches the interpreter while the archive unpacks.
        $uvExe = (Get-Command uv -CommandType Application | Select-Object -First 1).Source
        $fetchPython = Start-Process -FilePath $uvExe -ArgumentList "python install --quiet $Python" -WindowStyle Hidden -PassThru
        $null = $fetchPython.Handle
        Say "Downloading and unpacking the locked dependencies ($name, $([math]::Round($item.Bytes / 1e6)) MB)"
        New-Item -ItemType Directory -Force -Path $Venv | Out-Null
        $site = 'clio-deps/Lib/site-packages'
        # Ten parts of 3,000-6,000 files each (Defender's cost is per file); the last is
        # everything outside them: the launchers, and names that are not lowercase.
        $parts = @(@('[_a-b]', '[c-f]', '[g-k]', 'l', '[m-n]', 'o', 'p[a-x]', 'p[y-z]', '[q-z]') | ForEach-Object { "--include `"$site/$_*`"" }) + "--exclude `"$site/[_a-z]*`""
        Start-ClioUnpackers $Venv $item $parts
        if ($local) {
            $whole = $true
        } else {
            try { Save-ClioDownloads @($item) $item.Bytes } catch { $failed = "$_" }
            Stop-ClioDownloads
            $whole = -not $failed -and $item.Ok -and $item.Sha -eq $item.Expected
        }
        Stop-ClioUnpackers $(if ($whole) { $item.Bytes } else { -1 })
        $bad = @($script:ClioUnpackers | Where-Object { $_.ExitCode -ne 0 })
        if (-not $failed -and -not $whole) { $failed = "the download of $name failed or does not match its checksum" }
        elseif (-not $failed -and $script:ClioUnpacked -ne $item.Bytes) { $failed = "only $($script:ClioUnpacked) of $($item.Bytes) bytes of $name were unpacked" }
        elseif (-not $failed -and $bad.Count) { $failed = "tar.exe could not unpack $name (exit $($bad[0].ExitCode))" }
        $fetchPython.WaitForExit()
        if (-not $failed) {
            # The archive holds no pyvenv.cfg: uv writes this machine's, keeping what is installed.
            & uv venv --quiet --allow-existing --relocatable --python $Python $Venv
            if ($LASTEXITCODE -ne 0) { $failed = "uv could not make a virtualenv of $name (exit $LASTEXITCODE)" }
        }
    } catch {
        $failed = "$_"
    } finally {
        Remove-Item -LiteralPath $scratch -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($failed) {
        Warn "$failed; uv installs the dependencies"
        RemoveTree $Venv
        return
    }
    Say "Prebuilt dependencies laid in $([math]::Round($clock.Elapsed.TotalSeconds, 1)) s"
}

# New-ClioLaunchers SITE WHEEL: a wheel of no code whose entry points are the runtime's
# console and GUI scripts, so installing it into the virtualenv writes the Scripts\*.exe
# launchers (clio-agent.exe, clio_run.exe, ...) a PyPI install has.
function New-ClioLaunchers([string]$Site, [string]$Wheel) {
    $sections = [ordered]@{ console_scripts = [ordered]@{}; gui_scripts = [ordered]@{} }
    $read = {
        param([string[]]$Lines)
        $section = ''
        foreach ($line in $Lines) {
            $line = $line.Trim()
            if ($line -match '^\[(.+)\]$') { $section = $Matches[1]; continue }
            if (-not $sections.Contains($section) -or $line -notmatch '^([^=\s]+)\s*=\s*(.+)$') { continue }
            if (-not $sections[$section].Contains($Matches[1])) { $sections[$section][$Matches[1]] = $Matches[2] }
        }
    }
    foreach ($file in Get-ChildItem -LiteralPath $Site -Filter '*.dist-info' -Directory | ForEach-Object { Join-Path $_.FullName 'entry_points.txt' }) {
        if (Test-Path -LiteralPath $file) { & $read ([IO.File]::ReadAllLines($file)) }
    }
    # The runtime keeps its pure-Python packages in zips on sys.path (clio-pure.zip).
    Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
    foreach ($archive in Get-ChildItem -LiteralPath $Site -Filter '*.zip' -File) {
        $zip = [IO.Compression.ZipFile]::OpenRead($archive.FullName)
        try {
            foreach ($entry in $zip.Entries | Where-Object { $_.FullName -match '^[^/]+\.dist-info/entry_points\.txt$' }) {
                $reader = New-Object IO.StreamReader($entry.Open())
                try { & $read ($reader.ReadToEnd() -split "`r?`n") } finally { $reader.Dispose() }
            }
        } finally {
            $zip.Dispose()
        }
    }
    $info = 'clio_runtime_launchers-1.dist-info'
    $points = (@(foreach ($section in $sections.Keys) {
        "[$section]"
        foreach ($name in $sections[$section].Keys) { "$name = $($sections[$section][$name])" }
        ''
    }) -join "`n")
    $files = [ordered]@{
        "$info/METADATA" = "Metadata-Version: 2.1`nName: clio-runtime-launchers`nVersion: 1`nSummary: Launchers for the CLIO runtime this environment is built on`n"
        "$info/WHEEL" = "Wheel-Version: 1.0`nGenerator: clio-install`nRoot-Is-Purelib: true`nTag: py3-none-any`n"
        "$info/entry_points.txt" = $points
    }
    Add-Type -AssemblyName System.IO.Compression
    $utf8 = New-Object Text.UTF8Encoding($false)
    $sha = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::Open($Wheel, 'Create')
    $zip = New-Object IO.Compression.ZipArchive($stream, [IO.Compression.ZipArchiveMode]::Create)
    try {
        $record = New-Object Collections.Generic.List[string]
        foreach ($name in @($files.Keys) + "$info/RECORD") {
            $bytes = if ($name -like '*/RECORD') { $record.Add("$name,,"); $utf8.GetBytes(($record -join "`n") + "`n") } else { $utf8.GetBytes($files[$name]) }
            if ($name -notlike '*/RECORD') {
                $digest = [Convert]::ToBase64String($sha.ComputeHash($bytes)).TrimEnd('=').Replace('+', '-').Replace('/', '_')
                $record.Add("$name,sha256=$digest,$($bytes.Length)")
            }
            $entry = $zip.CreateEntry($name).Open()
            $entry.Write($bytes, 0, $bytes.Length)
            $entry.Dispose()
        }
    } finally {
        $zip.Dispose()
        $stream.Dispose()
        $sha.Dispose()
    }
}

# The protected-execution sandbox users (clio_agent.runtime.sandbox_cli.CODEX_SANDBOX_USERS).
$script:ClioSandboxUsers = @('CodexSandboxOffline', 'CodexSandboxOnline')

# Grant-ClioRuntimeAccessEarly ROOT PREVIOUS: when the runtime this install replaces
# (PREVIOUS) gave the sandbox users read access, give it to the new, still empty ROOT,
# so every unpacked file inherits it as it is created. The agent applies the same grant
# otherwise, after the unpack, rewriting the permissions of every file (7-9 s).
function Grant-ClioRuntimeAccessEarly([string]$Root, [string]$Previous) {
    if (-not $Previous) { return $false }
    $acl = (& icacls $Previous 2>$null) -join "`n"
    if ($LASTEXITCODE -ne 0) { return $false }
    foreach ($user in $script:ClioSandboxUsers) {
        if ($acl -notmatch "\\${user}:(\(I\))?\(OI\)\(CI\)\(RX\)") { return $false }
    }
    $grants = @($script:ClioSandboxUsers | ForEach-Object { "${_}:(OI)(CI)(RX)" })
    & icacls $Root /grant @grants /Q *> $null
    return $LASTEXITCODE -eq 0
}

# Install-ClioRuntime PLAN SCRATCH: the runtime archive, unpacked into $Venv as it arrives,
# and the virtualenv over it. The plan's other files (the npm packages the managed-package
# step installs) download beside it into SCRATCH\downloads, which that step reads.
function Install-ClioRuntime($Plan, [string]$Scratch, [string]$Previous = '') {
    $downloads = Join-Path $Scratch 'downloads'
    New-Item -ItemType Directory -Force -Path $downloads | Out-Null
    $items = @(foreach ($entry in $Plan) {
        $final = Join-Path $downloads $entry.Name
        [pscustomobject]@{ Name = $entry.Name; Url = $entry.Url; File = "$final.part"; Final = $final; Expected = $entry.Sha; Bytes = $entry.Bytes; Ranges = (Join-Path $Scratch 'ranges'); Kind = $entry.Kind }
    })
    # The browser archives are needed only by the managed-package step. They download once
    # the runtime archive is in: sharing the line slowed it ~8 s (the critical path), while
    # the line is idle as its last blocks unpack, and that ends well before the step needs them.
    $side = @($items | Where-Object { $_.Kind -eq 'browser' })
    $items = @($items | Where-Object { $_.Kind -ne 'browser' })
    $archive = @($items | Where-Object { $_.Kind -eq 'runtime' })[0]
    $total = [int64](($items | Measure-Object -Property Bytes -Sum).Sum)
    Say "Downloading and unpacking the CLIO runtime ($($archive.Name), $([math]::Round($archive.Bytes / 1e6)) MB)"
    New-Item -ItemType Directory -Force -Path $Venv | Out-Null
    $marker = Join-Path (Split-Path -Parent $Venv) 'runtime-access.json'
    Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue
    $granted = Grant-ClioRuntimeAccessEarly $Venv $Previous
    Start-ClioUnpackers $Venv $archive
    $failed = ''
    try {
        Save-ClioDownloads $items $total
    } catch {
        $failed = "$_"
    }
    Stop-ClioDownloads
    if (-not $failed -and $side.Count) {
        Say "Fetching while it unpacks: $(($side | ForEach-Object { $_.Name }) -join ', ')"
        foreach ($item in $side) { Start-ClioSideFetch $item }
    }
    $whole = -not $failed -and $archive.Ok -and $archive.Sha -eq $archive.Expected
    Stop-ClioUnpackers $(if ($whole) { $archive.Bytes } else { -1 })
    $bad = @($script:ClioUnpackers | Where-Object { $_.ExitCode -ne 0 })
    if (-not $failed -and -not $whole) { $failed = "the download of $($archive.Name) failed or does not match its checksum" }
    elseif (-not $failed -and $script:ClioUnpacked -ne $archive.Bytes) { $failed = "only $($script:ClioUnpacked) of $($archive.Bytes) bytes of $($archive.Name) were unpacked" }
    elseif (-not $failed -and $bad.Count) { $failed = "tar.exe could not unpack $($archive.Name) (exit $($bad[0].ExitCode))" }
    Remove-Item -LiteralPath $archive.Final, $archive.File -Force -ErrorAction SilentlyContinue
    if ($failed) { return $failed }
    if (-not (Test-Path -LiteralPath (Join-Path $Venv 'runtime.json'))) { return "$($archive.Name) holds no runtime.json" }
    if ($granted) {
        # Tells the agent's protected-execution step that the access is already in place.
        $record = [ordered]@{ schema = 1; runtime = (Split-Path -Leaf $Venv); users = $script:ClioSandboxUsers }
        Set-Content -LiteralPath $marker -Value (ConvertTo-Json $record -Compress) -Encoding ascii
    }
    # The managed-package step takes the plan's npm packages from here.
    $env:CLIO_INSTALL_DOWNLOADS = $downloads
    # The managed packages, Office included, are prepared from the runtime's own
    # interpreter while the environment, the data check and the store migration below
    # are done: it is the longest step, and it reads none of them.
    $site = Join-Path $Venv 'python\Lib\site-packages\clio_agent'
    $docInstall = Join-Path $site 'runtime\document_install.py'
    if ((Test-Path -LiteralPath $docInstall) -and (Test-Path -LiteralPath (Join-Path $site 'gact\store_migrate_command.py'))) {
        # On a fresh home there is no data to check first, so a document step that
        # upgrades the store itself does so while the packages install (as install.sh).
        $script:DocsMigrate = $FreshHome -and (Select-String -LiteralPath $docInstall -SimpleMatch '_run_store_migrations' -Quiet)
        $providerFlag = ''
        if ($ProviderSupport -and (Select-String -LiteralPath $docInstall -SimpleMatch 'provider-support' -Quiet)) { $providerFlag = " --provider-support $ProviderSupport" }
        if ($script:DocsMigrate) {
            Say 'Installing managed Python/uv and Node/pnpm packages and Office rendering, and creating the CLIO store'
            $script:DocJob = Start-ClioBackground (Join-Path $Venv 'python\python.exe') "-m clio_agent.runtime.document_install --setup-protected-execution$providerFlag" 'documents'
        } else {
            Say 'Installing managed Python/uv and Node/pnpm packages and Office rendering'
            $script:DocJob = Start-ClioBackground (Join-Path $Venv 'python\python.exe') "-m clio_agent.runtime.document_install --setup-protected-execution --skip-store-migrations$providerFlag" 'documents' -Live
        }
    }
    Initialize-ClioUv (Join-Path $Venv 'bin')
    $runtimePython = Join-Path $Venv 'python\python.exe'
    & uv venv --quiet --allow-existing --system-site-packages --python $runtimePython $Venv
    if ($LASTEXITCODE -ne 0) { return "uv could not create the environment over the CLIO runtime (exit $LASTEXITCODE)" }
    $wheel = Join-Path $Scratch 'clio_runtime_launchers-1-py3-none-any.whl'
    New-ClioLaunchers (Join-Path $Venv 'python\Lib\site-packages') $wheel
    & uv pip install --quiet --offline --no-deps --python (Join-Path $Venv 'Scripts\python.exe') $wheel
    if ($LASTEXITCODE -ne 0) { return "uv could not install the CLIO launchers (exit $LASTEXITCODE)" }
    # A PyPI environment has uv beside its Python too (the uv package's scripts).
    foreach ($name in 'uv.exe', 'uvx.exe') {
        $from = Join-Path $Venv "bin\$name"
        $to = Join-Path $Venv "Scripts\$name"
        if (-not (Test-Path -LiteralPath $from) -or (Test-Path -LiteralPath $to)) { continue }
        try { New-Item -ItemType HardLink -Path $to -Value $from -ErrorAction Stop | Out-Null } catch { Copy-Item -LiteralPath $from -Destination $to }
    }
    return ''
}

# Start-ClioBackground FILE ARGS NAME [-Live]: FILE ARGS running beside the steps that
# follow. Its output is kept in a log that Complete-ClioBackground shows once it is
# needed, or with -Live shown as it runs.
function Start-ClioBackground([string]$File, [string]$Arguments, [string]$Name, [switch]$Live) {
    $log = Join-Path ([System.IO.Path]::GetTempPath()) "clio-$Name-$([guid]::NewGuid()).log"
    try {
        $process = if ($Live) {
            Start-Process -FilePath $File -ArgumentList $Arguments -NoNewWindow -PassThru
        } else {
            Start-Process -FilePath $File -ArgumentList $Arguments -NoNewWindow -PassThru -RedirectStandardOutput $log -RedirectStandardError "$log.err"
        }
    } catch {
        return $null
    }
    # Windows PowerShell reports no exit code for a process whose handle was never read.
    $null = $process.Handle
    [pscustomobject]@{ Process = $process; Log = $(if ($Live) { '' } else { $log }); LastLine = '' }
}
# Complete-ClioBackground JOB: wait for JOB, show its kept output, return its exit code.
# The last line of its kept output stays in JOB.LastLine.
function Complete-ClioBackground($job) {
    $job.Process.WaitForExit()
    if ($job.Log) {
        foreach ($file in $job.Log, "$($job.Log).err") {
            if (Test-Path -LiteralPath $file) {
                $lines = @(Get-Content -LiteralPath $file | Where-Object { $_ })
                $lines | ForEach-Object { Write-Host $_ }
                if ($file -eq $job.Log -and $lines.Count) { $job.LastLine = $lines[-1] }
                Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
            }
        }
    }
    return $job.Process.ExitCode
}

# Set-ClioVenvAside VENV: rename the installed environment to VENV.previous and return
# that path ('' when nothing is installed). The rename is one step, so a CLIO that still
# runs from VENV leaves it whole and stops the install: moving it file by file would
# split it between the two directories.
function Set-ClioVenvAside([string]$Venv) {
    if (-not (Test-Path -LiteralPath $Venv)) { return '' }
    $previous = "$Venv.previous"
    Remove-ClioTreeLater $previous
    try {
        [IO.Directory]::Move($Venv, $previous)
    } catch {
        $root = [IO.Path]::GetFullPath($Venv).TrimEnd('\') + '\'
        $users = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($root, [StringComparison]::OrdinalIgnoreCase) })
        $detail = if ($users.Count) { ' These processes run from it: ' + (($users | ForEach-Object { "$($_.ProcessId) $($_.CommandLine)" } | Select-Object -First 6) -join '; ') + '.' } else { '' }
        Die "CLIO is still running from $Venv, so it cannot be replaced.$detail Stop it ('clio stop', or quit CLIO Desktop; CLIO's services, such as model servers, may need ending too) and run this installer again; the installed CLIO is unchanged."
    }
    return $previous
}

# ---------- install clio-agent ----------------------------------------
try {
$Venv = Join-Path $Prefix 'clio-agent\.venv'
$Src = $null
$ClioVersion = $null
$RuntimePlan = $null
$RuntimeScratch = $null
$DocJob = $null
$OfficePrepared = $false
$DocsMigrate = $false
# The CLIO this install replaces, named if the data check below cannot tell.
$PreviousVersion = $null
$PreviousPython = Join-Path $Venv 'Scripts\python.exe'
if (Test-Path $PreviousPython) {
    try {
        $PreviousVersion = (& $PreviousPython -c "from importlib.metadata import version; print(version('clio-agent'))").Trim()
    } catch {
        $PreviousVersion = $null
    }
}
# A fresh home: CLIO's default folders hold nothing but this install, and no setting
# moves its data elsewhere. No older CLIO wrote a store there, so the data check below
# is skipped. Were one found anyway, the store upgrade refuses it and says so, as the
# check would have.
$FreshHome = -not $PreviousVersion -and -not (@('CLIO_PREFIX', 'CLIO_AGENT_HOME', 'CLIO_USER_DIR', 'CLIO_AGENT_DATA_DIR', 'CLIO_AGENT_CONFIG_DIR', 'CLIO_ARC_STORE_CONFIG', 'CLIO_ARC_CTE_DIR') | Where-Object { [Environment]::GetEnvironmentVariable($_) }) -and
    -not @(Get-ChildItem -LiteralPath (Split-Path -Parent $Prefix) -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -ne 'app' }).Count -and
    -not (@((Join-Path $env:APPDATA 'clio-agent'), $LegacyPrefix, (Join-Path $HOME '.config\clio-agent')) | Where-Object { Test-Path -LiteralPath $_ })

if ($OptSource) {
    $AppDir = Join-Path $Prefix 'clio-agent'
    if ($Checkout -and -not $OptRef) {
        $Src = $Checkout
        Say "Installing the checkout in place: $Src"
        $existing = Get-Item -LiteralPath $AppDir -ErrorAction SilentlyContinue
        if ($existing -and -not $existing.LinkType) {
            if (Test-Path -LiteralPath (Join-Path $AppDir '.git')) {
                Die "Source reinstall refused: '$AppDir' is a clone that may hold your changes. Move it, or choose another CLIO_PREFIX."
            }
            RemoveTree $AppDir  # a release install's environment; data lives elsewhere
        } elseif ($existing) {
            Remove-Item -LiteralPath $AppDir -Force
        }
        New-Item -ItemType Junction -Path $AppDir -Target $Src | Out-Null
        RunNative git @('-C', $Src, 'submodule', 'update', '--init', '--recursive')
        $py = PythonOf (Join-Path $Src '.python-version')
        $syncArgs = @('sync', '--python', $py, '--extra', 'argonne', '--extra', 'flowcept')
    } else {
        $ref = if ($OptRef) { $OptRef } elseif ($OptDev) { 'develop' } else { 'main' }
        $Src = $AppDir
        if (Test-SourceCloneReusable $Src) {
            Update-SourceClone $Src $ref
        } else {
            Say "Cloning clio-agent at $ref (what the agent runs; 'git sparse-checkout disable' in the clone adds the docs, site and tests)"
            # Windows PowerShell 5.1 turns a native command's redirected stderr into an
            # error record, which ErrorActionPreference=Stop makes fatal: an annotated tag
            # clones with "warning: refs/tags/X is not a commit!". The exit code decides.
            $eap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
            try {
                & git clone --quiet --branch $ref --depth 1 --filter=blob:none --sparse $ClioRepo $Src 2>$null
            } finally { $ErrorActionPreference = $eap }
            if ($LASTEXITCODE -ne 0) {
                # A commit is not a branch or tag: fetch it by id.
                RemoveTree $Src
                RunNative git @('init', '-q', $Src)
                RunNative git @('-C', $Src, 'remote', 'add', 'origin', $ClioRepo)
                RunNative git (@('-C', $Src, 'sparse-checkout', 'set', '--cone') + $SourceSparseDirs)
                RunNative git @('-C', $Src, 'fetch', '-q', '--depth', '1', '--filter=blob:none', 'origin', $ref)
                RunNative git @('-C', $Src, 'checkout', '-q', 'FETCH_HEAD')
            } else {
                RunNative git (@('-C', $Src, 'sparse-checkout', 'set', '--cone') + $SourceSparseDirs)
            }
            $py = PythonOf (Join-Path $Src '.python-version')
            Start-OfficePrepare
            Update-SourceSubmodules $Src
            if ($LASTEXITCODE -ne 0) { Die "could not fetch the submodules of '$Src' (git's message is above); run this installer again" }
        }
        $py = PythonOf (Join-Path $Src '.python-version')
        Install-ClioSourceDeps $py
        $syncArgs = @('sync', '--locked', '--python', $py, '--extra', 'argonne', '--extra', 'flowcept')
    }
    Start-OfficePrepare
    Complete-ClioUvInstall
    Complete-ClioPythonPrefetch
    Say "Installing clio-agent deps (uv $($syncArgs -join ' '))"
    Push-Location $Src
    & uv @syncArgs
    $syncStatus = $LASTEXITCODE
    Pop-Location
    if ($syncStatus -ne 0) {
        DieWith $syncStatus "uv could not install clio-agent's dependencies (exit $syncStatus; uv's message above names the cause). Fix it and run this installer again."
    }
} else {
    if ($OptVersion) {
        $PackageVersion = $OptVersion -replace '^v', ''
        if ($PackageVersion -match '^([0-9]+\.[0-9]+\.[0-9]+)-beta\.([0-9]+)\.([0-9]+)$') {
            $PackageVersion = "$($Matches[1])b$($Matches[2]).post$($Matches[3])"
        } elseif ($PackageVersion -match '^([0-9]+\.[0-9]+\.[0-9]+)-beta\.([0-9]+)$') {
            $PackageVersion = "$($Matches[1])b$($Matches[2])"
        }
    } else {
        Say "Resolving the latest $(if ($OptDev) { 'release' } else { 'stable release' })"
        $release = Resolve-ClioRelease $OptDev
        if (-not $release) { Die 'could not read the clio-agent releases from GitHub' }
        $PackageVersion = $release.Version
    }
    $tag = Get-ClioReleaseTag $PackageVersion
    $RuntimeScratch = Join-Path ([System.IO.Path]::GetTempPath()) ("clio-runtime-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Force -Path $RuntimeScratch | Out-Null
    $RuntimePlan = Get-ClioRuntimePlan $tag $RuntimeScratch
}
if (-not $OptSource -and $RuntimePlan) {
    # The installed environment is set aside, not deleted, until the new one is in
    # place: an install that fails puts it back.
    $Previous = Set-ClioVenvAside $Venv
    New-Item -ItemType Directory -Force -Path (Join-Path $Prefix 'clio-agent') | Out-Null
    $failure = Install-ClioRuntime $RuntimePlan $RuntimeScratch $Previous | Select-Object -Last 1
    if ($failure) {
        Stop-ClioSideFetches
        if ($DocJob) { try { $DocJob.Process.Kill(); $DocJob.Process.WaitForExit() } catch { } }
        RemoveTree $Venv
        Remove-Item -LiteralPath (Join-Path $Prefix 'clio-agent\runtime-access.json') -Force -ErrorAction SilentlyContinue
        if ($Previous) { [IO.Directory]::Move($Previous, $Venv) }
        Die "could not install CLIO $PackageVersion from release $tag's runtime: $failure$(if ($Previous) { '; the previous installation is unchanged' })"
    }
    if ($Previous) { Remove-ClioTreeLater $Previous }
    $py = (& (Join-Path $Venv 'python\python.exe') -c "import sys; print('%d.%d' % sys.version_info[:2])").Trim()
    $ClioVersion = $PackageVersion
} elseif (-not $OptSource) {
    if ($RuntimeScratch) { RemoveTree $RuntimeScratch; $RuntimeScratch = $null }
    Initialize-ClioUv
    $LockDir = Join-Path ([System.IO.Path]::GetTempPath()) ("clio-lock-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Force -Path $LockDir | Out-Null
    foreach ($f in @('pyproject.toml', 'uv.lock', '.python-version')) {
        try {
            Invoke-WebRequest -UseBasicParsing -Uri "https://raw.githubusercontent.com/iowarp/clio-agent/$tag/$f" -OutFile (Join-Path $LockDir $f)
        } catch {
            if ($f -ne '.python-version') { Die "release $tag has no $f on GitHub" }
        }
    }
    $py = PythonOf (Join-Path $LockDir '.python-version')
    $Constraints = Join-Path $LockDir 'constraints.txt'
    Push-Location $LockDir
    RunNative uv @('export', '--quiet', '--frozen', '--no-hashes', '--no-emit-project', '--extra', 'argonne', '--extra', 'flowcept', '-o', $Constraints)
    Pop-Location
    $pkgSpec = "clio-agent[argonne,flowcept]==$PackageVersion"
    Say "Installing $pkgSpec from PyPI (Python $py, locked by $tag)"
    # The installed environment is set aside, not deleted, until the new one is in
    # place: an install that fails puts it back.
    $Previous = Set-ClioVenvAside $Venv
    New-Item -ItemType Directory -Force -Path (Join-Path $Prefix 'clio-agent') | Out-Null
    & uv venv --python $py $Venv
    $ok = $LASTEXITCODE -eq 0
    if ($ok) {
        & uv pip install --quiet --python (Join-Path $Venv 'Scripts\python.exe') --constraint $Constraints $pkgSpec
        $ok = $LASTEXITCODE -eq 0
    }
    if (-not $ok) {
        RemoveTree $Venv
        if ($Previous) { [IO.Directory]::Move($Previous, $Venv) }
        Die "could not install $pkgSpec$(if ($Previous) { '; the previous installation is unchanged' })"
    }
    if ($Previous) { Remove-ClioTreeLater $Previous }
    RemoveTree $LockDir
    $ClioVersion = $PackageVersion
}

$InstalledClioVersion = $null
$VenvPython = Join-Path $Venv 'Scripts\python.exe'
$PrefetchJob = $null
if ($OptSource) {
    Start-BytecodePrepare
    # Chromium and the default agents are fetches, not package work: the managed-tool
    # worker is still preparing, so they finish beside it instead of after it.
    if ($OfficeProc -and (Select-String -LiteralPath (Join-Path $Src 'src\clio_agent\runtime\document_install.py') -SimpleMatch 'prefetch-only' -Quiet -ErrorAction SilentlyContinue)) {
        $prefetchArgs = '-m clio_agent.runtime.document_install --prefetch-only'
        if ($ProviderSupport -and (Select-String -LiteralPath (Join-Path $Src 'src\clio_agent\runtime\document_install.py') -SimpleMatch 'provider-support' -Quiet -ErrorAction SilentlyContinue)) {
            $prefetchArgs += " --provider-support $ProviderSupport"
        }
        $PrefetchJob = Start-ClioBackground $VenvPython $prefetchArgs 'prefetch'
    }
}
if (Test-Path $VenvPython) {
    try {
        $InstalledClioVersion = (& $VenvPython -c "from importlib.metadata import version; print(version('clio-agent'))").Trim()
    } catch {
        $InstalledClioVersion = $null
    }
}

# A release's clio-kit launcher and Web bundle download while the store and the managed
# packages are prepared; they are completed below in their own steps.
$KitJob = $null
$WebJob = $null
if (-not $ClioKitPackage) {
    $ClioKitSpec = 'clio-kit==2.12.5'
} elseif ($ClioKitPackage.Contains('==')) {
    $ClioKitParts = $ClioKitPackage.Split('==', 2)
    $ClioKitSpec = "$($ClioKitParts[0])[science]==$($ClioKitParts[1])"
} else {
    $ClioKitSpec = "$ClioKitPackage[science]"
}
# A CLIO with its own prefix, home, data or user folder keeps clio-kit in uv tool
# folders inside its prefix, so it never replaces the user's own clio-kit; the launcher
# puts them on the agent's PATH. uv rebuilds a tool's environment for another Python by
# deleting it first: with one of its servers running, python.exe is locked and the
# environment is left gutted. So an environment with a running process is kept.
$KitToolDirs = @{}
if ($env:CLIO_PREFIX -or $env:CLIO_AGENT_HOME -or $env:CLIO_AGENT_DATA_DIR -or $env:CLIO_USER_DIR) {
    $KitToolDirs = @{ UV_TOOL_DIR = (Join-Path $Prefix 'uv-tools'); UV_TOOL_BIN_DIR = (Join-Path $Prefix 'uv-tools\bin') }
}
function Use-ClioKitToolDirs([switch]$Restore) {
    foreach ($name in $KitToolDirs.Keys) {
        if ($Restore) {
            [Environment]::SetEnvironmentVariable($name, $script:KitSavedEnv[$name])
        } else {
            if (-not $script:KitSavedEnv) { $script:KitSavedEnv = @{} }
            $script:KitSavedEnv[$name] = [Environment]::GetEnvironmentVariable($name)
            [Environment]::SetEnvironmentVariable($name, $KitToolDirs[$name])
        }
    }
}
function Get-ClioKitBusy([string]$Uv) {
    Use-ClioKitToolDirs
    try {
        $toolDir = (& $Uv tool dir 2>$null | Select-Object -First 1)
        $binDir = (& $Uv tool dir --bin 2>$null | Select-Object -First 1)
    } finally { Use-ClioKitToolDirs -Restore }
    if (-not $toolDir) { return $null }
    $envDir = [IO.Path]::GetFullPath((Join-Path $toolDir.Trim() 'clio-kit')) + '\'
    $shim = if ($binDir) { [IO.Path]::GetFullPath((Join-Path $binDir.Trim() 'clio-kit.exe')) } else { '' }
    Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -and ($_.Path.StartsWith($envDir, [StringComparison]::OrdinalIgnoreCase) -or $_.Path -ieq $shim)
    } | Select-Object -First 1
}
function Start-ClioKitInstall([string]$Uv) {
    $busy = Get-ClioKitBusy $Uv
    if ($busy) {
        $script:KitKept = "clio-kit is running ($($busy.ProcessName), pid $($busy.Id)); its environment was kept as it is. Stop it and run this installer again to update it to $ClioKitSpec."
        return $null
    }
    Use-ClioKitToolDirs
    try { return Start-ClioBackground $Uv "tool install --python $py `"$ClioKitSpec`"" 'kit' }
    finally { Use-ClioKitToolDirs -Restore }
}
$KitKept = $null
if (-not $OptSource) {
    $uvCommand = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($uvCommand) { $KitJob = Start-ClioKitInstall $uvCommand.Source }
    $tag = Get-ClioReleaseTag $(if ($InstalledClioVersion) { $InstalledClioVersion } else { $ClioVersion })
    $webver = $tag -replace '^v', ''
    $webUrl = "https://github.com/iowarp/clio-agent/releases/download/$tag/clio-web-$webver.zip"
    $webZip = Join-Path $Prefix 'clio-web.zip'
    $curl = Get-Command curl.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($curl) {
        Remove-Item -LiteralPath $webZip -Force -ErrorAction SilentlyContinue
        $WebJob = Start-ClioBackground $curl.Source "--fail --location --silent --show-error --retry 5 --retry-delay 2 --retry-connrefused --output `"$webZip`" `"$webUrl`"" 'web'
    }
}

# ---------- data an older CLIO wrote: moved aside, never deleted ------
# This release cannot open what CLIO 0.9.5.1 or older stored. `store-check` reads the
# store itself and says so in plain words (exit 5: written by 0.9.5.1 or older; 6 or
# 4: cannot tell; 3: CLIO is running). Old data is moved aside after a yes (without
# asking with -ResetOldData or when no console can ask); data whose writer cannot be
# told is moved only after a yes. `reset-store --yes` renames it, keeping every byte.
function Move-OldDataAside {
    & $VenvPython -m clio_agent.gact.store_reset_command reset --yes
    $moved = $LASTEXITCODE
    if ($moved -ne 0) {
        Warn "Your data was not moved (exit $moved, the reason is above). CLIO reports it when it starts; 'clio reset-store' moves it later."
    }
}
function Invoke-OldDataCheck {
    & $VenvPython -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('clio_agent.gact.store_reset_command') else 3)"
    if ($LASTEXITCODE -ne 0) { return }  # a release before the check: its store is its own
    Say 'Checking which CLIO version wrote your data'
    & $VenvPython -m clio_agent.gact.store_reset_command check
    $checkStatus = $LASTEXITCODE
    if ($checkStatus -eq 0) { return }
    if ($checkStatus -eq 3) {
        Warn "CLIO is running, so your data was not checked. This release cannot open data written by CLIO 0.9.5.1 or older: stop CLIO ('clio stop', or quit CLIO Desktop) and run this installer again, or run 'clio reset-store' if CLIO says your data is from an older version."
        return
    }
    if ($checkStatus -eq 5) {
        if ($OptResetOldData) {
            Say 'Moving it aside (-ResetOldData)'
        } elseif (Test-CanAsk) {
            if (-not (Read-YesNo 'Move it aside now? [Y/n]' 'y')) {
                Warn "Your data was left in place. CLIO $InstalledClioVersion cannot open it and says so when it starts. To move it later: 'clio stop', then 'clio reset-store'."
                return
            }
        } else {
            Say 'No console to ask on: moving it aside (pass -ResetOldData to say so explicitly)'
        }
    } else {
        Warn 'The installer cannot tell whether your data must be moved aside, so it does not move it on its own.'
        if ($PreviousVersion) { Say "The CLIO this install replaced was $PreviousVersion." }
        if (-not ((Test-CanAsk) -and (Read-YesNo 'Move it aside anyway? [y/N]' 'n'))) {
            Warn "Your data was left in place. If CLIO says it was written by CLIO 0.9.5.1 or older, stop CLIO and run 'clio reset-store'."
            return
        }
    }
    Move-OldDataAside
}
if ($DocsMigrate -or $FreshHome) {
    Say 'First CLIO on this machine: no earlier data to check'
} else {
    Invoke-OldDataCheck
}

# ---------- upgrade the store: migrations run here, never at boot ------
# Idempotent; a second run is a no-op. A running CLIO refuses it (store_in_use): its
# next boot runs the owed migrations once and reports them in the health rows.
$DocFlags = @('--setup-protected-execution')
if ($ProviderSupport -and (Test-Path $VenvPython)) {
    & $VenvPython -c "import importlib.util, sys; s = importlib.util.find_spec('clio_agent.runtime.document_install'); sys.exit(0 if s and '--provider-support' in open(s.origin, encoding='utf-8')    .read() else 3)"
    if ($LASTEXITCODE -eq 0) { $DocFlags += @('--provider-support', $ProviderSupport) }
}
$MigrateJob = $null
$MigrateStatus = $null
if ($DocsMigrate) {
    # The managed-package step started above upgrades the store; its summary reports it.
} else {
    & $VenvPython -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('clio_agent.gact.store_migrate_command') else 3)"
    if ($LASTEXITCODE -eq 0) {
        # The managed packages do not touch the store, so they are prepared while it migrates.
        Say 'Migrating the CLIO store (clio-agent migrate) while the managed packages are prepared'
        $MigrateJob = Start-ClioBackground $VenvPython '-m clio_agent.gact.store_migrate_command' 'migrate'
        if (-not $MigrateJob) {
            & $VenvPython -m clio_agent.gact.store_migrate_command
            $MigrateStatus = $LASTEXITCODE
        }
        $DocFlags += '--skip-store-migrations'
    } else {
        Say "clio-agent $InstalledClioVersion has no 'clio-agent migrate'; its boot migrates the store"
    }
}
# Get-ClioDocsMigrateStatus LINE: the `clio-agent migrate` exit status that the store
# upgrade in the managed-package step's summary (its last line) stands for.
function Get-ClioDocsMigrateStatus([string]$Line) {
    try { $m = ($Line | ConvertFrom-Json).store_migrations } catch { return 1 }
    if (-not $m) { return 1 }
    if ($m.status -in 'done', 'skipped') { return 0 }
    if ($m.reason -eq 'store_in_use') { return 3 }
    if ($m.status -eq 'refused') { return 5 }
    return 1
}
function Complete-ClioMigrate {
    if ($script:MigrateJob) {
        $script:MigrateStatus = Complete-ClioBackground $script:MigrateJob
        $script:MigrateJob = $null
    }
    if ($null -eq $script:MigrateStatus) { return }
    if ($script:MigrateStatus -eq 3) {
        Warn "store in use by a running CLIO: its next start runs the owed migrations once (or run 'clio stop' then 'clio-agent migrate')"
    } elseif ($script:MigrateStatus -eq 5) {
        Warn "clio-agent migrate did not run: your data was written by CLIO 0.9.5.1 or older (above); 'clio reset-store' moves it aside"
    } elseif ($script:MigrateStatus -ne 0) {
        Warn "clio-agent migrate failed (exit $($script:MigrateStatus), see the lines above): CLIO's next start retries the owed migrations once and reports them in 'clio doctor'"
    }
}

Complete-OfficePrepare
if ($PrefetchJob) {
    $null = Complete-ClioBackground $PrefetchJob
    $PrefetchJob = $null
}
if ($OfficePrepared -and (Select-String -LiteralPath (Join-Path $Src 'src\clio_agent\runtime\document_install.py') -SimpleMatch 'reuse-prepared' -Quiet -ErrorAction SilentlyContinue)) {
    $DocFlags += '--reuse-prepared'
}
if ($DocJob) {
    if ($DocsMigrate) { Say 'Finishing the managed packages, Office rendering and the CLIO store' }
    $docsStatus = Complete-ClioBackground $DocJob
    Stop-ClioSideFetches
    if ($DocsMigrate -and $docsStatus -eq 0) { $MigrateStatus = Get-ClioDocsMigrateStatus $DocJob.LastLine }
    Complete-ClioMigrate
    if ($docsStatus -ne 0) {
        DieWith $docsStatus "installing the managed packages and Office rendering failed (exit $docsStatus); the cause is in the lines above. Fix it and run this installer again: it resumes from what is already installed."
    }
} else {
    & $VenvPython -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('clio_agent.runtime.document_install') else 3)"
    if ($LASTEXITCODE -eq 0) {
        Say 'Installing managed Python/uv and Node/pnpm packages and Office rendering'
        $docArgs = @('-m', 'clio_agent.runtime.document_install') + $DocFlags
        & $VenvPython @docArgs
        $docsStatus = $LASTEXITCODE
        Complete-ClioMigrate
        if ($docsStatus -ne 0) {
            DieWith $docsStatus "installing the managed packages and Office rendering failed (exit $docsStatus); the cause is in the lines above. Fix it and run this installer again: it resumes from what is already installed."
        }
    } else {
        Complete-ClioMigrate
        # Releases before the managed document runtime set it up on their own.
        Say "clio-agent $InstalledClioVersion has no managed document runtime step; skipping it"
    }
}
if ($RuntimeScratch) {
    Stop-ClioSideFetches
    Remove-Item -LiteralPath $RuntimeScratch -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item Env:CLIO_INSTALL_DOWNLOADS -ErrorAction SilentlyContinue
}

# ---------- provision clio-kit MCP runtime ----------------------------
# Marketplace packs launch their MCP servers via the installed `clio-kit
# mcp-server <name>` launcher. The default matches this CLIO release's Kit pin;
# explicit legacy shared-runtime candidates select their science dependency union.
# Installed-tool launchers (not per-server `uvx` calls)
# avoid the concurrent cold-cache ephemeral-env race and `uv cache prune`
# deleting envs under running servers (astral-sh/uv#11694). `uv tool install`
# is idempotent (re-run is a no-op / version pin). Needs uv; without it the
# packs' tools simply stay unprovisioned until uv is installed.
if ($KitJob -or $KitKept -or (Have uv)) {
    if (-not $ClioKitPackage) {
        Say "Provisioning released clio-kit launcher ($ClioKitSpec)"
    } else {
        Say "Provisioning candidate shared clio-kit science runtime ($ClioKitSpec)"
    }
    if ($KitToolDirs.Count) { Say "clio-kit goes in this installation's own tool folder: $($KitToolDirs.UV_TOOL_DIR)" }
    if (-not $KitJob -and -not $KitKept) {
        $uvCommand = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        $KitJob = Start-ClioKitInstall $uvCommand.Source
    }
    if ($KitKept) {
        Warn $KitKept
        $kitStatus = 0
    } elseif ($KitJob) {
        $kitStatus = Complete-ClioBackground $KitJob
    } else {
        $kitStatus = 1
    }
    if ($kitStatus -ne 0) {
        Warn "clio-kit provisioning failed; marketplace pack tools will be unavailable until 'uv tool install `"$ClioKitSpec`"' succeeds and the dir from 'uv tool dir --bin' is on PATH"
    }
} else {
    Warn "uv not found - skipping clio-kit MCP runtime provisioning; install uv and rerun this installer"
}

# ---------- install the released Web frontend -------------------------
if (-not $OptSource) {
    # Web UI bundle (powers `clio --web`): optional, best-effort. The release
    # ships clio-web-<version>.zip containing a clio-web-<version>/ dist dir;
    # unpack it into $Prefix\clio-agent\web, which the launcher serves
    # same-origin. Mirrors install.sh. It downloads beside the steps above.
    try {
        if ($WebJob) {
            if ((Complete-ClioBackground $WebJob) -ne 0) { throw "could not download $webUrl" }
        } else {
            Save-ClioDownload $webUrl $webZip
        }
        $webTmp = Join-Path $Prefix '_webtmp'
        $webDir = Join-Path $Prefix 'clio-agent\web'
        RemoveTree $webTmp
        RemoveTree $webDir
        $bsdtar = Join-Path $env:SystemRoot 'System32\tar.exe'
        $unpacked = $false
        if (Test-Path -LiteralPath $bsdtar) {
            New-Item -ItemType Directory -Force -Path $webTmp | Out-Null
            & $bsdtar -x -f $webZip -C $webTmp
            $unpacked = $LASTEXITCODE -eq 0
            if (-not $unpacked) { RemoveTree $webTmp }
        }
        if (-not $unpacked) { Expand-Archive -LiteralPath $webZip -DestinationPath $webTmp -Force }
        $webInner = Join-Path $webTmp "clio-web-$webver"
        if (Test-Path $webInner) {
            Move-Item -LiteralPath $webInner -Destination $webDir
        } else {
            Move-Item -LiteralPath $webTmp -Destination $webDir
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
$launcherRef = if ($ClioInstallerRef) {
    $ClioInstallerRef
} elseif ($InstalledClioVersion) {
    Get-ClioReleaseTag $InstalledClioVersion
} else {
    'main'
}
$Raw         = "https://raw.githubusercontent.com/iowarp/clio-agent/$launcherRef/install"

# The installed release's own launcher lives in the install; `clio` on PATH pins
# this install's location, so a launcher from any release (whose built-in default
# location may differ) finds it.
$Launcher = Join-Path $BinDir 'clio.cmd'
$LibExec = Join-Path $Prefix 'libexec'
Say "Installing launcher: $Launcher"
New-Item -ItemType Directory -Force -Path $LibExec | Out-Null
if ($OptSource) {
    Copy-Item (Join-Path $Src 'install\clio.ps1') (Join-Path $LibExec 'clio.ps1') -Force
    Copy-Item (Join-Path $Src 'install\clio.cmd') (Join-Path $LibExec 'clio.cmd') -Force
} else {
    Invoke-WebRequest -UseBasicParsing -Uri "$Raw/clio.ps1" -OutFile (Join-Path $LibExec 'clio.ps1')
    Invoke-WebRequest -UseBasicParsing -Uri "$Raw/clio.cmd" -OutFile (Join-Path $LibExec 'clio.cmd')
}
Set-Content -LiteralPath $Launcher -Encoding ascii -Value @(
    '@echo off',
    "rem CLIO: the installation in $Prefix, through that installation's own launcher.",
    "if not defined CLIO_PREFIX set `"CLIO_PREFIX=$Prefix`"",
    "if not defined CLIO_BIN_DIR set `"CLIO_BIN_DIR=$BinDir`"",
    $(if ($env:CLIO_AGENT_HOME) { "if not defined CLIO_AGENT_HOME set `"CLIO_AGENT_HOME=$env:CLIO_AGENT_HOME`"" } else { 'rem default data home' }),
    "call `"$(Join-Path $LibExec 'clio.cmd')`" %*"
)
Set-Content -LiteralPath (Join-Path $BinDir 'clio.ps1') -Encoding utf8 -Value @(
    "# CLIO: the installation in $Prefix, through that installation's own launcher.",
    "if (-not `$env:CLIO_PREFIX) { `$env:CLIO_PREFIX = '$Prefix' }",
    "if (-not `$env:CLIO_BIN_DIR) { `$env:CLIO_BIN_DIR = '$BinDir' }",
    $(if ($env:CLIO_AGENT_HOME) { "if (-not `$env:CLIO_AGENT_HOME) { `$env:CLIO_AGENT_HOME = '$env:CLIO_AGENT_HOME' }" } else { '# default data home' }),
    "& '$(Join-Path $LibExec 'clio.ps1')' @args",
    'exit $LASTEXITCODE'
)

Say "Installing uninstaller: $Prefix\uninstall.ps1"
if ($OptSource) {
    Copy-Item (Join-Path $Src 'install\uninstall.ps1') (Join-Path $Prefix 'uninstall.ps1') -Force
} else {
    Invoke-WebRequest -UseBasicParsing -Uri "$Raw/uninstall.ps1" -OutFile (Join-Path $Prefix 'uninstall.ps1')
}

# ---------- finishing notes -------------------------------------------
Complete-BytecodePrepare
Say "Done."
$clioSrc = if ($OptSource) { "source: $Src" } else { "PyPI: $ClioVersion" }
Write-Host ""
Write-Host "Installed to:   $Prefix"
Write-Host "Launcher:       $Launcher"
Write-Host "clio-agent:     $clioSrc"
Write-Host "Frontend:       Web (release installs); Desktop is available with -Desktop"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Make sure $BinDir is on your PATH (it usually is on Win 10/11)."
Write-Host "  2. Run:   clio (Web), or clio start (headless service)"
Write-Host "  3. Manage the server: clio status | clio restart | clio logs"
Write-Host "  4. Tab-completion:    clio completion powershell >> `$PROFILE"
Write-Host "  5. Uninstall:         clio uninstall   (add -Purge to drop config)"
} finally {
    Stop-BytecodePrepare
    Stop-OfficePrepare
}
