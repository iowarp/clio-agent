"""Keep a deployment's ledger durable while it runs, and undo it when it fails.

The runtime calls :func:`persist_created` after every step that created
something, so a CLIO that stops mid-deploy still knows what is on the target;
:func:`remove_created` tears down what a failed or cancelled deploy created,
and :func:`forget_created` restores the ledger only when that teardown removed
everything (an incomplete teardown keeps the rows, so uninstall can finish it).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from clio_agent.gact.infrastructure.models import (
    CommandResult,
    CommandSpec,
    OwnedResource,
    ServiceActionRequest,
    ServiceRecord,
)
from clio_agent.gact.infrastructure.resource_ledger import merge as merge_owned
from clio_agent.gact.infrastructure.resource_ledger import removal_commands
from clio_agent.gact.infrastructure.store import InfrastructureStore
from clio_agent.gact.infrastructure.transport import TransportUnavailableError

logger = logging.getLogger(__name__)

Execute = Callable[[CommandSpec], Awaitable[CommandResult]]


def persist_created(
    store: InfrastructureStore,
    service_id: str,
    request: ServiceActionRequest,
    configuration: dict[str, str],
    installed: ServiceRecord | None,
    created: list[OwnedResource],
) -> None:
    """Record what this operation has created so far on the service's ledger."""

    base = installed.owned_resources if installed is not None else []
    record = store.service(request.target_id, service_id) or ServiceRecord(
        id=f"{request.target_id}:{service_id}",
        service_id=service_id,
        target_id=request.target_id,
        variant_id=request.variant_id,
        configuration=configuration,
        state="unknown",
    )
    store.put_service(record.model_copy(update={"owned_resources": merge_owned(base, created)}))


def hand_over_parents(
    store: InfrastructureStore, target_id: str, uninstalled: ServiceRecord
) -> None:
    """Give the parent directories an uninstalled deployment created to the ones left.

    Deployments on one host share parents (``.../clio/services/<host>``): the
    first deploy creates and records them, later ones find them existing and
    rightly do not. Removal of a parent is only-if-empty, so the first to
    uninstall cannot remove a parent another deployment still uses -- and
    without a hand-over its ledger row vanished with its record, and the
    parents outlived every deployment (seen live on ares). The remaining
    deployments on the target now own them, so the last one out removes them.
    """

    parents = [row for row in uninstalled.owned_resources if row.kind == "parent_directory"]
    if not parents:
        return
    for record in store.services():
        if record.target_id != target_id or record.service_id == uninstalled.service_id:
            continue
        store.update_service(
            target_id,
            record.service_id,
            owned_resources=merge_owned(record.owned_resources, parents),
        )


def forget_created(
    store: InfrastructureStore,
    target_id: str,
    service_id: str,
    installed: ServiceRecord | None,
) -> None:
    """After a complete teardown, restore the ledger as it was before the deploy."""

    if installed is None:
        store.delete_service(target_id, service_id)
    else:
        store.put_service(installed)


def held_elsewhere(store: InfrastructureStore, target_id: str, service_id: str) -> set[str]:
    """The shared images another service on ``target_id`` still holds."""

    return {
        row.ref
        for record in store.services()
        if record.target_id == target_id and record.service_id != service_id
        for row in record.owned_resources
        if row.kind == "shared_image"
    }


async def remove_created(
    execute: Execute, target_id: str, created: list[OwnedResource], target_os: str
) -> tuple[bool, str]:
    """Remove what a failed deploy created.

    Returns:
        Whether every resource was removed, and a sentence for the operation.
    """

    try:
        commands = removal_commands(created, target_os)
    except ValueError as exc:
        return False, f"Cleanup refused: {exc}"
    failures: list[str] = []
    for spec in commands:
        try:
            result = await execute(spec)
        except (OSError, RuntimeError, TransportUnavailableError) as exc:
            failures.append(str(exc))
            continue
        if result.exit_code not in spec.allowed_exit_codes:
            failures.append((result.stderr or result.stdout).strip() or spec.program)
    if failures:
        logger.warning(
            "infrastructure teardown incomplete: reason=teardown_error target=%s", target_id
        )
        return False, "Cleanup incomplete; uninstall removes the rest: " + "; ".join(failures)
    return True, f"Removed the {len(created)} things this deploy created."
