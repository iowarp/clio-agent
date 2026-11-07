"""Manual GPU placement of model deployments: which GPUs each one may use, checked.

A person places deployments on a node's GPUs by hand -- several services, each
on its own GPU(s), or one service across several -- through the engine's device
pin (``param.cuda_devices``, ``param.rocm_devices``; ``param.xpu_devices`` on
Intel). The ids are the host's own numbering as :attr:`TargetFacts.gpus` lists
it (``nvidia-smi``'s index; a CUDA pin also sets ``CUDA_DEVICE_ORDER=PCI_BUS_ID``
so CUDA agrees). CLIO never picks GPUs itself: an unpinned deployment may use
every GPU the host exposes, as before.

A pin is refused, typed (:class:`~.server_parameters.GpuDeviceError`), before
anything is launched:

* ``gpu_device_invalid`` -- not distinct non-negative whole numbers;
* ``gpu_device_unknown`` -- the host lists its GPUs and this one is not among
  them (or llama.cpp's ``main_gpu`` is not one of the pinned devices);
* ``gpu_device_count_mismatch`` -- vLLM runs one worker per device, so tensor
  parallel size x pipeline parallel size must equal the number pinned.

The format is checked without the host (so the action route refuses it with a
422 before queueing); existence needs the host's GPU list -- the last one the
catalog read when the route checks, the operation's own when the plan compiles.
Intel ``xpu_devices`` masks may name tiles (``0.1``) that ``xpu-smi`` does not
list, so only their count is checked.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from clio_agent.gact.infrastructure.models import GpuFact, GpuVendor
from clio_agent.gact.infrastructure.server_parameters import (
    PARAMETER_PREFIX,
    GpuDeviceError,
    parse_device_ids,
)

#: Device-pin parameters and the vendor whose GPUs they name.
DEVICE_PARAMETERS: dict[str, GpuVendor] = {
    "cuda_devices": "nvidia",
    "rocm_devices": "amd",
    "xpu_devices": "intel",
}
#: Pins whose entries are whole GPU ids (checked against ``facts.gpus``).
NUMBERED_PINS = frozenset({"cuda_devices", "rocm_devices"})
VENDOR_LABELS: dict[GpuVendor, str] = {"nvidia": "NVIDIA", "amd": "AMD", "intel": "Intel"}
#: Actions that launch a server with the pin.
LAUNCH_ACTIONS = frozenset({"install", "reinstall", "start"})


def _raw(configuration: Mapping[str, str], pid: str) -> str:
    return str(configuration.get(PARAMETER_PREFIX + pid, "") or "").strip()


def device_pin(configuration: Mapping[str, str]) -> tuple[str, tuple[str, ...]] | None:
    """``(parameter id, entries)`` of a deployment's device pin; ``None`` when unpinned."""

    for pid in DEVICE_PARAMETERS:
        raw = _raw(configuration, pid)
        if raw:
            return pid, tuple(part.strip() for part in raw.split(","))
    return None


def pinned_ids(configuration: Mapping[str, str]) -> tuple[int, ...] | None:
    """The numbered GPU ids a deployment is pinned to; ``None`` when unpinned or Intel.

    Raises:
        GpuDeviceError: ``gpu_device_invalid`` for a malformed pin.
    """

    for pid in NUMBERED_PINS:
        raw = _raw(configuration, pid)
        if raw:
            return parse_device_ids(raw, label=pid, parameter=pid)
    return None


def _whole(configuration: Mapping[str, str], pid: str) -> int | None:
    raw = _raw(configuration, pid)
    if not raw:
        return 1
    return int(raw) if raw.isascii() and raw.isdigit() else None


def check_gpu_devices(
    engine: str,
    configuration: Mapping[str, str],
    gpus: Sequence[GpuFact] = (),
) -> None:
    """Refuse a device pin CLIO cannot launch (see the module doc).

    Args:
        engine: ``vllm``, ``llama_cpp`` or ``ollama`` (the engine, not the instance id).
        configuration: The launch configuration (``param.*`` keys are read).
        gpus: The host's GPUs; empty when they are not known (existence is then
            not checked -- the engine reports an id it cannot see).

    Raises:
        GpuDeviceError: Typed ``gpu_device_invalid``, ``gpu_device_unknown`` or
            ``gpu_device_count_mismatch``.
    """

    pin = device_pin(configuration)
    if pin is None:
        return
    pid, entries = pin
    if pid in NUMBERED_PINS:
        ids = parse_device_ids(_raw(configuration, pid), label=pid, parameter=pid)
        vendor = DEVICE_PARAMETERS[pid]
        if gpus:
            listed = sorted({gpu.index for gpu in gpus if gpu.vendor == vendor})
            unknown = [device for device in ids if device not in listed]
            if unknown:
                have = (
                    f"it lists {VENDOR_LABELS[vendor]} GPUs {', '.join(map(str, listed))}"
                    if listed
                    else f"it lists no {VENDOR_LABELS[vendor]} GPU"
                )
                raise GpuDeviceError(
                    "gpu_device_unknown",
                    f"GPU {', '.join(map(str, unknown))} is not on this host ({have}).",
                    parameter=pid,
                    unknown=unknown,
                    available=listed,
                )
    count = len(entries)
    if engine == "vllm":
        tensor = _whole(configuration, "tensor_parallel_size")
        pipeline = _whole(configuration, "pipeline_parallel_size")
        if tensor is not None and pipeline is not None and tensor * pipeline != count:
            raise GpuDeviceError(
                "gpu_device_count_mismatch",
                f"vLLM runs one worker per GPU: tensor parallel size {tensor} x pipeline "
                f"parallel size {pipeline} = {tensor * pipeline}, but {count} GPU(s) are "
                f"pinned ({','.join(entries)}). Pin {tensor * pipeline} GPU(s), or set the "
                f"parallel sizes to multiply to {count}.",
                parameter=pid,
                devices=count,
                tensor_parallel_size=tensor,
                pipeline_parallel_size=pipeline,
            )
    if engine == "llama_cpp":
        main = _raw(configuration, "main_gpu")
        if main.isascii() and main.isdigit() and int(main) >= count:
            raise GpuDeviceError(
                "gpu_device_unknown",
                f"Main GPU {main} is counted among the {count} pinned GPU(s) from 0, so it "
                f"must be at most {count - 1}.",
                parameter="main_gpu",
                unknown=[int(main)],
                available=list(range(count)),
            )


def select_gpu_rows(
    configuration: Mapping[str, str], rows: Sequence[Mapping[str, Any]]
) -> list[Mapping[str, Any]]:
    """The memory rows (one per host GPU, in host order) of the GPUs a deployment may use.

    An unpinned deployment (or an Intel mask) keeps every row; a pin keeps the
    rows of its ids in pin order, skipping ids beyond what the host reported.
    A malformed pin keeps every row (it is refused before launch anyway).
    """

    try:
        ids = pinned_ids(configuration)
    except GpuDeviceError:
        return list(rows)
    if ids is None:
        return list(rows)
    return [rows[device] for device in ids if device < len(rows)]


def fact_rows(gpus: Iterable[GpuFact]) -> list[dict[str, int]]:
    """Memory rows from the host's GPU facts (total only; free is not probed there).

    Used for sizing when the launch-time probe reported no GPU: the total is
    what vLLM reserves its share of, so ``free`` is taken as the total.
    """

    ordered = sorted(gpus, key=lambda gpu: gpu.index)
    rows: list[dict[str, int]] = []
    for gpu in ordered:
        if gpu.memory_total_mib:
            total = gpu.memory_total_mib << 20
            rows.append({"total": total, "free": total, "index": gpu.index})
    # Positional rows: an id must land on its own GPU, so only a gap-free list is usable.
    if [row["index"] for row in rows] != list(range(len(rows))):
        return []
    return rows


__all__ = [
    "DEVICE_PARAMETERS",
    "LAUNCH_ACTIONS",
    "NUMBERED_PINS",
    "check_gpu_devices",
    "device_pin",
    "fact_rows",
    "pinned_ids",
    "select_gpu_rows",
]
