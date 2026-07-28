"""Producers that drive the tool boundary's already-wired seams (P2.3).

This module is the PRODUCER the ``ToolRuntimeHooks.tool_interceptor`` slot has
lacked since #735 wired it: a ``PreToolUse`` hook's ``modify``/``synthesize``
return now reaches the tool boundary and either mutates the input or skips the
real call with a fabricated (synthetic) result.

Single-fire discipline (the "each event fires exactly once" invariant): ``PreToolUse``
is dispatched EXACTLY ONCE, by the permission gate. The gate stashes the resulting
intercept decision on a per-call context var; the interceptor is a pure CONSUMER
that reads (and clears) it. The gate always runs immediately before the interceptor
in the same synchronous tool call on the same thread, so:

* the interceptor always reads the decision its OWN call's gate stashed;
* a call the gate denied never reaches the interceptor, and its stashed value (if
  any) is overwritten by the NEXT call's gate before that call's interceptor reads
  it — so a stale value can never be consumed;
* parallel tool calls run on distinct threads/contexts, so the context var isolates
  them.

The ``PostToolUse`` producer (``run_post_tool``) dispatches after a tool result and
applies the merged outcome to the model-visible observation only (rewrite +
deny-feedback) — it can never un-run the effect.
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from clio_agent.gact.hooks.dispatcher import dispatch_post_tool, dispatch_post_tool_batch
from clio_agent.gact.hooks.wire import HookOutcome
from clio_agent.tools.tool_hooks import InterceptDecision, PostToolHook

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

#: Per-call stash for the PreToolUse intercept decision. Context-local so parallel
#: tool calls (distinct threads/contexts) never see each other's decision.
_PENDING_INTERCEPT: contextvars.ContextVar["InterceptDecision | None"] = contextvars.ContextVar(
    "clio_pending_tool_intercept", default=None
)


def intercept_from_outcome(outcome: HookOutcome) -> "InterceptDecision | None":
    """Derive the tool-boundary intercept decision from a PreToolUse outcome.

    Returns ``None`` for a plain allow/ask (nothing to intercept). A denied outcome
    is handled by the gate itself (this is only ever called on the non-denied path).
    """

    if outcome.is_synthesize:
        return InterceptDecision(kind="synthesize", result=outcome.synthesize_result)
    if outcome.is_modify:
        return InterceptDecision(kind="modify", modified_args=dict(outcome.modify_input or {}))
    return None


def stash_pre_tool_intercept(outcome: "HookOutcome | None") -> None:
    """Record (or clear) the intercept decision for the current tool call.

    Called by the gate right after it dispatches ``PreToolUse`` exactly once.
    ``None`` (or a non-intercept outcome) clears any stale value, so the interceptor
    that runs next never consumes a prior call's decision.
    """

    decision = intercept_from_outcome(outcome) if outcome is not None else None
    _PENDING_INTERCEPT.set(decision)


def take_pre_tool_intercept() -> "InterceptDecision | None":
    """Return and CLEAR the current call's stashed intercept decision (consume-once)."""

    decision = _PENDING_INTERCEPT.get()
    _PENDING_INTERCEPT.set(None)
    return decision


def pre_tool_interceptor(name: str, args: Mapping[str, Any]) -> "InterceptDecision | None":
    """The ``tool_interceptor`` producer installed on ``app.state``.

    A pure consumer of the gate-stashed PreToolUse decision — it neither dispatches
    nor blocks (deny/ask stay the gate's job), keeping ``PreToolUse`` single-fire.
    """

    return take_pre_tool_intercept()


def run_post_tool(
    name: str,
    args: Mapping[str, Any],
    observation: Any,
    is_error: bool,
    synthetic: bool,
    *,
    session_id: str = "",
    turn_id: str = "",
    cwd: str = "",
    context: Mapping[str, Any] | None = None,
) -> Any:
    """Dispatch ``PostToolUse`` and apply its outcome to the model-visible observation.

    Applies, in order: a hook ``updatedToolOutput`` rewrite (replaces what the model
    sees), then a hook ``deny`` reason appended as FEEDBACK (the effect already ran;
    the deny only informs the model, it never un-runs anything). Returns the original
    observation unchanged when no PostToolUse hook alters it.
    """

    outcome = dispatch_post_tool(
        name,
        args,
        observation=observation,
        is_error=is_error,
        synthetic=synthetic,
        session_id=session_id,
        turn_id=turn_id,
        cwd=cwd,
        context=context,
    )
    result = observation
    if outcome.updated_output:
        result = outcome.updated_output
    if outcome.denied and outcome.reason:
        feedback = f"[PostToolUse blocked] {outcome.reason}"
        result = f"{result}\n\n{feedback}" if str(result) else feedback
    return result


def make_post_tool_hook(app: "FastAPI") -> PostToolHook:
    """Build the ``PostToolUse`` producer for ``ToolRuntimeHooks.post_tool``.

    A thin session-resolving adapter over :func:`run_post_tool` (logic stays here,
    not in the ``tool_observer`` god-file).
    """

    def post_tool(
        name: str, args: Mapping[str, Any], observation: Any, is_error: bool, synthetic: bool
    ) -> Any:
        from clio_agent.gact.runtime.globals import _resolve_tool_session  # noqa: PLC0415

        sid, current = _resolve_tool_session(app)
        return run_post_tool(
            name,
            args,
            observation,
            is_error,
            synthetic,
            session_id=sid,
            turn_id=str(getattr(current, "current_turn_id", "") or ""),
            cwd=str(getattr(current, "workspace_root", "") or ""),
        )

    return post_tool


def fire_post_tool_batch(
    tools_called: Sequence[Any], *, session_id: str, turn_id: str, cwd: str
) -> None:
    """Fire ``PostToolBatch`` once for a turn's resolved tool round (observation).

    Never raises into turn finalize: an observation hook must never break the turn.
    """

    try:
        dispatch_post_tool_batch(
            {
                "tool_count": len(tools_called),
                "tools": [
                    str((row or {}).get("name") or (row or {}).get("tool") or "")
                    for row in tools_called
                    if isinstance(row, dict)
                ],
            },
            session_id=session_id,
            turn_id=turn_id,
            cwd=cwd,
        )
    except Exception:  # noqa: BLE001 - an observation hook must never break turn finalize
        logger.warning(
            "PostToolBatch dispatch failed reason=post_tool_batch_dispatch_failed session=%s",
            session_id,
        )
