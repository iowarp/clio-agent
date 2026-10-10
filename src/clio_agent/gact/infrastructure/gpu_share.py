"""The GPU-memory share of each vLLM deployment on a target, and their sum.

Several vLLM instances may share one GPU only when the memory each reserves
fits together: vLLM reserves ``--gpu-memory-utilization`` of every device it
uses at start-up and does not give it back. An instance's share is its
``context.gpu_share`` (the context control's GPU budget), else its typed
``param.gpu_memory_utilization``, else vLLM's own default (0.9). A share chosen
through ``context.gpu_share`` is also what that instance is launched with, so
the context it is sized to and the memory it reserves agree.

Starting (or installing a container of) a GPU deployment is refused, typed,
when the shares of the target's running vLLM deployments plus its own exceed
1.0. The sum is per target: CLIO does not pin instances to devices, so every
GPU variant counts against the same budget.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

from clio_agent.context_sizing.controls import SHARE_KEY
from clio_agent.gact.infrastructure.context_sizing.deployment import (
    GPU_VARIANTS,
    VLLM_DEFAULT_UTILIZATION,
)
from clio_agent.gact.infrastructure.model_instances import engine_of
from clio_agent.gact.infrastructure.models import ServiceRecord
from clio_agent.gact.infrastructure.plan import DriverPlan
from clio_agent.gact.infrastructure.server_parameters import PARAMETER_PREFIX
from clio_agent.gact.infrastructure.store import InfrastructureStore

UTILIZATION_KEY = PARAMETER_PREFIX + "gpu_memory_utilization"
#: Shares may sum to 1.0 exactly; this absorbs float noise (0.45 + 0.55).
_TOLERANCE = 1e-6


class GpuShareExceededError(ValueError):
    """The target's running vLLM deployments plus this one would reserve more than its GPU.

    Attributes:
        reason: ``gpu_share_exceeded`` (the typed code).
        requested: This deployment's share.
        running: Each running deployment's share, by service id.
    """

    reason = "gpu_share_exceeded"

    def __init__(self, service_id: str, requested: float, running: dict[str, float]) -> None:
        self.service_id = service_id
        self.requested = requested
        self.running = dict(running)
        total = requested + sum(running.values())
        listed = ", ".join(f"{name} {share:g}" for name, share in sorted(running.items()))
        super().__init__(
            f"gpu_share_exceeded: {service_id} needs a GPU share of {requested:g}, but "
            f"{listed} already reserve {sum(running.values()):g} of this host's GPU memory "
            f"(total {total:g} > 1). Lower context.gpu_share, or stop another instance first."
        )

    def details(self) -> dict[str, object]:
        """The typed error's details for an API envelope."""

        return {
            "service_id": self.service_id,
            "requested": self.requested,
            "running": self.running,
            "total": self.requested + sum(self.running.values()),
        }


def gpu_share(variant_id: str, configuration: dict[str, str]) -> float | None:
    """The share of GPU memory a vLLM deployment reserves; ``None`` for a CPU variant."""

    if variant_id not in GPU_VARIANTS:
        return None
    for key in (SHARE_KEY, UTILIZATION_KEY):
        raw = str(configuration.get(key, "") or "").strip()
        if raw:
            try:
                return float(raw)
            except ValueError:
                continue
    return VLLM_DEFAULT_UTILIZATION


def _launches(action: str, variant_id: str) -> bool:
    """Whether ``action`` starts a server (a native install only installs packages)."""

    return action == "start" or (
        action in {"install", "reinstall"} and not variant_id.startswith("native")
    )


def check_gpu_share(
    store: InfrastructureStore,
    target_id: str,
    service_id: str,
    action: str,
    variant_id: str,
    configuration: dict[str, str],
) -> None:
    """Refuse a launch whose share would take the target's vLLM total above 1.0.

    Args:
        store: The infrastructure store (the running deployments' records).
        target_id: The target.
        service_id: The deployment being launched (``vllm`` or ``vllm@<name>``).
        action: The lifecycle action.
        variant_id: Its variant (an empty one is the installed variant).
        configuration: The launch configuration; the installed one fills gaps.

    Raises:
        GpuShareExceededError: When the shares would sum above 1.0.
    """

    if engine_of(service_id) != "vllm":
        return
    installed = store.service(target_id, service_id)
    if installed is not None and action != "install":
        # As the runtime merges it: a start runs what is installed, a
        # reinstall lets the values the person typed override it.
        typed = {k: v for k, v in configuration.items() if v.strip()}
        variant_id = installed.variant_id
        configuration = (
            {**installed.configuration, **typed}
            if action == "reinstall"
            else {**configuration, **installed.configuration}
        )
    requested = gpu_share(variant_id, configuration)
    if requested is None or not _launches(action, variant_id):
        return
    running = _running_shares(store.services(), target_id, service_id)
    if requested + sum(running.values()) > 1.0 + _TOLERANCE:
        raise GpuShareExceededError(service_id, requested, running)


def _running_shares(
    records: list[ServiceRecord], target_id: str, service_id: str
) -> dict[str, float]:
    shares: dict[str, float] = {}
    for row in records:
        if (
            row.target_id != target_id
            or row.service_id == service_id
            or engine_of(row.service_id) != "vllm"
            or row.state != "running"
        ):
            continue
        share = gpu_share(row.variant_id, row.configuration)
        if share is not None:
            shares[row.service_id] = share
    return shares


def share_launch(
    build: Callable[[dict[str, str]], DriverPlan], engine: str, variant_id: str
) -> Callable[[dict[str, str]], DriverPlan]:
    """Launch a vLLM deployment with its ``context.gpu_share`` as its memory utilization.

    Only when the person typed no utilization of their own. The value is a launch
    input, never persisted: a later change of the share takes effect on reinstall.
    """

    def launch(configuration: dict[str, str]) -> DriverPlan:
        share = str(configuration.get(SHARE_KEY, "") or "").strip()
        if (
            engine != "vllm"
            or variant_id not in GPU_VARIANTS
            or not share
            or str(configuration.get(UTILIZATION_KEY, "") or "").strip()
        ):
            return build(configuration)
        plan = build({**configuration, UTILIZATION_KEY: share})
        if plan.configuration is None:
            return plan
        settled = {k: v for k, v in (plan.configuration or {}).items() if k != UTILIZATION_KEY}
        return dataclasses.replace(plan, configuration=settled)

    return launch


__all__ = [
    "UTILIZATION_KEY",
    "GpuShareExceededError",
    "check_gpu_share",
    "gpu_share",
    "share_launch",
]
