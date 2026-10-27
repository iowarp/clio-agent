# CLIO installer (Windows / PowerShell).
#
#   install.ps1                  the latest stable release
#   install.ps1 --dev            the latest release overall, pre-releases included
#   install.ps1 --source         source at `main`, with what main pins (submodules, uv.lock)
#   install.ps1 --dev --source   source at `develop`
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
#   CLIO_KIT_PACKAGE   local candidate wheel/base spec (default: released 2.10.6)
#   CLIO_GIT_PROTOCOL  https | ssh            (default: https; source mode only)

$ErrorActionPreference = 'Stop'

function Say  ($m) { Write-Host "==> $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "!! $m"  -ForegroundColor Yellow }
function Die  ($m) { Write-Host "xx $m"  -ForegroundColor Red; exit 1 }
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
for ($i = 0; $i -lt $args.Count; $i++) {
    switch -Regex ([string]$args[$i]) {
        '^(--source|-Source)$' { $OptSource = $true; continue }
        '^(--dev|-Dev)$' { $OptDev = $true; continue }
        '^(--version|-Version)$' { $i++; $OptVersion = [string]$args[$i]; continue }
        '^--version=' { $OptVersion = ([string]$args[$i]).Substring(10); continue }
        '^(--ref|-Ref)$' { $i++; $OptRef = [string]$args[$i]; continue }
        '^--ref=' { $OptRef = ([string]$args[$i]).Substring(6); continue }
        default { Write-Host "clio installer: unknown option '$($args[$i])'" -ForegroundColor Red; exit 2 }
    }
}
if (-not $OptVersion -and $env:CLIO_VERSION) { $OptVersion = $env:CLIO_VERSION }
if ($env:CLIO_REF -and -not $OptRef) { $OptSource = $true; $OptRef = $env:CLIO_REF }
if ($OptSource -and $OptVersion) { Die '--version pins a release; use --ref <tag or commit> with --source' }
if (-not $OptSource -and $OptRef) { Die '--ref pins source; add --source' }
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

# ---------- install clio-agent ----------------------------------------
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
        Say "Installing clio-agent deps (uv sync --python $py --extra argonne --extra flowcept)"
        Push-Location $Src
        RunNative uv @('sync', '--python', $py, '--extra', 'argonne', '--extra', 'flowcept')
        Pop-Location
    } else {
        $ref = if ($OptRef) { $OptRef } elseif ($OptDev) { 'develop' } else { 'main' }
        $Src = $AppDir
        if (Test-Path -LiteralPath $Src) {
            Die 'Source reinstall refused: the installation already exists. Choose a new CLIO_PREFIX or explicitly move the existing installation after migrating its user data.'
        }
        Say "Cloning clio-agent at $ref"
        & git clone --quiet --recurse-submodules --shallow-submodules --branch $ref --depth 1 $ClioRepo $Src 2>$null
        if ($LASTEXITCODE -ne 0) {
            # A commit is not a branch or tag: fetch it by id.
            RemoveTree $Src
            RunNative git @('init', '-q', $Src)
            RunNative git @('-C', $Src, 'remote', 'add', 'origin', $ClioRepo)
            RunNative git @('-C', $Src, 'fetch', '-q', '--depth', '1', 'origin', $ref)
            RunNative git @('-C', $Src, 'checkout', '-q', 'FETCH_HEAD')
            RunNative git @('-C', $Src, 'submodule', 'update', '--init', '--recursive', '--depth', '1')
        }
        $py = PythonOf (Join-Path $Src '.python-version')
        Say "Installing clio-agent deps from uv.lock (uv sync --locked --python $py --extra argonne --extra flowcept)"
        Push-Location $Src
        RunNative uv @('sync', '--locked', '--python', $py, '--extra', 'argonne', '--extra', 'flowcept')
        Pop-Location
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
    RemoveTree $Venv
    New-Item -ItemType Directory -Force -Path (Join-Path $Prefix 'clio-agent') | Out-Null
    RunNative uv @('venv', '--python', $py, $Venv)
    RunNative uv @('pip', 'install', '--quiet', '--python', (Join-Path $Venv 'Scripts\python.exe'), '--constraint', $Constraints, $pkgSpec)
    RemoveTree $LockDir
    $ClioVersion = $PackageVersion
}

$InstalledClioVersion = $null
$VenvPython = Join-Path $Venv 'Scripts\python.exe'
if (Test-Path $VenvPython) {
    try {
        $InstalledClioVersion = (& $VenvPython -c "from importlib.metadata import version; print(version('clio-agent'))").Trim()
    } catch {
        $InstalledClioVersion = $null
    }
}

& $VenvPython -c 'import importlib.util, sys; sys.exit(0 if importlib.util.find_spec("clio_agent.runtime.document_install") else 3)'
if ($LASTEXITCODE -eq 0) {
    Say 'Installing managed Python/uv and Node/pnpm packages and Office rendering'
    RunNative $VenvPython @('-m', 'clio_agent.runtime.document_install', '--setup-protected-execution')
} else {
    # Releases before the managed document runtime set it up on their own.
    Say "clio-agent $InstalledClioVersion has no managed document runtime step; skipping it"
}

# ---------- provision clio-kit MCP runtime ----------------------------
# Marketplace packs launch their MCP servers via the installed `clio-kit
# mcp-server <name>` launcher. The released default retains legacy behavior; an
# explicit candidate selects the compatible science dependency union once.
# Installed-tool launchers (not per-server `uvx` calls)
# avoid the concurrent cold-cache ephemeral-env race and `uv cache prune`
# deleting envs under running servers (astral-sh/uv#11694). `uv tool install`
# is idempotent (re-run is a no-op / version pin). Needs uv; without it the
# packs' tools simply stay unprovisioned until uv is installed.
if (Have uv) {
    if (-not $ClioKitPackage) {
        $ClioKitSpec = 'clio-kit==2.10.6'
        Say "Provisioning released clio-kit launcher ($ClioKitSpec; legacy runtime semantics)"
    } elseif ($ClioKitPackage.Contains('==')) {
        $ClioKitParts = $ClioKitPackage.Split('==', 2)
        $ClioKitSpec = "$($ClioKitParts[0])[science]==$($ClioKitParts[1])"
        Say "Provisioning candidate shared clio-kit science runtime ($ClioKitSpec)"
    } else {
        $ClioKitSpec = "$ClioKitPackage[science]"
        Say "Provisioning candidate shared clio-kit science runtime ($ClioKitSpec)"
    }
    & uv tool install --python 3.13 $ClioKitSpec
    if ($LASTEXITCODE -ne 0) {
        Warn "clio-kit provisioning failed; marketplace pack tools will be unavailable until 'uv tool install `"$ClioKitSpec`"' succeeds and the dir from 'uv tool dir --bin' is on PATH"
    }
} else {
    Warn "uv not found - skipping clio-kit MCP runtime provisioning; install uv and rerun this installer"
}

# ---------- install gact ----------------------------------------------
$GactExe = Join-Path $Prefix 'gact.exe'

if ($OptSource -and -not $GactRef) {
    if ((Have go) -and (Have bash)) {
        Say "Building CLIO-branded TUI from clio-agent submodule"
        Push-Location $Src
        RunNative bash @('./scripts/build_clio_tui.sh', $GactExe)
        Pop-Location
    } else {
        Warn "TUI not built: go (>= 1.26) and bash are needed for it. Install them and run this installer again for the terminal UI; the desktop app works without it."
    }
} elseif ($GactRef) {
    Say "Cloning gact-tui at $GactRef (source-build mode)"
    $gactSrc = Join-Path $Prefix 'gact-tui'
    RemoveTree $gactSrc
    RunNative git @('clone', '--quiet', '--branch', $GactRef, '--depth', '1', $GactRepo, $gactSrc)
    Say "Building CLIO-branded TUI"
    Push-Location $Src
    $env:GACT_TUI_ROOT = $gactSrc
    RunNative bash @('./scripts/build_clio_tui.sh', $GactExe)
    Remove-Item Env:\GACT_TUI_ROOT -ErrorAction SilentlyContinue
    Pop-Location
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
    "call `"$(Join-Path $LibExec 'clio.cmd')`" %*"
)
Set-Content -LiteralPath (Join-Path $BinDir 'clio.ps1') -Encoding utf8 -Value @(
    "# CLIO: the installation in $Prefix, through that installation's own launcher.",
    "if (-not `$env:CLIO_PREFIX) { `$env:CLIO_PREFIX = '$Prefix' }",
    "if (-not `$env:CLIO_BIN_DIR) { `$env:CLIO_BIN_DIR = '$BinDir' }",
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
