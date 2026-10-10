"""llama.cpp as a native Windows process beside the active CLIO (no container runtime).

Every value (install root, model path, server flags) is embedded in the script
as a PowerShell literal (:mod:`clio_agent.gact.infrastructure.powershell`);
``-Command`` never binds trailing arguments to ``$args``.
"""

from __future__ import annotations

from clio_agent.gact.infrastructure import powershell
from clio_agent.gact.infrastructure.models import CommandSpec, InfrastructureTarget
from clio_agent.gact.infrastructure.plan import DriverPlan
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
                    prefix + "$pidFile=Join-Path $root 'server.pid'; "
                    "if (!(Test-Path -LiteralPath $pidFile)) { Write-Output 'stopped'; exit 0 }; "
                    "$process=Get-Process -Id (Get-Content -LiteralPath $pidFile) "
                    "-ErrorAction SilentlyContinue; "
                    "if ($process) { Write-Output 'running' } else { Write-Output 'stopped' }"
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
        return DriverPlan((_stop(prefix),))
    if action == "uninstall":
        return DriverPlan((_stop(prefix), _remove(prefix)))
    if not model_path:
        raise ValueError("model_path is required")
    commands: list[CommandSpec] = []
    if action == "reinstall":
        commands.extend((_stop(prefix), _remove(prefix)))
    if action in {"install", "reinstall"}:
        commands.append(
            powershell.command(
                "$ErrorActionPreference='Stop'; "
                + prefix
                + "New-Item -ItemType Directory -Force -Path $root | Out-Null; "
                "$archive=Join-Path $env:TEMP 'clio-llama.zip'; "
                f"Invoke-WebRequest -UseBasicParsing -Uri {powershell.literal(LLAMA_WINDOWS_CPU_ARCHIVE)} "
                "-OutFile $archive; "
                "$digest=(Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToLowerInvariant(); "
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
    server_args = [
        "-m",
        model_path,
        "--host",
        "127.0.0.1",
        "--port",
        str(NATIVE_PORT),
        *extra_flags,
    ]
    # Start-Process joins -ArgumentList with spaces and does not quote: a path
    # with a space must carry its own quotes.
    server_args = [f'"{arg}"' if " " in arg else arg for arg in server_args]
    launch = powershell.command(
        "$ErrorActionPreference='Stop'; "
        + prefix
        + "$exe=Get-ChildItem -LiteralPath $root -Filter 'llama-server.exe' -Recurse | "
        "Select-Object -First 1; if (!$exe) { throw 'Install llama.cpp before starting it' }; "
        "$stdout=Join-Path $root 'server.log'; $stderr=Join-Path $root 'server-error.log'; "
        f"$process=Start-Process -FilePath $exe.FullName -ArgumentList {powershell.array(server_args)} "
        "-RedirectStandardOutput $stdout -RedirectStandardError $stderr -WindowStyle Hidden "
        "-PassThru; Set-Content -LiteralPath (Join-Path $root 'server.pid') -Value $process.Id"
    )
    # The server process inherits the key from this PowerShell's environment.
    commands.append(
        with_secret_env(launch, "LLAMA_API_KEY", api_key, windows=True) if api_key else launch
    )
    return DriverPlan(tuple(commands), connection_port=NATIVE_PORT)


def _stop(prefix: str) -> CommandSpec:
    return powershell.command(
        prefix + "$pidFile=Join-Path $root 'server.pid'; if (Test-Path -LiteralPath $pidFile) { "
        "Stop-Process -Id (Get-Content -LiteralPath $pidFile) -ErrorAction SilentlyContinue; "
        "Remove-Item -LiteralPath $pidFile -Force }"
    )


def _remove(prefix: str) -> CommandSpec:
    return powershell.command(
        prefix
        + "if (Test-Path -LiteralPath $root) { Remove-Item -Recurse -Force -LiteralPath $root }"
    )
