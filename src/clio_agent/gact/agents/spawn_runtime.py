"""Spawn-runtime tools for react mains (#948 S4).

The routing surface that REPLACES the deleted settle/synthesis orchestration and
the deleted inline per-child delegate/fan-out tools. A tier-1 main is now a react
agent whose answer IS the user deliverable; instead of a typed routing field
consumed by a settle loop, it CALLS these tools:

* ``spawn_agent_task(agent, task)`` — spawn a declared child as a REAL child turn
  (S3 ``spawn_child_turn``) and return its ``task_id``.
* ``wait_agent_tasks(task_ids, timeout_s)`` — block on the children's completion
  and return their results (spawn + wait COMPOSE the old synchronous delegate;
  the child runs on the dedicated pool so waiting here never starves it).
* ``check_agent_tasks()`` — the parent's spawned tasks + status (consumes a
  finished child: collect-and-close).
* ``observe_agent_tasks(task_ids, cursor=...)`` — the OBSERVE posture (#1000):
  read a child's event stream incrementally without consuming it (owner module
  ``observe_runtime``).
* ``spawn_agents_parallel(spawns)`` — fan out several children at once.

Each tool re-emits the wire-facing ``blueprint.delegation.*`` / ``blueprint.fanout.*``
events AND appends the ``expert_handoff`` Parts the deleted sync-delegate path
appended (wire parity, #948 S4 findings [6]/[7]): events feed the activity label /
trace / active-agent indicator, while the transcript renderer keys the delegation
header/nesting/return row off ``type=='expert_handoff'`` Parts exclusively. The
child sessions + AgentTask records are the real substrate — no inline in-thread
child forward, no settle-loop routing vocabulary.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any

from clio_agent.gact import context as _ctx
from clio_agent.gact.agents.spawn_group import (
    failed_spawn_metadata_row,
    spawn_group_fields,
    wait_structured_row,
    wait_summary,
)
from clio_agent.gact.agents.spawn_placement import run_handle_fields
from clio_agent.gact.runtime.globals import (
    _active_semantic_trace_id,
    _active_semantic_turn_id,
    _emit_semantic_event,
)
from clio_agent.gact.tool_observer import _append_live_assistant_part, _handoff_part_metadata
from clio_agent.gact.types import Part

if TYPE_CHECKING:
    from clio_agent.gact.agents.types import AgentDef

logger = logging.getLogger(__name__)


def _fanout_batch_bound(agent_def: "AgentDef") -> int:
    """The declaring parent's fan-out admission bound (#948 S5).

    Reads ``fanout.max_workers`` when the ``fanout`` block is enabled and declares a
    positive worker count; returns 0 (unbounded — only the global per-depth cap
    applies) when the block is absent, disabled, or malformed. A quoted author-error
    ``enabled: "false"`` is treated as disabled (never silently on)."""

    fanout = getattr(agent_def, "fanout", None)
    if not isinstance(fanout, Mapping) or not fanout:
        return 0
    enabled = fanout.get("enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() not in {"false", "0", "no", "off", "disabled"}
    if not enabled:
        return 0
    try:
        max_workers = int(fanout.get("max_workers") or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, max_workers)


def _blueprint_block(parent: "AgentDef", child_id: str) -> dict[str, str]:
    return {
        "agent_blueprint_id": parent.metadata.get("agent_blueprint_id") or "",
        "parent_expert": parent.id,
        "child_expert": child_id,
    }


def _current_session_depth(app: Any, session_id: str) -> int:
    """The agent-task depth of the CURRENT session (0 for a root / non-child session).

    A child session carries the ``session_type=='agent_task'`` projection; its depth
    lives on the AgentTask record. The next spawn is this depth + 1, so nested spawns
    increment (a root spawns at depth 1) and the runaway backstop
    (``MAX_SPAWN_DEPTH``) is reachable through the real tool path — not only via a
    hand-built TaskSpec (#948 S4 adversarial review)."""

    from clio_agent.gact.agent_tasks import AgentTask  # noqa: PLC0415

    sess = app.state.sessions.get(session_id)
    if sess is None:
        return 0
    task = AgentTask.from_session(sess)
    return task.depth if task is not None else 0


def _resolve_verbatim_output(app: Any, task: Any) -> tuple[str, dict[str, str]]:
    """Resolve the child's FULL final message text — the #880 verbatim contract:
    the delegation ``output`` IS the child's answer, byte-for-byte, ALWAYS. The
    AgentTask record keeps only a BOUNDED excerpt (registry memory stays bounded),
    so the full text is re-read at wait-time via the result's ``message_ref``.

    Returns ``(output, markers)``: ``markers`` empty on success, else a typed
    fallback to the bounded excerpt (``output_source='excerpt_fallback'`` +
    ``output_fallback_reason='child_message_gone'``) when the message is gone.
    """

    from clio_agent.gact.turn_spawn import _message_text  # noqa: PLC0415

    result = task.result or {}
    excerpt = result.get("answer_excerpt", "")
    message_ref = result.get("message_ref", "")
    child_sid = getattr(task, "child_session_id", "")
    if not message_ref or not child_sid:
        # No message to resolve (a failed/empty child carries no ref): the excerpt IS
        # the authoritative (empty) output — no degradation occurred, no marker.
        return excerpt, {}
    messages = app.state.messages.get(child_sid, []) or []
    for msg in messages:
        if getattr(msg, "id", "") == message_ref:
            return _message_text(msg), {}
    return excerpt, {
        "output_source": "excerpt_fallback",
        "output_fallback_reason": "child_message_gone",
    }


def _persist_delegation_reported(app: Any, task: Any) -> None:
    """Persist the once-per-task report flag to the child-session metadata so a
    boot-rebuilt registry does not re-emit the terminal event. Best-effort: a gone
    child session cannot survive a reboot either (no re-emit risk) -- surface the
    typed reason, never crash the wait (no-silent-fallback)."""

    from clio_agent.gact.agent_tasks import AgentTaskError, persist_agent_task  # noqa: PLC0415

    # Catch the FULL raise surface (child-gone + the store's disk-flush OSError
    # family) so a transient store fault never crashes the wait/collect ([3] parity).
    try:
        persist_agent_task(app, task)
    except (AgentTaskError, OSError) as exc:
        logger.warning(
            "delegation_reported not persisted reason=%s task=%s",
            getattr(exc, "reason", type(exc).__name__),
            getattr(task, "task_id", "?"),
        )


def _merge_wait_workflow_states(
    results: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Deterministic request-order merge of a wait batch's per-run workflow_state
    (#948 S5). Only rows carrying BOTH ``run_index`` and a mapping ``workflow_state``
    contribute; ordering/conflict detection delegates to ``workflow_state.merge``."""

    from clio_agent.gact.workflow_state.merge import (  # noqa: PLC0415
        RunWorkflowState,
        merge_run_workflow_states,
    )

    runs = [
        RunWorkflowState(
            run_index=int(row.get("run_index", 0)),
            task_id=str(row.get("task_id", "")),
            workflow_state=row["workflow_state"],
            # #953 [1]: attribute each run to its child expert so a heterogeneous batch's
            # same-index runs are distinguishable in the conflict rows.
            agent_id=str(row.get("agent_id", "")),
        )
        for row in results
        if isinstance(row.get("workflow_state"), dict) and "run_index" in row
    ]
    return merge_run_workflow_states(runs)


def _completion_payload(app: Any, task: Any) -> dict[str, Any]:
    """The delegate.completed payload shape (wire parity with the old tool).

    ``output`` is the child's FULL answer byte-for-byte (#880), re-read from the
    child session at wait-time; a typed marker is added if it must fall back to the
    bounded excerpt (see :func:`_resolve_verbatim_output`)."""

    result = task.result or {}
    output, markers = _resolve_verbatim_output(app, task)
    payload = {
        "agent_id": task.agent_ref.get("expert_id", ""),
        "parent_id": task.agent_ref.get("requesting_expert_id", ""),
        "task_id": task.task_id,
        # Ensemble run identity (#948 S5): which run of a repeated child this is (0,1,2…
        # in spawn order). On the payload so a same-child ensemble's return rows / merge
        # conflict rows are attributable to a specific run.
        "run_index": task.run_index,
        "status": task.status,
        "stage": "delegate.completed" if task.status == "completed" else f"delegate.{task.status}",
        "output": output,
        "workflow_state": result.get("workflow_state", {}),
        "message_ref": result.get("message_ref", ""),
        "error_reason": task.error_reason,
    }
    payload.update(markers)
    return payload


def _started_handoff_part(
    agent_def: "AgentDef", child_id: str, task_text: str, depth: int, spawned: Any
) -> Part:
    """The ``delegate.started`` expert_handoff Part appended to the PARENT transcript
    when a child is spawned (#948 S4 finding [7]). The transcript renderer drives the
    delegation header/depth/nesting off ``type=='expert_handoff'`` Parts — NOT the
    semantic events — so without this Part a spawned child renders nothing."""

    started_row = {
        "agent_id": child_id,
        "parent_id": agent_def.id,
        "status": "running",
        "stage": "delegate.started",
        "question": task_text,
        "depth": depth,
        "run_index": spawned.run_index,
    }
    started_row.update(spawn_group_fields(spawned))
    handle_fields = run_handle_fields(spawned, child_id)
    return Part(
        id=f"live_handoff_{uuid.uuid4().hex[:12]}",
        type="expert_handoff",
        agent_id=agent_def.id,
        parent_agent=agent_def.id,
        child_agent=child_id,
        stage="delegate.started",
        handle_id=handle_fields["handle_id"],
        run_label=handle_fields["run_label"],
        live_state=handle_fields["live_state"],
        host=handle_fields["host"],
        placement=handle_fields["placement"],
        status="running",
        text=f"{agent_def.id} -> {child_id}",
        metadata={**_handoff_part_metadata(started_row), "stream_source": "live"},
    )


def emit_spawn_started(
    app: Any,
    session_id: str,
    agent_def: "AgentDef",
    child_id: str,
    task_text: str,
    depth: int,
    spawned: Any,
) -> None:
    """Publish one canonical started handoff for any real child-task spawn.

    Both declared-blueprint spawns and skill-seeded dynamic spawns use this
    seam so the parent transcript records the child exactly where it was
    launched. The handoff part is the presentation; a generic tool row would
    duplicate the same action.
    """

    _emit_semantic_event(
        app,
        session_id,
        "blueprint.delegation.started",
        turn_id=_active_semantic_turn_id(),
        trace_id=_active_semantic_trace_id(),
        status="running",
        summary=f"{agent_def.id} spawned {child_id}",
        actor={"agent_id": agent_def.id, "role": "parent_expert"},
        subject={"agent_id": child_id, "role": "child_expert"},
        blueprint=_blueprint_block(agent_def, child_id),
        payload={"run_index": spawned.run_index},
    )
    _append_live_assistant_part(
        app,
        session_id,
        _started_handoff_part(agent_def, child_id, task_text, depth, spawned),
    )


def _failed_spawn_handoff_part(
    agent_def: "AgentDef", child_id: str, spawn_group_id: str, group_size: int, exc: Exception
) -> Part:
    """Terminal Part for a batch sibling refused before it ever spawned (finding
    [E]): builds directly on the terminal lane so the group's declared total
    always reconciles even when one sibling never got a child session."""

    reason = getattr(exc, "reason", type(exc).__name__)
    row = failed_spawn_metadata_row(child_id, agent_def.id, reason, spawn_group_id, group_size)
    return Part(
        id=f"live_handoff_{uuid.uuid4().hex[:12]}",
        type="expert_handoff",
        agent_id=agent_def.id,
        parent_agent=agent_def.id,
        child_agent=child_id,
        stage="delegate.completed",
        status="failed",
        text=f"{agent_def.id} -> {child_id}",
        metadata={**_handoff_part_metadata(row), "stream_source": "live"},
    )


def _return_handoff_part(agent_def: "AgentDef", task: Any, payload: dict[str, Any]) -> Part:
    """The terminal RETURN expert_handoff Part appended to the PARENT transcript when a
    spawned child reaches a terminal state (#948 S4 finding [7]). Success AND failure
    conclude on the SAME terminal lane — ``stage='delegate.completed'`` with the
    outcome riding ``status`` (#882) — so a failed child is visible, not buried in
    raw tool JSON. ``metadata.output`` is the child's FULL answer byte-for-byte
    (#880, resolved in ``payload``)."""

    child_id = task.agent_ref.get("expert_id", "")
    return_row = {
        "agent_id": child_id,
        "parent_id": agent_def.id,
        "status": task.status,
        "stage": "delegate.completed",
        "output": payload.get("output", ""),
        "workflow_state": payload.get("workflow_state", {}),
        "error": task.error_reason or "",
        "run_index": task.run_index,
    }
    return_row.update(spawn_group_fields(task))
    # Surface the verbatim-output degradation markers (never silent) onto the Part too.
    for marker in ("output_source", "output_fallback_reason"):
        if marker in payload:
            return_row[marker] = payload[marker]
    handle_fields = run_handle_fields(task, child_id)
    return Part(
        id=f"live_handoff_{uuid.uuid4().hex[:12]}",
        type="expert_handoff",
        agent_id=agent_def.id,
        parent_agent=agent_def.id,
        child_agent=child_id,
        stage="delegate.completed",
        handle_id=handle_fields["handle_id"],
        run_label=handle_fields["run_label"],
        live_state=handle_fields["live_state"],
        host=handle_fields["host"],
        placement=handle_fields["placement"],
        status=task.status,
        duration_ms=_task_duration_ms(task),
        text=f"{agent_def.id} <- {child_id}",
        metadata={**_handoff_part_metadata(return_row), "stream_source": "live"},
    )


def _task_duration_ms(task: Any) -> float:
    """The child's wall-clock duration from its task record timestamps
    (``created_at`` at spawn, ``updated_at`` at terminal transition). Unparseable
    timestamps leave the Part unstamped (0.0) — never fail on decoration."""

    try:
        created = datetime.fromisoformat(str(task.created_at))
        updated = datetime.fromisoformat(str(task.updated_at))
    except (TypeError, ValueError):
        return 0.0
    delta_ms = (updated - created).total_seconds() * 1000
    return delta_ms if delta_ms > 0 else 0.0


def _emit_delegation_terminal(app: Any, session_id: str, agent_def: "AgentDef", task: Any) -> None:
    """Emit a terminal task's once-per-task delegation event + return Part + resume.

    Shared by :func:`wait_agent_tasks` and :func:`emit_workflow_step_return` (both
    spawn+wait through the same invoker); ``mark_delegation_reported`` guarantees
    exactly-once wire emission no matter which path gets there first (the server
    owns the de-duplicated stream)."""

    registry = app.state.agent_task_registry
    reported = registry.mark_delegation_reported(task.task_id)
    if reported is None:
        return
    _persist_delegation_reported(app, reported)
    # Clean-wire (owner, 2026-08-05): a task collected here may have reached
    # terminal off THIS process's result-sealing fold seam (boot-refolded, seeded,
    # or forwarded) -- stamp the return-to-parent edge here too, idempotent per task.
    from clio_agent.gact.delegation_return import stamp_delegation_return  # noqa: PLC0415

    stamp_delegation_return(app, task)
    payload = _completion_payload(app, task)
    child_id = task.agent_ref.get("expert_id", "")
    event_type = (
        "blueprint.delegation.completed"
        if task.status == "completed"
        else "blueprint.delegation.failed"
    )
    _emit_semantic_event(
        app,
        session_id,
        event_type,
        turn_id=_active_semantic_turn_id(),
        trace_id=_active_semantic_trace_id(),
        status=task.status,
        summary=f"{child_id} returned to {agent_def.id}",
        actor={"agent_id": child_id, "role": "child_expert"},
        subject={"agent_id": agent_def.id, "role": "parent_expert"},
        blueprint=_blueprint_block(agent_def, child_id),
        payload=dict(payload),
    )
    _append_live_assistant_part(app, session_id, _return_handoff_part(agent_def, task, payload))
    _emit_semantic_event(
        app,
        session_id,
        "blueprint.delegation.parent_resumed",
        turn_id=_active_semantic_turn_id(),
        trace_id=_active_semantic_trace_id(),
        status="completed",
        summary=f"{agent_def.id} resumed after {child_id}",
        actor={"agent_id": agent_def.id, "role": "parent_expert"},
        subject={"agent_id": child_id, "role": "child_expert"},
        blueprint=_blueprint_block(agent_def, child_id),
        payload={
            "agent_id": agent_def.id,
            "resumed_from": child_id,
            "run_index": task.run_index,
            "status": "completed",
            "stage": "parent.resumed",
            "output": payload.get("output", ""),
            "workflow_state": payload.get("workflow_state", {}),
        },
    )


def emit_workflow_step_start(
    app: Any, session_id: str, agent_def: "AgentDef", child_id: str, task_text: str, spawned: Any
) -> None:
    """Wire parity for a declared-workflow step spawn (#948 S5 work item 4).

    A workflow step spawns its child through the invoker (never the
    model's ``spawn_agent_task`` tool), so the runner re-emits the same
    ``blueprint.delegation.started`` event + started ``expert_handoff`` Part the tool
    path emits — otherwise the step's child would render nothing in the parent
    transcript."""

    _emit_semantic_event(
        app,
        session_id,
        "blueprint.delegation.started",
        turn_id=_active_semantic_turn_id(),
        trace_id=_active_semantic_trace_id(),
        status="running",
        summary=f"{agent_def.id} spawned {child_id} (workflow)",
        actor={"agent_id": agent_def.id, "role": "parent_expert"},
        subject={"agent_id": child_id, "role": "child_expert"},
        blueprint=_blueprint_block(agent_def, child_id),
        payload={"run_index": spawned.run_index, "workflow_step": True},
    )
    _append_live_assistant_part(
        app,
        session_id,
        _started_handoff_part(agent_def, child_id, task_text, spawned.depth, spawned),
    )


def emit_workflow_step_return(app: Any, session_id: str, agent_def: "AgentDef", task: Any) -> None:
    """Terminal wire parity for a declared-workflow step (delegates to the shared
    once-per-task terminal emission)."""

    if task.is_terminal:
        _emit_delegation_terminal(app, session_id, agent_def, task)


def build_spawn_runtime_tools(
    base_agent: Any,
    agent_def: "AgentDef",
    *,
    enable_skill_task_collection: bool = False,
) -> list[Any]:
    """Build the react-main spawn tools bound to ``agent_def`` as the requesting
    (parent) expert. Resolved lazily against the active app/session at call time.

    A skill effect may create a temporary child without declaring a static child
    blueprint. In that case ``enable_skill_task_collection`` exposes only the
    wait/check/message/observe tools needed to collect that real child task. It
    does not expose the declared-child spawn or workflow controls.
    """

    from clio_agent.gact.agent_messaging import build_message_agent_tool  # noqa: PLC0415
    from clio_agent.gact.agents.invoker import (  # noqa: PLC0415
        InvokerError,
        SpawnError,
        TaskHandle,
        TaskSpec,
    )
    from clio_agent.gact.agents.observe_runtime import build_observe_tool  # noqa: PLC0415
    from clio_agent.gact.agents.resolution import _runtime_declared_child_ids  # noqa: PLC0415
    from clio_agent.gact.agents.spawn_placement import (  # noqa: PLC0415
        invoker_for_placement,
        invoker_for_task,
        resolve_batch_placement,
    )
    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415
    from clio_agent.gact.spawn_context import bind_task_spec_to_parent  # noqa: PLC0415

    # Declared children own the complete routing surface. A spawn-effect skill can
    # independently mint a real child task through ``spawn_skill_task``, so it
    # needs the collector half of the surface even when the blueprint is otherwise
    # a leaf. A true leaf with neither capability still gets no routing tools.
    _app = _ctx.active_app()
    _sid = _ctx.active_session_id()
    declared_child_ids = (
        _runtime_declared_child_ids(_app, agent_def.id, session_id=_sid)
        if _app is not None
        else set()
    )
    has_declared_children = bool(declared_child_ids)
    if _app is None or (not has_declared_children and not enable_skill_task_collection):
        return []

    def _ctx_app_session() -> tuple[Any, str]:
        app = _ctx.active_app()
        session_id = _ctx.active_session_id()
        if app is None or not session_id:
            raise RuntimeError("spawn-runtime tool requires an active CLIO app/session context")
        return app, session_id

    def _do_spawn(
        agent: str,
        task: str,
        *,
        fanout_bound: int = 0,
        placement: str | None = None,
        spawn_group_id: str = "",
        group_size: int = 0,
    ) -> str:
        """Spawn one declared child through the invoker + emit the started wire
        parity. ``fanout_bound`` (> 0) caps how many of THIS parent's concurrent
        children at this depth may run before a spawn queues — the fan-out admission
        bound (#948 S5); 0 means only the global per-depth cap applies. ``spawn_group_id``
        / ``group_size`` (P5 wire semantics) are set ONLY by ``spawn_agents_parallel``
        (one id shared by the whole batch) — a bare ``spawn_agent_task`` call leaves
        them at their empty/0 default, so the minted record carries neither field."""

        app, session_id = _ctx_app_session()
        # Computed depth: a child spawns at (its own depth) + 1, so nesting
        # increments through the real tool path and the runaway backstop is
        # reachable (a root session spawns at depth 1) (#948 S4 adversarial review).
        depth = _current_session_depth(app, session_id) + 1
        try:
            binding = invoker_for_placement(app, session_id, placement)
            spawned = binding.invoker.invoke(
                bind_task_spec_to_parent(
                    app,
                    TaskSpec(
                        child_expert_id=agent,
                        task_text=task,
                        parent_session_id=session_id,
                        requesting_expert_id=agent_def.id,
                        # #953 [2]/[8]: stamp the ACTIVE turn id so run_index resets per
                        # parent turn (else it accumulates across the whole session).
                        parent_turn_id=_active_semantic_turn_id(),
                        depth=depth,
                        # #948 S6: ONE honest semantic — every model-driven spawn is
                        # fire-and-forget (notify-later). The child is untied to this
                        # turn's lifetime; on completion it sets notify_pending, which
                        # the model collects in-turn (wait/check) or observes next turn
                        # (injection). Both mark it consumed exactly once. (The declared-
                        # workflow runner keeps mode="sync": it always collects its steps
                        # within run_workflow, never observe-later.)
                        mode="async",
                        fanout_bound=fanout_bound,
                        placement=binding.placement,
                        spawn_group_id=spawn_group_id,
                        group_size=group_size,
                    ),
                ),
            )
        except SpawnError as exc:
            if spawn_group_id:
                # A batch sibling's slot must reconcile even on refusal (finding [E]).
                _append_live_assistant_part(
                    app,
                    session_id,
                    _failed_spawn_handoff_part(agent_def, agent, spawn_group_id, group_size, exc),
                )
            return json.dumps({"error": exc.reason, "message": str(exc)}, sort_keys=True)
        emit_spawn_started(app, session_id, agent_def, agent, task, depth, spawned)
        return json.dumps(
            {
                "task_id": spawned.task_id,
                "status": spawned.status,
                "run_index": spawned.run_index,
                # Typed queued_reason at the concurrency cap (#948 S6): the handle
                # returns IMMEDIATELY as queued|running, never blocking on admission.
                "queued_reason": spawned.queued_reason,
                **run_handle_fields(spawned, agent),
            },
            sort_keys=True,
        )

    def spawn_agent_task(agent: str, task: str, placement: str | None = None) -> str:
        """Spawn a declared child expert as a background child turn; returns its
        task_id IMMEDIATELY (status queued|running). Fire-and-forget: the child runs
        untied to this turn — collect it now with wait_agent_tasks, poll it with
        check_agent_tasks, or let its result surface in your NEXT turn. Prefer to spawn
        ALL independent children before waiting on any."""

        return _do_spawn(agent, task, placement=placement)

    def wait_agent_tasks(task_ids: list[str], timeout_s: float) -> str:
        """Block until the given spawned tasks finish (up to timeout_s), then return
        each one's result. ``timeout_s`` is REQUIRED — pass your own budget; on
        timeout you get the current statuses and YOU decide (keep waiting, keep
        working, or finish). The children run on a dedicated pool, so waiting here
        never starves them. Prefer a short budget (e.g. 30-60s) and continue on a
        partial — don't block the whole turn on one long child."""

        app, session_id = _ctx_app_session()
        registry = app.state.agent_task_registry
        import time as _time  # noqa: PLC0415

        from clio_agent.gact.agent_tasks import (  # noqa: PLC0415
            consume_notification,
            display_run_name,
        )

        call_start = _time.monotonic()
        deadline = call_start + max(0.0, float(timeout_s or 0.0))
        results = []
        # Typed structured shape (owner ruling, P5): a tool DECLARES its wire
        # presentation instead of the UI inferring it from JSON key order —
        # built alongside ``results`` from the SAME per-task facts, and declared
        # onto the wire's structured_content channel below (never returned to
        # the model — that lane stays the compact ``results``/conflict rows).
        structured_rows: list[dict[str, Any]] = []
        for tid in task_ids or []:
            # Validate the id BEFORE waiting: registry.event() would setdefault a
            # fresh never-set Event for an unknown/typo id and block the FULL budget
            # (starving every real id after it via the shared deadline). An unknown
            # id returns immediately with a typed row and emits nothing.
            task = registry.get(tid)
            if task is None:
                results.append({"task_id": tid, "error": "unknown_task"})
                structured_rows.append(wait_structured_row(tid, "unknown_task", 0.0, ""))
                continue
            remaining = max(0.0, deadline - _time.monotonic())
            try:
                binding = invoker_for_task(app, task)
                task_result = binding.invoker.wait(TaskHandle.from_task(task), timeout_s=remaining)
            except (InvokerError, SpawnError) as exc:
                results.append({"task_id": tid, "error": exc.reason})
                structured_rows.append(wait_structured_row(tid, exc.reason, 0.0, ""))
                continue
            payload = _completion_payload(app, task_result)
            results.append(payload)
            structured_rows.append(
                wait_structured_row(
                    display_run_name(
                        task_result.agent_ref.get("expert_id", ""),
                        task_result.run_index,
                        task_result.run_label,
                    ),
                    task_result.status,
                    _task_duration_ms(task_result),
                    (task_result.result or {}).get("answer_excerpt", ""),
                )
            )
            if task_result.is_terminal:
                # Collecting a terminal task in-turn consumes its observe-later
                # notification (#948 S6): the model saw the result HERE, so the next
                # turn must not re-inject it. Exactly-once via the notify_pending gate.
                consume_notification(app, task_result.task_id)
                # Once-per-task wire emission: the ROW above is returned on EVERY wait
                # (the model may legitimately re-collect), but the terminal EVENT +
                # return Part + parent-resume fire exactly once — the server owns the
                # de-duplicated stream. A re-wait (partial-timeout re-collect, id
                # repeated in a batch) claims nothing and emits nothing. Shared with the
                # declared-workflow runner (both reach a terminal task via the same
                # invoker boundary).
                _emit_delegation_terminal(app, session_id, agent_def, task_result)
        # Deterministic ensemble merge (#948 S5): when this wait collected several
        # runs whose typed workflow_state sections COLLIDE, merge them in REQUEST
        # ORDER (run_index, never completion order) and surface every collision as a
        # typed ``workflow_state_merge_conflict`` row + a structured log — no silent
        # last-writer. The model reads the conflict rows and decides.
        merged_state, conflicts = _merge_wait_workflow_states(results)
        for conflict in conflicts:
            logger.warning(
                "workflow_state_merge_conflict key=%s winner_run=%s winner_agent=%s "
                "loser_runs=%s session=%s",
                conflict["key"],
                conflict["winner"]["run_index"],
                conflict["winner"].get("agent_id", ""),
                [
                    (loser["run_index"], loser.get("agent_id", ""))
                    for loser in conflict["loser_runs"]
                ],
                session_id,
            )
        # Declare the typed structured shape (owner ruling, P5 wire semantics):
        # summary line first, per-task rows, conflicts, merged state last — wired
        # through the SAME structured_content channel MCP tool results use, so the
        # UI's existing result ladder renders it with zero wait-specific client
        # code. This does NOT change what the model itself receives below.
        from clio_agent.gact.agents.tool_instrumentation import (  # noqa: PLC0415
            declare_structured_content,
        )

        elapsed_s = _time.monotonic() - call_start
        declare_structured_content(
            {
                "summary": wait_summary(elapsed_s, structured_rows),
                "results": structured_rows,
                "workflow_state_conflicts": conflicts,
                "merged_workflow_state": merged_state,
            }
        )
        # The model-facing return stays the FULL-fidelity per-task rows (verbatim
        # #880 output, typed conflicts, merged state) — compact is a UI concern,
        # not a fidelity cut. Key order matches the declared shape's tail (harmless
        # + helps the raw-JSON view); it is NOT the presentation mechanism.
        return json.dumps(
            {
                "results": results,
                "workflow_state_conflicts": conflicts,
                "merged_workflow_state": merged_state,
            },
            default=str,
        )

    def check_agent_tasks(task_ids: list[str] | None = None) -> str:
        """Non-blocking poll of this session's spawned tasks: each one's status AND,
        for finished tasks, a bounded result excerpt + message_ref (full text via
        wait_agent_tasks). Pass ``task_ids`` to poll a subset, or omit for all.
        Polling a finished task collects it (its result won't re-surface next turn).
        Use it to collect finished children while you keep working, instead of
        blocking in wait."""

        app, session_id = _ctx_app_session()
        from clio_agent.gact.agent_tasks import consume_notification  # noqa: PLC0415

        tasks = app.state.agent_task_registry.for_parent(session_id)
        wanted = set(task_ids or [])
        if wanted:
            tasks = [t for t in tasks if t.task_id in wanted]
        grouped: list[tuple[Any, list[TaskHandle]]] = []
        for task in tasks:
            invoker = invoker_for_task(app, task).invoker
            for grouped_invoker, handles in grouped:
                if grouped_invoker is invoker:
                    handles.append(TaskHandle.from_task(task))
                    break
            else:
                grouped.append((invoker, [TaskHandle.from_task(task)]))
        by_id: dict[str, Any] = {}
        for invoker, handles in grouped:
            by_id.update({result.task_id: result for result in invoker.check(handles)})
        task_results = [by_id[task.task_id] for task in tasks]
        rows: list[dict[str, Any]] = []
        for t in task_results:
            row: dict[str, Any] = {
                "task_id": t.task_id,
                "agent": t.agent_ref.get("expert_id", ""),
                "status": t.status,
                "queued_reason": t.queued_reason,
            }
            if t.is_terminal:
                result = t.result or {}
                # Uniform structured fields for EVERY terminal task — success and
                # failure alike (the model decides): a size-bounded excerpt, the
                # message_ref for the full text, the typed error_reason, and the
                # child session id. artifact_ref is reserved (#670).
                row["result"] = {
                    "answer_excerpt": str(result.get("answer_excerpt", "")),
                    "message_ref": str(result.get("message_ref", "")),
                    "error_reason": t.error_reason,
                    "child_session_id": t.child_session_id,
                    "artifact_ref": t.artifact_ref,
                }
                # A poll that surfaces the finished result consumes its observe-later
                # notification (exactly-once via the notify_pending gate) AND closes
                # the delegation on the wire — the SAME terminal choreography wait
                # emits (blueprint.delegation.completed|failed + return Part +
                # parent_resumed), through the shared delegation_reported once-gate so
                # a later wait can't double-emit ([1]/[9]). Without this a polled async
                # child left a started with no terminal (a dangling delegation).
                consume_notification(app, t.task_id)
                _emit_delegation_terminal(app, session_id, agent_def, t)
            rows.append(row)
        # Declared structured payload (P5 wire semantics, wait_agent_tasks's
        # treatment): message FIRST, rows after. The tally/format logic lives
        # in the owner module task_summary (shared with observe_agent_tasks).
        from clio_agent.gact.agents.task_summary import task_status_message  # noqa: PLC0415
        from clio_agent.gact.agents.tool_instrumentation import (  # noqa: PLC0415
            declare_structured_content,
        )

        declare_structured_content(
            {"message": task_status_message([r.get("status", "") for r in rows]), "tasks": rows}
        )
        return json.dumps({"tasks": rows}, sort_keys=True)

    def spawn_agents_parallel(spawns: list[dict], placement: str | None = None) -> str:
        """Fan out several declared children at once. ``spawns`` is a list of
        {agent, task}; returns their task_ids (collect with wait_agent_tasks).

        When this parent declares ``fanout.max_workers`` (#948 S5), the batch's
        concurrent admission is bounded by it: spawns beyond the bound QUEUE with a
        typed reason (the per-depth cap remains the global bound). Absent a declared
        bound the batch admits up to the global per-depth cap."""

        app, session_id = _ctx_app_session()
        bound = _fanout_batch_bound(agent_def)
        spawn_list = spawns or []
        # Fan-out GROUP identity (P5 wire semantics): ONE id minted for this whole
        # call and stamped on every sibling's started + completed expert_handoff
        # metadata (spawn_group_fields) — the server emits explicit grouping so
        # the UI never infers sibling Call boxes by adjacency/timing. A bare
        # spawn_agent_task call never mints one.
        spawn_group_id = f"fanout_{uuid.uuid4().hex[:12]}"
        group_size = len(spawn_list)
        _emit_semantic_event(
            app,
            session_id,
            "blueprint.fanout.started",
            turn_id=_active_semantic_turn_id(),
            trace_id=_active_semantic_trace_id(),
            status="running",
            summary=f"{agent_def.id} fanned out to {group_size} children",
            actor={"agent_id": agent_def.id, "role": "parent_expert"},
            blueprint=_blueprint_block(agent_def, ""),
            payload={
                **({"max_workers": bound} if bound else {}),
                "spawn_group_id": spawn_group_id,
                "group_size": group_size,  # finding [13]: declared total, not just the id
            },
        )
        out = []
        placement = resolve_batch_placement(app, session_id, placement)
        for entry in spawn_list:
            agent = str((entry or {}).get("agent") or "")
            task = str((entry or {}).get("task") or "")
            out.append(
                json.loads(
                    _do_spawn(
                        agent,
                        task,
                        fanout_bound=bound,
                        placement=placement,
                        spawn_group_id=spawn_group_id,
                        group_size=group_size,
                    )
                )
            )
        return json.dumps({"spawned": out}, sort_keys=True)

    def run_workflow(request: str = "") -> str:
        """Execute this blueprint's DECLARED deterministic workflow — an a->b->c child
        pathway gated on typed workflow_state, run in declaration order by the runner
        (the pack author's declaration IS the decision; clio executes it, never
        infers). Returns the full run record: per-step task ids + results, the
        accumulated workflow_state, and a terminal status (completed | stalled). A
        stall means a step's gate could not be satisfied or a child failed — read the
        stall reason (step, predicate, observed) and YOU decide how to proceed."""

        from clio_agent.gact.workflows import run_declared_workflow  # noqa: PLC0415

        app, session_id = _ctx_app_session()
        record = run_declared_workflow(
            app, agent_def, session_id, requesting_expert_id=agent_def.id, request=request
        )
        return json.dumps(record, sort_keys=True, default=str)

    # Declared presentation (tool_instrumentation): the spawn/fan-out/workflow
    # tools' wire representation IS their ``expert_handoff`` part — declared
    # ``handoff`` so the seam-attached observer records telemetry without a
    # second representation on the wire. The collectors are plain ``row`` tools
    # (owner, 2026-08-05: a wait/check is a REAL call, never invisible mechanism
    # the narration references).
    tools = [
        native_tool(
            spawn_agent_task,
            name="spawn_agent_task",
            desc=spawn_agent_task.__doc__,
            title="Spawn Agent",
            representation="handoff",
            args={
                "agent": {"type": "string", "description": "Declared child expert id to spawn."},
                "task": {"type": "string", "description": "The specific task for that child."},
                "placement": {
                    "type": "string",
                    "description": (
                        "Optional execution placement: local or relay:<cluster>. "
                        "Omit to use the session policy, then the local default."
                    ),
                },
            },
        ),
        native_tool(
            wait_agent_tasks,
            name="wait_agent_tasks",
            desc=wait_agent_tasks.__doc__,
            title="Wait",
            args={
                "task_ids": {"type": "array", "description": "Task ids returned by spawn."},
                "timeout_s": {
                    "type": "number",
                    "description": (
                        "REQUIRED max seconds to wait before returning current "
                        "statuses (a wait without a budget is a hang). You decide "
                        "how to proceed on a partial result. Prefer a short budget "
                        "(e.g. 30-60s) and re-collect finished children with a follow-up "
                        "wait or check_agent_tasks rather than blocking the whole turn."
                    ),
                },
            },
        ),
        native_tool(
            check_agent_tasks,
            name="check_agent_tasks",
            desc=check_agent_tasks.__doc__,
            title="Check Tasks",
            args={
                "task_ids": {
                    "type": "array",
                    "description": "Optional subset of task ids to poll (omit for all).",
                },
            },
        ),
        build_message_agent_tool(agent_def),
        # OBSERVE posture (#1000): the read-only sibling of check_agent_tasks, built in
        # its owner module (observe_runtime) so this file stays under the size ratchet.
        build_observe_tool(),
        native_tool(
            spawn_agents_parallel,
            name="spawn_agents_parallel",
            desc=spawn_agents_parallel.__doc__,
            title="Spawn Agents",
            representation="handoff",
            args={
                "spawns": {"type": "array", "description": "List of {agent, task} to fan out."},
                "placement": {
                    "type": "string",
                    "description": ("Optional placement applied to every spawn in this batch."),
                },
            },
        ),
    ]
    if not has_declared_children:
        collection_names = {
            "wait_agent_tasks",
            "check_agent_tasks",
            "message_agent",
            "observe_agent_tasks",
        }
        tools = [tool for tool in tools if getattr(tool, "name", "") in collection_names]

    # run_workflow is gated on a DECLARED workflow (mirroring the children-gated
    # toolset above): a blueprint with no ``workflow:`` block never sees the tool.
    from clio_agent.gact.workflows import parse_workflow  # noqa: PLC0415

    if has_declared_children and parse_workflow(agent_def) is not None:
        tools.append(
            native_tool(
                run_workflow,
                name="run_workflow",
                desc=run_workflow.__doc__,
                title="Run Workflow",
                representation="handoff",
                args={
                    "request": {
                        "type": "string",
                        "description": "The user's request, grounding each declared step's task.",
                    },
                },
            )
        )
    return tools
