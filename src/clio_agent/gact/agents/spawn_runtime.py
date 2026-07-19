"""Spawn-runtime tools for react mains (#948 S4).

The routing surface that REPLACES the deleted settle/synthesis orchestration and
the deleted inline per-child delegate/fan-out tools. A tier-1 main is now a react
agent whose answer IS the user deliverable; instead of emitting a typed routing
field consumed by a settle loop, it CALLS these tools:

* ``spawn_agent_task(agent, task)`` — spawn a declared child as a REAL child turn
  (S3 ``spawn_child_turn``, on the dedicated executor) and return its ``task_id``.
* ``wait_agent_tasks(task_ids, timeout_s)`` — block on the children's completion
  Events and return their results (spawn + wait COMPOSE the old synchronous
  delegate; the child runs on the dedicated pool so the waiting parent thread can
  never starve it).
* ``check_agent_tasks()`` — the parent's spawned tasks + their status.
* ``spawn_agents_parallel(spawns)`` — fan out several children at once (replaces
  the deleted inline fan-out tool).

Each tool re-emits the wire-facing ``blueprint.delegation.*`` / ``blueprint.fanout.*``
events the old tools emitted AND appends the ``expert_handoff`` Parts the deleted
sync-delegate path appended, so TUI handoff rendering stays lit (wire parity): the
semantic events feed the activity label / execution trace / active-agent indicator,
while the canonical transcript renderer keys the delegation header / nesting / return
row exclusively off ``type=='expert_handoff'`` Parts (#948 S4 findings [6]/[7]).
The child sessions + AgentTask records are the real substrate underneath — no
inline in-thread child forward, no settle-loop routing vocabulary.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from clio_agent.gact import context as _ctx
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

# Bounded wait so a stuck child never wedges the parent's react loop forever; the
# model passes its own timeout and decides how to proceed on a partial result.
_DEFAULT_WAIT_TIMEOUT_S = 300.0


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
    the delegation ``output`` IS the child's answer, byte-for-byte, ALWAYS.

    The AgentTask record deliberately keeps only a BOUNDED excerpt (registry memory
    stays bounded), so the full text is re-read at wait-time from the child session's
    message store via the result's ``message_ref``.

    Returns ``(output, markers)``. On success ``markers`` is empty and ``output`` is
    the byte-identical child answer. If the child session/message is gone, falls back
    to the bounded excerpt WITH a typed marker (never silently):
    ``output_source='excerpt_fallback'`` + ``output_fallback_reason='child_message_gone'``.
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
    boot-rebuilt registry does not re-emit the terminal event.

    Best-effort: if the child session is already gone the flag cannot be durably
    written, but the task will not survive a reboot either (the boot fold folds only
    existing sessions), so there is no re-emit risk — surface the typed reason,
    never crash the wait (no-silent-fallback)."""

    from clio_agent.gact.agent_tasks import AgentTaskError, persist_agent_task  # noqa: PLC0415

    try:
        persist_agent_task(app, task)
    except AgentTaskError as exc:
        logger.warning(
            "delegation_reported not persisted reason=%s task=%s",
            getattr(exc, "reason", "unknown"),
            getattr(task, "task_id", "?"),
        )


def _merge_wait_workflow_states(
    results: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Deterministic request-order merge of a wait batch's per-run workflow_state
    (#948 S5). Considers only rows that carry BOTH a ``run_index`` and a mapping
    ``workflow_state`` (a completed run) — ``unknown_task`` / failed-empty rows
    contribute nothing. Delegates the ordering + conflict detection to the owner
    module (``workflow_state.merge``); returns ``(merged, conflicts)``."""

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
    agent_def: "AgentDef", child_id: str, task_text: str, depth: int, run_index: int = 0
) -> Part:
    """The ``delegate.started`` expert_handoff Part appended to the PARENT transcript
    when a child is spawned (#948 S4 finding [7]).

    The canonical transcript renderer (transcriptDelegationModel.ts) drives the
    delegation header, depth/indentation (from ``child_agent``/``parent_agent`` links)
    and nested rows off ``type=='expert_handoff'`` Parts — NOT the semantic events — so
    without this Part a spawned child renders nothing in the main transcript. Field
    shape matches the pinned TUI: ``child_agent``/``parent_agent`` links, ``stage``
    lifecycle, and ``metadata.question`` (the task the header shows)."""

    started_row = {
        "agent_id": child_id,
        "parent_id": agent_def.id,
        "status": "running",
        "stage": "delegate.started",
        "question": task_text,
        "depth": depth,
        "run_index": run_index,
    }
    return Part(
        id=f"live_handoff_{uuid.uuid4().hex[:12]}",
        type="expert_handoff",
        agent_id=agent_def.id,
        parent_agent=agent_def.id,
        child_agent=child_id,
        stage="delegate.started",
        status="running",
        text=f"{agent_def.id} -> {child_id}",
        metadata={**_handoff_part_metadata(started_row), "stream_source": "live"},
    )


def _return_handoff_part(agent_def: "AgentDef", task: Any, payload: dict[str, Any]) -> Part:
    """The terminal RETURN expert_handoff Part appended to the PARENT transcript when a
    spawned child reaches a terminal state (#948 S4 finding [7]).

    Both success AND failure conclude on the SAME terminal lane —
    ``stage='delegate.completed'`` with the outcome riding ``status`` (#882) — so a
    verbatim (dedup-free) client renders one return row per child, never a second
    header, and a FAILED child is visible (not buried in raw tool JSON).
    ``metadata.output`` is the child's FULL answer byte-for-byte (#880, resolved in
    ``payload``); a failure carries empty output with the typed reason on
    ``status``/``error`` and any verbatim-output degradation marker."""

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
    # Surface the verbatim-output degradation markers (never silent) onto the Part too.
    for marker in ("output_source", "output_fallback_reason"):
        if marker in payload:
            return_row[marker] = payload[marker]
    return Part(
        id=f"live_handoff_{uuid.uuid4().hex[:12]}",
        type="expert_handoff",
        agent_id=agent_def.id,
        parent_agent=agent_def.id,
        child_agent=child_id,
        stage="delegate.completed",
        status=task.status,
        text=f"{agent_def.id} <- {child_id}",
        metadata={**_handoff_part_metadata(return_row), "stream_source": "live"},
    )


def _emit_delegation_terminal(app: Any, session_id: str, agent_def: "AgentDef", task: Any) -> None:
    """Emit a terminal task's once-per-task delegation event + return Part + resume.

    Shared by :func:`wait_agent_tasks` and the declared-workflow runner
    (:func:`emit_workflow_step_return`): BOTH spawn+wait through the same substrate,
    so BOTH reach a terminal task and must render it exactly once. The once-per-task
    claim (``mark_delegation_reported``) guarantees exactly-once wire emission no
    matter which path gets there first (the server owns the de-duplicated stream)."""

    registry = app.state.agent_task_registry
    reported = registry.mark_delegation_reported(task.task_id)
    if reported is None:
        return
    _persist_delegation_reported(app, reported)
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

    A workflow step spawns its child directly through the substrate (never the
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
        _started_handoff_part(agent_def, child_id, task_text, spawned.depth, spawned.run_index),
    )


def emit_workflow_step_return(app: Any, session_id: str, agent_def: "AgentDef", task: Any) -> None:
    """Terminal wire parity for a declared-workflow step (delegates to the shared
    once-per-task terminal emission)."""

    if task.is_terminal:
        _emit_delegation_terminal(app, session_id, agent_def, task)


def build_spawn_runtime_tools(base_agent: Any, agent_def: "AgentDef") -> list[Any]:
    """Build the react-main spawn tools bound to ``agent_def`` as the requesting
    (parent) expert. Resolved lazily against the active app/session at call time."""

    import dspy  # noqa: PLC0415

    from clio_agent.gact.agents.resolution import _runtime_declared_child_ids  # noqa: PLC0415
    from clio_agent.gact.turn_spawn import (  # noqa: PLC0415
        SpawnError,
        TaskSpec,
        spawn_child_turn_threadsafe,
    )

    # Only an agent with DECLARED children gets the routing surface — a leaf expert
    # has nothing to spawn (and spawn would reject an undeclared child anyway).
    _app = _ctx.active_app()
    _sid = _ctx.active_session_id()
    if _app is None or not _runtime_declared_child_ids(_app, agent_def.id, session_id=_sid):
        return []

    def _ctx_app_session() -> tuple[Any, str]:
        app = _ctx.active_app()
        session_id = _ctx.active_session_id()
        if app is None or not session_id:
            raise RuntimeError("spawn-runtime tool requires an active CLIO app/session context")
        return app, session_id

    def _do_spawn(agent: str, task: str, *, fanout_bound: int = 0) -> str:
        """Spawn one declared child through the substrate + emit the started wire
        parity. ``fanout_bound`` (> 0) caps how many of THIS parent's concurrent
        children at this depth may run before a spawn queues — the fan-out admission
        bound (#948 S5); 0 means only the global per-depth cap applies."""

        app, session_id = _ctx_app_session()
        # Computed depth: a child spawns at (its own depth) + 1, so nesting
        # increments through the real tool path and the runaway backstop is
        # reachable (a root session spawns at depth 1) (#948 S4 adversarial review).
        depth = _current_session_depth(app, session_id) + 1
        try:
            spawned = spawn_child_turn_threadsafe(
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
                    mode="sync",
                    fanout_bound=fanout_bound,
                ),
            )
        except SpawnError as exc:
            return json.dumps({"error": exc.reason, "message": str(exc)}, sort_keys=True)
        _emit_semantic_event(
            app,
            session_id,
            "blueprint.delegation.started",
            turn_id=_active_semantic_turn_id(),
            trace_id=_active_semantic_trace_id(),
            status="running",
            summary=f"{agent_def.id} spawned {agent}",
            actor={"agent_id": agent_def.id, "role": "parent_expert"},
            subject={"agent_id": agent, "role": "child_expert"},
            blueprint=_blueprint_block(agent_def, agent),
            payload={"run_index": spawned.run_index},
        )
        # Transcript render parity (#948 S4 finding [7]): the delegation header /
        # nesting is driven off the expert_handoff Part, not the semantic event.
        # Spawn happens once per task, so the started Part is inherently once-per-task.
        # The ensemble run_index (#948 S5) rides the Part so a same-child ensemble's
        # started rows are distinguishable.
        _append_live_assistant_part(
            app,
            session_id,
            _started_handoff_part(agent_def, agent, task, depth, spawned.run_index),
        )
        return json.dumps(
            {"task_id": spawned.task_id, "status": spawned.status, "run_index": spawned.run_index},
            sort_keys=True,
        )

    def spawn_agent_task(agent: str, task: str) -> str:
        """Spawn a declared child expert as a background child turn; returns its
        task_id (use wait_agent_tasks to collect its result)."""

        return _do_spawn(agent, task)

    def wait_agent_tasks(task_ids: list[str], timeout_s: float = _DEFAULT_WAIT_TIMEOUT_S) -> str:
        """Block until the given spawned tasks finish (up to timeout_s), then return
        each one's result. The children run on a dedicated pool, so waiting here
        never starves them."""

        app, session_id = _ctx_app_session()
        registry = app.state.agent_task_registry
        import time as _time  # noqa: PLC0415

        deadline = _time.monotonic() + max(0.0, float(timeout_s or 0.0))
        results = []
        for tid in task_ids or []:
            # Validate the id BEFORE waiting: registry.event() would setdefault a
            # fresh never-set Event for an unknown/typo id and block the FULL budget
            # (starving every real id after it via the shared deadline). An unknown
            # id returns immediately with a typed row and emits nothing.
            if registry.get(tid) is None:
                results.append({"task_id": tid, "error": "unknown_task"})
                continue
            remaining = max(0.0, deadline - _time.monotonic())
            registry.event(tid).wait(timeout=remaining)
            task = registry.get(tid)
            if task is None:  # pragma: no cover - retained records are never removed
                results.append({"task_id": tid, "error": "unknown_task"})
                continue
            payload = _completion_payload(app, task)
            results.append(payload)
            if task.is_terminal:
                # Once-per-task wire emission: the ROW above is returned on EVERY wait
                # (the model may legitimately re-collect), but the terminal EVENT +
                # return Part + parent-resume fire exactly once — the server owns the
                # de-duplicated stream. A re-wait (partial-timeout re-collect, id
                # repeated in a batch) claims nothing and emits nothing. Shared with the
                # declared-workflow runner (both reach a terminal task via the same
                # substrate).
                _emit_delegation_terminal(app, session_id, agent_def, task)
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
        return json.dumps(
            {
                "results": results,
                "merged_workflow_state": merged_state,
                "workflow_state_conflicts": conflicts,
            },
            sort_keys=True,
            default=str,
        )

    def check_agent_tasks() -> str:
        """List the tasks this session has spawned and their current status."""

        app, session_id = _ctx_app_session()
        tasks = app.state.agent_task_registry.for_parent(session_id)
        return json.dumps(
            {
                "tasks": [
                    {
                        "task_id": t.task_id,
                        "agent": t.agent_ref.get("expert_id", ""),
                        "status": t.status,
                        "queued_reason": t.queued_reason,
                    }
                    for t in tasks
                ]
            },
            sort_keys=True,
        )

    def spawn_agents_parallel(spawns: list[dict]) -> str:
        """Fan out several declared children at once. ``spawns`` is a list of
        {agent, task}; returns their task_ids (collect with wait_agent_tasks).

        When this parent declares ``fanout.max_workers`` (#948 S5), the batch's
        concurrent admission is bounded by it: spawns beyond the bound QUEUE with a
        typed reason (the per-depth cap remains the global bound). Absent a declared
        bound the batch admits up to the global per-depth cap."""

        app, session_id = _ctx_app_session()
        bound = _fanout_batch_bound(agent_def)
        _emit_semantic_event(
            app,
            session_id,
            "blueprint.fanout.started",
            turn_id=_active_semantic_turn_id(),
            trace_id=_active_semantic_trace_id(),
            status="running",
            summary=f"{agent_def.id} fanned out to {len(spawns or [])} children",
            actor={"agent_id": agent_def.id, "role": "parent_expert"},
            blueprint=_blueprint_block(agent_def, ""),
            payload={"max_workers": bound} if bound else {},
        )
        out = []
        for entry in spawns or []:
            agent = str((entry or {}).get("agent") or "")
            task = str((entry or {}).get("task") or "")
            out.append(json.loads(_do_spawn(agent, task, fanout_bound=bound)))
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

    tools = [
        dspy.Tool(
            func=spawn_agent_task,
            name="spawn_agent_task",
            desc=spawn_agent_task.__doc__,
            args={
                "agent": {"type": "string", "description": "Declared child expert id to spawn."},
                "task": {"type": "string", "description": "The specific task for that child."},
            },
        ),
        dspy.Tool(
            func=wait_agent_tasks,
            name="wait_agent_tasks",
            desc=wait_agent_tasks.__doc__,
            args={
                "task_ids": {"type": "array", "description": "Task ids returned by spawn."},
                "timeout_s": {"type": "number", "description": "Max seconds to wait."},
            },
        ),
        dspy.Tool(
            func=check_agent_tasks,
            name="check_agent_tasks",
            desc=check_agent_tasks.__doc__,
            args={},
        ),
        dspy.Tool(
            func=spawn_agents_parallel,
            name="spawn_agents_parallel",
            desc=spawn_agents_parallel.__doc__,
            args={
                "spawns": {"type": "array", "description": "List of {agent, task} to fan out."},
            },
        ),
    ]
    # run_workflow is gated on a DECLARED workflow (mirroring the children-gated
    # toolset above): a blueprint with no ``workflow:`` block never sees the tool.
    from clio_agent.gact.workflows import parse_workflow  # noqa: PLC0415

    if parse_workflow(agent_def) is not None:
        tools.append(
            dspy.Tool(
                func=run_workflow,
                name="run_workflow",
                desc=run_workflow.__doc__,
                args={
                    "request": {
                        "type": "string",
                        "description": "The user's request, grounding each declared step's task.",
                    },
                },
            )
        )
    return tools
