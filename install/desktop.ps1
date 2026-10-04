# Install the verified CLIO Desktop x64 setup on Windows (also works under ARM emulation).
# No Python, uv, or Node prerequisite. -DownloadOnly verifies without running setup.
param(
    [string]$Version = $env:CLIO_VERSION,
    [switch]$DownloadOnly,
    [string]$DownloadDirectory = (Join-Path ([IO.Path]::GetTempPath()) 'clio-desktop-downloads')
)
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'Use desktop.sh on macOS or the bundled .deb/.rpm on Linux.'
}
if (-not [Environment]::Is64BitOperatingSystem) { throw 'CLIO Desktop requires 64-bit Windows.' }
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$repo = 'https://github.com/iowarp/clio-agent'
if (-not $Version) {
    $release = Invoke-RestMethod 'https://api.github.com/repos/iowarp/clio-agent/releases/latest'
    $Version = $release.tag_name
}
$Version = $Version -replace '^v', ''
if ($Version -match '^(\d+\.\d+\.\d+)b(\d+)$') { $Version = "$($Matches[1])-beta.$($Matches[2])" }
if ($Version -notmatch '^\d+\.\d+\.\d+(?:\.\d+|-beta\.\d+)?$') { throw "Invalid release version: $Version" }
$tag = "v$Version"
$asset = "CLIO.Desktop_${Version}_x64-setup-bundled.exe"
$checksums = 'SHA256SUMS.x86_64-pc-windows-msvc.bundled.txt'
$scratch = Join-Path ([IO.Path]::GetTempPath()) ("clio-desktop-" + [Guid]::NewGuid().ToString('N'))
$scratch = [IO.Path]::GetFullPath($scratch)
$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
if (-not $scratch.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid temporary directory' }
New-Item -ItemType Directory -Path $scratch | Out-Null
try {
    Write-Host "Downloading CLIO Desktop $tag (bundled x64)"
    $installer = Join-Path $scratch $asset
    Invoke-WebRequest "$repo/releases/download/$tag/$asset" -OutFile $installer -UseBasicParsing
    $manifest = (Invoke-WebRequest "$repo/releases/download/$tag/$checksums" -UseBasicParsing).Content
    if ($manifest -is [byte[]]) { $manifest = [Text.Encoding]::UTF8.GetString($manifest) }
    $entries = @($manifest -split '\r?\n' | Where-Object { $_ -match ('^([a-fA-F0-9]{64})  ' + [regex]::Escape($asset) + '$') })
    if ($entries.Count -ne 1) { throw 'Release checksum missing or ambiguous; nothing installed' }
    $expected = $entries[0].Substring(0, 64)
    if ((Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash -ne $expected) {
        throw 'Download checksum mismatch; nothing installed'
    }
    if ($DownloadOnly) {
        New-Item -ItemType Directory -Force -Path $DownloadDirectory | Out-Null
        $verified = Join-Path $DownloadDirectory $asset
        Copy-Item -LiteralPath $installer -Destination $verified
        Write-Host "Verified download: $verified"
    } else {
        # NSIS /S installs silently; retain the installed application's update policy.
        $setup = Start-Process -FilePath $installer -ArgumentList '/S' -Wait -PassThru -WindowStyle Hidden
        if ($setup.ExitCode -ne 0) { throw "CLIO Desktop setup failed (exit $($setup.ExitCode))" }
        Write-Host 'CLIO Desktop installed. Open it from the Start menu.'
    }
} finally {
    # Delete only the exact, validated temporary directory created by this invocation.
    if ([IO.Path]::GetFullPath($scratch).StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $scratch -Recurse -Force
    }
}
