# CLIO installer (Windows / PowerShell).
#
#   install.ps1                  the latest stable release
#   install.ps1 --dev            the latest release overall, pre-releases included
#   install.ps1 --source         source at `main`, with what main pins (submodules, uv.lock)
#   install.ps1 --dev --source   source at `develop`
#   install.ps1 -Desktop         the desktop app, bundled with its agent (x64 setup;
#                                runs under emulation on ARM); -Dev/-Version pick the release
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
#   GACT_VERSION       legacy override for TUI release tag (default: match CLIO)
#   CLIO_INSTALLER_REF pin launcher scripts   (default: the installed release's tag)
#   GACT_REF           gact-tui branch for the TUI (source mode only)
#   CLIO_KIT_PACKAGE   local candidate wheel/base spec (default: released 2.11.1)
#   CLIO_GIT_PROTOCOL  https | ssh            (default: https; source mode only)

$ErrorActionPreference = 'Stop'

function Say  ($m) { Write-Host "==> $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "!! $m"  -ForegroundColor Yellow }
# A TUI build still running in the background (Start-TuiBuild) stops when the install
# fails: its process tree, its half-built binary and its logs.
$TuiProc = $null
$TuiOut = $null
$TuiLog = $null
$TuiErr = $null
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
function Stop-TuiBuild {
    if ($script:TuiProc) {
        if (-not $script:TuiProc.HasExited) { & taskkill /T /F /PID $script:TuiProc.Id *> $null }
        Remove-Item -LiteralPath $script:TuiOut, $script:TuiLog, $script:TuiErr -Force -ErrorAction SilentlyContinue
    }
    $script:TuiProc = $null
}
function Die  ($m) { Stop-BytecodePrepare; Stop-OfficePrepare; Stop-TuiBuild; Write-Host "xx $m"  -ForegroundColor Red; exit 1 }
# DieWith CODE MESSAGE: Die, keeping the failed command's exit status.
function DieWith ([int]$code, $m) { Stop-BytecodePrepare; Stop-OfficePrepare; Stop-TuiBuild; Write-Host "xx $m" -ForegroundColor Red; exit $code }
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

$OptSource = $false
$OptDev = $false
$OptVersion = ''
$OptRef = ''
$OptDesktop = $false
$OptDownloadOnly = ''
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
        default { Write-Host "clio installer: unknown option '$($args[$i])'" -ForegroundColor Red; exit 2 }
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
    } elseif ($OptDev) {
        # The most recently published release, pre-releases included (drafts are not listed).
        $tag = (Invoke-RestMethod 'https://api.github.com/repos/iowarp/clio-agent/releases?per_page=30' |
            Where-Object { $_.tag_name -like 'v*' -and -not $_.draft } | Select-Object -First 1).tag_name
    } else {
        $tag = (Invoke-RestMethod 'https://api.github.com/repos/iowarp/clio-agent/releases/latest').tag_name
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
        Invoke-WebRequest "$repo/releases/download/$tag/$asset" -OutFile $installer -UseBasicParsing
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
    exit 0
}
if ($OptDownloadOnly) { Die '-DownloadOnly goes with -Desktop' }
$GactVersion = if ($env:GACT_VERSION)      { $env:GACT_VERSION }     else { 'latest' }
$ClioInstallerRef = $env:CLIO_INSTALLER_REF
$GactRef     = $env:GACT_REF
$ClioKitPackage = $env:CLIO_KIT_PACKAGE
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
if ($GactRef) {
    if (-not (Have git)) { Die "git required when GACT_REF is set (source-build mode)" }
    if (-not (Have go))  { Die "go (>= 1.26) required to build gact from source" }
    if (-not $OptSource) { Die 'GACT_REF builds the TUI from source and needs --source' }
}

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

# ---------- the TUI build, beside the slow stages ---------------------
# A source install builds the TUI with go (through bash). It runs in the background from
# the moment the source is in place, alongside the dependency, store, managed-package
# and clio-kit stages, at a lower CPU priority; its output goes to temporary logs,
# printed (and kept) if it fails. The binary is built beside the installed one and moved
# into place only when it built. Mirrors install.sh (start_tui_build / finish_tui_build).
$GactExe = Join-Path $Prefix 'gact.exe'
$TuiOut = Join-Path $Prefix "gact.build-$PID.exe"
$TuiStarted = $null

function Start-TuiBuild {
    if ($GactRef) {
        Say "Cloning gact-tui at $GactRef (source-build mode)"
        $gactSrc = Join-Path $Prefix 'gact-tui'
        RemoveTree $gactSrc
        & git clone --quiet --branch $GactRef --depth 1 $GactRepo $gactSrc
        if ($LASTEXITCODE -ne 0) { Die "could not clone gact-tui at '$GactRef' from $GactRepo (git's message is above)" }
        $env:GACT_TUI_ROOT = $gactSrc
    } elseif (-not ((Have go) -and (Have bash))) {
        Warn "TUI not built: go (>= 1.26) and bash are needed for it. Install them and run this installer again for the terminal UI; the desktop app works without it."
        return
    }
    $bash = (Get-Command bash -CommandType Application | Select-Object -First 1).Source
    $stem = Join-Path ([IO.Path]::GetTempPath()) ("clio-tui-build-" + [Guid]::NewGuid().ToString('N'))
    $script:TuiLog = "$stem.log"
    $script:TuiErr = "$stem.err.log"
    Say "Building the CLIO-branded TUI in the background (log: $($script:TuiLog))"
    try {
        $script:TuiProc = Start-Process -FilePath $bash -ArgumentList "./scripts/build_clio_tui.sh `"$TuiOut`"" `
            -WorkingDirectory $Src -NoNewWindow -PassThru `
            -RedirectStandardOutput $script:TuiLog -RedirectStandardError $script:TuiErr
    } finally {
        if ($GactRef) { Remove-Item Env:\GACT_TUI_ROOT -ErrorAction SilentlyContinue }
    }
    $null = $script:TuiProc.Handle  # keeps the handle, so ExitCode is readable after it exits
    $script:TuiStarted = Get-Date
    # The stages on the install's critical path go first; the compiler takes what they
    # leave. Only a speed preference: a process that already exited has no priority to set.
    try { $script:TuiProc.PriorityClass = 'BelowNormal' } catch { Warn "TUI build runs at normal priority: $($_.Exception.Message)" }
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

function Complete-TuiBuild {
    if (-not $script:TuiProc) { return }
    Say 'Waiting for the TUI build'
    $proc = $script:TuiProc
    $proc.WaitForExit()
    $code = $proc.ExitCode
    $script:TuiProc = $null
    if ($code -ne 0) {
        Remove-Item -LiteralPath $TuiOut -Force -ErrorAction SilentlyContinue
        Write-Host "---- TUI build output ($($script:TuiLog), $($script:TuiErr)) ----" -ForegroundColor Yellow
        Get-Content -LiteralPath $script:TuiLog, $script:TuiErr -Tail 80 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host $_ }
        Write-Host '---- end of TUI build output ----' -ForegroundColor Yellow
        Die "the TUI build failed (exit $code; full logs: $($script:TuiLog), $($script:TuiErr)); its cause is in the output above. Fix it (often: go >= 1.26 cannot be downloaded, or the gact-tui submodule is incomplete: git -C '$Src' submodule update --init --recursive) and run this installer again."
    }
    try {
        Move-Item -LiteralPath $TuiOut -Destination $GactExe -Force
    } catch {
        Remove-Item -LiteralPath $TuiOut -Force -ErrorAction SilentlyContinue
        Die "could not move the built TUI into place at ${GactExe}: $($_.Exception.Message). Close a running CLIO terminal UI, then run this installer again."
    }
    Remove-Item -LiteralPath $script:TuiLog, $script:TuiErr -Force -ErrorAction SilentlyContinue
    Say "TUI built in $([int]($proc.ExitTime - $proc.StartTime).TotalSeconds) s: $GactExe"
}

# ---------- install clio-agent ----------------------------------------
try {
$Venv = Join-Path $Prefix 'clio-agent\.venv'
$Src = $null
$ClioVersion = $null

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
    # The TUI builds from the source now, beside the slow stages below.
    Start-OfficePrepare
    Start-TuiBuild
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
        # The published GitHub Releases are the release list (PyPI can hold strays that
        # never became a release). The default takes the highest stable version; -Dev the
        # most recently published release, pre-releases included.
        $which = if ($OptDev) { 'any' } else { 'stable' }
        Say "Resolving the latest $(if ($OptDev) { 'release' } else { 'stable release' })"
        $releases = Invoke-WebRequest -UseBasicParsing -Uri 'https://api.github.com/repos/iowarp/clio-agent/releases?per_page=100'
        $code = @'
import json, re, sys
from packaging.version import InvalidVersion, Version
found = []
for release in json.load(sys.stdin):
    if release.get("draft"):
        continue
    tag = release.get("tag_name", "")
    match = re.fullmatch(r"v(\d+\.\d+\.\d+(?:\.\d+)?)-beta\.(\d+)(?:\.(\d+))?", tag)
    text = (f"{match[1]}b{match[2]}" + (f".post{match[3]}" if match[3] else "")) if match else tag[1:]
    try:
        version = Version(text)
    except InvalidVersion:
        continue
    if sys.argv[1] == "stable" and version.is_prerelease:
        continue
    found.append((release.get("published_at") or "", version))
# Stable: the highest stable version. --dev: the most recently published release,
# whatever its version says (an old alpha can sort above the newest beta).
print(max(v for _, v in found) if sys.argv[1] == "stable" else max(found)[1])
'@
        $PackageVersion = ($releases.Content | & uv run --quiet --no-project --python 3.13 --with packaging python -c $code $which).Trim()
        if ($LASTEXITCODE -ne 0 -or -not $PackageVersion) { Die 'could not read the clio-agent releases from GitHub' }
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

# ---------- upgrade the store: migrations run here, never at boot ------
# Idempotent; a second run is a no-op. A running CLIO refuses it (store_in_use): its
# next boot runs the owed migrations once and reports them in the health rows.
$DocFlags = @('--setup-protected-execution')
& $VenvPython -c 'import importlib.util, sys; sys.exit(0 if importlib.util.find_spec("clio_agent.gact.store_migrate_command") else 3)'
if ($LASTEXITCODE -eq 0) {
    Say 'Migrating the CLIO store (clio-agent migrate)'
    & $VenvPython -m clio_agent.gact.store_migrate_command
    $MigrateStatus = $LASTEXITCODE
    if ($MigrateStatus -eq 3) {
        Warn "store in use by a running CLIO: its next start runs the owed migrations once (or run 'clio stop' then 'clio-agent migrate')"
    } elseif ($MigrateStatus -ne 0) {
        Warn "clio-agent migrate failed (exit $MigrateStatus, see the lines above): CLIO's next start retries the owed migrations once and reports them in 'clio doctor'"
    }
    $DocFlags += '--skip-store-migrations'
} else {
    Say "clio-agent $InstalledClioVersion has no 'clio-agent migrate'; its boot migrates the store"
}

Complete-OfficePrepare
& $VenvPython -c 'import importlib.util, sys; sys.exit(0 if importlib.util.find_spec("clio_agent.runtime.document_install") else 3)'
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

# ---------- install gact ----------------------------------------------
if ($OptSource) {
    Complete-TuiBuild  # started with the source, above
} else {
    $tag = $GactVersion
    if ($tag -eq 'latest') {
        if ($InstalledClioVersion -or $ClioVersion) {
            $tag = Get-ClioReleaseTag $(if ($InstalledClioVersion) { $InstalledClioVersion } else { $ClioVersion })
        } else {
            Say "Resolving latest clio-agent release"
            $rel = Invoke-RestMethod -UseBasicParsing -Uri 'https://api.github.com/repos/iowarp/clio-agent/releases/latest'
            $tag = $rel.tag_name
            if (-not $tag) { Die "couldn't resolve clio-agent latest release tag" }
        }
    }
    # Pick the native TUI for the host CPU. Windows on ARM can run the x64
    # binary via emulation, but ship the native arm64 build when on ARM.
    $cpu = "$env:PROCESSOR_ARCHITECTURE $env:PROCESSOR_ARCHITEW6432"
    if ($cpu -match 'ARM64') { $asset = 'clio-tui-windows-arm64.exe' }
    else                     { $asset = 'clio-tui-windows-amd64.exe' }
    $url   = "https://github.com/iowarp/clio-agent/releases/download/$tag/$asset"
    Say "Downloading $asset from clio-agent $tag"
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $GactExe

    # Web UI bundle (powers `clio --web`): optional, best-effort. The release
    # ships clio-web-<version>.zip containing a clio-web-<version>/ dist dir;
    # unpack it into $Prefix\clio-agent\web, which the launcher serves
    # same-origin. Mirrors install.sh.
    $webver = $tag -replace '^v', ''
    $webUrl = "https://github.com/iowarp/clio-agent/releases/download/$tag/clio-web-$webver.zip"
    $webZip = Join-Path $Prefix 'clio-web.zip'
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $webUrl -OutFile $webZip -ErrorAction Stop
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
$gactSrc = if ($GactRef) { "source: $GactRef" } elseif ($OptSource) { "built from the checkout's submodule" } else { "release: $tag" }
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
} finally {
    Stop-BytecodePrepare
    Stop-OfficePrepare
    Stop-TuiBuild
}
