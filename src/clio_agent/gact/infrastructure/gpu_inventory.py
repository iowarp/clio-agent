"""The GPUs a target has, as its vendor tools list them (``TargetFacts.gpus``).

Inspection already decides the target's accelerator; this lists the devices
behind it so a person can place deployments on them by hand (see
:mod:`~clio_agent.gact.infrastructure.gpu_devices`). One read per vendor, best
effort -- a missing or failing tool is an empty list, never a failed inspection:

* NVIDIA: ``nvidia-smi --query-gpu=index,name,memory.total,uuid
  --format=csv,noheader,nounits`` (memory in MiB);
* AMD: ``rocm-smi --showproductname --showmeminfo vram --showuniqueid --json``
  (cards ``card<N>``, memory in bytes);
* Intel: ``xpu-smi discovery -j`` (``device_list``; memory when reported).

Local targets run the tools directly; SSH targets run :data:`POSIX_GPU_PROBE`
inside the one inspection command, which prints each tool's answer on lines
tagged ``clio-gpu-<vendor>|`` (the Windows inspection prints the NVIDIA lines
the same way).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable
from typing import Any, cast

from clio_agent.gact.infrastructure.models import GpuFact, GpuVendor

#: Prefix of an inspection line carrying one vendor tool's GPU answer.
LINE_PREFIX = "clio-gpu-"
NVIDIA_QUERY = [
    "nvidia-smi",
    "--query-gpu=index,name,memory.total,uuid",
    "--format=csv,noheader,nounits",
]
ROCM_QUERY = ["rocm-smi", "--showproductname", "--showmeminfo", "vram", "--showuniqueid", "--json"]
XPU_QUERY = ["xpu-smi", "discovery", "-j"]
#: Run inside the POSIX inspection after ``$gpu`` is decided; one line per answer.
POSIX_GPU_PROBE = (
    'if [ "$gpu" = nvidia ]; then '
    "nvidia-smi --query-gpu=index,name,memory.total,uuid --format=csv,noheader,nounits "
    "2>/dev/null | while IFS= read -r l; do printf 'clio-gpu-nvidia|%s\\n' \"$l\"; done; "
    'elif [ "$gpu" = amd ] && command -v rocm-smi >/dev/null 2>&1; then '
    "printf 'clio-gpu-amd|%s\\n' \"$(rocm-smi --showproductname --showmeminfo vram "
    "--showuniqueid --json 2>/dev/null | tr -d '\\n')\"; "
    'elif [ "$gpu" = intel ] && command -v xpu-smi >/dev/null 2>&1; then '
    "printf 'clio-gpu-intel|%s\\n' \"$(xpu-smi discovery -j 2>/dev/null | tr -d '\\n')\"; "
    "fi"
)
#: The Windows inspection's NVIDIA lines (PowerShell).
POWERSHELL_GPU_PROBE = (
    "if($gpu -eq 'nvidia'){nvidia-smi --query-gpu=index,name,memory.total,uuid "
    "--format=csv,noheader,nounits 2>$null | "
    "ForEach-Object { Write-Output ('clio-gpu-nvidia|'+$_) }};"
)
_CARD = re.compile(r"card(\d+)$")


def _mib(value: Any) -> int | None:
    text = str(value).strip()
    return int(text) if text.isascii() and text.isdigit() else None


def parse_nvidia(text: str) -> list[GpuFact]:
    """``index, name, memory.total, uuid`` CSV lines -> GPUs (a name may hold commas)."""

    gpus: list[GpuFact] = []
    for line in text.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 4 or not parts[0].isascii() or not parts[0].isdigit():
            continue
        gpus.append(
            GpuFact(
                index=int(parts[0]),
                name=", ".join(parts[1:-2]),
                vendor="nvidia",
                memory_total_mib=_mib(parts[-2]),
                uuid=parts[-1] if parts[-1].startswith(("GPU-", "MIG-")) else "",
            )
        )
    return gpus


def _field(card: dict[str, Any], *needles: str, exclude: str = "") -> Any:
    for key, value in card.items():
        if all(needle in key for needle in needles) and not (exclude and exclude in key):
            return value
    return None


def parse_rocm(text: str) -> list[GpuFact]:
    """``rocm-smi ... --json`` (``{"card0": {...}}``) -> GPUs, memory from bytes."""

    try:
        payload = json.loads(text)
    except ValueError:
        return []
    gpus: list[GpuFact] = []
    for key, card in payload.items() if isinstance(payload, dict) else []:
        found = _CARD.match(str(key))
        if not found or not isinstance(card, dict):
            continue
        total_bytes = _mib(_field(card, "Total Memory", exclude="Used"))
        name = next(
            (
                str(card[label])
                for label in ("Card Series", "Card series", "Card SKU", "Card Model", "Card model")
                if card.get(label)
            ),
            "",
        )
        gpus.append(
            GpuFact(
                index=int(found.group(1)),
                name=str(name).strip(),
                vendor="amd",
                memory_total_mib=total_bytes >> 20 if total_bytes else None,
                uuid=str(_field(card, "Unique ID") or "").strip(),
            )
        )
    return sorted(gpus, key=lambda gpu: gpu.index)


def parse_xpu(text: str) -> list[GpuFact]:
    """``xpu-smi discovery -j`` (``{"device_list": [...]}``) -> GPUs."""

    try:
        payload = json.loads(text)
    except ValueError:
        return []
    rows = payload.get("device_list") if isinstance(payload, dict) else None
    gpus: list[GpuFact] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or _mib(row.get("device_id")) is None:
            continue
        size = _mib(row.get("memory_physical_size_byte"))
        gpus.append(
            GpuFact(
                index=int(row["device_id"]),
                name=str(row.get("device_name") or "").strip(),
                vendor="intel",
                memory_total_mib=size >> 20 if size else None,
                uuid=str(row.get("uuid") or "").strip(),
            )
        )
    return sorted(gpus, key=lambda gpu: gpu.index)


_PARSERS: dict[GpuVendor, Callable[[str], list[GpuFact]]] = {
    "nvidia": parse_nvidia,
    "amd": parse_rocm,
    "intel": parse_xpu,
}


def parse_gpu_lines(lines: Iterable[str]) -> list[GpuFact]:
    """The GPUs in an inspection's ``clio-gpu-<vendor>|...`` lines (other lines ignored)."""

    answers: dict[GpuVendor, list[str]] = {}
    for line in lines:
        text = line.strip()
        if not text.startswith(LINE_PREFIX):
            continue
        tag, _, body = text[len(LINE_PREFIX) :].partition("|")
        if tag in _PARSERS:
            answers.setdefault(cast(GpuVendor, tag), []).append(body)
    gpus: list[GpuFact] = []
    for vendor, bodies in answers.items():
        gpus.extend(_PARSERS[vendor]("\n".join(bodies)))
    return gpus


def _run(command: list[str], resolve: Callable[[str], str | None]) -> str:
    resolved = resolve(command[0])
    if resolved is None:
        return ""
    try:
        done = subprocess.run(
            [resolved, *command[1:]],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout if done.returncode == 0 else ""


def local_gpus(
    accelerator: str, resolve: Callable[[str], str | None] = shutil.which
) -> list[GpuFact]:
    """The local host's GPUs for its detected ``accelerator`` (empty when unlisted)."""

    if accelerator == "nvidia":
        return parse_nvidia(_run(NVIDIA_QUERY, resolve))
    if accelerator == "amd":
        return parse_rocm(_run(ROCM_QUERY, resolve))
    if accelerator == "intel":
        return parse_xpu(_run(XPU_QUERY, resolve))
    return []


__all__ = [
    "LINE_PREFIX",
    "POSIX_GPU_PROBE",
    "POWERSHELL_GPU_PROBE",
    "local_gpus",
    "parse_gpu_lines",
    "parse_nvidia",
    "parse_rocm",
    "parse_xpu",
]
