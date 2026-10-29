// Process evidence for the Windows desktop acceptance driver. Creation times are
// part of the identity: a reused PID must never become owned by this run.
import { execFile, execFileSync } from 'node:child_process';
import path from 'node:path';
import { promisify } from 'node:util';

/** Read command/parent identities from CIM and resident bytes from Get-Process. */
const script = `
    $ErrorActionPreference = 'Stop'
    $resident = @{}
    Get-Process | ForEach-Object { $resident[$_.Id] = $_.WorkingSet64 }
    $rows = @(Get-CimInstance Win32_Process | ForEach-Object {
      [pscustomobject]@{
        pid = [int]$_.ProcessId
        parent = [int]$_.ParentProcessId
        created = if ($_.CreationDate) { $_.CreationDate.ToUniversalTime().ToString('o') } else { '' }
        executable = [string]$_.ExecutablePath
        cmd = [string]$_.CommandLine
        rss_mb = [math]::Round($resident[[int]$_.ProcessId] / 1MB, 1)
      }
    })
    ConvertTo-Json -InputObject $rows -Compress
  `;
const command = ['-NoProfile', '-NonInteractive', '-Command', script];
const options = { encoding: 'utf8', windowsHide: true, timeout: 15_000, maxBuffer: 8 * 1024 * 1024 };

/** Retain useful process evidence without persisting a launch's bearer token. */
export const redactCommand = (commandLine) => commandLine.replace(
  /(--(?:auth-)?token(?:=|\s+))(?:"[^"]*"|\S+)/gi,
  '$1<redacted>',
);

function parseProcesses(stdout) {
  return JSON.parse(stdout).map((row) => ({ ...row, cmd: redactCommand(row.cmd) }));
}

/** Initial inventory, taken before the launch timer starts. */
export function windowsProcesses() {
  return parseProcesses(execFileSync('powershell.exe', command, options));
}

/** Collect telemetry without blocking the WebView's readiness polling. */
export async function readWindowsProcesses() {
  const { stdout } = await promisify(execFile)('powershell.exe', command, options);
  return parseProcesses(stdout);
}

export const identity = (row) => `${row.pid}:${row.created}`;

/** Follow the new application and its descendants, retaining exited ancestors. */
export function processTracker(application, baseline) {
  const excluded = new Set(baseline.map(identity));
  const known = new Map();
  const superseded = new Set();
  const normalize = (value) => path.win32.resolve(value).toLowerCase();
  return (rows) => {
    for (const previous of known.values()) {
      const replacement = rows.find((row) => row.pid === previous.pid);
      if (replacement && identity(replacement) !== identity(previous)) superseded.add(identity(previous));
    }
    const available = rows.filter((row) => !excluded.has(identity(row)));
    for (const row of available) {
      if (row.executable && normalize(row.executable) === normalize(application)) {
        known.set(identity(row), row);
      }
    }
    let changed;
    do {
      changed = false;
      for (const row of available) {
        if (known.has(identity(row))) continue;
        // Prefer the live identity of a parent. If it exited, keep the recorded
        // ancestor only until that PID is observed with another creation time.
        const liveParent = rows.find((candidate) => candidate.pid === row.parent);
        const parent = liveParent
          ? known.get(identity(liveParent))
          : [...known.values()].find((candidate) => candidate.pid === row.parent && !superseded.has(identity(candidate)));
        if (parent && Date.parse(row.created) >= Date.parse(parent.created)) {
          known.set(identity(row), row);
          changed = true;
        }
      }
    } while (changed);
    return available.filter((row) => known.has(identity(row)));
  };
}
