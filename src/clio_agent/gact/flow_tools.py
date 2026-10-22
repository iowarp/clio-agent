"""Model-facing tools that start flows and edit running plans.

``run_flow`` and ``run_plan`` come from the ``orchestration`` plugin. Each starts
the flow as owned work: a child assignment of this agent (pinned to this
session's exact resolution) whose turn runs the flow, so the call returns the
child's handle at acceptance. The model keeps working; the result arrives
through the mailbox; Stop ends only the model's wait; ``cancel_tasks`` on the
handle cancels the flow and its subtree.

``plan_add_node`` and ``plan_cancel_node`` are not claimable by a plugin: the
``plan`` flow grants them to an editor for the duration of its own call (the
child session carries ``plan_editor``). They only propose; the plan flow reads
the proposals from the editor's settled result and applies them within the
plan's bounds, with approval when the plan asks for it.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from clio_agent.gact import context
from clio_agent.gact.agents.tool_instrumentation import native_tool
from clio_agent.tools.task_call_context import TASK_CALL

PLAN_EDITOR_TOOLS = ("plan_add_node", "plan_cancel_node")


def _runtime() -> tuple[Any, Any]:
    app = context.active_app()
    if app is None or not context.active_session_id():
        raise ValueError("Flows run inside a product conversation")
    return app, app.state.conversations.caller(context.active_session_id())


def _flows(app: Any, session_id: str) -> list[dict[str, Any]]:
    from clio_agent.gact.agents.resolution import session_resolution  # noqa: PLC0415

    return list(session_resolution(app, session_id).flows)


async def start_flow(
    flow_id: str, flow_input: Any, options: dict[str, Any] | None
) -> dict[str, Any]:
    """Accept a child assignment that runs ``flow_id`` and return its handle."""

    from clio_agent.flows.values import _plain, render_input  # noqa: PLC0415

    app, caller = _runtime()
    available = await app.state.task_history.io.run(_flows, app, caller.session_id)
    if not any(row["id"] == flow_id for row in available):
        names = ", ".join(sorted(row["id"] for row in available)) or "none"
        return {
            "accepted": False,
            "reason": "flow_not_resolved",
            "error": f"no resolved plugin provides flow {flow_id!r}; available: {names}",
        }
    session = app.state.sessions.get(caller.session_id)
    parent_flow = (getattr(session, "metadata", None) or {}).get("agent_flow") or {}
    invocation = TASK_CALL.get().invocation_id or uuid4().hex
    accepted = await app.state.conversations.spawn(
        caller,
        render_input(flow_input) or f"Run the {flow_id} flow.",
        "self",
        "flow:" + invocation,
        session_metadata={
            "agent_flow": {
                "id": flow_id,
                "options": _plain(options or {}),
                "input": _plain(flow_input),
                "depth": int(parent_flow.get("depth") or 0) + 1,
            }
        },
    )
    return {**accepted, "flow": flow_id}


def _run_flow_description() -> str:
    """``run_flow``'s description, listing the flows this session's agent resolves."""

    from clio_agent.gact.agents.resolution import SessionAgentUnavailable  # noqa: PLC0415

    base = (
        "Start one of this agent's flows as owned work and return its task handle at "
        "acceptance. Keep working; its result arrives as a task result. wait_tasks(handle) "
        "waits for it; cancel_tasks(handle) cancels it and its subtree."
    )
    try:
        app, caller = _runtime()
        rows = _flows(app, caller.session_id)
    except (ValueError, SessionAgentUnavailable):
        return base  # Built outside a conversation: no session flows to list.
    listed = [
        f"- {row['id']}: {row['description']}"
        for row in rows
        if row["id"] not in {"react", "plan"} and row.get("description")
    ]
    return base + ("\nFlows:\n" + "\n".join(listed) if listed else "")


def build_run_flow_tool() -> Any:
    async def run_flow(
        flow: str,
        input: Any = None,
        options: dict[str, Any] | None = None,  # noqa: A002
    ) -> dict[str, Any]:
        return await start_flow(str(flow or "").strip(), input, options)

    return native_tool(
        run_flow,
        name="run_flow",
        domain="tasks",
        title="Run flow",
        desc=_run_flow_description(),
        args={
            "flow": {"type": "string", "description": "The flow id."},
            "input": {"description": "The flow's task: text or a JSON object."},
            "options": {
                "anyOf": [{"type": "object"}, {"type": "null"}],
                "default": None,
                "description": "Flow options, e.g. council members.",
            },
        },
        presentation="tasks",
    )


def build_run_plan_tool() -> Any:
    async def run_plan(
        plan: dict[str, Any],
        task: str = "",
        editors: list[str] | None = None,
        max_added: int = 6,
        max_rounds: int = 3,
        allowed_targets: list[str] | None = None,
        approve_edits: bool = False,
    ) -> dict[str, Any]:
        from clio_agent.sdk.plan import Plan, PlanError  # noqa: PLC0415

        try:
            normalized = Plan.from_dict(plan).to_dict()
        except PlanError as exc:
            return {"accepted": False, "reason": "plan_invalid", "error": str(exc)}
        if max_added < 0 or max_rounds < 0:
            return {"accepted": False, "reason": "plan_invalid", "error": "bounds must be >= 0"}
        flow_input = {
            "plan": normalized,
            "task": task,
            "editors": list(editors or []),
            "max_added": max_added,
            "max_rounds": max_rounds,
            "allowed_targets": allowed_targets,
            "approve_edits": approve_edits,
        }
        return await start_flow("plan", flow_input, None)

    node_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "target": {
                "type": "string",
                "description": "A subagent, an installed agent (marketplace/id), a flow or a tool.",
            },
            "input": {"description": "Text or an object; $task and $<node> refer to inputs."},
            "after": {"type": "array", "items": {"type": "string"}},
            "options": {
                "type": "object",
                "description": "For a flow target: its options, e.g. arena contestants.",
            },
        },
        "required": ["id", "target"],
    }
    return native_tool(
        run_plan,
        name="run_plan",
        domain="tasks",
        title="Run plan",
        desc=(
            "Start a plan (nodes with targets, inputs and dependencies) as owned work and return "
            "its task handle at acceptance. Each node starts as soon as the nodes it waits on "
            "finish. Named editors may add or cancel nodes while it runs, within max_added and "
            "max_rounds; approve_edits asks the user before each added node runs."
        ),
        args={
            "plan": {
                "type": "object",
                "properties": {"nodes": {"type": "array", "items": node_schema}},
                "required": ["nodes"],
            },
            "task": {"type": "string", "default": ""},
            "editors": {
                "anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}],
                "default": None,
            },
            "max_added": {"type": "integer", "minimum": 0, "default": 6},
            "max_rounds": {"type": "integer", "minimum": 0, "default": 3},
            "allowed_targets": {
                "anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}],
                "default": None,
            },
            "approve_edits": {"type": "boolean", "default": False},
        },
        presentation="tasks",
    )


def build_plan_editor_tools(editor: dict[str, Any]) -> list[Any]:
    """The editor tools for a plan editor's call (``editor`` = its ``plan_editor`` metadata)."""

    run_id = str(editor.get("run") or "")

    def plan_add_node(
        node_id: str,
        target: str,
        input: Any = None,  # noqa: A002
        after: list[str] | None = None,
        reason: str = "",
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not str(node_id).strip() or not str(target).strip():
            raise ValueError("plan_add_node needs node_id and target")
        return {
            "proposed": True,
            "run": run_id,
            "node_id": node_id,
            "target": target,
            "after": list(after or []),
            **({"options": dict(options)} if options else {}),
            "note": "The plan applies this after your call, within its bounds and approvals.",
        }

    def plan_cancel_node(node_id: str, reason: str = "") -> dict[str, Any]:
        if not str(node_id).strip():
            raise ValueError("plan_cancel_node needs node_id")
        return {"proposed": True, "run": run_id, "node_id": node_id, "cancel": True}

    return [
        native_tool(
            plan_add_node,
            name="plan_add_node",
            domain="tasks",
            title="Propose plan node",
            desc=(
                "Propose a new node for the running plan you are editing: a target, its input "
                "and the nodes it waits on (by default your own node). The plan applies it after "
                "your call, within its growth bounds and approvals."
            ),
            args={
                "node_id": {"type": "string"},
                "target": {"type": "string"},
                "input": {"description": "Text or an object; $task and $<node> refer to inputs."},
                "after": {
                    "anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}],
                    "default": None,
                },
                "reason": {"type": "string", "default": ""},
                "options": {
                    "anyOf": [{"type": "object"}, {"type": "null"}],
                    "default": None,
                    "description": "For a flow target: its options, e.g. arena contestants.",
                },
            },
            presentation="fields:node_id,target,note",
        ),
        native_tool(
            plan_cancel_node,
            name="plan_cancel_node",
            domain="tasks",
            title="Propose cancelling a plan node",
            desc="Propose cancelling a plan node that has not started yet.",
            args={"node_id": {"type": "string"}, "reason": {"type": "string", "default": ""}},
            presentation="fields:node_id,cancel",
        ),
    ]
