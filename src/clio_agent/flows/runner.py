"""Run an agent's selected flow for one turn.

The agent module calls :func:`run_agent_flow` inside the turn's worker, with
the agent's LM bound. The flow class comes from the plugin that ships it (by
id, from the pinned resolution); ``react`` from ``core`` is the default and
runs the agent's ClioReAct program through ``ctx.react``.
"""

from __future__ import annotations

from typing import Any

from clio_agent.runtime.fresh_loop import run_in_new_loop
from clio_agent.sdk.flow import Flow, FlowOptionsInvalid


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
    try:
        checked = flow_class.checked_options(options)
    except FlowOptionsInvalid as exc:
        raise FlowUnavailable(flow_id, "flow_options_invalid", "; ".join(exc.problems)) from exc
    flow = flow_class()
    for key, value in checked.items():
        setattr(flow, key, value)
    return flow


def flow_selection(app: Any, session_id: str, agent_def: Any) -> dict[str, Any]:
    """The flow this session's turn runs: a started flow's override, else the agent's.

    A flow started through ``run_flow``/``run_workflow`` or ``ctx.call`` runs in a
    child session whose metadata carries ``agent_flow`` (id, options, input,
    depth); every other session runs its agent's selected flow.
    """

    sessions = getattr(getattr(app, "state", None), "sessions", None)
    session = sessions.get(session_id) if sessions is not None and session_id else None
    override = (getattr(session, "metadata", None) or {}).get("agent_flow")
    if isinstance(override, dict) and override.get("id"):
        return dict(override)
    return dict(agent_def.metadata.get("flow") or {"id": "react", "options": {}})


def accept_flow_input(module: Any, question: str, session_id: str) -> None:
    """Confirm this turn's input for a flow other than ``react``, before it runs.

    The agent's ReAct program confirms the turn's staged input with its first model
    request. Any other flow (a started council or workflow, or an agent whose own flow is
    not ``react``) makes no request in this session, so its input is confirmed here;
    unconfirmed, the assignment keeps eligible mail, yields instead of completing,
    and is admitted again at every turn end.
    """

    from dspy.lm15 import Message, TextPart  # noqa: PLC0415

    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.gact.agents.clio_react import NoContextStoreError  # noqa: PLC0415
    from clio_agent.gact.agents.clio_react_record import StepRecorder, arc_scope  # noqa: PLC0415

    app = _ctx.active_app()
    if str(flow_selection(app, session_id, module.agent_def).get("id") or "react") == "react":
        return
    arc, session, scope = arc_scope()
    if arc is None:
        raise NoContextStoreError()
    recorder = StepRecorder(arc, session, scope, expert_id=str(module.agent_def.id))
    recorder.user_message(Message(role="user", parts=(TextPart(text=question),)))
    recorder.injections(_ctx.turn_injections())
    recorder.read_steps()
    recorder.commit_input(0)


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
    resume_from: list[tuple[str, str]] = []
    if selection.get("input") is not None or "depth" in selection:
        # A started flow (run_flow, run_workflow, ctx.call) is its whole assignment:
        # a later turn of the same assignment resumes the same run.
        marker = root_handle
        invocation, predecessor = _lineage(app, root_handle)
        if predecessor and invocation.startswith("restart:"):
            # A restart continues the runs before it: finished work is kept.
            resume_from = _earlier_runs(app, flow_id, predecessor)
        elif predecessor and app is not None:
            trigger = app.state.task_history.mail_item(session_id, turn.input_notification_id)
            written = trigger is not None and trigger.kind in {"human", "peer", "delegation"}
            message = str(kwargs.get("question") or "") if written else ""
            return _after_the_run(flow_id, predecessor, session_id, message)
    else:
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
    ctx.journal.resume_from = resume_from
    return run_in_new_loop(ctx.run())


def _lineage(app: Any, handle: str) -> tuple[str, str]:
    """The assignment's invocation id and the assignment it follows ("" for the first)."""

    from clio_agent.arc.task_types import AssignmentDetails  # noqa: PLC0415

    history = getattr(getattr(app, "state", None), "task_history", None)
    if history is None or not handle:
        return "", ""
    task = history.task_snapshot(handle)
    if task is None:
        return "", ""
    details = (
        AssignmentDetails.model_validate(history.read_content(task.details))
        if task.details is not None
        else AssignmentDetails()
    )
    return str(task.invocation_id or ""), str(details.predecessor or "")


def _earlier_runs(app: Any, flow_id: str, handle: str) -> list[tuple[str, str]]:
    """The runs of ``flow_id`` at or before ``handle`` in its assignment chain, oldest first.

    Assignments that only passed later input on recorded no run; they are skipped.
    """

    history = app.state.task_history
    runs: list[tuple[str, str]] = []
    seen: set[str] = set()
    while handle and handle not in seen:
        seen.add(handle)
        invocation = f"flow:{flow_id}:{handle}"
        if history.workflow_checkpoint(handle, invocation) is not None:
            runs.append((handle, invocation))
        handle = _lineage(app, handle)[1]
    return runs[::-1]


def _after_the_run(flow_id: str, ended: str, session_id: str, message: str) -> Any:
    """Input reaching a started flow's session after its run ended.

    The flow does not run again (that would repeat its work). The assignment's
    result says so, and carries a written message on, so it reaches the agent that
    started the flow; task outcomes stay recorded in the flow's conversation.
    """

    import dspy  # noqa: PLC0415

    answer = (
        f"The {flow_id} run in conversation {session_id} already ended (assignment {ended}), "
        "so it does not run again. Task outcomes that arrive later stay recorded there."
    )
    if message:
        answer += "\n\nThis message arrived afterwards and is passed on as written:\n\n" + message
    return dspy.Prediction(answer=answer)
