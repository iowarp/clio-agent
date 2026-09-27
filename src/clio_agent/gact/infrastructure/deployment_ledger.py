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
