# CLIO installer (Windows / PowerShell).
#
#   install.ps1                  the latest stable release
#   install.ps1 --dev            the latest release overall, pre-releases included
#   install.ps1 --source         source at `main`, with what main pins (submodules, uv.lock)
#   install.ps1 --dev --source   source at `develop`
#   install.ps1 -Desktop         the desktop app, bundled with its agent (x64 setup;
#                                runs under emulation on ARM); -Dev/-Version pick the release
#   install.ps1 -ResetOldData    move data CLIO 0.9.5.1 or older wrote aside without asking
#                                (this release cannot open it; it is kept, never deleted)
#
# -Source/-Dev/-Version/-Ref are the same options. From a URL:
#   & ([scriptblock]::Create((irm <url>/install.ps1))) --dev
# --source run from inside a clio-agent checkout installs that checkout in place; run
# from a URL it clones. A release is installed from PyPI, constrained by that release
# tag's own uv.lock, on the Python that release declares (.python-version; 3.13 before).
#
# Pins: --version <pypi version> (release), --ref <git ref> (source). Older spellings
# still work: $env:CLIO_VERSION is --version, $env:CLIO_REF is --source --ref.
#
# Honors environment overrides:
#   CLIO_PREFIX        install root           (default: <agent data>\app)
#   CLIO_BIN_DIR       launcher location      (default: ...\WindowsApps)
#   CLIO_INSTALLER_REF pin launcher scripts   (default: the installed release's tag)
#   CLIO_KIT_PACKAGE   local candidate wheel/base spec (default: released 2.11.1)
#   CLIO_GIT_PROTOCOL  https | ssh            (default: https; source mode only)

$ErrorActionPreference = 'Stop'
# Windows PowerShell 5.1 redraws Invoke-WebRequest's progress bar for every chunk, which
# makes a large download many times slower than the network (the desktop setup took
# 10-15 minutes). Large files go through Save-ClioDownload, which shows curl's own meter.
$ProgressPreference = 'SilentlyContinue'

function Say  ($m) { Write-Host "==> $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "!! $m"  -ForegroundColor Yellow }
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
}
# Run as a file (-File, CI), the installer exits with its status. Run from a URL
# (`irm ... | iex`, or a script block) it has no file, and `exit` would close the
# user's PowerShell window and the error with it: it throws or returns instead.
function Exit-Installer ([int]$code) {
    if ($PSCommandPath) { exit $code }
    if ($code -ne 0) { throw "CLIO installer stopped (exit $code)" }
}
function Die  ($m) { Stop-BytecodePrepare; Stop-OfficePrepare; Write-Host "xx $m"  -ForegroundColor Red; Exit-Installer 1 }
# DieWith CODE MESSAGE: Die, keeping the failed command's exit status.
function DieWith ([int]$code, $m) { Stop-BytecodePrepare; Stop-OfficePrepare; Write-Host "xx $m" -ForegroundColor Red; Exit-Installer $code }
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

# Save-ClioDownload URI FILE: the large downloads (the desktop setup, the web UI).
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

# Resolve-ClioRelease DEV: the release to install from the published GitHub Releases
# (PyPI can hold strays that never became a release). Stable: the highest stable
# version. -Dev: the most recently published release, whatever its version says (an
# old alpha can sort above the newest beta). Drafts are never listed.
function Resolve-ClioRelease([bool]$Dev) {
    $releases = Invoke-RestMethod -UseBasicParsing -Uri 'https://api.github.com/repos/iowarp/clio-agent/releases?per_page=100'
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
$OptDev = $false
$OptVersion = ''
$OptRef = ''
$OptDesktop = $false
$OptDownloadOnly = ''
$OptResetOldData = $false
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
        default { Write-Host "clio installer: unknown option '$($args[$i])'" -ForegroundColor Red; Exit-Installer 2 }
    }
}
if (-not $OptVersion -and $env:CLIO_VERSION) { $OptVersion = $env:CLIO_VERSION }
if ($env:CLIO_REF -and -not $OptRef) { $OptSource = $true; $OptRef = $env:CLIO_REF }
if ($OptSource -and $OptVersion) { Die '--version pins a release; use --ref <tag or commit> with --source' }
if (-not $OptSource -and $OptRef) { Die '--ref pins source; add --source' }

# ---------- desktop app (-Desktop) ------------------------------------
# The released desktop setup carries its own agent, so -Desktop needs no Python, uv or
# git: it picks the release, downloads the bundled x64 setup, verifies it against the
# release's checksum manifest, and runs it silently. -DownloadOnly DIR stops after the
# verified download.
function Install-ClioDesktop {
    if ($OptSource) { Die '-Desktop installs a released app; -Source builds the agent only' }
    if (-not [Environment]::Is64BitOperatingSystem) { Die 'CLIO Desktop requires 64-bit Windows.' }
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $repo = 'https://github.com/iowarp/clio-agent'
    if ($OptVersion) {
        $tag = Get-ClioReleaseTag $OptVersion
    } else {
        $tag = (Resolve-ClioRelease $OptDev).Tag
    }
    if (-not $tag) { Die 'Could not find a CLIO release on GitHub.' }
    $version = $tag -replace '^v', ''
    $asset = "CLIO.Desktop_${version}_x64-setup-bundled.exe"
    $checksums = 'SHA256SUMS.x86_64-pc-windows-msvc.bundled.txt'
    $scratch = [IO.Path]::GetFullPath((Join-Path ([IO.Path]::GetTempPath()) ("clio-desktop-" + [Guid]::NewGuid().ToString('N'))))
    New-Item -ItemType Directory -Path $scratch | Out-Null
    try {
        Say "Downloading CLIO Desktop $tag ($asset)"
        $installer = Join-Path $scratch $asset
        Save-ClioDownload "$repo/releases/download/$tag/$asset" $installer
        $manifest = (Invoke-WebRequest "$repo/releases/download/$tag/$checksums" -UseBasicParsing).Content
        if ($manifest -is [byte[]]) { $manifest = [Text.Encoding]::UTF8.GetString($manifest) }
        # GitHub replaces spaces in asset names with dots; older checksum manifests
        # keep the original name. Accept sha256sum's text and binary markers.
        $entries = @($manifest -split '\r?\n' | Where-Object {
            ($_ -replace 'CLIO Desktop', 'CLIO.Desktop') -match ('^([a-fA-F0-9]{64}) [ *]' + [regex]::Escape($asset) + '$')
        })
        if ($entries.Count -ne 1) { Die 'Release checksum missing or ambiguous; nothing installed' }
        if ((Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash -ne $entries[0].Substring(0, 64)) {
            Die 'Download checksum mismatch; nothing installed'
        }
        if ($OptDownloadOnly) {
            New-Item -ItemType Directory -Force -Path $OptDownloadOnly | Out-Null
            $verified = Join-Path $OptDownloadOnly $asset
            Copy-Item -LiteralPath $installer -Destination $verified
            Say "Verified download: $verified"
            return
        }
        # The installer's own pages ask for the model provider and infrastructure, so it runs
        # with its window rather than silently.
        Say 'Opening the CLIO Desktop installer...'
        $setup = Start-Process -FilePath $installer -Wait -PassThru
        if ($setup.ExitCode -ne 0) { Die "CLIO Desktop setup did not finish (exit $($setup.ExitCode))" }
        Say 'CLIO Desktop installed. Open it from the Start menu.'
    } finally {
        Remove-Item -LiteralPath $scratch -Recurse -Force -ErrorAction SilentlyContinue
    }
}
if ($OptDesktop) {
    Install-ClioDesktop
    Exit-Installer 0
    return
}
if ($OptDownloadOnly) { Die '-DownloadOnly goes with -Desktop' }
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
# uv installs Python, the environment and the locked dependencies in every mode.
New-Item -ItemType Directory -Force -Path $Prefix, $BinDir | Out-Null
if (-not (Have uv)) {
    Say "Installing uv into $BinDir"
    $env:UV_INSTALL_DIR = $BinDir
    $env:UV_NO_MODIFY_PATH = '1'
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:PATH = "$BinDir;$env:PATH"
    if (-not (Have uv)) { Die 'could not install uv; install it (https://docs.astral.sh/uv/) and run this installer again' }
}
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
    & git -C $Path submodule update --init --recursive --depth 1
    if ($LASTEXITCODE -ne 0) { Die "could not update the submodules of '$Path' to '$Ref' (git's message is above); run this installer again" }
}

# Managed tools need only the locked bootstrap packages, so their preparation
# overlaps the agent's much larger sync without racing the main virtualenv.
function Start-OfficePrepare {
    if ($script:OfficeProc) { return }
    $worker = Join-Path $Src 'install/prepare-office.ps1'
    if (-not (Test-Path -LiteralPath $worker)) { return }  # older source ref
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
    if ($code -ne 0) {
        Get-Content -LiteralPath $script:OfficeLog, $script:OfficeErr -Tail 80 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host $_ }
        DieWith $code "Managed-tool preparation failed (logs: $($script:OfficeLog), $($script:OfficeErr)); installation was not marked ready."
    }
    Say "Managed-tool preparation completed in $seconds s; the managed-package step will verify it again"
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

# ---------- install clio-agent ----------------------------------------
try {
$Venv = Join-Path $Prefix 'clio-agent\.venv'
$Src = $null
$ClioVersion = $null
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
            Say "Cloning clio-agent at $ref"
            & git clone --quiet --branch $ref --depth 1 $ClioRepo $Src 2>$null
            if ($LASTEXITCODE -ne 0) {
                # A commit is not a branch or tag: fetch it by id.
                RemoveTree $Src
                RunNative git @('init', '-q', $Src)
                RunNative git @('-C', $Src, 'remote', 'add', 'origin', $ClioRepo)
                RunNative git @('-C', $Src, 'fetch', '-q', '--depth', '1', 'origin', $ref)
                RunNative git @('-C', $Src, 'checkout', '-q', 'FETCH_HEAD')
            }
            $py = PythonOf (Join-Path $Src '.python-version')
            Start-OfficePrepare
            RunNative git @('-C', $Src, 'submodule', 'update', '--init', '--recursive', '--depth', '1')
        }
        $py = PythonOf (Join-Path $Src '.python-version')
        $syncArgs = @('sync', '--locked', '--python', $py, '--extra', 'argonne', '--extra', 'flowcept')
    }
    Start-OfficePrepare
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
    $Previous = ''
    if (Test-Path -LiteralPath $Venv) {
        $Previous = "$Venv.previous"
        RemoveTree $Previous
        Move-Item -LiteralPath $Venv -Destination $Previous
    }
    New-Item -ItemType Directory -Force -Path (Join-Path $Prefix 'clio-agent') | Out-Null
    & uv venv --python $py $Venv
    $ok = $LASTEXITCODE -eq 0
    if ($ok) {
        & uv pip install --quiet --python (Join-Path $Venv 'Scripts\python.exe') --constraint $Constraints $pkgSpec
        $ok = $LASTEXITCODE -eq 0
    }
    if (-not $ok) {
        RemoveTree $Venv
        if ($Previous) { Move-Item -LiteralPath $Previous -Destination $Venv }
        Die "could not install $pkgSpec$(if ($Previous) { '; the previous installation is unchanged' })"
    }
    if ($Previous) { RemoveTree $Previous }
    RemoveTree $LockDir
    $ClioVersion = $PackageVersion
}

$InstalledClioVersion = $null
$VenvPython = Join-Path $Venv 'Scripts\python.exe'
if ($OptSource) { Start-BytecodePrepare }
if (Test-Path $VenvPython) {
    try {
        $InstalledClioVersion = (& $VenvPython -c "from importlib.metadata import version; print(version('clio-agent'))").Trim()
    } catch {
        $InstalledClioVersion = $null
    }
}

# ---------- data an older CLIO wrote: moved aside, never deleted ------
# This release cannot open what CLIO 0.9.5.1 or older stored. `store-check` reads the
# store itself and says so in plain words (exit 5: written by 0.9.5.1 or older; 6 or
# 4: cannot tell; 3: CLIO is running). Old data is moved aside after a yes (without
# asking with -ResetOldData or when no console can ask); data whose writer cannot be
# told is moved only after a yes. `reset-store --yes` renames it, keeping every byte.
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
Invoke-OldDataCheck

# ---------- upgrade the store: migrations run here, never at boot ------
# Idempotent; a second run is a no-op. A running CLIO refuses it (store_in_use): its
# next boot runs the owed migrations once and reports them in the health rows.
$DocFlags = @('--setup-protected-execution')
& $VenvPython -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('clio_agent.gact.store_migrate_command') else 3)"
if ($LASTEXITCODE -eq 0) {
    Say 'Migrating the CLIO store (clio-agent migrate)'
    & $VenvPython -m clio_agent.gact.store_migrate_command
    $MigrateStatus = $LASTEXITCODE
    if ($MigrateStatus -eq 3) {
        Warn "store in use by a running CLIO: its next start runs the owed migrations once (or run 'clio stop' then 'clio-agent migrate')"
    } elseif ($MigrateStatus -eq 5) {
        Warn "clio-agent migrate did not run: your data was written by CLIO 0.9.5.1 or older (above); 'clio reset-store' moves it aside"
    } elseif ($MigrateStatus -ne 0) {
        Warn "clio-agent migrate failed (exit $MigrateStatus, see the lines above): CLIO's next start retries the owed migrations once and reports them in 'clio doctor'"
    }
    $DocFlags += '--skip-store-migrations'
} else {
    Say "clio-agent $InstalledClioVersion has no 'clio-agent migrate'; its boot migrates the store"
}

Complete-OfficePrepare
& $VenvPython -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('clio_agent.runtime.document_install') else 3)"
if ($LASTEXITCODE -eq 0) {
    Say 'Installing managed Python/uv and Node/pnpm packages and Office rendering'
    $docArgs = @('-m', 'clio_agent.runtime.document_install') + $DocFlags
    & $VenvPython @docArgs
    $docsStatus = $LASTEXITCODE
    if ($docsStatus -ne 0) {
        DieWith $docsStatus "installing the managed packages and Office rendering failed (exit $docsStatus); the cause is in the lines above. Fix it and run this installer again: it resumes from what is already installed."
    }
} else {
    # Releases before the managed document runtime set it up on their own.
    Say "clio-agent $InstalledClioVersion has no managed document runtime step; skipping it"
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
if (Have uv) {
    if (-not $ClioKitPackage) {
        $ClioKitSpec = 'clio-kit==2.11.1'
        Say "Provisioning released clio-kit launcher ($ClioKitSpec)"
    } elseif ($ClioKitPackage.Contains('==')) {
        $ClioKitParts = $ClioKitPackage.Split('==', 2)
        $ClioKitSpec = "$($ClioKitParts[0])[science]==$($ClioKitParts[1])"
        Say "Provisioning candidate shared clio-kit science runtime ($ClioKitSpec)"
    } else {
        $ClioKitSpec = "$ClioKitPackage[science]"
        Say "Provisioning candidate shared clio-kit science runtime ($ClioKitSpec)"
    }
    & uv tool install --python $py $ClioKitSpec
    if ($LASTEXITCODE -ne 0) {
        Warn "clio-kit provisioning failed; marketplace pack tools will be unavailable until 'uv tool install `"$ClioKitSpec`"' succeeds and the dir from 'uv tool dir --bin' is on PATH"
    }
} else {
    Warn "uv not found - skipping clio-kit MCP runtime provisioning; install uv and rerun this installer"
}

# ---------- install the released Web frontend -------------------------
if (-not $OptSource) {
    $tag = Get-ClioReleaseTag $(if ($InstalledClioVersion) { $InstalledClioVersion } else { $ClioVersion })
    # Web UI bundle (powers `clio --web`): optional, best-effort. The release
    # ships clio-web-<version>.zip containing a clio-web-<version>/ dist dir;
    # unpack it into $Prefix\clio-agent\web, which the launcher serves
    # same-origin. Mirrors install.sh.
    $webver = $tag -replace '^v', ''
    $webUrl = "https://github.com/iowarp/clio-agent/releases/download/$tag/clio-web-$webver.zip"
    $webZip = Join-Path $Prefix 'clio-web.zip'
    try {
        Save-ClioDownload $webUrl $webZip
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
