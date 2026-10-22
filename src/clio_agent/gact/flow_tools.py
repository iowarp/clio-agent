"""Model-facing tools that start flows and edit running workflows.

``run_flow`` and ``run_workflow`` come from the ``orchestration`` plugin. Each starts
the flow as owned work: a child assignment of this agent (pinned to this
session's exact resolution) whose turn runs the flow, so the call returns the
child's handle at acceptance. The model keeps working; the result arrives
through the mailbox; Stop ends only the model's wait; ``cancel_tasks`` on the
handle cancels the flow and its subtree.

``workflow_add_step`` and ``workflow_cancel_step`` are not claimable by a plugin: the
``workflow`` flow grants them to an editor for the duration of its own call (the
child session carries ``workflow_editor``). They only propose; the workflow flow reads
the proposals from the editor's settled result and applies them within the
workflow's bounds, with approval when the workflow asks for it.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from clio_agent.gact import context
from clio_agent.gact.agents.tool_instrumentation import native_tool
from clio_agent.tools.task_call_context import TASK_CALL

WORKFLOW_EDITOR_TOOLS = ("workflow_add_step", "workflow_cancel_step")


def _runtime() -> tuple[Any, Any]:
    app = context.active_app()
    if app is None or not context.active_session_id():
        raise ValueError("Flows run inside a product conversation")
    return app, app.state.conversations.caller(context.active_session_id())


def _flows(app: Any, session_id: str) -> list[dict[str, Any]]:
    from clio_agent.gact.agents.resolution import session_resolution  # noqa: PLC0415

    return list(session_resolution(app, session_id).flows)


async def permit_flow(
    app: Any, caller: Any, flow_id: str, flow_input: Any, options: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Ask the person to run ``flow_id`` when its plugin is not trusted in its installed
    version; ``None`` when it may start (or no installed plugin declares it), else the
    refusal. Allowed, the plugin is trusted and added to this conversation. A flow of a
    trusted plugin this agent lacks is added to the conversation without a question."""

    from clio_agent.flows.values import _plain  # noqa: PLC0415
    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        SessionAgentUnavailable,
        plugin_service,
    )
    from clio_agent.gact.plugin_authoring import (  # noqa: PLC0415
        add_to_conversation,
        outside_flows,
        permit_untrusted_flow,
    )

    store = plugin_service(app).store
    available = await app.state.task_history.io.run(_flows, app, caller.session_id)
    row = next((row for row in available if row["id"] == flow_id), None)
    waiting = {r.id for r in await asyncio.to_thread(store.pending)}
    if row is not None and row["plugin"] not in waiting:
        return None
    if row is None:
        resolved = {r["id"] for r in available}
        rows = await asyncio.to_thread(outside_flows, store, resolved)
        outside = next((r for r in rows if r["flow"] == flow_id), None)
        if outside is not None:
            try:
                await asyncio.to_thread(
                    add_to_conversation, app, caller.session_id, [outside["ref"]]
                )
            except SessionAgentUnavailable as exc:
                return {"accepted": False, "reason": "flow_not_added", "error": str(exc)}
            return None
    call = {"input": _plain(flow_input), "options": _plain(options or {})}
    permit = await permit_untrusted_flow(app, caller.session_id, flow_id, call)
    return permit if permit is not None and not permit.get("trusted") else None


async def start_flow(
    flow_id: str, flow_input: Any, options: dict[str, Any] | None
) -> dict[str, Any]:
    """Accept a child assignment that runs ``flow_id`` and return its handle."""

    from clio_agent.flows.values import _plain, render_input  # noqa: PLC0415

    app, caller = _runtime()
    refused = await permit_flow(app, caller, flow_id, flow_input, options)
    if refused is not None:
        return refused
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
    """``run_flow``'s description: the flows this session's agent resolves, and the flows
    installed plugins declare that are not trusted yet (running one asks the person)."""

    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        SessionAgentUnavailable,
        plugin_service,
    )
    from clio_agent.gact.plugin_authoring import outside_flows, untrusted_flows  # noqa: PLC0415

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
        if row["id"] not in {"react", "workflow"} and row.get("description")
    ]
    described = base + ("\nFlows:\n" + "\n".join(listed) if listed else "")
    store = plugin_service(app).store
    outside = [
        f"- {row['flow']}: {row['description']} (the {row['plugin']} plugin)"
        for row in outside_flows(store, {row["id"] for row in rows})
    ]
    if outside:
        described += (
            "\nInstalled and trusted, not in this agent yet; running one (here or as a "
            "workflow step) adds its plugin to this conversation:\n" + "\n".join(outside)
        )
    # An untrusted plugin's own description is text no one has reviewed: only its
    # flow id and who wrote it reach the model (in every conversation).
    waiting = [
        f"- {row['flow']} (the {row['plugin']} plugin, by {row['author']})"
        for row in untrusted_flows(store)
    ]
    if waiting:
        described += (
            "\nInstalled but not trusted yet; running one (here or as a workflow step) asks "
            "the person first:\n" + "\n".join(waiting)
        )
    return described


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


def build_run_workflow_tool() -> Any:
    async def run_workflow(
        workflow: dict[str, Any],
        task: str = "",
        editors: list[str] | None = None,
        max_added_steps: int | None = None,
        max_edit_rounds: int | None = None,
        allowed_targets: list[str] | None = None,
        approve_edits: bool = False,
    ) -> dict[str, Any]:
        from clio_agent.sdk.workflow import Workflow, WorkflowError  # noqa: PLC0415

        try:
            normalized = Workflow.from_dict(workflow).to_dict()
        except WorkflowError as exc:
            return {"accepted": False, "reason": "workflow_invalid", "error": str(exc)}
        if any(bound is not None and bound < 0 for bound in (max_added_steps, max_edit_rounds)):
            return {"accepted": False, "reason": "workflow_invalid", "error": "bounds must be >= 0"}
        # Each step that runs a flow from a plugin not trusted yet is asked about now, in
        # this conversation, before any step runs: a running workflow's agent is fixed.
        app, caller = _runtime()
        for step in normalized["steps"]:
            refused = await permit_flow(
                app, caller, step["target"], step.get("input"), step.get("options")
            )
            if refused is not None:
                return {**refused, "step": step["id"]}
        flow_input = {
            "workflow": normalized,
            "task": task,
            "editors": list(editors or []),
            "max_added_steps": max_added_steps,
            "max_edit_rounds": max_edit_rounds,
            "allowed_targets": allowed_targets,
            "approve_edits": approve_edits,
        }
        return await start_flow("workflow", flow_input, None)

    step_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "target": {
                "type": "string",
                "description": "A subagent, an installed agent (marketplace/id), a flow or a tool.",
            },
            "input": {"description": "Text or an object; $task and $<step> refer to inputs."},
            "after": {"type": "array", "items": {"type": "string"}},
            "options": {
                "type": "object",
                "description": "For a flow target: its options, e.g. arena contestants.",
            },
        },
        "required": ["id", "target"],
    }
    return native_tool(
        run_workflow,
        name="run_workflow",
        domain="tasks",
        title="Run workflow",
        desc=(
            "Start a workflow (steps with targets, inputs and dependencies) as owned work and return "
            "its task handle at acceptance. Each step starts as soon as the steps it waits on "
            "finish. Named editors may add or cancel steps while it runs, within max_added_steps and "
            "max_edit_rounds when set; approve_edits asks the user before each added step runs. "
            "A step may run a flow that is installed but not trusted yet (run_flow lists them): "
            "starting the workflow asks the person about it first."
        ),
        args={
            "workflow": {
                "type": "object",
                "properties": {"steps": {"type": "array", "items": step_schema}},
                "required": ["steps"],
            },
            "task": {"type": "string", "default": ""},
            "editors": {
                "anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}],
                "default": None,
            },
            "max_added_steps": {
                "anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}],
                "default": None,
                "description": "Most steps the editors may add; unset leaves it open.",
            },
            "max_edit_rounds": {
                "anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}],
                "default": None,
                "description": "Most editing rounds; unset leaves it open.",
            },
            "allowed_targets": {
                "anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}],
                "default": None,
            },
            "approve_edits": {"type": "boolean", "default": False},
        },
        presentation="tasks",
    )


def build_workflow_editor_tools(editor: dict[str, Any]) -> list[Any]:
    """The editor tools for a workflow editor's call (``editor`` = its ``workflow_editor`` metadata)."""

    run_id = str(editor.get("run") or "")

    def workflow_add_step(
        step_id: str,
        target: str,
        input: Any = None,  # noqa: A002
        after: list[str] | None = None,
        reason: str = "",
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not str(step_id).strip() or not str(target).strip():
            raise ValueError("workflow_add_step needs step_id and target")
        return {
            "proposed": True,
            "run": run_id,
            "step_id": step_id,
            "target": target,
            "after": list(after or []),
            **({"options": dict(options)} if options else {}),
            "note": "The workflow applies this after your call, within its bounds and approvals.",
        }

    def workflow_cancel_step(step_id: str, reason: str = "") -> dict[str, Any]:
        if not str(step_id).strip():
            raise ValueError("workflow_cancel_step needs step_id")
        return {"proposed": True, "run": run_id, "step_id": step_id, "cancel": True}

    return [
        native_tool(
            workflow_add_step,
            name="workflow_add_step",
            domain="tasks",
            title="Propose workflow step",
            desc=(
                "Propose a new step for the running workflow you are editing: a target, its input "
                "and the steps it waits on (by default your own step). The workflow applies it after "
                "your call, within its growth bounds and approvals."
            ),
            args={
                "step_id": {"type": "string"},
                "target": {"type": "string"},
                "input": {"description": "Text or an object; $task and $<step> refer to inputs."},
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
            presentation="fields:step_id,target,note",
        ),
        native_tool(
            workflow_cancel_step,
            name="workflow_cancel_step",
            domain="tasks",
            title="Propose cancelling a workflow step",
            desc="Propose cancelling a workflow step that has not started yet.",
            args={"step_id": {"type": "string"}, "reason": {"type": "string", "default": ""}},
            presentation="fields:step_id,cancel",
        ),
    ]
