"""The session metadata a flow call's child starts with."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from clio_agent.flows.values import _plain

if TYPE_CHECKING:
    from clio_agent.flows.context import FlowCall


def child_session_metadata(call: "FlowCall", depth: int, run_id: str) -> dict[str, Any] | None:
    """What the child of ``call`` is told about the flow it runs in, if anything.

    A flow call runs this agent (``self``) on the named flow; an editor's call gets
    the workflow editor tools; ``self`` alone is this agent as a worker on its
    ``react`` flow, not its selected flow, so a flow's helpers never re-enter it (a
    flow recurses via ``ctx.call(<flow>)``).
    """

    metadata: dict[str, Any] | None = None
    if call.kind == "flow":
        reference = call.target if isinstance(call.target, Mapping) else {"id": call.name}
        metadata = {
            "agent_flow": {
                "id": reference["id"],
                "options": _plain(reference.get("options") or {}),
                "input": _plain(call.input),
                "depth": depth,
                "parent_run": run_id,
            }
        }
    if call.editor is not None:
        metadata = {**(metadata or {}), "workflow_editor": dict(call.editor)}
    elif call.name == "self":
        metadata = {
            "agent_flow": {"id": "react", "options": {}, "depth": depth, "parent_run": run_id}
        }
    if call.label:
        metadata = {**(metadata or {}), "subagent_name": call.label}
    return metadata


def child_model(app: Any, handle: str) -> str:
    """``account/model`` the child of task ``handle`` runs on, or ``""`` when unknown."""

    snapshot = app.state.task_history.task_snapshot(handle)
    session = app.state.sessions.get(snapshot.owner.session_id) if snapshot is not None else None
    model = dict(getattr(session, "model", None) or {})
    if model.get("provider_id") and model.get("model_id"):
        return f"{model['provider_id']}/{model['model_id']}"
    return ""


__all__ = ["child_model", "child_session_metadata"]
