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

    import threading  # noqa: PLC0415

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


def spawn_child_turn(app: "FastAPI", spec: TaskSpec) -> AgentTask:
    """Spawn ``spec``'s declared child expert as a real child turn; return its
    :class:`AgentTask` record (already ``running``, or ``queued`` at the cap).

    Must be called on the app event loop (S3): it stages a turn via the turn
    runner. S5's model-facing tools call it cross-thread via the executor seam.
    """

    from clio_agent.gact.agents.resolution import _runtime_declared_child_ids  # noqa: PLC0415

    # ---- structural guards -------------------------------------------------
    if spec.depth > MAX_SPAWN_DEPTH:
        raise SpawnError(
            f"spawn depth {spec.depth} exceeds max {MAX_SPAWN_DEPTH}",
            reason="spawn_depth_exceeded",
        )
    declared = _runtime_declared_child_ids(
        app, spec.requesting_expert_id, session_id=spec.parent_session_id
    )
    if spec.child_expert_id not in declared:
        raise SpawnError(
            f"{spec.child_expert_id!r} is not a declared child of "
            f"{spec.requesting_expert_id!r} (declared: {sorted(declared)})",
            reason="undeclared_child",
        )

    # ---- backpressure: queue (never fail) at the cap ----------------------
    # PER-DEPTH admission: each depth has its own pool, so the cap is counted
    # against RUNNING tasks at the SAME depth. A depth-2 fan-out never queues
    # behind depth-1 siblings on a different pool (#948 S4 adversarial review).
    reg = app.state.agent_task_registry
    cap = getattr(app.state, "max_concurrent_agent_tasks", 3)
    running = sum(1 for t in reg.snapshot() if t.status == STATUS_RUNNING and t.depth == spec.depth)
    at_cap = running >= cap

    # ---- mint the child session (authoritative store) ----------------------
    parent = app.state.sessions.get(spec.parent_session_id)
    workspace_id = getattr(parent, "workspace_id", "ws_default") if parent else "ws_default"
    child = app.state.sessions.create(
        workspace_id=workspace_id,
        title=f"{spec.child_expert_id} task",
        parent_session_id=spec.parent_session_id,
        agent={"id": spec.child_expert_id, "mode": "subagent"},
    )
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
        status=STATUS_QUEUED,
        queued_reason="concurrency_cap" if at_cap else "",
        created_at=now,
        updated_at=now,
    )
    persist_agent_task(app, task)
    # Persist the launch data on the child session so a queued task can be launched
    # faithfully later (the AgentTask record deliberately carries no task_text).
    app.state.sessions.update(
        child.id,
        metadata_patch={
            "pending_spawn": {
                "task_text": spec.task_text,
                "workflow_state": spec.workflow_state or {},
                "mode": spec.mode,
            }
        },
    )
    publish_agent_task_event(app, task, AGENT_TASK_EVENTS[STATUS_QUEUED])
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


def cancel_children_of(app: "FastAPI", parent_session_id: str) -> int:
    """Cancel every non-terminal child task of ``parent_session_id`` (the cancel
    cascade): cooperatively + hard-cancel each child's in-flight turn and mark the
    task cancelled. Returns the count cancelled. Called when a parent turn/task is
    cancelled so children never outlive the parent that spawned them."""

    reg = getattr(app.state, "agent_task_registry", None)
    if reg is None:
        return 0
    n = 0
    for task in reg.for_parent(parent_session_id):
        if task.is_terminal:
            continue
        child_sid = task.child_session_id
        app.state.cancel_flags.add(child_sid)
        event = app.state.cancel_events.get(child_sid)
        if event is not None:
            event.set()
        in_flight = app.state.in_flight_turns.get(child_sid)
        if in_flight is not None and not in_flight.done():
            in_flight.cancel()
        try:
            updated = reg.transition(task.task_id, STATUS_CANCELLED, updated_at=_now())
        except Exception:  # noqa: BLE001 - already terminal via a racing completion
            continue
        persist_agent_task(app, updated)
        publish_agent_task_event(app, updated, AGENT_TASK_EVENTS[STATUS_CANCELLED])
        n += 1
    # Cancelling running children frees concurrency slots — admit queued tasks
    # (possibly of OTHER parents) into them, else they strand forever (the
    # completion hook won't: a cancelled task is already terminal when its
    # done-callback fires, so it early-returns before the admission).
    if n:
        _admit_next_queued(app)
    return n


def _launch(app: "FastAPI", task: AgentTask, spec: TaskSpec) -> AgentTask:
    """Stage the child turn + wire the completion hook. Transitions queued→running."""

    from clio_agent.gact.turn import _start_background_user_turn  # noqa: PLC0415

    child = app.state.sessions.get(task.child_session_id)
    # workflow_state round-trip: inject the parent's typed state into the child's
    # staged user message so the child sees the shared plan (the child's own state
    # rides back on result.workflow_state at completion).
    text = spec.task_text
    if spec.workflow_state:
        import json  # noqa: PLC0415

        text = f"{spec.task_text}\n\n[workflow_state]\n{json.dumps(spec.workflow_state, sort_keys=True, default=str)}"

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


def _on_child_done(app: "FastAPI", task_id: str, child_sid: str, mode: str) -> None:
    """Completion hook: read the child's terminal message, transition the task to a
    terminal state with a result (message ref + bounded excerpt + workflow_state),
    publish + fire the wait-Event, and admit one queued task into the freed slot."""

    reg = app.state.agent_task_registry
    task = reg.get(task_id)
    if task is None or task.is_terminal:
        return
    now = _now()

    # HITL-in-child: an unattended child cannot answer its own permission / user
    # question. If its turn paused (waiting_user), FAIL the task with a typed reason
    # rather than leave it hanging — the parent (the model) decides how to proceed.
    child_sess = app.state.sessions.get(child_sid)
    if child_sess is not None and getattr(child_sess, "status", "") == "waiting_user":
        try:
            updated = reg.transition(
                task_id, STATUS_FAILED, error_reason="child_requires_user_input", updated_at=now
            )
        except Exception:  # noqa: BLE001
            updated = reg.get(task_id) or task
        persist_agent_task(app, updated)
        publish_agent_task_event(app, updated, AGENT_TASK_EVENTS[updated.status])
        _admit_next_queued(app)
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
            updated = reg.transition(task_id, STATUS_CANCELLED, updated_at=now)
        elif code:
            updated = reg.transition(
                task_id, STATUS_FAILED, error_reason="agent_error", updated_at=now
            )
        elif final is None:
            updated = reg.transition(
                task_id, STATUS_FAILED, error_reason="agent_error", updated_at=now
            )
        else:
            result = {
                "message_ref": getattr(final, "id", ""),
                "answer_excerpt": _message_text(final)[:_ANSWER_EXCERPT_MAX],
                "workflow_state": _child_workflow_state(app, child_sid, final),
            }
            updated = reg.transition(
                task_id,
                STATUS_COMPLETED,
                result=result,
                notify_pending=(mode == "async"),
                updated_at=now,
            )
    except Exception:  # noqa: BLE001 - a hook error must not vanish (no-silent-fallback)
        logger.exception("agent_task completion hook failed task=%s child=%s", task_id, child_sid)
        updated = reg.get(task_id) or task

    persist_agent_task(app, updated)
    publish_agent_task_event(app, updated, AGENT_TASK_EVENTS[updated.status])
    _admit_next_queued(app)


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
        for t in snap:
            if t.status == STATUS_RUNNING:
                running_by_depth[t.depth] = running_by_depth.get(t.depth, 0) + 1
        queued = sorted((t for t in snap if t.status == STATUS_QUEUED), key=lambda t: t.created_at)
        task = next((t for t in queued if running_by_depth.get(t.depth, 0) < cap), None)
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
        )
        reg.register(replace(task, queued_reason=""))  # clear queued_reason as it launches
        _launch(app, reg.get(task.task_id), spec)
