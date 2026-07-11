"""ARC working-set capture helpers for the delegation settle engine (#767).

These two writers are the settle engine's private ARC capture helpers, relocated
verbatim from :mod:`clio_agent.gact.turn_delegation` (#736 tenancy cleanup: they
are pure ARC-write concerns, not settle-loop control flow, and homing them here
keeps the settle module under its size ratchet). They append a predict/CoT
expert's own reasoning + answer/route into its OWN ARC scope so the working-set
trajectory is not empty (the ReAct leaves already stream their own segments).
Both are best-effort and never break a turn.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from clio_agent.runtime import trace

if TYPE_CHECKING:
    from fastapi import FastAPI


def _arc_write_terminal_expert(
    app: "FastAPI",
    sid: str,
    scope: str,
    pred: Any,
    turn_id: str,
) -> None:
    """WS2: append a TERMINAL CoT/predict expert's reasoning (``thought``) + final
    answer (``answer`` kind) to its OWN ARC scope.

    ReAct leaves already stream their own thought/tool_call/observation, and a
    *delegating* orchestrator's per-round reasoning + route is written by
    :func:`_arc_write_orchestrator_route` from the settle loop. The remaining gap is
    the expert that produces an answer WITHOUT delegating and WITHOUT a tool loop --
    the synthesis stage, and every orchestrator's own ``finish`` round. Without this,
    those scopes stay empty in ARC even though the answer reached the wire (Principle
    1: ARC must hold the complete trajectory). Best-effort; never breaks a turn."""

    arc = getattr(getattr(app, "state", None), "arc", None)
    if arc is None or not sid or not scope:
        return
    reasoning = str(getattr(pred, "reasoning", "") or "").strip()
    answer = str(getattr(pred, "answer", "") or "").strip()
    if not reasoning and not answer:
        return
    expert_span_id = f"{turn_id}:{scope}" if turn_id else scope
    try:
        step = 0
        if reasoning:
            arc.append_segment(
                sid,
                scope,
                "thought",
                {"text": reasoning},
                step=step,
                token_count=max(1, len(reasoning) // 4),
                turn_id=turn_id,
                expert_span_id=expert_span_id,
            )
            step += 1
        if answer:
            # The deliverable rides the dedicated ``answer`` kind (an expert/turn final
            # message) -- substrate-complete but outside the working-set/prompt render,
            # so it never re-enters a downstream prompt.
            arc.append_segment(
                sid,
                scope,
                "answer",
                {"text": answer},
                step=step,
                token_count=max(1, len(answer) // 4),
                turn_id=turn_id,
                expert_span_id=expert_span_id,
            )
    except Exception:  # noqa: BLE001 - ARC capture is best-effort, never break a turn
        if trace.HF_ON:
            trace.hot("ARC-TERMINAL-WRITE-FAIL", "%s", scope)


def _arc_write_orchestrator_route(
    app: "FastAPI",
    sid: str,
    scope: str,
    pred: Any,
    next_expert: str,
    next_task: str,
    turn_id: str,
) -> None:
    """WS2: append an orchestrator's reasoning (thought) + delegation (tool_call) to
    its OWN ARC scope, so a predict/CoT orchestrator's working-set trajectory is no
    longer empty (the ReAct leaves already cover themselves). Best-effort; never
    breaks a turn."""

    arc = getattr(getattr(app, "state", None), "arc", None)
    if arc is None or not sid or not scope:
        return
    expert_span_id = f"{turn_id}:{scope}" if turn_id else scope
    try:
        import json as _json  # noqa: PLC0415

        step = 0
        reasoning = str(getattr(pred, "reasoning", "") or "").strip()
        if reasoning:
            arc.append_segment(
                sid,
                scope,
                "thought",
                {"text": reasoning},
                step=step,
                token_count=max(1, len(reasoning) // 4),
                turn_id=turn_id,
                expert_span_id=expert_span_id,
            )
            step += 1
        # A delegation IS a call to a child expert (the ReAct path already models
        # children as tools), so it rides the ``tool_call`` kind.
        delegation = {"name": next_expert, "args": {"task": next_task}}
        arc.append_segment(
            sid,
            scope,
            "tool_call",
            delegation,
            step=step,
            token_count=max(1, len(_json.dumps(delegation, default=str)) // 4),
            turn_id=turn_id,
            expert_span_id=expert_span_id,
        )
    except Exception:  # noqa: BLE001 - ARC capture is best-effort, never break a turn
        if trace.HF_ON:
            trace.hot("ARC-ROUTE-WRITE-FAIL", "%s", scope)
