"""Settle deployment receipts without conflating installation and serving readiness."""

from __future__ import annotations

from typing import TYPE_CHECKING

from clio_agent.gact.infrastructure.deployment_ledger import hand_over_parents
from clio_agent.gact.infrastructure.models import (
    ConnectionStrategy,
    OwnedResource,
    ServiceActionRequest,
    ServiceRecord,
    ServiceState,
)
from clio_agent.gact.infrastructure.service_observation import parse_observation

if TYPE_CHECKING:
    from clio_agent.gact.infrastructure.runtime import InfrastructureRuntime


async def settle_service(
    self: InfrastructureRuntime,
    service_id: str,
    request: ServiceActionRequest,
    connection_port: int | None,
    output: list[str],
    owned: list[OwnedResource] | None = None,
    *,
    resolved_root: str | None = None,
    retain_record: bool = False,
) -> None:
    """Persist observed state and preserve evidence through runtime removal."""
    previous = self.store.service(request.target_id, service_id)
    if request.action == "delete_data" or (request.action == "uninstall" and not retain_record):
        if previous is not None:
            hand_over_parents(self.store, request.target_id, previous)
        self.store.delete_service(request.target_id, service_id)
        return
    state: ServiceState = previous.state if previous else "unknown"
    observation = parse_observation(output)
    if observation is not None:
        state = observation.service_state
    elif request.action in {"install", "reinstall", "start"}:
        state = "running"
    elif request.action == "stop":
        state = "stopped"
    elif request.action == "status" and output:
        observed = output[-1].strip().casefold()
        state = (
            "running" if "running" in observed else "stopped" if "exited" in observed else "unknown"
        )
    connection_url = previous.connection_url if previous else None
    strategy: ConnectionStrategy | None = previous.connection_strategy if previous else None
    if connection_port and state == "running":
        try:
            connection_url, strategy = await self._resolve_connection(
                request.target_id,
                connection_port,
                service_id=service_id,
                previous_url=connection_url,
                previous_strategy=strategy,
                wait_for_direct=request.action in {"install", "reinstall", "start"},
            )
        except (OSError, RuntimeError, ValueError):
            connection_url = previous.connection_url if previous else None
            strategy = previous.connection_strategy if previous else None
    record = self.store.put_service(
        ServiceRecord(
            id=f"{request.target_id}:{service_id}",
            service_id=service_id,
            target_id=request.target_id,
            variant_id=request.variant_id,
            # `on_conflict` is this operation's own answer, never a
            # persisted fact about the service (#1528 review); stripped
            # here too, defensively, however `request` reached this point.
            configuration={
                k: v
                for k, v in request.configuration.items()
                if k not in {"on_conflict", "conflict_pid", "conflict_root"}
            },
            state=state,
            observation=observation or (previous.observation if previous else None),
            connection_url=connection_url,
            connection_strategy=strategy,
            owned_resources=owned
            if owned is not None
            else (previous.owned_resources if previous else []),
            access=previous.access if previous else None,
            # None means "no fresh answer this operation" -- keep whatever
            # was already recorded; an explicit value (including "") is
            # this operation's own finding and replaces it.
            resolved_root=(
                resolved_root
                if resolved_root is not None
                else (previous.resolved_root if previous else "")
            ),
        )
    )
    if state == "running" and request.action in {"install", "reinstall", "start", "status"}:
        await self._refresh_effective(request.target_id, record)
