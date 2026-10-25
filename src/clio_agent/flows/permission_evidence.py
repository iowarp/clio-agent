"""Checkpoint and explicit refresh boundaries for the flow's permission evidence."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from clio_agent.flows.journal import digest
from clio_agent.gact.permission_evidence import permission_receipts as read_receipts

if TYPE_CHECKING:
    from clio_agent.flows.context import FlowContext


async def permission_receipts(ctx: FlowContext, *, refresh: bool) -> dict[str, Any]:
    """Pin historical observations; refresh adds a new immutable content version."""
    await ctx._ensure_open()  # noqa: SLF001 - context-owned primitive
    invocation = ""
    if not refresh:
        invocation = ctx._invocation(  # noqa: SLF001
            "evidence", "permissions", {}, site="permission_receipts"
        )
        row = ctx.journal.step(invocation)
        if row is not None:
            return copy.deepcopy(row["result"])
    result = await ctx.app.state.task_history.io.run(
        read_receipts, ctx.app, ctx.session_id, ctx.root_handle
    )
    if refresh:
        invocation = ctx._invocation(  # noqa: SLF001
            "evidence", "permissions", {"revision": digest(result)}, site="permission_refresh"
        )
        row = ctx.journal.step(invocation)
        if row is not None:
            return copy.deepcopy(row["result"])
    await ctx.journal.record_step(invocation, {"name": "permission_receipts", "result": result})
    return copy.deepcopy(result)
