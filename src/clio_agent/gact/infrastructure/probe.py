"""Host fact inspection for local and Desktop-attached SSH targets.

Besides the accelerator kind, inspection lists the target's GPUs
(:mod:`~clio_agent.gact.infrastructure.gpu_inventory`) -- the same vendor
queries locally and over SSH -- so deployments can be pinned to them.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from collections.abc import Awaitable, Callable

from anyio.to_thread import run_sync

from clio_agent.gact.infrastructure.gpu_inventory import (
    POSIX_GPU_PROBE,
    POWERSHELL_GPU_PROBE,
    local_gpus,
    parse_gpu_lines,
)
from clio_agent.gact.infrastructure.models import (
    CommandResult,
    CommandSpec,
    InfrastructureTarget,
    TargetFacts,
)
from clio_agent.gact.infrastructure.runtime_probe import (
    POSIX_RUNTIME_PROBE,
    local_identity,
    local_runtime_facts,
    parse_probe,
)
from clio_agent.gact.infrastructure.terminal_output import structured_stdout
from clio_agent.paths import user_data_dir

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


def _clio_uv_runs() -> bool:
    """Whether CLIO's own uv (or, failing that, the host's) runs on this computer."""
    from clio_agent.gact.infrastructure.native_service_platform import clio_tool  # noqa: PLC0415

    uv = clio_tool("uv")
    return uv is not None and _command_available(uv, ["--version"])


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


#: PCI vendor of Intel GPUs and the 3D-controller class Intel Data Center GPUs
#: (Max/Ponte Vecchio, Flex) report; integrated graphics report VGA (0x0300).
INTEL_PCI_VENDOR = "0x8086"
PCI_3D_CONTROLLER = "0x0380"


def _intel_datacenter_gpu(drm: str = "/sys/class/drm") -> bool:
    """Whether Linux sysfs lists an Intel Data Center GPU (the XPU runtimes' target)."""
    try:
        cards = [name for name in os.listdir(drm) if name.startswith("card") and "-" not in name]
    except OSError:
        return False
    for card in cards:
        try:
            with open(os.path.join(drm, card, "device", "vendor"), encoding="ascii") as handle:
                vendor = handle.read().strip().lower()
            with open(os.path.join(drm, card, "device", "class"), encoding="ascii") as handle:
                device_class = handle.read().strip().lower()
        except OSError:
            continue
        if vendor == INTEL_PCI_VENDOR and device_class.startswith(PCI_3D_CONTROLLER):
            return True
    return False


def _local_facts(target: InfrastructureTarget) -> TargetFacts:
    runtimes = local_runtime_facts(_resolve_command)
    docker = next(fact for fact in runtimes if fact.name == "docker")
    docker_installed = docker.installed
    docker_available = docker.usable
    identity, home = local_identity()
    accelerator = "none"
    if _command_available("nvidia-smi", ["--query-gpu=name", "--format=csv,noheader"]):
        accelerator = "nvidia"
    elif _command_available("rocminfo", ["--version"]):
        accelerator = "amd"
    elif _intel_datacenter_gpu():
        accelerator = "intel"
    elif platform.system() == "Windows":
        accelerator = _windows_gpu()
    elif _amd_gpu():
        accelerator = "amd"
    return TargetFacts(
        target_id=target.id,
        label=target.label,
        os=_normalize_os(platform.system()),
        arch=_normalize_arch(platform.machine() or os.environ.get("PROCESSOR_ARCHITECTURE", "")),
        accelerator=accelerator,
        docker_available=docker_available,
        docker_installed=docker_installed,
        uv_available=_clio_uv_runs(),
        transport_state="connected",
        container_runtimes=runtimes,
        identity=identity,
        home=home,
        agent_data_root=str(user_data_dir()),
        hostname=platform.node().split(".")[0],
        gpus=local_gpus(accelerator, _resolve_command),
    )


def _windows_gpu() -> str:
    """Read display-controller PCI vendors, without requiring ROCm on Windows."""
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty PNPDeviceID",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return "none"
    devices = result.stdout.upper() if result.returncode == 0 else ""
    return next(
        (
            vendor
            for pci, vendor in (("VEN_10DE", "nvidia"), ("VEN_1002", "amd"), ("VEN_8086", "intel"))
            if pci in devices
        ),
        "none",
    )


def _amd_gpu(drm: str = "/sys/class/drm") -> bool:
    """Detect an AMD display controller even when rocminfo is not installed."""
    from pathlib import Path  # noqa: PLC0415

    for vendor in Path(drm).glob("card[0-9]*/device/vendor"):
        try:
            if vendor.read_text().strip().lower() == "0x1002":
                return True
        except OSError:
            continue
    return False


async def probe_target(
    target: InfrastructureTarget,
    execute: CommandExecutor | None = None,
) -> TargetFacts:
    """Inspect a target without inferring capabilities from its display name."""

    if target.kind == "local":
        return await run_sync(_local_facts, target)
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
                "$gpu='none';if(Get-Command nvidia-smi -ErrorAction SilentlyContinue){"
                "$names=nvidia-smi --query-gpu=name --format=csv,noheader 2>$null;"
                "if($LASTEXITCODE -eq 0 -and $names){$gpu='nvidia'}}"
                "if($gpu -eq 'none'){$pci=(Get-CimInstance Win32_VideoController | "
                "Select-Object -ExpandProperty PNPDeviceID) -join ' ';"
                "if($pci -match 'VEN_1002'){$gpu='amd'}elseif($pci -match 'VEN_8086'){$gpu='intel'}};"
                "Write-Output ('windows|'+$env:PROCESSOR_ARCHITECTURE+'|'+$gpu+'|'+"
                "[int][bool]$d+'|'+[int]$ready+'|'+[int][bool]$u);" + POWERSHELL_GPU_PROBE,
            ],
            timeout_seconds=20,
        )
    else:
        spec = CommandSpec(
            program="sh",
            args=[
                "-lc",
                "os=$(uname -s); arch=$(uname -m); gpu=none; "
                "if nvgpu=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null) "
                '&& [ -n "$nvgpu" ]; then gpu=nvidia; fi; '
                'if [ "$gpu" = none ] && rocminfo 2>/dev/null | grep -q "Name:.*gfx"; then gpu=amd; fi; '
                'if [ "$gpu" = none ]; then for c in /sys/class/drm/card*/device/vendor; do '
                '[ "$(cat "$c" 2>/dev/null)" = 0x1002 ] && gpu=amd; done; fi; '
                'if [ "$gpu" = none ]; then for c in /sys/class/drm/card*/device; do '
                '[ "$(cat "$c/vendor" 2>/dev/null)" = 0x8086 ] && case "$(cat "$c/class" 2>/dev/null)" in '
                "0x0380*) gpu=intel ;; esac; done; fi; "
                "di=0; dr=0; uv=0; command -v docker >/dev/null 2>&1 && di=1; "
                "docker info >/dev/null 2>&1 && dr=1; command -v uv >/dev/null 2>&1 && uv=1; "
                # A deployed CLIO brings its own uv into its root (default location).
                '[ -x "${XDG_DATA_HOME:-$HOME/.local/share}/clio-agent/app/bin/uv" ] && uv=1; '
                'printf \'%s|%s|%s|%s|%s|%s\\n\' "$os" "$arch" "$gpu" "$di" "$dr" "$uv"; '
                + POSIX_GPU_PROBE
                + "; "
                + POSIX_RUNTIME_PROBE
                + '; agent_data="${CLIO_AGENT_DATA_DIR:-${CLIO_AGENT_HOME:+$CLIO_AGENT_HOME/data}}"; '
                + 'agent_data="${agent_data:-${CLIO_USER_DIR:+$CLIO_USER_DIR/data}}"; '
                + 'if [ -z "$agent_data" ]; then '
                + 'if [ "$os" = Darwin ]; then agent_data="$HOME/Library/Application Support/clio-agent/data"; '
                + 'else xdg_data="${XDG_DATA_HOME:-}"; '
                + 'case "$xdg_data" in /*) ;; *) xdg_data="$HOME/.local/share" ;; esac; '
                + 'agent_data="$xdg_data/clio-agent"; fi; fi; '
                + 'case "$agent_data" in /*) ;; *) echo "Agent data root must be absolute" >&2; exit 75 ;; esac; '
                + 'printf "clio-agent-data|%s\\n" "$agent_data"',
            ],
            timeout_seconds=60,
        )
    result = await execute(spec)
    if result.exit_code != 0:
        raise RuntimeError(result.stderr.strip() or "Remote target inspection failed")
    # Desktop SSH uses a terminal transport; terminal display sequences are not host facts.
    stdout = structured_stdout(result.stdout)
    line = next((item.strip() for item in stdout.splitlines() if item.count("|") == 5), "")
    fields = line.split("|")
    if len(fields) != 6:
        raise RuntimeError("Remote target returned an invalid capability probe")
    runtimes, identity, home, hostname = parse_probe(stdout)
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
        container_runtimes=runtimes,
        identity=identity,
        home=home,
        agent_data_root=next(
            (
                line.split("|", 1)[1]
                for line in stdout.splitlines()
                if line.startswith("clio-agent-data|")
            ),
            "",
        ),
        hostname=hostname,
        gpus=parse_gpu_lines(stdout.splitlines()),
    )
