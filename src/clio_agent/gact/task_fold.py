"""Transport TaskEvent/TaskResult folding into the live AgentTask owner (#1123)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.gact.agent_task_artifacts import ArtifactRefValue
from clio_agent.gact.agent_tasks import (
    AGENT_TASK_EVENTS,
    STATUS_COMPLETED,
    STATUS_FAILED,
    AgentTask,
    AgentTaskError,
    persist_agent_task,
    publish_agent_task_event,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.agents.invoker import TaskEvent, TaskResult


@dataclass(frozen=True)
class AgentTaskFoldOutcome:
    """Result of folding one transport lifecycle observation.

    ``applied`` is true only for the contender that won the registry transition.
    A terminal duplicate or callback race loser is returned as the typed no-op
    ``reason == "already_terminal"`` with the first winner's immutable task.
    """

    task: AgentTask
    applied: bool
    reason: str = ""


def fold_agent_task_transition(
    app: "FastAPI",
    task_id: str,
    status: str,
    *,
    error_reason: str = "",
    result: Optional[dict[str, Any]] = None,
    artifact_ref: Optional[ArtifactRefValue] = None,
    notify_pending: Optional[bool] = None,
    updated_at: str = "",
) -> AgentTaskFoldOutcome:
    """Apply and publish one lifecycle edge with atomic first-terminal-wins safety.

    The registry transition is the claim point shared by the local done-callback and
    transport fold. Only its winner persists and publishes. A terminal race loser
    reuses the registry's typed ``already_terminal`` result as a no-op and returns
    the immutable winning record.
    """

    reg = app.state.agent_task_registry
    # ``transition()`` updates the projection before the authoritative session
    # write and ledger publication. Keep that complete lifecycle edge ordered so
    # an older nonterminal fold cannot persist or publish after a terminal fold.
    with reg.lifecycle_lock:
        try:
            updated = reg.transition(
                task_id,
                status,
                error_reason=error_reason,
                result=result,
                artifact_ref=artifact_ref,
                notify_pending=notify_pending,
                updated_at=updated_at,
            )
        except AgentTaskError as exc:
            if exc.reason != "already_terminal":
                raise
            current = reg.get(task_id)
            if current is None:  # pragma: no cover - transition found it a moment ago
                raise AgentTaskError(f"unknown task {task_id!r}", reason="unknown_task") from exc
            return AgentTaskFoldOutcome(task=current, applied=False, reason=exc.reason)

        persist_agent_task(app, updated)
        publish_agent_task_event(app, updated, AGENT_TASK_EVENTS[updated.status])
        return AgentTaskFoldOutcome(task=updated, applied=True)


def finish_agent_task_transition(app: "FastAPI", outcome: AgentTaskFoldOutcome) -> None:
    """Run winner-only terminal effects in historical callback order.

    Publishing already happened at the claim seam. The winner now fires the stop
    hook, enqueues the live-producer wake, and admits queued work. Non-terminal
    observations and typed race losers are no-ops.
    """

    if not outcome.applied or not outcome.task.is_terminal:
        return
    from clio_agent.gact.background_exit import reconcile_stored_handoff_part  # noqa: PLC0415
    from clio_agent.gact.delegation_return import stamp_delegation_return  # noqa: PLC0415
    from clio_agent.gact.loop_inbox import enqueue_completion_wake  # noqa: PLC0415
    from clio_agent.gact.turn_spawn import (  # noqa: PLC0415
        _admit_next_queued,
        finalize_child_task_terminal,
    )

    # Clean-wire (owner, 2026-08-05): the child's final assistant message carries
    # its return-to-parent edge on the persisted record BEFORE any downstream
    # effect observes the terminal. Idempotent per task and never-raising, so a
    # re-fold or the collect-seam stamp can never duplicate it.
    stamp_delegation_return(app, outcome.task)
    # Round-9 wire defect: a parent that never waits (idle, or the runaway
    # circuit breaker) must not leave its child's delegate.started handoff part
    # stuck "running" on the STORED message forever -- close it here,
    # independent of whether/when the parent gets another turn. Idempotent and
    # never-raising, the same discipline as stamp_delegation_return above.
    reconcile_stored_handoff_part(app, outcome.task)
    # #1305: fires SubagentStop + releases the child's provider connection(s)
    # deterministically (see turn_spawn.finalize_child_task_terminal). This is
    # ONE of FOUR terminal paths that call it -- NOT the sole choke point (a
    # prior version of this comment claimed that; #1305 review round F3
    # proved 3 others -- turn_spawn._cancel_one_child_task and
    # child_forward.fail_child_task / _complete_forwarded_task -- also
    # terminalize a child task and now route through the SAME shared helper).
    # This one is race-guarded exactly-once by ``outcome.applied`` +
    # ``outcome.task.is_terminal`` above; _cancel_one_child_task carries an
    # equivalent guard (a swallowed transition exception returns early,
    # never reaching the helper). fail_child_task / _complete_forwarded_task
    # do NOT -- round 3 finding, stated honestly rather than silently
    # papered over: both swallow a losing reg.transition() and fall back to
    # the current row regardless, so either can double-fire SubagentStop (and
    # a harmless redundant release call) on an already-terminal race. Real
    # exactly-once hardening for those two is a tracked follow-up, not built
    # here.
    finalize_child_task_terminal(app, outcome.task, outcome.task.child_session_id)
    enqueue_completion_wake(app, outcome.task)
    _admit_next_queued(app)


def fold_agent_task_event(
    app: "FastAPI",
    observation: "TaskEvent | TaskResult",
    *,
    notify_pending: Optional[bool] = None,
) -> AgentTaskFoldOutcome:
    """Fold a transport ``TaskEvent`` or ``TaskResult`` into the live task owner.

    ``TaskEvent.payload`` is the callback publisher's full ``AgentTask`` payload;
    ``TaskResult`` carries the executor boundary subset. Both resolve through the
    same registry transition/persist/publish/wake choreography used by the local
    done-callback. Completed/failed results without an explicit notification bit are
    conservatively observe-later pending so a detached result cannot be lost.
    """

    payload_obj = getattr(observation, "payload", None)
    if isinstance(payload_obj, Mapping):
        payload: dict[str, Any] = dict(payload_obj)
    else:
        try:
            payload = asdict(observation)
        except TypeError as exc:
            raise AgentTaskError(
                "fold input must be a TaskEvent or TaskResult", reason="wrong_input"
            ) from exc

    task_id = str(getattr(observation, "task_id", "") or payload.get("task_id", ""))
    status = str(getattr(observation, "status", "") or payload.get("status", ""))
    if not task_id:
        raise AgentTaskError("fold input is missing task_id", reason="missing_task_id")
    payload_task_id = str(payload.get("task_id", ""))
    if payload_task_id and payload_task_id != task_id:
        raise AgentTaskError("fold task_id disagrees with payload", reason="task_id_mismatch")
    payload_status = str(payload.get("status", ""))
    if payload_status and payload_status != status:
        raise AgentTaskError("fold status disagrees with payload", reason="status_mismatch")

    event_type = str(getattr(observation, "event_type", ""))
    if event_type:
        expected_event_type = AGENT_TASK_EVENTS.get(status)
        if expected_event_type != event_type:
            raise AgentTaskError(
                "fold event_type disagrees with status", reason="event_type_mismatch"
            )

    result_obj = payload.get("result")
    if result_obj is not None and not isinstance(result_obj, Mapping):
        raise AgentTaskError("fold result must be a mapping or null", reason="wrong_input")
    result = dict(result_obj) if isinstance(result_obj, Mapping) else None
    error_reason = str(payload.get("error_reason", ""))
    updated_at = str(payload.get("updated_at", ""))
    artifact_obj = payload.get("artifact_ref")
    artifact_ref: ArtifactRefValue | None
    if isinstance(artifact_obj, Mapping):
        artifact_ref = dict(artifact_obj)
    elif artifact_obj is not None:
        artifact_ref = str(artifact_obj)
    else:
        artifact_ref = None

    folded_notify = notify_pending
    if folded_notify is None and isinstance(payload.get("notify_pending"), bool):
        folded_notify = payload["notify_pending"]
    if folded_notify is None and status in (STATUS_COMPLETED, STATUS_FAILED):
        folded_notify = True

    outcome = fold_agent_task_transition(
        app,
        task_id,
        status,
        error_reason=error_reason,
        result=result,
        artifact_ref=artifact_ref,
        notify_pending=folded_notify,
        updated_at=updated_at,
    )
    finish_agent_task_transition(app, outcome)
    return outcome
