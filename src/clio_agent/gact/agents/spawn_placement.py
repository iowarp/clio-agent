"""Placement resolution for the single model-facing spawn surface (#1127).

Placement chooses an :class:`ExpertInvoker`; it never creates another spawn tool.
An explicit per-call value wins over the parent session's ``spawn_placement`` policy,
and an absent policy retains the established in-process default. Relay invokers are
published by cluster on ``app.state.relay_expert_invokers``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from clio_agent.gact.turn_spawn import SpawnError

SESSION_PLACEMENT_KEY = "spawn_placement"
LOCAL_PLACEMENT = "local"
RELAY_PREFIX = "relay:"


@dataclass(frozen=True)
class PlacementBinding:
    """One resolved placement and the invoker/host that serves it."""

    invoker: Any
    placement: str
    host: str


def _session_placement(app: Any, session_id: str) -> str | None:
    """Return the adopted session placement policy, when explicitly present."""

    session = app.state.sessions.get(session_id)
    if session is None:
        return None
    metadata = getattr(session, "metadata", None) or {}
    value = metadata.get(SESSION_PLACEMENT_KEY)
    return str(value) if value is not None else None


def _normalize_placement(value: str) -> tuple[str, str]:
    """Validate a placement and return its canonical value plus host label."""

    if value == LOCAL_PLACEMENT:
        return LOCAL_PLACEMENT, LOCAL_PLACEMENT
    if value.startswith(RELAY_PREFIX):
        cluster = value[len(RELAY_PREFIX) :]
        if cluster and cluster.strip() == cluster:
            return value, cluster
    raise SpawnError(
        f"invalid spawn placement {value!r}; expected 'local' or 'relay:<cluster>'",
        reason="invalid_placement",
    )


def invoker_for_placement(
    app: Any,
    session_id: str,
    placement: str | None = None,
) -> PlacementBinding:
    """Resolve explicit -> session policy -> local and select the serving invoker.

    ``None`` alone means absent. An explicitly empty or malformed value is rejected
    with a typed error instead of silently falling back to local execution.
    """

    selected = placement if placement is not None else _session_placement(app, session_id)
    canonical, host = _normalize_placement(LOCAL_PLACEMENT if selected is None else str(selected))
    if canonical == LOCAL_PLACEMENT:
        invoker = getattr(app.state, "expert_invoker", None)
    else:
        relay_invokers = getattr(app.state, "relay_expert_invokers", None) or {}
        invoker = relay_invokers.get(host)
    if invoker is None:
        raise SpawnError(
            f"spawn placement {canonical!r} has no configured ExpertInvoker",
            reason="placement_unavailable",
        )
    return PlacementBinding(invoker=invoker, placement=canonical, host=host)


def resolve_batch_placement(app: Any, session_id: str, placement: str | None) -> str | None:
    """Pin one batch policy; defer invalid selections to the typed per-spawn result."""

    try:
        return invoker_for_placement(app, session_id, placement).placement
    except SpawnError:
        return placement


def invoker_for_task(app: Any, task: Any) -> PlacementBinding:
    """Resolve the invoker retained on an existing task's durable placement."""

    placement = str(getattr(task, "placement", "") or LOCAL_PLACEMENT)
    return invoker_for_placement(app, task.parent_session_id, placement)


def run_handle_fields(run: Any, child_id: str) -> dict[str, str]:
    """Return the additive run-handle render fields with compatible defaults."""

    task_id = str(getattr(run, "task_id", "") or "")
    run_index = int(getattr(run, "run_index", 0) or 0)
    status = str(getattr(run, "status", "") or "")
    placement = str(getattr(run, "placement", "") or LOCAL_PLACEMENT)
    host = str(
        getattr(run, "host", "")
        or (placement.split(":", 1)[1] if placement.startswith(RELAY_PREFIX) else LOCAL_PLACEMENT)
    )
    return {
        "handle_id": str(getattr(run, "handle_id", "") or task_id),
        "run_label": str(getattr(run, "run_label", "") or f"{child_id} #{run_index + 1}"),
        "live_state": str(getattr(run, "live_state", "") or status),
        "host": host,
        "placement": placement,
    }
