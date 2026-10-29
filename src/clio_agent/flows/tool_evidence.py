"""Pinned, authorized reads of tool results owned by a flow's child assignment."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from clio_agent.arc.task_types import CoreReference
from clio_agent.flows.outcomes import CallRefused
from clio_agent.gact.task_access import TaskAccess, TaskAccessDenied

if TYPE_CHECKING:
    from clio_agent.flows.context import FlowContext


def _require_root(ctx: FlowContext, handle: str) -> None:
    history = ctx.app.state.task_history
    if handle not in history.descendant_handles(ctx.root_handle):
        raise CallRefused(
            "Tool evidence must belong to this flow's assignment subtree", handle=handle
        )
    TaskAccess(history, ctx.caller).require(handle, "read")


def _pin(ctx: FlowContext, handle: str) -> list[dict[str, Any]]:
    """Choose confirmed terminal tool observations, without starting or waiting for work."""
    _require_root(ctx, handle)
    history = ctx.app.state.task_history
    rows = []
    for child in sorted(history.descendant_handles(handle)):
        task = history.task_snapshot(child)
        if task is None or task.kind in {"Session", "Subagent", "Question"}:
            continue
        if task.terminal_at is None or task.recording != "confirmed":
            continue
        TaskAccess(history, ctx.caller).require(child, "read")
        rows.append(task.model_dump(mode="json"))
    return rows


def _read(ctx: FlowContext, handle: str, pins: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reauthorize every pinned body before and after its immutable content read."""
    _require_root(ctx, handle)
    history = ctx.app.state.task_history
    rows = []
    for pin in pins:
        reference = CoreReference.model_validate(pin["result"]) if pin.get("result") else None
        before = TaskAccess(history, ctx.caller).require(pin["handle"], "read")
        if before.result != reference:
            raise TaskAccessDenied("Pinned tool result changed")
        value = history.read_content(reference) if reference is not None else None
        after = TaskAccess(history, ctx.caller).require(pin["handle"], "read")
        if after.result != reference:
            raise TaskAccessDenied("Pinned tool result changed during disclosure")
        rows.append({"task": pin, "result_reference": pin.get("result"), "value": value})
    return rows


async def tool_results(ctx: FlowContext, handle: str) -> list[dict[str, Any]]:
    """Pin a passive inventory in the flow checkpoint, then read its exact tool results.

    Bodies remain in their canonical content-addressed store; checkpoints keep
    only task metadata and references. Replay cannot silently add later tools or
    switch result hashes, and never bypasses current read authorization.
    """
    await ctx._ensure_open()  # noqa: SLF001 - context-owned primitive
    invocation = ctx._invocation(  # noqa: SLF001
        "evidence", "tool_results", {"handle": handle}, site="tool_results"
    )
    row = ctx.journal.step(invocation)
    history = ctx.app.state.task_history
    if row is None:
        pins = await history.io.run(_pin, ctx, handle)
        await ctx.journal.record_step(invocation, {"name": "tool_results", "result": pins})
    else:
        pins = row["result"]
    return copy.deepcopy(await history.io.run(_read, ctx, handle, pins))
