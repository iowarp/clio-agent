#requires -Version 7.0
<#
.SYNOPSIS
Measure one installed Windows desktop launch through tauri-driver/WebView2.
.DESCRIPTION
Set the reference store's env.sh variables in the calling shell first, with no
model provider. Reuse HomeDir and LocalAppData for the repeat. This wrapper owns
only its driver and the application identities recorded by desktop_launch.mjs.
#>
param(
    [Parameter(Mandatory)][string]$App,
    [Parameter(Mandatory)][string]$HomeDir,
    [Parameter(Mandatory)][string]$LocalAppData,
    [Parameter(Mandatory)][string]$Out,
    [Parameter(Mandatory)][string]$NativeDriver,
    [string]$TauriDriver = 'tauri-driver.exe',
    [string]$BootLog = '',
    [ValidatePattern('^[a-zA-Z0-9_-]+$')][string]$Label = 'launch',
    [ValidateRange(1, 65534)][int]$Port = 4455,
    [ValidateRange(0, 65535)][int]$NativePort = 0,
    [int]$ExpectedSessions = 0,
    [int]$PackageReadyLimit = 0,
    [int]$ReadyLimit = 300,
    [int]$QuitLimit = 60
)
$ErrorActionPreference = 'Stop'
$App = [IO.Path]::GetFullPath($App)
$NativeDriver = [IO.Path]::GetFullPath($NativeDriver)
foreach ($file in @($App, $NativeDriver)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Missing executable: $file" }
}
if (Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath -ieq $App }) {
    throw "The application is already running: $App"
}
if (-not $NativePort) { $NativePort = $Port + 1 }
if ($NativePort -eq $Port) { throw 'The intermediary and native driver need different ports' }
foreach ($candidate in @($Port, $NativePort)) {
    if (Get-NetTCPConnection -LocalPort $candidate -State Listen -ErrorAction SilentlyContinue) {
        throw "Driver port $candidate is already in use"
    }
}
$Out = [IO.Path]::GetFullPath($Out)
$env:HOME = [IO.Path]::GetFullPath($HomeDir)
$env:USERPROFILE = $env:HOME
$env:CODEX_HOME = Join-Path $env:HOME '.codex'
$env:LOCALAPPDATA = [IO.Path]::GetFullPath($LocalAppData)
$env:APPDATA = Join-Path $env:HOME 'AppData/Roaming'
$env:TEMP = Join-Path $env:HOME 'Temp'
$env:TMP = $env:TEMP
$env:WEBVIEW2_USER_DATA_FOLDER = Join-Path $env:HOME 'WebView2'
foreach ($folder in @($Out, $env:HOME, $env:LOCALAPPDATA, $env:APPDATA, $env:TEMP)) {
    New-Item -ItemType Directory -Force -Path $folder | Out-Null
}
$driverExe = (Get-Command $TauriDriver -CommandType Application).Source
$start = [Diagnostics.ProcessStartInfo]::new($driverExe)
$start.UseShellExecute = $false
$start.CreateNoWindow = $true
$start.RedirectStandardOutput = $true
$start.RedirectStandardError = $true
foreach ($argument in @('--native-driver', $NativeDriver, '--port', "$Port", '--native-port', "$NativePort")) {
    $start.ArgumentList.Add($argument)
}
# Record the launch intent before starting and its immutable identity immediately after.
$driverIntent = @{ executable = $driverExe; label = $Label; app = $App; launched_at = (Get-Date).ToUniversalTime().ToString('o') }
$driverIntent |
    ConvertTo-Json | Set-Content (Join-Path $Out "$Label-driver.json")
$driver = [Diagnostics.Process]::Start($start)
$driverStart = $driver.StartTime
$driverIntent['pid'] = $driver.Id
$driverIntent['created'] = $driverStart.ToUniversalTime().ToString('o')
$driverIntent | ConvertTo-Json | Set-Content (Join-Path $Out "$Label-driver.json")
$driverOut = $driver.StandardOutput.ReadToEndAsync()
$driverErr = $driver.StandardError.ReadToEndAsync()
$env:TAURI_DRIVER_URL = "http://127.0.0.1:$Port"
$forced = @()
$driverChildren = @()
$code = 1
try {
    $deadline = (Get-Date).AddSeconds(30)
    $ready = $false
    while ((Get-Date) -lt $deadline) {
        if ($driver.HasExited) { throw "tauri-driver exited: $($driver.ExitCode)" }
        try {
            $null = Invoke-RestMethod "$env:TAURI_DRIVER_URL/status" -TimeoutSec 1
            $ready = $true
            break
        } catch { Start-Sleep -Milliseconds 250 }
    }
    if (-not $ready) { throw 'tauri-driver did not answer within 30 seconds' }
    $driverChildren = @(Get-CimInstance Win32_Process | Where-Object {
        $_.ParentProcessId -eq $driver.Id -and $_.CreationDate -ge $driverStart
    } | Select-Object ProcessId, CreationDate, ExecutablePath)
    $driverChildren | ConvertTo-Json -Depth 3 | Set-Content (Join-Path $Out "$Label-native-driver.json")
    $harnessArgs = @((Join-Path $PSScriptRoot 'desktop_launch.mjs'), '--app', $App, '--home', $env:HOME,
        '--out', $Out, '--label', $Label, '--ready-limit', "$ReadyLimit", '--quit-limit', "$QuitLimit",
        '--expected-sessions', "$ExpectedSessions", '--package-ready-limit', "$PackageReadyLimit")
    if ($BootLog) { $harnessArgs += @('--boot-log', $BootLog) }
    & node @harnessArgs
    $code = $LASTEXITCODE
} finally {
    # The harness first calls the app's Quit and waits. Any survivors here are
    # forced, explicitly recorded; never infer graceful exit from cleanup.
    $ledger = Join-Path $Out "$Label-processes.json"
    if (Test-Path -LiteralPath $ledger) {
        foreach ($record in @(Get-Content -LiteralPath $ledger -Raw | ConvertFrom-Json)) {
            $current = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
            if ($current -and [Math]::Abs(($current.StartTime.ToUniversalTime() - [datetime]$record.created).TotalMilliseconds) -lt 1) {
                Stop-Process -Id $current.Id -Force
                $null = $current.WaitForExit(5000)
                $forced += @{ pid = $record.pid; created = $record.created; cmd = $record.cmd }
            }
        }
    }
    $currentDriver = Get-Process -Id $driver.Id -ErrorAction SilentlyContinue
    if ($currentDriver -and $currentDriver.StartTime -eq $driverStart) {
        # Take down the native driver too, after the measured app shutdown.
        & taskkill /PID $driver.Id /T /F *> $null
        $null = $driver.WaitForExit(5000)
    }
    foreach ($record in $driverChildren) {
        $current = Get-Process -Id $record.ProcessId -ErrorAction SilentlyContinue
        if ($current -and [Math]::Abs(($current.StartTime.ToUniversalTime() - $record.CreationDate.ToUniversalTime()).TotalMilliseconds) -lt 1) {
            Stop-Process -Id $current.Id -Force
            $null = $current.WaitForExit(5000)
        }
    }
    $driverSurvivors = @(@(@{ ProcessId = $driver.Id; CreationDate = $driverStart }) + $driverChildren | Where-Object {
        $current = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
        $current -and [Math]::Abs(($current.StartTime.ToUniversalTime() - $_.CreationDate.ToUniversalTime()).TotalMilliseconds) -lt 1
    })
    $survivors = @($forced | Where-Object {
        $current = Get-Process -Id $_.pid -ErrorAction SilentlyContinue
        $current -and [Math]::Abs(($current.StartTime.ToUniversalTime() - [datetime]$_.created).TotalMilliseconds) -lt 1
    })
    if ($survivors.Count -or $forced.Count -or $driverSurvivors.Count) { $code = 1 }
    foreach ($entry in @(@{ task = $driverOut; suffix = 'driver.log' }, @{ task = $driverErr; suffix = 'driver-error.log' })) {
        if ($entry.task.Wait(5000)) {
            $entry.task.Result | Set-Content (Join-Path $Out "$Label-$($entry.suffix)")
        } else {
            'Driver output did not close after cleanup.' | Set-Content (Join-Path $Out "$Label-$($entry.suffix)")
            $code = 1
        }
    }
    @{ driver_pid = $driver.Id; driver_created = $driverStart.ToUniversalTime().ToString('o');
       forced_application_processes = $forced; survivors = $survivors;
       driver_survivors = $driverSurvivors; exit_code = $code } |
        ConvertTo-Json -Depth 4 | Set-Content (Join-Path $Out "$Label-cleanup.json")
    $driver.Dispose()
}
exit $code
