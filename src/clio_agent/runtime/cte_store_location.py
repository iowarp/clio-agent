"""Doctor row: does the clio-core store's recorded location match where it is now?

A moved data folder's ``cte.yaml`` and clio-core pool log still name the old location
until the next start relocates them (:mod:`clio_agent.arc.clio_core_relocate`). This row
reads both without changing anything, so ``clio-agent doctor`` reports the mismatch
before that start, and ``/v1/health`` reports the relocation a start made.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from clio_agent.arc import clio_core_config
from clio_agent.arc.clio_core_relocate import (
    CTE_STORE_RELOCATED,
    inspect_store_location,
    last_relocation,
)
from clio_agent.runtime.status import IntegrationState, IntegrationStatus

#: A moved store the next start relocates (nothing refused it yet).
CTE_STORE_RELOCATION_PENDING = "cte_store_relocation_pending"

_SOURCE = "cte.yaml, clio-core pool log"


def probe_cte_store_location(*, env: Mapping[str, str] | None = None) -> list[IntegrationStatus]:
    """One ``cte_store_location`` row for the default store, none for another config.

    Only the default store (``arc.cte.dir`` or the per-host default) is relocated at
    start; an explicit or per-workspace config names its paths on purpose.
    """
    cte_dir = clio_core_config._default_cte_dir()
    if (
        clio_core_config._resolve_config_path(env if env is not None else os.environ)
        != cte_dir / "cte.yaml"
    ):
        return []
    location = inspect_store_location(cte_dir)
    if location is None:
        return []
    details: dict[str, object] = {"cte_dir": str(cte_dir)}
    if location.refusal is not None:
        return [
            IntegrationStatus(
                name="cte_store_location",
                state=IntegrationState.MISCONFIGURED,
                summary=f"The clio-core store in {cte_dir} cannot start: {location.cause}.",
                config_source=_SOURCE,
                next_action=location.fix[0].upper() + location.fix[1:] + ".",
                details={**details, "reason": location.refusal, "recorded": location.moved_from},
            )
        ]
    if location.moved_from is not None:
        return [
            IntegrationStatus(
                name="cte_store_location",
                state=IntegrationState.DEGRADED,
                summary=(
                    f"The clio-core store was moved: {', '.join(location.naming)} still "
                    f"name {location.moved_from}, not {cte_dir}."
                ),
                config_source=_SOURCE,
                next_action=(
                    f"Start CLIO with nothing using {location.moved_from}: it relocates the "
                    "store to where it is now on that start."
                ),
                details={
                    **details,
                    "reason": CTE_STORE_RELOCATION_PENDING,
                    "recorded": location.moved_from,
                    "naming": list(location.naming),
                },
                required=False,
            )
        ]
    relocated = last_relocation()
    summary = f"The clio-core store's recorded location is {cte_dir}."
    if relocated is not None:
        summary += f" It was relocated from {relocated['from']} at this start."
        details = {**details, **relocated}
    else:
        details["reason"] = "cte_store_location_matches"
    return [
        IntegrationStatus(
            name="cte_store_location",
            state=IntegrationState.READY,
            summary=summary,
            config_source=_SOURCE,
            next_action="No action required.",
            details=details,
            required=False,
        )
    ]


__all__ = ["CTE_STORE_RELOCATED", "CTE_STORE_RELOCATION_PENDING", "probe_cte_store_location"]
