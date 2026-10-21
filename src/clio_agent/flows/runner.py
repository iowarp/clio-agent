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


def run_agent_flow(module: Any, kwargs: dict[str, Any]) -> Any:
    """Run ``module``'s flow on this turn's inputs and return its prediction."""

    from clio_agent.flows.context import TurnFlowContext  # noqa: PLC0415

    selection = dict(module.agent_def.metadata.get("flow") or {"id": "react", "options": {}})
    flow = load_flow(
        module.resolution, str(selection.get("id") or "react"), selection.get("options")
    )
    ctx = TurnFlowContext(module=module, kwargs=kwargs, flow=flow)
    return asyncio.run(ctx.run(kwargs["question"]))
