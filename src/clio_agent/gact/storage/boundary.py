"""Apply source permission changes only after existing tool processes have drained."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from clio_agent.tools.reaper import RESTART_NO_RESIDENT, RESTART_RESTARTED_LIVE


@contextmanager
def source_policy_change(agent: Any) -> Iterator[None]:
    """Exclude concurrent turn leases while recycling children with compiled permissions.

    Source exclusions are host-wide. Refuse a change while any live turn leases a
    fleet or a tool call is in flight; an old child must never outlive a newly
    claimed restriction. Residency holds (views, waiting sessions) do not refuse.
    The no-agent server supports trusted setup without starting an agent.
    """
    if agent is None:
        yield
        return
    if not callable(getattr(agent, "_workspace_state", None)):
        if callable(getattr(agent, "_active_tool_executor", None)):
            raise ValueError("This agent does not expose a safe tool-process restart boundary")
        # Protocol-only agents have no CLIO-owned tool fleet to drain. Their
        # running turns are still protected by the caller's revision gate.
        yield
        return
    lock, executors, leases = agent._workspace_state()
    reaper = getattr(agent, "_workspace_reaper", None)
    with lock:
        # A conversation on screen or waiting on its user holds only residency; its
        # idle fleet is restarted below like any other. A live turn still refuses.
        turn_leased = (
            any(reaper.turn_leased(root) for root in leases)
            if reaper is not None
            else any(leases.values())
        )
        if (
            getattr(agent, "_source_policy_changing", False)
            or turn_leased
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
