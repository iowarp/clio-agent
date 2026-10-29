"""An agent forks itself: the ``fork`` tool's work (owner module).

A fork is a copy of the caller, conversation included, that runs beside it under
the caller's parent; whoever waits on the caller also waits on the fork
(:mod:`~clio_agent.gact.task_forks`). Where it lands depends on the caller:

* **A root conversation** (a conversation or a person's branch): a new branch in
  the left panel, made by the same code as a person's fork, with the agent named as
  its author and the note sent as its first input. Nobody waits on it.
* **A delegated child**: a sibling task of the same parent, accepted through the
  idempotent spawn path (``fork:<session>`` target) with ``fork_of`` set to the
  caller's assignment, so the parent's wait covers both.
* **A workflow step**: the same sibling task, which the run draws beside its step
  (``<step>~<n>``). It counts as an added step against the run's growth bounds, and
  under ``approve_edits`` it first raises the same kind of Question an edit does.

Every refusal is a typed result: ``fork_limit_reached`` (Settings, Forks per task),
``max_added_reached`` (the workflow's bounds), ``not_approved`` (the person said no).
A replay of the caller re-issues the same call, which addresses the same fork.
"""

from __future__ import annotations

import hashlib
from typing import Any

from clio_agent.gact.session_kind import session_kind

FORK_LIMIT_REACHED = "fork_limit_reached"
MAX_ADDED_REACHED = "max_added_reached"
NOT_APPROVED = "not_approved"
APPROVAL_PENDING = "approval_pending"


async def fork_caller(
    app: Any, caller: Any, note: str, title: str | None, invocation: str
) -> dict[str, Any]:
    """Fork the calling session with ``note`` as the fork's next input."""

    if not str(note or "").strip():
        raise ValueError("fork needs a note: what the fork should do differently")
    session = app.state.sessions.get(caller.session_id)
    if session is None:
        raise ValueError("The calling session is unavailable")
    if session_kind(session) in {"conversation", "branch"}:
        return await _fork_conversation(app, session, note.strip(), title, invocation)
    return await _fork_task(app, session, note.strip(), title, invocation)


def _agent_forks(app: Any, session_id: str) -> list[Any]:
    from clio_agent.gact.conversation_origin import FORK_ORIGIN_KEY  # noqa: PLC0415

    return [
        row
        for row in app.state.sessions.list()
        if row.parent_session_id == session_id
        and (row.metadata.get(FORK_ORIGIN_KEY) or {}).get("author", {}).get("kind") == "agent"
    ]


async def _fork_conversation(
    app: Any, session: Any, note: str, title: str | None, invocation: str
) -> dict[str, Any]:
    from clio_agent.gact.agent_task_limits import max_forks_per_task  # noqa: PLC0415
    from clio_agent.gact.conversation_copy import create_branch  # noqa: PLC0415
    from clio_agent.gact.conversation_origin import FORK_ORIGIN_KEY  # noqa: PLC0415
    from clio_agent.gact.message_contract import PostMessageRequest  # noqa: PLC0415
    from clio_agent.gact.message_submission import accept_message_async  # noqa: PLC0415
    from clio_agent.gact.off_loop import run_off_loop  # noqa: PLC0415
    from clio_agent.gact.parts import Part  # noqa: PLC0415

    forks = await run_off_loop(_agent_forks, app, session.id)
    branch: Any = next(
        (
            row
            for row in forks
            if (row.metadata.get(FORK_ORIGIN_KEY) or {}).get("invocation") == invocation
        ),
        None,
    )
    if branch is None:
        limit = max_forks_per_task()
        if limit and len(forks) >= limit:
            return _refused(FORK_LIMIT_REACHED, f"this conversation already has {limit} forks")
        origin = {
            "author": {"kind": "agent", "agent_id": str(session.agent.get("id") or "")},
            "invocation": invocation,
            "note": note,
        }
        branch = await run_off_loop(
            create_branch, app, session, title=title or "", open_turn=True, origin=origin
        )
    await accept_message_async(
        app,
        app.state.gact_deps,
        branch.id,
        PostMessageRequest(
            parts=[Part(type="text", text=note)],
            idempotency_key="fork-note:" + invocation,
            metadata={"fork_note": True},
        ),
        server_metadata={"fork_note": True, "author": "agent"},
    )
    return {
        "forked": True,
        "kind": "conversation",
        "session_id": branch.id,
        "title": branch.title,
        "note": note,
        "waited_on_by": "nobody: the person reads both conversations",
    }


async def _fork_task(
    app: Any, session: Any, note: str, title: str | None, invocation: str
) -> dict[str, Any]:
    from clio_agent.gact.spawn_context import FORK_TARGET  # noqa: PLC0415

    history = app.state.task_history
    conversations = app.state.conversations
    plan = await history.io.run(_task_plan, app, session, invocation)
    if plan.get("refused"):
        return plan
    if plan.get("ask"):
        answer = await _approved(app, session, {**plan, "note": note}, invocation)
        if answer is None:
            return _refused(APPROVAL_PENDING, "the approval question is still open")
        if not answer:
            return _refused(NOT_APPROVED, "the person did not approve this fork")
    parent_caller = await history.io.run(conversations.caller, plan["parent_session"])
    label = plan["label"] if plan.get("step") else (title or plan["label"])
    # The fork's copy answers this call as the fork sees it (conversation_copy.py).
    metadata: dict[str, Any] = {
        "subagent_name": label,
        "fork_call": {"call_id": invocation, "note": note},
    }
    if plan.get("step"):
        metadata["workflow_step_of"] = {"run": plan["run"], "step": plan["step"]}
    accepted = await conversations.spawn(
        parent_caller,
        note,
        FORK_TARGET + session.id,
        plan["invocation"],
        parent_handle=plan["parent_handle"],
        session_metadata=metadata,
        fork_of=plan["fork_of"],
    )
    return {
        "forked": bool(accepted.get("accepted")),
        "kind": "task",
        "handle": accepted.get("handle", ""),
        "session_id": accepted.get("session_id", ""),
        "label": label,
        "fork_of": plan["fork_of"],
        "note": note,
        "waited_on_by": "whoever waits on your task also waits on this fork",
        **({"error": accepted["error"]} if accepted.get("error") else {}),
    }


def _task_plan(app: Any, session: Any, invocation: str) -> dict[str, Any]:
    """Where a delegated session's fork lands and whether it may be made (off the loop)."""

    from clio_agent.flows.run_view import flow_run  # noqa: PLC0415
    from clio_agent.gact.agent_task_limits import max_forks_per_task  # noqa: PLC0415
    from clio_agent.gact.task_forks import fork_family  # noqa: PLC0415

    history = app.state.task_history
    assignment = history.latest_assignment(session.id)
    if assignment is None or assignment.owner.parent_handle is None:
        raise ValueError("This session has no delegated assignment to fork")
    parent = history.task_snapshot(assignment.owner.parent_handle)
    if parent is None:
        raise ValueError("The delegating assignment is unavailable")
    plan: dict[str, Any] = {
        "fork_of": assignment.handle,
        "parent_handle": parent.handle,
        "parent_session": parent.owner.session_id,
        "invocation": f"fork:{session.id}:{invocation}",
    }
    handle = (
        "task_"
        + hashlib.sha256((plan["parent_session"] + "\0" + plan["invocation"]).encode()).hexdigest()
    )
    existing = history.task_snapshot(handle)
    original = assignment
    while original.fork_of and (earlier := history.task_snapshot(original.fork_of)) is not None:
        original = earlier
    family = fork_family(history, original.handle)
    step = session.metadata.get("workflow_step_of") or {}
    if existing is not None:
        # A replay addresses the fork it made the first time.
        name = app.state.sessions.get(existing.owner.session_id)
        plan["label"] = str((name.metadata if name else {}).get("subagent_name") or existing.label)
        plan.update(step=step.get("step", ""), run=step.get("run", ""))
        return plan
    if step:
        run = flow_run(history, parent.handle) or {}
        view = run.get("view") or {}
        forks_in_run = sum(
            1
            for member in history.descendant_handles(parent.handle)
            if (task := history.task_snapshot(member)) is not None and task.fork_of
        )
        bound = view.get("max_added_steps")
        if bound is not None and int(view.get("added") or 0) + forks_in_run >= int(bound):
            return _refused(MAX_ADDED_REACHED, f"the workflow may grow by at most {bound} steps")
        plan.update(
            step=str(step.get("step") or ""),
            run=str(step.get("run") or ""),
            label=f"{step.get('step')}~{len(family)}",
            ask=bool(view.get("approve_edits")),
        )
        return plan
    limit = max_forks_per_task()
    if limit and len(family) - 1 >= limit:
        return _refused(FORK_LIMIT_REACHED, f"this task already has {limit} forks")
    base = app.state.sessions.get(original.owner.session_id)
    plan["label"] = (
        f"{(base.metadata.get('subagent_name') if base else '') or session.title}~{len(family)}"
    )
    return plan


async def _approved(app: Any, session: Any, plan: dict[str, Any], invocation: str) -> bool | None:
    """Ask the person, as a workflow edit does, and wait for the answer; ``None`` when
    the wait ended (Stop) before an answer came."""

    from clio_agent.arc.task_types import QuestionDefinition, TaskOwner  # noqa: PLC0415

    history = app.state.task_history
    caller = await history.io.run(app.state.conversations.caller, session.id)
    handle = (
        "task_"
        + hashlib.sha256((session.id + "\0fork-approval\0" + invocation).encode()).hexdigest()
    )

    def post() -> None:
        if history.task_snapshot(handle) is not None:
            return
        assignment = history.task_snapshot(plan["fork_of"])
        definition = QuestionDefinition.model_validate(
            {
                "prompt": f"Fork step {plan['step']!r} as {plan['label']!r}: "
                f"{_brief(plan.get('note', ''))}Allow?",
                "kind": "choice",
                "options": [{"label": "Yes", "value": "yes"}, {"label": "No", "value": "no"}],
                "allow_freeform": False,
                "source": "fork",
                "metadata": {"workflow_run": plan["run"], "step": plan["step"]},
            }
        )
        app.state.questions.create(
            TaskOwner(
                human_id=caller.human_id,
                session_id=session.id,
                assignment_id=assignment.handle,
                parent_handle=assignment.handle,
            ),
            definition,
            invocation_id="fork-approval:" + invocation,
            handle=handle,
        )

    await history.io.run(post)
    waited = await app.state.task_operations.wait(caller, handle, "all")
    if waited.get("pending"):
        return None

    def collect() -> None:
        # The tool consumed the answer: its notice must not also wake this session.
        with history.serialized():
            for item in history.pending_mail(session.id):
                if item.handle == handle and item.kind in {"question_answer", "task_result"}:
                    history.withdraw_mail(session.id, item.id)

    await history.io.run(collect)
    outcome = (waited.get("outcomes") or [{}])[0]
    reply = outcome.get("result") or {}
    selected = list(reply.get("selected_options") or []) if isinstance(reply, dict) else []
    answer = str(
        (reply.get("answer") if isinstance(reply, dict) else reply) or (selected or [""])[0]
    )
    return answer.strip().lower() in {"yes", "true", "approve", "approved", "y"}


def _brief(note: str) -> str:
    return f"{note.strip().rstrip('.')}. " if note.strip() else ""


def _refused(reason: str, message: str) -> dict[str, Any]:
    return {"forked": False, "refused": True, "reason": reason, "error": message}


__all__ = [
    "APPROVAL_PENDING",
    "FORK_LIMIT_REACHED",
    "MAX_ADDED_REACHED",
    "NOT_APPROVED",
    "fork_caller",
]
