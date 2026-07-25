"""Plan-mode reminder attachment — the per-turn contract that surfaces plan mode (P1.2 #1064).

Owner module for the plan-mode PROMPT surface. ``session.mode`` used to be plumbed to every
agent ``forward()`` and then discarded (``builders.py``), so the model got ZERO signal it was
in plan mode — and any signal placed in the system prompt is lost once the KV-cache prefix is
compacted. This module re-injects the plan-mode contract into the model's *turn input* each
turn (exactly like ``enrichment.inject_pending_agent_task_notifications`` re-injects observe-
later results), so it survives compaction without invalidating the prefix.

Kept LEAN so plan mode never bloats context: a FULL reminder is injected only on the first plan
turn, immediately after a compaction (which drops the earlier one from the model's view), and
once per :data:`_PLAN_REMINDER_FULL_INTERVAL` turns; every turn in between carries a single-line
marker. The tiny suppression counter lives on ``session.metadata`` (the #948 ``AgentTask``
no-fifth-store projection pattern) — NOT ``workflow_state``, NOT a new store.

The block is SERVER grounding prepended to the turn input — never user text, never model output
— so it carries a stable, greppable marker (:data:`PLAN_MODE_REMINDER_MARKER`, the #881 marker
discipline). ``turn.py`` calls :func:`inject_plan_mode_reminder` from its enrichment step.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

#: Marker heading the plan-mode reminder block (stable + greppable; #881 discipline).
PLAN_MODE_REMINDER_MARKER = "## Plan Mode active — read-only except the plan file"

#: Re-inject the FULL reminder at most once per this many plan-mode turns (a sparse one-liner
#: in between); a compaction forces a full re-inject immediately regardless of the window.
_PLAN_REMINDER_FULL_INTERVAL = 10

#: ``session.metadata`` key holding the tiny per-session suppression counter (no fifth store).
_PLAN_REMINDER_STATE_KEY = "plan_mode_reminder"


def _session_compaction_count(app: "FastAPI", sid: str) -> int:
    """Return how many compaction summaries are in this session's live transcript.

    A compaction (``POST /v1/sessions/{sid}/compact`` or the auto path) appends an assistant
    message carrying a single ``compaction`` part (SPEC §4.5). Counting those parts gives a
    monotonic "a compaction happened since the last full plan reminder" signal — the trigger to
    re-inject the full contract (it would otherwise have been compacted out of the model's view).
    """

    count = 0
    for msg in app.state.messages.get(sid, []) or []:
        for part in getattr(msg, "parts", []) or []:
            if getattr(part, "type", "") == "compaction":
                count += 1
    return count


def _plan_mode_reminder_block(*, full: bool) -> str:
    """Compose the plan-mode reminder block (full contract or sparse one-liner).

    The FULL block carries the turn-ending contract, the read-only restriction, and the sole
    writable path (the plan file). The SPARSE block is a single line naming the same restriction
    + plan path, so most turns cost almost nothing (do not bloat context).
    """

    from clio_agent.gact.runtime.grant_resolver import plans_dir  # noqa: PLC0415

    plan_glob = f"{plans_dir()}{os.sep}*.md"
    if not full:
        return (
            PLAN_MODE_REMINDER_MARKER
            + f" ({plan_glob}). Keep writing your plan there; end your turn to hand it back "
            "for approval rather than executing it yourself."
        )
    return (
        PLAN_MODE_REMINDER_MARKER
        + "\n\n"
        "You are in PLAN MODE. Investigate freely, but do NOT modify the system: every write, "
        "edit, and file-mutating tool is blocked.\n"
        f"- The SOLE writable path is the plan file at {plan_glob}. Create it and record your "
        "plan there, editing it incrementally as you learn.\n"
        "- Turn-ending contract: when the plan is complete, END YOUR TURN and hand it back for "
        "approval — do NOT try to execute the plan while in plan mode."
    )


def inject_plan_mode_reminder(app: "FastAPI", sid: str, session: Any, enriched_text: str) -> str:
    """Prepend the plan-mode reminder to this turn's input when the session is in plan mode.

    Returns ``enriched_text`` unchanged for every non-plan mode (edit/architect) — the
    attachment is scoped to ``plan`` for now (P1.2). In plan mode it prepends a reminder block
    and advances a tiny suppression counter on ``session.metadata`` so the FULL contract is
    injected on the first plan turn, immediately after any compaction, and once per
    :data:`_PLAN_REMINDER_FULL_INTERVAL` turns; a one-line marker is injected on every turn in
    between. Because it rides the per-turn input (not the system prompt), it survives compaction
    without invalidating the KV-cache prefix — the fix for "plan mode lost after compaction".
    """

    mode = str(getattr(session, "mode", "") or "")
    if mode != "plan":
        return enriched_text

    metadata = getattr(session, "metadata", None)
    state = metadata.get(_PLAN_REMINDER_STATE_KEY) if isinstance(metadata, Mapping) else None
    first_time = not isinstance(state, Mapping)

    compactions = _session_compaction_count(app, sid)
    prev_turn = int(state.get("turn_index", 0)) if isinstance(state, Mapping) else 0
    last_full = int(state.get("last_full_turn", 0)) if isinstance(state, Mapping) else 0
    last_full_compactions = (
        int(state.get("compactions_at_last_full", 0)) if isinstance(state, Mapping) else 0
    )
    turn_index = prev_turn + 1

    compacted_since_full = compactions > last_full_compactions
    window_elapsed = (turn_index - last_full) >= _PLAN_REMINDER_FULL_INTERVAL
    full = first_time or compacted_since_full or window_elapsed

    app.state.sessions.update(
        sid,
        metadata_patch={
            _PLAN_REMINDER_STATE_KEY: {
                "turn_index": turn_index,
                "last_full_turn": turn_index if full else last_full,
                "compactions_at_last_full": compactions if full else last_full_compactions,
            }
        },
    )
    return _plan_mode_reminder_block(full=full) + "\n\n---\n\n" + enriched_text
