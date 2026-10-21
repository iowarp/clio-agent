"""Run an agent's selected flow for one turn.

The agent module calls :func:`run_agent_flow` inside the turn's worker, with
the agent's LM bound. The flow class comes from the plugin that ships it (by
id, from the pinned resolution); ``react`` from ``core`` is the default and
runs the agent's ClioReAct program through ``ctx.react``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from clio_agent.sdk.flow import Flow


class FlowUnavailable(RuntimeError):
    """The agent's selected flow cannot be loaded; ``reason`` is typed."""

    def __init__(self, flow_id: str, reason: str, detail: str = "") -> None:
        super().__init__(
            f"flow {flow_id!r} is unavailable: {reason}" + (f" ({detail})" if detail else "")
        )
        self.flow_id = flow_id
        self.reason = reason


def load_flow(resolution: Any, flow_id: str, options: dict[str, Any] | None = None) -> Flow:
    """Instantiate flow ``flow_id`` from the resolved plugin that ships it."""

    from clio_agent.plugins.loader import load_attribute  # noqa: PLC0415

    row = next((row for row in resolution.flows if row["id"] == flow_id), None)
    if row is None:
        raise FlowUnavailable(flow_id, "flow_not_resolved", "no resolved plugin provides it")
    try:
        flow_class = load_attribute(row["root"], row["module"], row["attr"])
    except Exception as exc:  # noqa: BLE001 - a broken flow module is a typed refusal
        raise FlowUnavailable(
            flow_id, "flow_import_failed", f"{type(exc).__name__}: {exc}"
        ) from exc
    if not (isinstance(flow_class, type) and issubclass(flow_class, Flow)):
        raise FlowUnavailable(flow_id, "flow_invalid", f"{row['attr']} is not a Flow")
    flow = flow_class()
    for key, value in (options or {}).items():
        setattr(flow, key, value)
    return flow


def flow_selection(app: Any, session_id: str, agent_def: Any) -> dict[str, Any]:
    """The flow this session's turn runs: a started flow's override, else the agent's.

    A flow started through ``run_flow``/``run_plan`` or ``ctx.call`` runs in a
    child session whose metadata carries ``agent_flow`` (id, options, input,
    depth); every other session runs its agent's selected flow.
    """

    sessions = getattr(getattr(app, "state", None), "sessions", None)
    session = sessions.get(session_id) if sessions is not None and session_id else None
    override = (getattr(session, "metadata", None) or {}).get("agent_flow")
    if isinstance(override, dict) and override.get("id"):
        return dict(override)
    return dict(agent_def.metadata.get("flow") or {"id": "react", "options": {}})


def run_agent_flow(module: Any, kwargs: dict[str, Any]) -> Any:
    """Run ``module``'s flow on this turn's inputs and return its prediction."""

    import hashlib  # noqa: PLC0415

    from clio_agent.flows.context import FlowContext  # noqa: PLC0415
    from clio_agent.gact import context as _ctx  # noqa: PLC0415

    app = _ctx.active_app()
    session_id = str(kwargs.get("session_id") or _ctx.active_session_id())
    selection = flow_selection(app, session_id, module.agent_def)
    flow_id = str(selection.get("id") or "react")
    flow = load_flow(module.resolution, flow_id, selection.get("options"))
    task = selection["input"] if selection.get("input") is not None else kwargs["question"]
    turn = _ctx.current_turn()
    root_handle = turn.assignment_handle
    if not root_handle and app is not None:
        latest = app.state.task_history.latest_assignment(session_id)
        root_handle = latest.handle if latest is not None else ""
    marker = (
        turn.input_notification_id
        or hashlib.sha256(str(kwargs.get("question") or "").encode("utf-8")).hexdigest()[:24]
    )
    ctx = FlowContext(
        app=app,
        session_id=session_id,
        module=module,
        flow=flow,
        root_handle=root_handle,
        run_invocation=f"flow:{flow_id}:{marker}",
        task=task,
        depth=int(selection.get("depth") or 0),
        kwargs=kwargs,
    )
    return asyncio.run(ctx.run())
