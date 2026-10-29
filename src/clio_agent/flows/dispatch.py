"""Identity-preserving acceptance and recovery for flow child calls."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from clio_agent.flows.call_metadata import child_session_metadata
from clio_agent.flows.outcomes import CallCancelled, CallRefused, TaskStateUncertain
from clio_agent.flows.values import render_input
from clio_agent.gact.task_history_queries import direct_child_by_invocation

if TYPE_CHECKING:
    from clio_agent.flows.context import FlowCall, FlowContext


async def accept_call(ctx: FlowContext, call: FlowCall) -> str:
    """Return one accepted child handle, recovering only the exact direct invocation."""
    from clio_agent.gact.task_access import AdmissionClosed  # noqa: PLC0415

    row = ctx.journal.call(call.invocation)
    if row is not None and row.get("handle"):
        return str(row["handle"])
    history = ctx.app.state.task_history
    caller = ctx.caller
    matches = await history.io.run(
        direct_child_by_invocation,
        history,
        ctx.root_handle,
        call.invocation,
        session_id=caller.session_id,
        human_id=caller.human_id,
    )
    if len(matches) > 1:
        raise TaskStateUncertain(
            f"the accepted state of {call.kind} {call.name!r} is ambiguous",
            invocation=call.invocation,
        )
    if matches:
        task = matches[0]
        handle = task.handle
        await ctx.journal.record_call(
            call.invocation,
            {
                **(row or {}),
                "kind": call.kind,
                "target": call.name,
                "handle": handle,
                "session_id": task.owner.session_id,
                "status": "accepted",
                **({"together": call.together} if call.together else {}),
            },
        )
        return handle
    if row is not None and row.get("status") in {"dispatching", "cleanup_uncertain"}:
        raise TaskStateUncertain(
            f"the acceptance state of {call.kind} {call.name!r} is unknown",
            invocation=call.invocation,
        )
    await ctx._budget.check(call.name)
    app = ctx.app
    agent_id = "self" if call.kind == "flow" else call.name
    session_metadata = child_session_metadata(call, ctx._depth + 1, ctx.journal.run_id)
    launch_context: dict[str, Any] | None = None
    try:
        await ctx.journal.record_call(
            call.invocation,
            {
                "kind": call.kind,
                "target": call.name,
                "status": "dispatching",
                **({"together": call.together} if call.together else {}),
            },
        )
        if call.evidence:
            from clio_agent.gact.assignment_evidence import (  # noqa: PLC0415
                select_assignment_evidence,
            )

            pins = await app.state.task_history.io.run(
                select_assignment_evidence, app.state.task_history, caller, call.evidence
            )
            launch_context = {"input_task_refs": [pin.model_dump(mode="json") for pin in pins]}
        accepted = await app.state.conversations.spawn(
            caller,
            render_input(call.input),
            agent_id,
            call.invocation,
            launch_context=launch_context,
            session_metadata=session_metadata,
            parent_handle=ctx.root_handle,
            model=call.model,
        )
    except AdmissionClosed as exc:
        await ctx.journal.record_call(
            call.invocation,
            {
                **(ctx.journal.call(call.invocation) or {}),
                "kind": call.kind,
                "target": call.name,
                "status": "not_accepted",
                "reason": str(exc),
            },
        )
        if exc.stopping:
            raise CallCancelled(str(exc), target=call.name, status="cancelled") from exc
        raise CallRefused(str(exc), target=call.name) from exc
    except ValueError as exc:
        code = str(getattr(exc, "code", "") or "")
        await ctx.journal.record_call(
            call.invocation,
            {
                **(ctx.journal.call(call.invocation) or {}),
                "kind": call.kind,
                "target": call.name,
                "status": "not_accepted",
                **({"reason_code": code} if code else {}),
            },
        )
        raise CallRefused(str(exc), target=call.name, reason_code=code) from exc
    except Exception:
        matches = await history.io.run(
            direct_child_by_invocation,
            history,
            ctx.root_handle,
            call.invocation,
            session_id=caller.session_id,
            human_id=caller.human_id,
        )
        if len(matches) == 1:
            task = matches[0]
            handle = task.handle
            await ctx.journal.record_call(
                call.invocation,
                {
                    **(ctx.journal.call(call.invocation) or {}),
                    "kind": call.kind,
                    "target": call.name,
                    "handle": handle,
                    "session_id": task.owner.session_id,
                    "status": "accepted",
                    **({"together": call.together} if call.together else {}),
                },
            )
            return handle
        if len(matches) > 1:
            raise TaskStateUncertain(
                f"the accepted state of {call.kind} {call.name!r} is ambiguous",
                invocation=call.invocation,
            ) from None
        raise
    if not accepted.get("accepted", False):
        await ctx.journal.record_call(
            call.invocation,
            {
                **(ctx.journal.call(call.invocation) or {}),
                "kind": call.kind,
                "target": call.name,
                "status": "not_accepted",
                "reason": str(accepted.get("reason") or "refused"),
            },
        )
        raise CallRefused(
            str(accepted.get("error") or accepted.get("reason") or "call was not accepted"),
            target=call.name,
            reason_code=accepted.get("reason", ""),
        )
    handle = str(accepted["handle"])
    await ctx.journal.record_call(
        call.invocation,
        {
            "kind": call.kind,
            "target": call.name,
            "handle": handle,
            "session_id": accepted.get("session_id", ""),
            "status": "accepted",
            **({"together": call.together} if call.together else {}),
        },
    )
    return handle
