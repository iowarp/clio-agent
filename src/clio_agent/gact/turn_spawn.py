"""Child-turn substrate (#948 S3, #951): spawn a declared child expert as a REAL
turn in a REAL child session, projected as an :class:`AgentTask`.

``spawn_child_turn(app, TaskSpec) -> AgentTask`` mints a child session (created
BEFORE the run, with ``parent_session_id`` lineage,
``agent={"id": <child expert>}``, ``session_type=="agent_task"`` metadata),
stages a real turn through the same ``_start_background_user_turn`` a user POST uses
(so status / SSE / cancellation behave identically), and drives the task lifecycle
to a terminal record via a completion hook on the child turn task.

This is the #671 federation seam: :class:`TaskSpec` / the returned record are
serializable from day one, so a remote executor can later swap in behind it.
Child forwards run on a DEDICATED executor (never the default pool) so a parent
blocked in a future wait (#948 S6) can never starve its own children.
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.gact.agent_tasks import (
    AGENT_TASK_EVENTS,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    AgentTask,
    persist_agent_task,
    publish_agent_task_event,
)
from clio_agent.gact.spawn_context import validate_task_spec
from clio_agent.gact.task_fold import finish_agent_task_transition, fold_agent_task_transition

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


def _err_code(error_info: Any) -> str:
    """The typed error code from a message's ``error_info``, whether it is a dict
    (wire form) or an ``ErrorInfo`` object (in-memory form)."""

    if not error_info:
        return ""
    if isinstance(error_info, dict):
        return str(error_info.get("error") or "")
    return str(getattr(error_info, "error", "") or "")


# Runaway backstop (NOT a 3-tier rule): ``tier`` is semantic weight, not depth —
# deep chains (tier-1 → tier-2 → tier-2 → tier-2 → tier-3s) are legitimate. This
# only refuses a spawn whose computed depth would exceed the backstop, bounding
# runaway self-spawning (and, per the per-depth pools below, the number of pools).
MAX_SPAWN_DEPTH = 8
_ANSWER_EXCERPT_MAX = 2000

# P1.1 (#1031 governance-surfaces, "subagents inherit structurally"): a child spawned
# from a RESTRICTIVE parent (plan/architect) must NOT be minted in the default
# ``edit`` mode — that would let a plan-mode parent escape its own write-deny via a
# full-authority child (both the normal spawn_agent_task tool and the P1.0
# spawn_subagent_with_skill effect go through this shared path). The child instead
# INHERITS the parent's mode verbatim (plan→plan, architect→architect, edit→edit —
# the net change is only for restrictive parents; an edit parent's children are
# unaffected). Exiting a restrictive mode stays the user-gated ``plan_exit`` flow;
# nothing here lets a child relax below its inherited posture.
_RESTRICTIVE_SESSION_MODES = frozenset({"plan", "architect"})


@dataclass(frozen=True)
class TaskSpec:
    """A serializable spawn request (the #671 seam — serializable in AND out)."""

    child_expert_id: str
    task_text: str
    parent_session_id: str
    requesting_expert_id: str = "main"
    parent_turn_id: str = ""
    depth: int = 1
    mode: str = "async"  # "sync" (a waiter will collect) | "async" (notify-later)
    workflow_state: Optional[dict[str, Any]] = None
    # Fan-out admission bound (#948 S5): when > 0, the declaring parent's
    # ``fanout.max_workers`` — at most this many of the same (parent, requesting
    # expert, depth) children RUN before the next spawn queues. 0 = only the global
    # per-depth cap applies.
    fanout_bound: int = 0
    # Fan-out GROUP identity (wire semantics, P5): set by ``spawn_agents_parallel``
    # to ONE id shared by every spawn in that call (empty for a single
    # ``spawn_agent_task`` spawn / a declared workflow step — never invented).
    # Rides onto the minted :class:`AgentTask` so it survives to the completed
    # ``expert_handoff`` Part at wait-time. See ``AgentTask.spawn_group_id``.
    spawn_group_id: str = ""
    group_size: int = 0
    # P1.0 (#1062): verbatim context prepended to the child's staged user message —
    # used by ``spawn_subagent_with_skill`` to SEED a fresh subagent with a skill body
    # instead of inlining it into the caller's context. Empty for a normal spawn.
    seed_context: str = ""
    # P1.0 (#1062): skip the declared-child routing guard for a SELF-directed spawn (a
    # skill-as-subagent running the caller's own expert in a fresh context — not a
    # routing decision to a different declared capability). A documented seam, not a
    # silent bypass: the depth backstop still applies and the child expert must resolve.
    skip_declared_check: bool = False
    # P2.4 (#1122): execution-context bindings for a detached executor. ``None``
    # means absent and permits the unchanged live-parent inheritance path; any
    # present value wins over the parent field independently. ``mode`` above is
    # already the sync/async collection semantic, hence the unambiguous
    # ``session_mode`` name for the inherited plan/edit/architect posture.
    workspace_id: Optional[str] = None
    session_mode: Optional[str] = None
    session_scope_metadata: Optional[dict[str, Any]] = None
    # P2.10 (#1127): resolved execution placement carried across the invoker seam.
    placement: str = "local"
    # Spotter-ai (#1034 follow-on): a caller-chosen display label for the minted
    # :class:`AgentTask`, overriding the default ``"<expert_id> #<run_index+1>"``
    # (e.g. the spotter watcher spawns with ``"SPOTTER AI"`` so it reads as a
    # named surveillance task in the tray, not an ensemble run). Empty keeps the
    # existing default-label behavior verbatim.
    run_label: str = ""
    # Spotter-ai standing-watcher follow-on: when False, mint the child session +
    # AgentTask record WITHOUT starting a first turn -- the record transitions
    # straight to RUNNING (never QUEUED-at-cap, never ``_launch``ed) so it stands
    # as a live, non-terminal row a later independent wake can drive turns on
    # (see ``gact/spotter_watcher.py``). ``task_text``/``workflow_state``/
    # ``seed_context`` are unused on this path (no turn ever reads them). Default
    # ``True`` preserves the existing "mint AND start a turn" behavior verbatim.
    start_turn: bool = True


class SpawnError(Exception):
    """A refused spawn (undeclared child, depth exceeded). Carries a typed reason."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def install_agent_task_executor(app: "FastAPI") -> None:
    """Install the DEDICATED child-forward pool machinery (never the default
    executor). Pools are created lazily PER DEPTH by
    :func:`agent_task_executor_for_depth`: a child turn at depth ``d`` runs on
    ``pool[d]``, so a waiter blocked on ``pool[d]`` can never starve its own
    children on ``pool[d+1]`` (a single shared pool deadlocks nested orchestrators
    — see #948 S4 adversarial review). Each pool is sized to the concurrency cap;
    the depth backstop (:data:`MAX_SPAWN_DEPTH`) bounds the number of pools."""

    from clio_agent import conf  # noqa: PLC0415

    cap = conf.resolve(
        "agent_tasks.max_concurrent",
        env="CLIO_MAX_CONCURRENT_AGENT_TASKS",
        default=3,
        cast=conf.as_int,
    )
    cap = max(1, int(cap or 3))
    app.state.max_concurrent_agent_tasks = cap
    app.state.agent_task_executors = {}
    app.state.agent_task_executor_lock = threading.Lock()


def agent_task_executor_for_depth(
    app: "FastAPI", depth: int
) -> concurrent.futures.ThreadPoolExecutor:
    """Return the dedicated child-forward pool for turns at ``depth`` (lazily
    created, one per depth). Same ``max_concurrent`` cap and shutdown semantics as
    every other depth's pool; thread-safe against concurrent child launches."""

    depth = max(1, int(depth or 1))
    pools: dict[int, concurrent.futures.ThreadPoolExecutor] = app.state.agent_task_executors
    lock = app.state.agent_task_executor_lock
    with lock:
        pool = pools.get(depth)
        if pool is None:
            cap = getattr(app.state, "max_concurrent_agent_tasks", 3)
            pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=cap, thread_name_prefix=f"clio-agent-task-d{depth}"
            )
            pools[depth] = pool
        return pool


def shutdown_agent_task_executors(app: "FastAPI") -> None:
    """Shut down every per-depth child-forward pool (symmetric to their lazy
    install). Non-daemon workers otherwise leak across app lifecycles and a worker
    still in a slow child forward blocks process exit."""

    pools = getattr(app.state, "agent_task_executors", None) or {}
    for pool in list(pools.values()):
        pool.shutdown(wait=False, cancel_futures=True)


def _batch_key(
    parent_session_id: str, requesting_expert_id: str, depth: int
) -> tuple[str, str, int]:
    """The fan-out batch identity: the same parent expert's children at one depth.

    The ``fanout.max_workers`` bound (#948 S5) caps how many tasks sharing this key
    may RUN concurrently — a per-parent-expert concurrency limit distinct from the
    global per-depth cap."""

    return (parent_session_id, requesting_expert_id, depth)


def _queued_admissible(
    task: "AgentTask",
    running_by_depth: dict[int, int],
    running_by_batch: dict[tuple[str, str, int], int],
    cap: int,
) -> bool:
    """Whether a queued task may be admitted: the global per-depth cap AND (when the
    task declares a ``fanout_bound``) the fan-out batch bound must both have room —
    else admitting it would exceed the parent's declared max_workers (#948 S5)."""

    if running_by_depth.get(task.depth, 0) >= cap:
        return False
    if task.fanout_bound > 0:
        key = _batch_key(
            task.parent_session_id, task.agent_ref.get("requesting_expert_id", ""), task.depth
        )
        if running_by_batch.get(key, 0) >= task.fanout_bound:
            return False
    return True


def _running_in_batch(snapshot: Any, batch_key: tuple[str, str, int]) -> int:
    """Count RUNNING tasks in ``batch_key``'s fan-out batch (over a registry snapshot)."""

    return sum(
        1
        for t in snapshot
        if t.status == STATUS_RUNNING
        and _batch_key(t.parent_session_id, t.agent_ref.get("requesting_expert_id", ""), t.depth)
        == batch_key
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _message_text(msg: Any) -> str:
    parts = getattr(msg, "parts", None) or []
    out = []
    for p in parts:
        text = getattr(p, "text", None)
        if text is None and isinstance(p, dict):
            text = p.get("text")
        if getattr(p, "type", None) == "text" or (isinstance(p, dict) and p.get("type") == "text"):
            out.append(str(text or ""))
    return "".join(out).strip()


def _next_run_index(app: "FastAPI", spec: TaskSpec) -> int:
    """The ensemble run index for a new spawn (#948 S5).

    Assigned in spawn order per ``(parent_session_id, parent_turn_id, child expert)``:
    the count of tasks ALREADY spawned for that triple. So spawning the same declared
    child three times in one parent turn yields run_index 0, 1, 2 — durable per record.

    Race-free: ``spawn_child_turn`` runs on the single app event loop (S5's tools reach
    it via ``spawn_child_turn_threadsafe`` → ``run_coroutine_threadsafe``), so the
    registry count and the subsequent ``persist_agent_task`` are serialized — two
    concurrent fan-outs cannot read the same count and collide on an index."""

    reg = app.state.agent_task_registry
    return sum(
        1
        for t in reg.snapshot()
        if t.parent_session_id == spec.parent_session_id
        and t.parent_turn_id == spec.parent_turn_id
        and t.agent_ref.get("expert_id") == spec.child_expert_id
    )


def spawn_child_turn(app: "FastAPI", spec: TaskSpec) -> AgentTask:
    """Spawn ``spec``'s declared child expert as a real child turn; return its
    :class:`AgentTask` record (already ``running``, or ``queued`` at the cap).

    Must be called on the app event loop (S3): it stages a turn via the turn
    runner. S5's model-facing tools call it cross-thread via the executor seam.

    The SAME declared child may be spawned N times concurrently in one parent turn
    (an ensemble): each call mints its own child session + task record (task ids are
    unique per child session), disambiguated by a durable ``run_index`` (#948 S5).
    """

    # ---- structural guards -------------------------------------------------
    workspace_id, parent_mode, session_scope_metadata = validate_task_spec(app, spec)

    # ---- backpressure: queue (never fail) at the cap ----------------------
    # PER-DEPTH admission: each depth has its own pool, so the cap is counted
    # against RUNNING tasks at the SAME depth. A depth-2 fan-out never queues
    # behind depth-1 siblings on a different pool (#948 S4 adversarial review).
    reg = app.state.agent_task_registry
    cap = getattr(app.state, "max_concurrent_agent_tasks", 3)
    snap = reg.snapshot()
    running = sum(1 for t in snap if t.status == STATUS_RUNNING and t.depth == spec.depth)
    global_at_cap = running >= cap
    # Fan-out admission bound (#948 S5): a declared ``fanout.max_workers`` caps this
    # parent expert's concurrent children at this depth. A batch spawn beyond the
    # bound QUEUES (typed ``concurrency_cap``) even when the global per-depth cap has
    # room — the per-depth cap stays the global bound.
    fanout_at_cap = spec.fanout_bound > 0 and (
        _running_in_batch(
            snap, _batch_key(spec.parent_session_id, spec.requesting_expert_id, spec.depth)
        )
        >= spec.fanout_bound
    )
    at_cap = global_at_cap or fanout_at_cap

    # ---- mint the child session (authoritative store) ----------------------
    # Structural mode inheritance (P1.1 "subagents inherit structurally"): the child
    # is minted in the PARENT's session mode, not the default ``edit`` — else a
    # plan/architect-mode parent could spawn a full-authority edit-mode child and
    # write what the parent itself is denied (the plan-override bypass). See
    # ``_RESTRICTIVE_SESSION_MODES`` above.
    child = app.state.sessions.create(
        workspace_id=workspace_id,
        title=f"{spec.child_expert_id} task",
        parent_session_id=spec.parent_session_id,
        agent={"id": spec.child_expert_id, "mode": "subagent"},
        mode=parent_mode,
    )
    if parent_mode in _RESTRICTIVE_SESSION_MODES:
        # Typed, queryable note (no-silent-fallback ground rule): this is a real
        # behavior change from the pre-fix default (child always got ``edit``), so
        # it must be traceable, not just applied silently.
        logger.info(
            "spawn_child_turn: child session %s inherits restrictive mode %r from "
            "parent %s (plan-mode subagent isolation, requesting_expert=%s)",
            child.id,
            parent_mode,
            spec.parent_session_id,
            spec.requesting_expert_id,
        )
    # Ensemble run index (#948 S5): computed from the registry BEFORE this task is
    # persisted, so the first spawn of the child in this parent turn gets 0, the next 1…
    run_index = _next_run_index(app, spec)
    now = _now()
    task = AgentTask(
        task_id="task_" + child.id.split("_")[-1],
        parent_session_id=spec.parent_session_id,
        child_session_id=child.id,
        parent_turn_id=spec.parent_turn_id,
        agent_ref={
            "expert_id": spec.child_expert_id,
            "requesting_expert_id": spec.requesting_expert_id,
        },
        depth=spec.depth,
        run_index=run_index,
        fanout_bound=spec.fanout_bound,
        spawn_group_id=spec.spawn_group_id,
        group_size=spec.group_size,
        handle_id="task_" + child.id.split("_")[-1],
        run_label=spec.run_label or f"{spec.child_expert_id} #{run_index + 1}",
        live_state=STATUS_QUEUED,
        host=(spec.placement.split(":", 1)[1] if spec.placement.startswith("relay:") else "local"),
        placement=spec.placement,
        status=STATUS_QUEUED,
        queued_reason="concurrency_cap" if at_cap else "",
        created_at=now,
        updated_at=now,
    )
    persist_agent_task(app, task)
    # Persist the launch data on the child session so a queued task can be launched
    # faithfully later (the AgentTask record deliberately carries no task_text), AND
    # inherit the parent's session-scoped blueprint / expert-pack activation keys so
    # the child resolves its expert against the parent's active blueprint (not the
    # global/default catalog — see ``inherited_session_scope_metadata``).
    app.state.sessions.update(
        child.id,
        metadata_patch={
            **session_scope_metadata,
            "spawn_placement": spec.placement,
            # Queryable audit trail for the mode-inheritance fix above: present (and
            # truthy) only when the child's mode was structurally inherited from a
            # restrictive parent, so the API/trace can distinguish "child is plan mode
            # because its own agent defaults there" from "child is plan mode because
            # its parent was" without guessing from ``session.mode`` alone.
            **(
                {"spawn_mode_inherited_from": parent_mode}
                if parent_mode in _RESTRICTIVE_SESSION_MODES
                else {}
            ),
            "pending_spawn": {
                "task_text": spec.task_text,
                "workflow_state": spec.workflow_state or {},
                "mode": spec.mode,
                # P1.0 (#1062): persist the skill seed + self-directed flag so a QUEUED
                # skill-subagent launches faithfully later (parity with task_text).
                "seed_context": spec.seed_context,
                "skip_declared_check": spec.skip_declared_check,
                # Persist resolved values so queued admission stays self-contained
                # even when the parent session disappears before launch.
                "workspace_id": workspace_id,
                "session_mode": parent_mode,
                "session_scope_metadata": session_scope_metadata,
            },
        },
    )
    publish_agent_task_event(app, task, AGENT_TASK_EVENTS[STATUS_QUEUED])
    if not spec.start_turn:
        # Standing task: never queued-at-cap, never _launch()ed -- transitions
        # straight to its RUNNING/"waiting" standing state. A later, independent
        # wake (gact/spotter_watcher.py) drives real turns on this same child
        # session without ever re-entering spawn_child_turn.
        running = reg.transition(task.task_id, STATUS_RUNNING, updated_at=_now())
        persist_agent_task(app, running)
        publish_agent_task_event(app, running, AGENT_TASK_EVENTS[STATUS_RUNNING])
        return running
    if at_cap:
        # FIFO admission happens when a running task frees a slot (completion hook).
        # Return the queued record; the model decides whether to wait.
        return task

    return _launch(app, task, spec)


def spawn_child_turn_threadsafe(app: "FastAPI", spec: TaskSpec) -> AgentTask:
    """Loop-safe entry point: run :func:`spawn_child_turn` on the app event loop
    regardless of the caller's thread. S5's model-facing tools call this from the
    parent's forward (an executor thread); tests / the live-gate seam call it from
    the main thread. Directly reentrant when already on the loop."""

    import asyncio  # noqa: PLC0415

    loop = getattr(app.state, "mcp_app_loop", None)
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if loop is None or running is loop:
        return spawn_child_turn(app, spec)

    async def _call() -> AgentTask:
        return spawn_child_turn(app, spec)

    return asyncio.run_coroutine_threadsafe(_call(), loop).result(timeout=60)


def _cancel_one_child_task(app: "FastAPI", reg: Any, task: "AgentTask") -> Optional["AgentTask"]:
    """Cooperatively + hard-cancel one non-terminal child task's in-flight turn and mark
    the task cancelled. Returns the updated record, or ``None`` if it raced to terminal.
    The single per-task cancel primitive shared by the cascade + the per-task cancel."""

    child_sid = task.child_session_id
    app.state.cancel_flags.add(child_sid)
    event = app.state.cancel_events.get(child_sid)
    if event is not None:
        event.set()
    in_flight = app.state.in_flight_turns.get(child_sid)
    if in_flight is not None and not in_flight.done():
        in_flight.cancel()
    # #993: the cooperative flag / future cancel above stops the ReAct loop at its next
    # decision point, but an LM call ALREADY in flight keeps its provider transport
    # streaming — on the pooled claude_code SDK that is a CLI subprocess that keeps
    # producing late ops the settled transcript must refuse. Kill that child's in-flight
    # SDK stream NOW (typed cancelled_transport_killed) so it stops producing. Only this
    # child session's stream is terminated; unrelated sessions on the shared pool survive.
    # No-op for a child with no in-flight SDK stream (non-claude_code transport / idle).
    from clio_agent.providers.claude_code_cancel import abort_session_streams  # noqa: PLC0415

    abort_session_streams(child_sid)
    try:
        updated = reg.transition(task.task_id, STATUS_CANCELLED, updated_at=_now())
    except Exception:  # noqa: BLE001 - already terminal via a racing completion
        return None
    persist_agent_task(app, updated)
    publish_agent_task_event(app, updated, AGENT_TASK_EVENTS[STATUS_CANCELLED])
    return updated


def cancel_children_of(app: "FastAPI", parent_session_id: str) -> int:
    """Cancel every non-terminal DESCENDANT task of ``parent_session_id`` (the cancel
    cascade): cooperatively + hard-cancel each descendant's in-flight turn and mark the
    task cancelled. Returns the count cancelled. Called when a parent turn/task is
    cancelled so no child turn outlives the parent that spawned it.

    TRANSITIVE (#953 [3]): S5 makes nested spawns first-class (declared workflows,
    nested experts, ``run_workflow`` reachable from a child), so a direct child may have
    its own children. This recurses depth-first into each child's own ``for_parent`` set
    (cycle-safe via a ``seen`` set over session ids) — a grandchild is descended into even
    when its parent already settled, since a grandchild can outlive a completed child."""

    reg = getattr(app.state, "agent_task_registry", None)
    if reg is None:
        return 0
    n = 0
    seen: set[str] = set()
    stack: list[str] = [parent_session_id]
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        for task in reg.for_parent(pid):
            # Descend into the child's OWN children regardless of the child's terminality
            # (a grandchild can still be running under a completed child).
            if task.child_session_id and task.child_session_id not in seen:
                stack.append(task.child_session_id)
            if task.is_terminal:
                continue
            if _cancel_one_child_task(app, reg, task) is not None:
                n += 1
    # Cancelling running children frees concurrency slots — admit queued tasks
    # (possibly of OTHER parents) into them, else they strand forever (the
    # completion hook won't: a cancelled task is already terminal when its
    # done-callback fires, so it early-returns before the admission).
    if n:
        _admit_next_queued(app)
    return n


def cancel_agent_task(app: "FastAPI", task_id: str) -> bool:
    """Cancel a SINGLE agent task by id (the per-task cancel machinery) AND cascade to
    its own descendants, so a cancelled task's children never outlive it. Returns whether
    anything was cancelled. Used by the declared-workflow runner on a step timeout (#953
    [7]) to stop an orphaned still-running child."""

    reg = getattr(app.state, "agent_task_registry", None)
    if reg is None:
        return False
    task = reg.get(task_id)
    if task is None:
        return False
    updated = None
    if not task.is_terminal:
        updated = _cancel_one_child_task(app, reg, task)
    descendants = cancel_children_of(app, task.child_session_id)
    if updated is not None:
        _admit_next_queued(app)
    return updated is not None or descendants > 0


def _launch(app: "FastAPI", task: AgentTask, spec: TaskSpec) -> AgentTask:
    """Stage the child turn + wire the completion hook. Transitions queued→running."""

    from clio_agent.gact.turn import _start_background_user_turn  # noqa: PLC0415

    child = app.state.sessions.get(task.child_session_id)
    # workflow_state round-trip: inject the parent's typed state into the child's
    # staged user message so the child sees the shared plan (the child's own state
    # rides back on result.workflow_state at completion).
    text = spec.task_text
    # P1.0 (#1062): a seeded subagent (skill-as-subagent) gets the skill body prepended
    # verbatim to its staged input, so the child runs the skill's procedure in a fresh
    # context instead of the caller inlining the body.
    if spec.seed_context:
        text = f"{spec.seed_context}\n\n---\n\n{text}"
    if spec.workflow_state:
        import json  # noqa: PLC0415

        text = f"{text}\n\n[workflow_state]\n{json.dumps(spec.workflow_state, sort_keys=True, default=str)}"

    _start_background_user_turn(
        app,
        task.child_session_id,
        child,
        text,
        metadata={"agent_task_id": task.task_id, "spawned_by": spec.requesting_expert_id},
        prev_status="idle",
        turn_agent_id=spec.child_expert_id,
    )
    running = app.state.agent_task_registry.transition(
        task.task_id, STATUS_RUNNING, updated_at=_now()
    )
    persist_agent_task(app, running)
    publish_agent_task_event(app, running, AGENT_TASK_EVENTS[STATUS_RUNNING])
    # P2.3 SubagentStart lifecycle hook (observation): fires exactly once when the
    # child turn transitions queued→running (reuses the AgentTask lifecycle).
    from clio_agent.gact.hooks import dispatch_subagent_start  # noqa: PLC0415

    dispatch_subagent_start(
        session_id=task.child_session_id,
        cwd=str(getattr(child, "workspace_root", "") or ""),
        payload={
            "task_id": task.task_id,
            "parent_session_id": task.parent_session_id,
            "child_expert_id": spec.child_expert_id,
            "depth": task.depth,
            "mode": spec.mode,
        },
    )

    child_task = app.state.in_flight_turns.get(task.child_session_id)
    if child_task is not None:
        child_task.add_done_callback(
            lambda _t, tid=task.task_id, csid=task.child_session_id, mode=spec.mode: _on_child_done(
                app, tid, csid, mode
            )
        )
    else:
        # The turn already settled (a very fast child); collect now.
        _on_child_done(app, task.task_id, task.child_session_id, spec.mode)
    return running


def _fire_subagent_stop(app: "FastAPI", updated: AgentTask, child_sid: str) -> None:
    """Fire the ``SubagentStop`` lifecycle hook exactly once at a child's terminal.

    Observation-only (this slice): reuses the AgentTask terminal transition. The
    ``is_terminal`` guard in :func:`_on_child_done` ensures one call per child.
    """

    from clio_agent.gact.hooks import dispatch_subagent_stop  # noqa: PLC0415

    child_sess = app.state.sessions.get(child_sid)
    dispatch_subagent_stop(
        session_id=child_sid,
        cwd=str(getattr(child_sess, "workspace_root", "") or ""),
        payload={
            "task_id": updated.task_id,
            "parent_session_id": updated.parent_session_id,
            "status": updated.status,
            "error_reason": getattr(updated, "error_reason", "") or "",
        },
    )


def _on_child_done(app: "FastAPI", task_id: str, child_sid: str, mode: str) -> None:
    """Completion hook: read the child's terminal message, transition the task to a
    terminal state with a result (message ref + bounded excerpt + workflow_state),
    publish + fire the wait-Event, and admit one queued task into the freed slot."""

    reg = app.state.agent_task_registry
    task = reg.get(task_id)
    if task is None or task.is_terminal:
        return
    now = _now()

    # HITL-in-child (#1113): an unattended child cannot answer its own user question.
    # If its turn paused (waiting_user), FORWARD the pending question to the parent's
    # HITL surface instead of failing (replaces the deleted child_requires_user_input
    # fail path). Every edge terminates typed, nothing hangs: no pending question to
    # forward -> typed terminal now; forwarded -> the task stays in progress but arms a
    # bounded unattended-parent deadline that terminates it typed and frees the slot;
    # a parent answer resumes the child (then _on_child_done runs again at true
    # completion); a parent cancel/decline relays down and fails the task.
    child_sess = app.state.sessions.get(child_sid)
    if child_sess is not None and getattr(child_sess, "status", "") == "waiting_user":
        from clio_agent.gact.child_forward import (  # noqa: PLC0415
            arm_forward_deadline,
            fail_child_task,
        )
        from clio_agent.gact.elicitation_bridge import (  # noqa: PLC0415
            forward_child_question_to_parent,
        )

        forwarded_qid = forward_child_question_to_parent(app, task, child_sid)
        if forwarded_qid is None:
            fail_child_task(app, task, child_sid, "child_question_forward_failed", mode)
        else:
            arm_forward_deadline(app, forwarded_qid)
        return

    msgs = app.state.messages.get(child_sid, []) or []
    finals = [
        m
        for m in msgs
        if getattr(m, "role", "") == "assistant"
        and not (getattr(m, "metadata", {}) or {}).get("live")
    ]
    final = finals[-1] if finals else None
    code = _err_code(getattr(final, "error_info", None) if final is not None else None)

    try:
        if code == "cancelled":
            # A cancelled child is NOT observed-later: cancellation is parent-driven
            # (session cancel cascade), so the parent already knows — no notify.
            outcome = fold_agent_task_transition(
                app, task_id, STATUS_CANCELLED, notify_pending=False, updated_at=now
            )
        elif code:
            outcome = fold_agent_task_transition(
                app,
                task_id,
                STATUS_FAILED,
                error_reason="agent_error",
                notify_pending=(mode == "async"),
                updated_at=now,
            )
        elif final is None:
            outcome = fold_agent_task_transition(
                app,
                task_id,
                STATUS_FAILED,
                error_reason="agent_error",
                notify_pending=(mode == "async"),
                updated_at=now,
            )
        else:
            result = {
                "message_ref": getattr(final, "id", ""),
                "answer_excerpt": _message_text(final)[:_ANSWER_EXCERPT_MAX],
                "workflow_state": _child_workflow_state(app, child_sid, final),
            }
            outcome = fold_agent_task_transition(
                app,
                task_id,
                STATUS_COMPLETED,
                result=result,
                notify_pending=(mode == "async"),
                updated_at=now,
            )
    except Exception:  # noqa: BLE001 - a hook error must not vanish (no-silent-fallback)
        logger.exception("agent_task completion hook failed task=%s child=%s", task_id, child_sid)
        return

    finish_agent_task_transition(app, outcome)


def _child_workflow_state(app: "FastAPI", child_sid: str, final: Any) -> dict[str, Any]:
    """The child's typed workflow_state riding back on the result (empty when none)."""

    meta = getattr(final, "metadata", {}) or {}
    wf = meta.get("workflow_state")
    if isinstance(wf, dict):
        return wf
    sess = app.state.sessions.get(child_sid)
    smeta = getattr(sess, "metadata", {}) or {}
    wf = smeta.get("workflow_state")
    return wf if isinstance(wf, dict) else {}


def _admit_next_queued(app: "FastAPI") -> None:
    """FIFO PER DEPTH: launch queued tasks into every currently-free slot (a cancel
    can free several at once, so admit up to the free-slot count, not just one).

    Each depth has its own pool + its own cap, so admission is counted per depth: a
    freed depth-``d`` slot admits the oldest queued depth-``d`` task, never a
    deeper/shallower one waiting on a different pool (#948 S4 adversarial review)."""

    from dataclasses import replace  # noqa: PLC0415

    reg = app.state.agent_task_registry
    cap = getattr(app.state, "max_concurrent_agent_tasks", 3)
    while True:
        snap = reg.snapshot()
        running_by_depth: dict[int, int] = {}
        running_by_batch: dict[tuple[str, str, int], int] = {}
        for t in snap:
            if t.status == STATUS_RUNNING:
                running_by_depth[t.depth] = running_by_depth.get(t.depth, 0) + 1
                key = _batch_key(
                    t.parent_session_id, t.agent_ref.get("requesting_expert_id", ""), t.depth
                )
                running_by_batch[key] = running_by_batch.get(key, 0) + 1
        queued = sorted((t for t in snap if t.status == STATUS_QUEUED), key=lambda t: t.created_at)
        task = next(
            (t for t in queued if _queued_admissible(t, running_by_depth, running_by_batch, cap)),
            None,
        )
        if task is None:
            return
        child = app.state.sessions.get(task.child_session_id)
        pending = (getattr(child, "metadata", {}) or {}).get("pending_spawn", {}) if child else {}
        spec = TaskSpec(
            child_expert_id=task.agent_ref.get("expert_id", ""),
            task_text=pending.get("task_text", ""),
            parent_session_id=task.parent_session_id,
            requesting_expert_id=task.agent_ref.get("requesting_expert_id", "main"),
            parent_turn_id=task.parent_turn_id,
            depth=task.depth,
            mode=pending.get("mode", "async"),
            workflow_state=pending.get("workflow_state") or None,
            fanout_bound=task.fanout_bound,
            seed_context=pending.get("seed_context", ""),
            skip_declared_check=bool(pending.get("skip_declared_check", False)),
            workspace_id=pending.get("workspace_id"),
            session_mode=pending.get("session_mode"),
            session_scope_metadata=pending.get("session_scope_metadata"),
        )
        reg.register(replace(task, queued_reason=""))  # clear queued_reason as it launches
        _launch(app, reg.get(task.task_id), spec)
