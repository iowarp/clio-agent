"""llama.cpp as a native Windows process beside the active CLIO (no container runtime).

Every value (install root, model path, server flags) is embedded in the script
as a PowerShell literal (:mod:`clio_agent.gact.infrastructure.powershell`);
``-Command`` never binds trailing arguments to ``$args``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from clio_agent.gact.infrastructure import powershell
from clio_agent.gact.infrastructure.model_runtime_installation import runtime_observation
from clio_agent.gact.infrastructure.model_runtime_readiness import (
    health_command,
    identity_health_command,
)
from clio_agent.gact.infrastructure.models import CommandSpec, InfrastructureTarget
from clio_agent.gact.infrastructure.node_service import MARKER
from clio_agent.gact.infrastructure.plan import DriverPlan, Readiness
from clio_agent.gact.infrastructure.secret_env import with_secret_env

# One llama.cpp build for the Windows archive and the ghcr server images.
# ghcr keeps server images only for recent builds (b10621 was pruned and its
# pull failed with "manifest unknown"), so this pin must move with releases.
LLAMA_BUILD = "b11206"
LLAMA_WINDOWS_CPU_ARCHIVE = (
    "https://github.com/ggml-org/llama.cpp/releases/download/"
    f"{LLAMA_BUILD}/llama-{LLAMA_BUILD}-bin-win-cpu-x64.zip"
)
#: The release asset's sha256 (GitHub release digest, resolved 2026-10-08):
#: an archive that does not match is never unpacked (F027).
LLAMA_WINDOWS_CPU_SHA256 = "c17f1e3233fc5f5b8915472affa939adee0c95785882503b106d5b14aba01002"
NATIVE_PORT = 8088


def _prefix(root: str) -> str:
    return (
        f"$root={powershell.literal(root)}; if (!$root) {{ "
        "$data=if ($env:CLIO_AGENT_DATA_DIR) { $env:CLIO_AGENT_DATA_DIR } "
        "elseif ($env:CLIO_AGENT_HOME) { Join-Path $env:CLIO_AGENT_HOME 'data' } "
        "else { Join-Path $env:LOCALAPPDATA 'clio-agent\\data' }; "
        f"$root=Join-Path $data 'services\\llama.cpp\\{LLAMA_BUILD}'"
        " };"
    )


def native_windows_llama_plan(
    action: str,
    model_path: str,
    target: InfrastructureTarget | None,
    extra_flags: tuple[str, ...] = (),
    api_key: str | None = None,
) -> DriverPlan:
    """Compile one lifecycle action for the native Windows llama.cpp server.

    Args:
        action: The lifecycle action.
        model_path: The GGUF file on this computer (required to start).
        target: The local target (the only one this variant runs on).
        extra_flags: Compiled server parameters (``--parallel 2`` ...).
        api_key: The deployment's API key, handed to the server process as
            ``LLAMA_API_KEY`` through the environment (never an argument).
    """

    if target is None or target.kind != "local":
        raise ValueError("Native Windows llama.cpp can run only beside the active CLIO")
    prefix = _prefix(target.install_root.strip())
    if action == "status":
        return DriverPlan(
            (
                powershell.command(
                    prefix
                    + "$installed=[bool](Get-ChildItem -LiteralPath $root -Filter 'llama-server.exe' -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1); "
                    "$pidFile=Join-Path $root 'server.pid'; $running=$false; "
                    "if (Test-Path -LiteralPath $pidFile) { $running=[bool](Get-Process -Id (Get-Content -LiteralPath $pidFile) -ErrorAction SilentlyContinue) }; "
                    "$phase=if ($running) { 'running' } elseif ($installed) { 'stopped' } else { 'not_installed' }; "
                    "$receipt=@{phase=$phase;installed=$installed;running=$running;serving=$false;observed_at=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()}; "
                    f"Write-Output ({powershell.literal(MARKER)} + ($receipt | ConvertTo-Json -Compress))"
                ),
            ),
            connection_port=NATIVE_PORT,
        )
    if action == "logs":
        return DriverPlan(
            (
                powershell.command(
                    prefix + "Get-Content -LiteralPath (Join-Path $root 'server.log') -Tail 80 "
                    "-ErrorAction SilentlyContinue; Get-Content -LiteralPath "
                    "(Join-Path $root 'server-error.log') -Tail 80 -ErrorAction SilentlyContinue"
                ),
            )
        )
    if action == "stop":
        return DriverPlan(
            (_stop(prefix), *native_windows_llama_plan("status", "", target).commands)
        )
    if action == "uninstall":
        return DriverPlan((_stop(prefix), _remove(prefix)))
    if action == "start" and not model_path:
        raise ValueError("model_path is required")
    if action not in {"install", "reinstall", "start"}:
        raise ValueError(f"Unsupported lifecycle action {action!r}")
    commands: list[CommandSpec] = []
    if action == "reinstall":
        commands.extend((_stop(prefix), _remove(prefix)))
    if action in {"install", "reinstall"}:
        commands.append(
            powershell.command(
                "$ErrorActionPreference='Stop'; "
                + prefix
                + "New-Item -ItemType Directory -Force -Path $root | Out-Null; "
                "$archive=Join-Path $root 'clio-llama.zip'; "
                f"Invoke-WebRequest -UseBasicParsing -Uri {powershell.literal(LLAMA_WINDOWS_CPU_ARCHIVE)} "
                "-OutFile $archive; "
                "$stream=[IO.File]::OpenRead($archive); $sha=[Security.Cryptography.SHA256]::Create(); "
                "try { $digest=([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-','').ToLowerInvariant() } "
                "finally { $sha.Dispose(); $stream.Dispose() }; "
                f"if ($digest -ne {powershell.literal(LLAMA_WINDOWS_CPU_SHA256)}) {{ "
                "Remove-Item -LiteralPath $archive -Force; "
                'throw "llama.cpp archive sha256 $digest does not match the pinned release" }; '
                "Expand-Archive -LiteralPath $archive -DestinationPath $root -Force; "
                "Remove-Item -LiteralPath $archive -Force; "
                "$exe=Get-ChildItem -LiteralPath $root -Filter 'llama-server.exe' -Recurse | "
                "Select-Object -First 1; if (!$exe) { throw 'llama-server.exe was not installed' }; "
                "& $exe.FullName --version | Out-Null; if ($LASTEXITCODE -ne 0) { "
                "throw 'llama-server --version failed' }",
                timeout_seconds=900,
            )
        )
        commands.append(runtime_observation(True))
        return DriverPlan(tuple(commands))
    server_args = [
        "-m",
        model_path,
        "--host",
        "127.0.0.1",
        "--port",
        str(NATIVE_PORT),
        *extra_flags,
    ]
    # Start-Process can inherit extra pipe handles even with redirected output,
    # leaving the caller waiting for EOF until the server stops. Python supplies
    # an explicit child handle list and quotes model paths with spaces correctly.
    launch = CommandSpec(
        program=sys.executable,
        args=[
            "-c",
            _LEGACY_LAUNCH,
            json.dumps({"root": target.install_root.strip(), "arguments": server_args}),
        ],
        timeout_seconds=120,
    )
    # The server process inherits the key from this PowerShell's environment.
    commands.append(_stop(prefix))
    commands.append(
        with_secret_env(launch, "LLAMA_API_KEY", api_key, windows=True) if api_key else launch
    )
    url = f"http://127.0.0.1:{NATIVE_PORT}"
    alive = powershell.command(
        prefix
        + "$pidFile=Join-Path $root 'server.pid'; if ((Test-Path -LiteralPath $pidFile) -and (Get-Process -Id (Get-Content -LiteralPath $pidFile) -ErrorAction SilentlyContinue)) { 'running' } else { 'exited' }"
    )
    readiness = Readiness(
        health=identity_health_command(
            url + "/health", url + "/v1/models", "", "LLAMA_API_KEY", api_key, True
        )
        if api_key
        else health_command(url + "/health", True),
        alive=alive,
        logs=native_windows_llama_plan("logs", "", target).commands[0],
        label="llama.cpp",
    )
    return DriverPlan(
        tuple(commands),
        connection_port=NATIVE_PORT,
        readiness=readiness,
        after_ready=(runtime_observation(True, serving=True),),
    )


def _stop(prefix: str) -> CommandSpec:
    return powershell.command(
        prefix + "$pidFile=Join-Path $root 'server.pid'; if (Test-Path -LiteralPath $pidFile) { "
        "$process=Get-Process -Id (Get-Content -LiteralPath $pidFile) -ErrorAction SilentlyContinue; "
        "if ($process) { $handle=$process.Handle; "
        "$info=Get-CimInstance Win32_Process -Filter ('ProcessId='+$process.Id); "
        "$base=[IO.Path]::GetFullPath($root).TrimEnd('\\')+'\\'; "
        "if (!$info.ExecutablePath -or !$info.ExecutablePath.StartsWith($base,[StringComparison]::OrdinalIgnoreCase) "
        "-or [IO.Path]::GetFileName($info.ExecutablePath) -ne 'llama-server.exe' "
        f"-or $info.CommandLine -notmatch '--port[ =]+{NATIVE_PORT}(?:\\s|$)') {{ "
        "throw 'The saved PID belongs to another process; it was not stopped' }; "
        "$process.Kill(); $process.WaitForExit(15000) | Out-Null }; "
        "Remove-Item -LiteralPath $pidFile -Force }"
    )


_LEGACY_LAUNCH = """\
import json, os, subprocess, sys
from pathlib import Path
request = json.loads(sys.argv[1])
data = os.environ.get('CLIO_AGENT_DATA_DIR')
if not data:
    data = str(Path(os.environ['CLIO_AGENT_HOME']) / 'data') if os.environ.get('CLIO_AGENT_HOME') else str(Path(os.environ['LOCALAPPDATA']) / 'clio-agent/data')
root = Path(request['root'] or str(Path(data) / 'services/llama.cpp/b11206')).resolve()
matches = [path for path in root.rglob('llama-server.exe') if path.is_file()]
if len(matches) != 1 or not matches[0].resolve().is_relative_to(root):
    raise ValueError('Install llama.cpp before starting it; expected one owned executable')
with (root/'server.log').open('wb') as output, (root/'server-error.log').open('wb') as errors:
    process = subprocess.Popen([str(matches[0]), *request['arguments']], stdin=subprocess.DEVNULL,
        stdout=output, stderr=errors, close_fds=True, creationflags=0x8 | 0x200)
(root/'server.pid').write_text(str(process.pid), encoding='ascii')
"""


def _remove(prefix: str) -> CommandSpec:
    files = Path(__file__).with_name("llama_windows_cpu_files.json").read_text(encoding="utf-8")
    inventory = powershell.literal(json.dumps(json.loads(files)))
    return powershell.command(
        "$ErrorActionPreference='Stop'; " + prefix + f"$inventory=ConvertFrom-Json {inventory}; "
        "$base=[IO.Path]::GetFullPath($root).TrimEnd('\\')+'\\'; "
        "foreach ($entry in $inventory.PSObject.Properties) { "
        "$path=[IO.Path]::GetFullPath((Join-Path $root $entry.Name)); "
        "if (!$path.StartsWith($base,[StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe release member' }; "
        "if (Test-Path -LiteralPath $path -PathType Leaf) { "
        "$item=Get-Item -LiteralPath $path; "
        "if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Release member is a link' }; "
        "$stream=[IO.File]::OpenRead($path); $sha=[Security.Cryptography.SHA256]::Create(); "
        "try { $digest=([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-','').ToLowerInvariant() } "
        "finally { $sha.Dispose(); $stream.Dispose() }; "
        "if ($digest -eq $entry.Value) { "
        "Remove-Item -LiteralPath $path -Force } } }"
    )
