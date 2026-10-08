"""Staging and committing bounded task notifications on the existing turn boundary."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from clio_agent.gact.agent_task_artifacts import emit_commission_parent_use

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from fastapi import FastAPI


def pending_task_notifications(app: "FastAPI", sid: str) -> tuple[str, list[str]]:
    """A bounded block of completed-but-unconsumed background task results as this
    turn's CLIO addition, and the selected task ids to STAGE (do not consume)
    (#948 S6 observe-later; adversarial-review [1]/[4]).

    An async child spawned in a PRIOR turn that was never collected (via
    wait/check) in that turn sets ``notify_pending`` at completion. Here — during
    the parent's next turn's ENRICHMENT — those results are composed into a
    server-grounding block (marked with :data:`PENDING_TASK_NOTIFICATION_MARKER`) so
    the model SEES them and decides what to do; clio never auto-acts on the content.

    Consumption is DEFERRED to the commit-to-run seam
    (:func:`consume_pending_agent_task_notifications`): this function only composes
    the block and RETURNS ``(block, selected_task_ids)`` so the caller stages the ids
    on the turn state WITHOUT consuming. If the turn then aborts after enrichment (a
    pre_message hook veto, a cancellation before forward), the tasks stay
    ``notify_pending`` and the NEXT turn injects them again — the observe-later
    guarantee is never at-most-once-dropped. Bounded to :data:`_MAX_NOTIFY_BLOCKS`;
    a typed note reports any remaining (they surface next turn — never dropped)."""

    from clio_agent.gact.agent_tasks import pending_notifications  # noqa: PLC0415
    from clio_agent.gact.enrichment import (
        _MAX_NOTIFY_BLOCKS,
        PENDING_TASK_NOTIFICATION_MARKER,
        _notify_block,
    )

    pending = pending_notifications(app, sid)
    selected = pending[:_MAX_NOTIFY_BLOCKS]
    from clio_agent.gact.task_delivery import pending_completions

    shared_block, shared_handles = pending_completions(
        app, sid, limit=_MAX_NOTIFY_BLOCKS - len(selected)
    )
    if not pending:
        return shared_block, shared_handles
    blocks = [_notify_block(task, app=app) for task in selected]
    remaining = len(pending) - len(selected)
    truncation = (
        f"\n\n_({remaining} more finished task(s) pending — they will surface next turn.)_"
        if remaining > 0
        else ""
    )
    block = PENDING_TASK_NOTIFICATION_MARKER + "\n\n" + "\n\n".join(blocks) + truncation
    return block + ("\n\n" + shared_block if shared_block else ""), [
        task.task_id for task in selected
    ] + shared_handles


def consume_pending_agent_task_notifications(
    app: "FastAPI", sid: str, task_ids: list[str]
) -> list[str]:
    """Consume the observe-later notifications staged by
    :func:`pending_task_notifications` AND emit each one's delegation
    terminal — at the COMMIT-TO-RUN seam, never at compose time (#948 S6
    adversarial-review [1]/[4]).

    Called once the turn is committed to forward with the enriched input (after the
    last abort/veto seam), so a turn vetoed/aborted after enrichment leaves the
    tasks ``notify_pending`` for the next turn. For each staged task:

    * consume it exactly once (durable ``consumed_at`` + ``agent.task.consumed``),
      atomically via the ``notify_pending`` once-guard; and
    * emit its delegation TERMINAL — the SAME choreography ``wait_agent_tasks``
      emits (``blueprint.delegation.completed|failed`` + the
      return ``expert_handoff`` Part + ``blueprint.delegation.parent_resumed``),
      keyed to the parent expert that requested the child.

    The terminal emission goes through the SHARED ``delegation_reported`` once-gate
    (``_emit_delegation_terminal``), so if a later ``wait_agent_tasks`` also reaches
    this task it does not double-emit — and vice versa (exactly-once on the wire in
    either order). Without this, an async child collected only via observe-later
    left a ``blueprint.delegation.started`` with no terminal on the wire (a dangling
    delegation that renders the child perpetually in-progress). The return Part is
    appended to THIS (parent-session) turn's live transcript — the natural home,
    since the parent-session transcript is where the delegation renders."""

    from clio_agent.gact.enrichment import _notify_block

    if not task_ids:
        return []
    from clio_agent.gact.agent_tasks import consume_notification  # noqa: PLC0415
    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        _runtime_active_agent_blueprint_id,
    )
    from clio_agent.gact.agents.spawn_runtime import _emit_delegation_terminal  # noqa: PLC0415
    from clio_agent.gact.types import AgentDef  # noqa: PLC0415

    reg = app.state.agent_task_registry
    blueprint_id = _runtime_active_agent_blueprint_id(app, sid) or ""
    delivered: list[str] = []
    for task_id in task_ids:
        from clio_agent.gact.task_projection import resolve_task

        try:
            row = resolve_task(app, sid, task_id)
        except ValueError as exc:
            logger.warning("Staged task result unavailable handle=%s reason=%s", task_id, exc)
            continue
        if row["task_kind"] != "Subagent":
            from clio_agent.gact.task_delivery import completion_block, consume_task

            if consume_task(app, sid, task_id):
                delivered.append(completion_block(row))
            continue
        task_id = row["id"]
        task = reg.get(task_id)
        # Consume (atomic once-guard); a concurrent wait may already have consumed
        # it, in which case this no-ops. The terminal emission below is separately
        # once-gated, so we ALWAYS attempt it (exactly-once regardless of order).
        claimed = consume_notification(app, task_id)
        if claimed is not None:
            delivered.append(_notify_block(claimed, app=app))
            from clio_agent.gact.background_exit import (  # noqa: PLC0415
                emit_background_exit_part,
            )

            emit_background_exit_part(app, sid, claimed)
        parent_id = task.agent_ref.get("requesting_expert_id", "") or "main"
        parent_def = AgentDef(
            id=parent_id,
            title=parent_id,
            metadata={"agent_blueprint_id": blueprint_id},
        )
        _emit_delegation_terminal(app, sid, parent_def, claimed or task)
        if claimed is not None:
            emit_commission_parent_use(app, sid, claimed)
    return delivered
