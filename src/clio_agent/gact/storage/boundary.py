"""Apply source permission changes only after existing tool processes have drained."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from clio_agent.tools.reaper import RESTART_NO_RESIDENT, RESTART_RESTARTED_LIVE


@contextmanager
def source_policy_change(agent: Any) -> Iterator[None]:
    """Exclude concurrent turn leases while recycling children with compiled permissions.

    Source exclusions are host-wide. Refuse a change while any workspace is
    using its fleet; an old child must never outlive a newly claimed restriction.
    The no-agent server supports trusted setup without starting an agent.
    """
    if agent is None:
        yield
        return
    lock, executors, leases = agent._workspace_state()
    with lock:
        if (
            getattr(agent, "_source_policy_changing", False)
            or any(leases.values())
            or any(executor.busy for executor in executors.values())
        ):
            raise ValueError("Wait for active CLIO turns to finish before changing data access")
        agent._source_policy_changing = True
        roots = list(executors)
    try:
        for root in roots:
            outcome = agent.request_fleet_restart(root)
            if outcome not in {RESTART_RESTARTED_LIVE, RESTART_NO_RESIDENT}:
                raise RuntimeError("CLIO could not safely refresh data access; try again when idle")
        yield
    finally:
        with lock:
            agent._source_policy_changing = False
