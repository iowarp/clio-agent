"""Bounded cleanup of exact child dispatches after an unexpected gather failure."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from clio_agent.gact.conversation_dispatch_fence import (
    close_spawn_invocations,
    release_spawn_invocations,
)

if TYPE_CHECKING:
    from clio_agent.flows.context import FlowCall, FlowContext


async def quiesce_gather(
    ctx: FlowContext, calls: list[FlowCall], tasks: Sequence[asyncio.Future[Any]]
) -> None:
    """Cancel and confirm only accepted direct children of a failed all-gather."""
    history = ctx.app.state.task_history
    invocation_ids = tuple(call.invocation for call in calls if call.kind != "tool")
    fence_confirmed = False
    try:
        fence_future = history.io.submit(
            close_spawn_invocations,
            ctx.app.state.conversations,
            ctx.root_handle,
            invocation_ids,
        )
        confirmed_children = await asyncio.wait_for(
            asyncio.shield(asyncio.wrap_future(fence_future)),
            timeout=5,
        )
        fence_confirmed = confirmed_children is not None
    except Exception:
        confirmed_children = {}
    for task in tasks:
        if not task.done():
            task.cancel()
    _, pending_calls = await asyncio.wait(tasks, timeout=5)

    caller = ctx.caller
    for call in calls:
        if call.kind == "tool":
            continue
        try:
            row = ctx.journal.call(call.invocation) or {}
            handles = confirmed_children.get(call.invocation, ())
            if not handles and row.get("handle"):
                handles = (str(row["handle"]),)
            if not handles:
                row = ctx.journal.call(call.invocation)
                if row is not None and row.get("status") in {
                    "dispatching",
                    "cleanup_uncertain",
                }:
                    await ctx.journal.record_call(
                        call.invocation,
                        {
                            **row,
                            "status": "not_accepted" if fence_confirmed else "cleanup_uncertain",
                        },
                    )
                continue
            outcomes = []
            for handle in handles:
                await asyncio.wait_for(
                    ctx.app.state.conversations.cancel(caller, handle), timeout=5
                )
                outcomes.append(
                    await asyncio.wait_for(
                        ctx.app.state.task_operations.wait(
                            caller, handle, "all", settle_awaiting_input=True
                        ),
                        timeout=10,
                    )
                )
            terminal = (outcomes[0].get("outcomes") or [{}])[0]
            uncertain = len(handles) != 1 or any(item.get("pending") for item in outcomes)
            row = ctx.journal.call(call.invocation) or {}
            await ctx.journal.record_call(
                call.invocation,
                {
                    **row,
                    "kind": call.kind,
                    "target": call.name,
                    **({"handle": handles[0]} if len(handles) == 1 else {}),
                    "status": "cleanup_uncertain"
                    if uncertain
                    else str(terminal.get("status") or "cleanup_uncertain"),
                    "cleanup_outcome": outcomes,
                    **({"together": call.together} if call.together else {}),
                },
            )
        except Exception:
            # The owning flow still fails with its original error. A missing
            # control/settlement receipt is represented as unresolved history.
            try:
                await ctx._settle(call.invocation, "cleanup_uncertain")
            except Exception:
                pass
    if not pending_calls:
        try:
            history.io.submit(
                release_spawn_invocations,
                ctx.app.state.conversations,
                ctx.root_handle,
                invocation_ids,
            )
        except Exception:
            # The queued release is not required for safety: terminal parents
            # reject future work, and a delayed release stays ordered after closure.
            pass
