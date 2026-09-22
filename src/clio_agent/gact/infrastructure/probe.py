"""Host fact inspection for local and Desktop-attached SSH targets."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from collections.abc import Awaitable, Callable

import anyio

from clio_agent.gact.infrastructure.models import (
    CommandResult,
    CommandSpec,
    InfrastructureTarget,
    TargetFacts,
)

CommandExecutor = Callable[[CommandSpec], Awaitable[CommandResult]]


def _normalize_os(value: str) -> str:
    lowered = value.casefold()
    if "windows" in lowered:
        return "windows"
    if "darwin" in lowered or "mac" in lowered:
        return "macos"
    if "linux" in lowered:
        return "linux"
    return lowered or "unknown"


def _normalize_arch(value: str) -> str:
    lowered = value.casefold()
    if lowered in {"amd64", "x86_64", "x64"}:
        return "x86_64"
    if lowered in {"arm64", "aarch64"}:
        return "aarch64"
    return lowered or "unknown"


def _resolve_command(program: str) -> str | None:
    """Resolve an executable, preferring the real Windows binary over extensionless shims."""

    candidates = [f"{program}.exe", program] if platform.system() == "Windows" else [program]
    return next((resolved for name in candidates if (resolved := shutil.which(name))), None)


def _command_available(program: str, args: list[str]) -> bool:
    resolved = _resolve_command(program)
    if resolved is None:
        return False
    try:
        completed = subprocess.run(
            [resolved, *args],
            check=False,
            capture_output=True,
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _local_facts(target: InfrastructureTarget) -> TargetFacts:
    docker_installed = _resolve_command("docker") is not None
    docker_available = docker_installed and _command_available("docker", ["info"])
    accelerator = "none"
    if _command_available("nvidia-smi", ["--query-gpu=name", "--format=csv,noheader"]):
        accelerator = "nvidia"
    elif _command_available("rocminfo", ["--version"]):
        accelerator = "amd"
    return TargetFacts(
        target_id=target.id,
        label=target.label,
        os=_normalize_os(platform.system()),
        arch=_normalize_arch(platform.machine() or os.environ.get("PROCESSOR_ARCHITECTURE", "")),
        accelerator=accelerator,
        docker_available=docker_available,
        docker_installed=docker_installed,
        uv_available=_command_available("uv", ["--version"]),
        transport_state="connected",
    )


async def probe_target(
    target: InfrastructureTarget,
    execute: CommandExecutor | None = None,
) -> TargetFacts:
    """Inspect a target without inferring capabilities from its display name."""

    if target.kind == "local":
        return await anyio.to_thread.run_sync(_local_facts, target)
    if target.kind == "direct":
        return TargetFacts(
            target_id=target.id,
            label=target.label,
            os="external",
            arch="unknown",
            transport_state=target.transport_state,
        )
    if execute is None or target.transport_state != "connected":
        return TargetFacts(
            target_id=target.id,
            label=target.label,
            os="unknown",
            arch="unknown",
            transport_state=target.transport_state,
        )

    route = target.ssh
    platform_hint = route.platform if route else "auto"
    if platform_hint == "windows":
        spec = CommandSpec(
            program="powershell",
            args=[
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$d=(Get-Command docker -ErrorAction SilentlyContinue);"
                "$u=(Get-Command uv -ErrorAction SilentlyContinue);"
                "$ready=$false;if($d){docker info *> $null;$ready=$LASTEXITCODE -eq 0};"
                "$gpu='none';if(Get-Command nvidia-smi -ErrorAction SilentlyContinue){$gpu='nvidia'}"
                "elseif(Get-Command rocminfo -ErrorAction SilentlyContinue){$gpu='amd'};"
                "Write-Output ('windows|'+$env:PROCESSOR_ARCHITECTURE+'|'+$gpu+'|'+"
                "[int][bool]$d+'|'+[int]$ready+'|'+[int][bool]$u)",
            ],
            timeout_seconds=20,
        )
    else:
        spec = CommandSpec(
            program="sh",
            args=[
                "-lc",
                "os=$(uname -s); arch=$(uname -m); gpu=none; "
                "command -v nvidia-smi >/dev/null 2>&1 && gpu=nvidia; "
                '[ "$gpu" = none ] && command -v rocminfo >/dev/null 2>&1 && gpu=amd; '
                "di=0; dr=0; uv=0; command -v docker >/dev/null 2>&1 && di=1; "
                "docker info >/dev/null 2>&1 && dr=1; command -v uv >/dev/null 2>&1 && uv=1; "
                'printf \'%s|%s|%s|%s|%s|%s\\n\' "$os" "$arch" "$gpu" "$di" "$dr" "$uv"',
            ],
            timeout_seconds=20,
        )
    result = await execute(spec)
    if result.exit_code != 0:
        raise RuntimeError(result.stderr.strip() or "Remote target inspection failed")
    line = next((item.strip() for item in result.stdout.splitlines() if item.count("|") == 5), "")
    fields = line.split("|")
    if len(fields) != 6:
        raise RuntimeError("Remote target returned an invalid capability probe")
    return TargetFacts(
        target_id=target.id,
        label=target.label,
        os=_normalize_os(fields[0]),
        arch=_normalize_arch(fields[1]),
        accelerator=fields[2] or "none",
        docker_installed=fields[3] == "1",
        docker_available=fields[4] == "1",
        uv_available=fields[5] == "1",
        transport_state=target.transport_state,
    )
