"""Bridge gact's expert delegation to the transport-abstracted invoker (epic #667,
issues #671/#441).

``_run_dynamic_agent_sync`` returns a rich ``dspy.Prediction``; the settle loop reads
``answer``, ``next_expert``/``next_task``, ``expert_handoffs`` (the parent's routing
decision) and ``workflow_state`` off it. To run a child through the
:class:`ExpertInvoker` boundary — in-process today, detached on a cluster — those
fields must cross as a serializable :class:`ExpertResult` and come back without
losing the parent's decision.

This module is the mapping (the integration's core), kept separate and unit-tested
so the live wiring in ``_settle_dynamic_agent_delegations`` is a thin, low-risk swap
rather than a rewrite. The routing decision rides as a dedicated ``routing`` event so
it survives the wire verbatim — clio carries the decision, it does not re-derive it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clio_agent.runtime.expert_invoker import ExpertEvent, ExpertRequest, ExpertResult

ROUTING_EVENT = "routing"


def expert_request_for(
    agent_def: Any, prompt: str, *, session_id: str = "", scope: str = "", context: dict | None = None
) -> ExpertRequest:
    """Build the serializable request a child expert is invoked with."""
    return ExpertRequest(
        expert_id=str(getattr(agent_def, "id", "") or ""),
        question=prompt,
        session_id=session_id,
        scope=scope,
        context=dict(context or {}),
    )


def _jsonable(value: Any) -> Any:
    """Coerce a prediction field to something JSON-safe (strings/lists pass; other
    objects degrade to their string form rather than break the wire)."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return str(value)


def expert_result_from_prediction(
    pred: Any, *, expert_id: str, status: str = "completed"
) -> ExpertResult:
    """Map a dspy.Prediction to a serializable :class:`ExpertResult`, preserving the
    answer, the typed ``workflow_state``, and the parent's routing decision."""
    ws = getattr(pred, "workflow_state", None)
    workflow_state = {str(k): _jsonable(v) for k, v in ws.items()} if isinstance(ws, Mapping) else {}
    routing = {
        "next_expert": str(getattr(pred, "next_expert", "") or ""),
        "next_task": str(getattr(pred, "next_task", "") or ""),
        "expert_handoffs": _jsonable(getattr(pred, "expert_handoffs", None)),
    }
    return ExpertResult(
        expert_id=expert_id,
        answer=str(getattr(pred, "answer", "") or ""),
        status=status,
        events=[ExpertEvent(ROUTING_EVENT, routing)],
        workflow_state=workflow_state,
    )


def routing_from_result(result: ExpertResult) -> dict[str, Any]:
    """Recover the parent's routing decision (next_expert/next_task/expert_handoffs)
    from a result that crossed the boundary. Empty dict if none was carried."""
    for ev in result.events:
        if ev.kind == ROUTING_EVENT:
            return dict(ev.payload)
    return {}
