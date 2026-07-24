"""#1035 (epic #1031 Pillar 2): loop-inbox core — structure, drain carrier, Producer A.

Covers the four invariants of the mid-turn wake slice:

* :class:`LoopInbox` put/drain under concurrent writers + the typed overflow drop
  (no silent loss — the next-turn ``notify_pending`` fallback still carries it).
* :func:`drain_active_session_inbox` composes a ``_notify_block`` and marks the
  completion consumed through the EXISTING once-gate, so a mid-turn drain and the
  next-turn injection never double-surface the same task (BOTH orders).
* The tool-executor carrier appends the drained block to the model-observation
  string return but NOT the raw path, and cancellation precedes injection.
* Producer A enqueues only when the parent is busy and never raises.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from clio_agent.errors import CancellationError
from clio_agent.gact import context as ctx
from clio_agent.gact.agent_tasks import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    AgentTask,
    consume_notification,
    pending_notifications,
    persist_agent_task,
)
from clio_agent.gact.app import build_app
from clio_agent.gact.enrichment import (
    PENDING_TASK_NOTIFICATION_MARKER,
    inject_pending_agent_task_notifications,
)
from clio_agent.gact.loop_inbox import (
    InboxEvent,
    LoopInbox,
    drain_active_session_inbox,
    enqueue_completion_wake,
    inbox_for,
)
from clio_agent.gact.runtime.globals import _gact_app_context
from clio_agent.tools.execution import (
    SyncMCPToolExecutor,
    ToolRuntimeHooks,
    set_tool_runtime_fallback,
)

pytestmark = pytest.mark.usefixtures("host_agent_executor")


# --------------------------------------------------------------------------- #
# 1. LoopInbox structure — put / drain / peek / concurrency / overflow         #
# --------------------------------------------------------------------------- #


def test_put_drain_roundtrip() -> None:
    inbox = LoopInbox()
    assert inbox.peek_nonempty() is False
    inbox.put(InboxEvent(kind="child_completed", task_id="t1"))
    inbox.put(InboxEvent(kind="child_failed", task_id="t2"))
    assert inbox.peek_nonempty() is True
    events = inbox.drain()
    assert [(e.kind, e.task_id) for e in events] == [
        ("child_completed", "t1"),
        ("child_failed", "t2"),
    ]
    # Drain is pop-all: a second drain is empty.
    assert inbox.drain() == []
    assert inbox.peek_nonempty() is False


def test_concurrent_writers_no_loss() -> None:
    """Many writer threads racing put must lose nothing under the RLock (total
    stays within the bound so every event survives)."""

    inbox = LoopInbox(maxlen=4096)
    writers = 16
    per_writer = 100
    barrier = threading.Barrier(writers)

    def _write(w: int) -> None:
        barrier.wait()
        for i in range(per_writer):
            inbox.put(InboxEvent(kind="child_completed", task_id=f"w{w}-{i}"))

    threads = [threading.Thread(target=_write, args=(w,)) for w in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    events = inbox.drain()
    assert len(events) == writers * per_writer
    # No duplication / corruption: every id is distinct.
    assert len({e.task_id for e in events}) == writers * per_writer


def test_overflow_drops_oldest_with_typed_reason(caplog: pytest.LogCaptureFixture) -> None:
    """On overflow the OLDEST is dropped and a TYPED reason is logged (never
    silent); the surviving window is the most-recent ``maxlen`` events."""

    inbox = LoopInbox(maxlen=2)
    inbox.put(InboxEvent(kind="child_completed", task_id="oldest"))
    inbox.put(InboxEvent(kind="child_completed", task_id="mid"))
    with caplog.at_level("WARNING", logger="clio_agent.gact.loop_inbox"):
        inbox.put(InboxEvent(kind="child_completed", task_id="newest"))

    assert "reason=inbox_full" in caplog.text
    assert "dropped_task=oldest" in caplog.text
    surviving = [e.task_id for e in inbox.drain()]
    assert surviving == ["mid", "newest"]


# --------------------------------------------------------------------------- #
# Shared app helpers (mirror test_async_observe_s6)                             #
# --------------------------------------------------------------------------- #


@contextmanager
def _active_turn(app: Any, session_id: str) -> Iterator[None]:
    with _gact_app_context(app):
        token = ctx.set_session_id(session_id)
        try:
            yield
        finally:
            ctx.reset(token)


def _seed_terminal_task(
    app: Any,
    parent_sid: str,
    *,
    status: str = STATUS_COMPLETED,
    excerpt: str = "the staged CSV is ready",
    error_reason: str = "",
    task_id: str = "task_seed",
) -> AgentTask:
    """Mint a REAL child session + a terminal, notify-pending AgentTask over it."""

    child = app.state.sessions.create(
        workspace_id="ws_default", title="c", parent_session_id=parent_sid
    )
    task = AgentTask(
        task_id=task_id,
        parent_session_id=parent_sid,
        child_session_id=child.id,
        agent_ref={"expert_id": "data_expert", "requesting_expert_id": "main"},
        status=status,
        error_reason=error_reason,
        notify_pending=True,
        result={"answer_excerpt": excerpt, "message_ref": "msg_x", "workflow_state": {}},
        created_at="2026-07-19T00:00:00+00:00",
        updated_at="2026-07-19T00:00:00+00:00",
    )
    persist_agent_task(app, task)
    return task


def _bus(app: Any, sid: str, etype: str) -> list[Any]:
    return [e for e in app.state.bus._history.get(sid, []) if e.type == etype]


# --------------------------------------------------------------------------- #
# 2. drain_active_session_inbox — compose + once-gate (BOTH orders)            #
# --------------------------------------------------------------------------- #


def test_drain_composes_block_marks_consumed_then_inject_empty(tmp_path: Path) -> None:
    """Order A: drain first. It composes a _notify_block, marks the completion
    consumed, publishes a parent-session progress event, and the SUBSEQUENT
    next-turn injection finds nothing (no double-surface)."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=None)
    with TestClient(app):
        parent = app.state.sessions.create(workspace_id="ws_default", title="p").id
        task = _seed_terminal_task(app, parent, task_id="task_seed")
        inbox_for(app, parent).put(InboxEvent(kind="child_completed", task_id=task.task_id))

        with _active_turn(app, parent):
            block = drain_active_session_inbox(app)

        assert PENDING_TASK_NOTIFICATION_MARKER in block
        assert "task_seed" in block
        assert "the staged CSV is ready" in block
        # Watchdog liveness: a progress event was published on the PARENT session.
        assert _bus(app, parent, "loop_inbox.drained"), "drain must publish parent liveness"
        # Once-gate: the task is now consumed, so the next-turn injection is empty.
        assert pending_notifications(app, parent) == []
        text, ids = inject_pending_agent_task_notifications(app, parent, "BASE")
        assert ids == []
        assert text == "BASE"


def test_drain_emits_delegation_terminal_no_dangle(tmp_path: Path) -> None:
    """A mid-turn-drained fire-and-forget completion must emit the delegation TERMINAL —
    it claims ``delegation_reported`` and runs the SAME choreography wait/check/next-turn
    emit (completed + return Part + parent_resumed) — not merely clear ``notify_pending``.
    Otherwise the child dangles started-with-no-terminal (perpetually in-progress) because
    the next-turn path now skips the already-consumed task. The claim is once-gated, so a
    later commit path never double-emits."""

    from clio_agent.gact.enrichment import consume_pending_agent_task_notifications

    app = build_app(sessions_path=tmp_path / "s.json", agent=None)
    with TestClient(app):
        parent = app.state.sessions.create(workspace_id="ws_default", title="p").id
        task = _seed_terminal_task(app, parent, task_id="task_seed")
        reg = app.state.agent_task_registry
        assert reg.get(task.task_id).delegation_reported is False
        inbox_for(app, parent).put(InboxEvent(kind="child_completed", task_id=task.task_id))

        with _active_turn(app, parent):
            drain_active_session_inbox(app)

        # The drain claimed + emitted the delegation terminal (no dangling started-only).
        assert reg.get(task.task_id).delegation_reported is True, (
            "mid-turn drain must emit the delegation terminal, not just consume notify_pending"
        )
        # Once-gated: a later next-turn commit for the same task is a no-op (no double-emit /
        # no re-claim), proving exactly-once terminal in either order.
        consume_pending_agent_task_notifications(app, parent, [task.task_id])
        assert reg.get(task.task_id).delegation_reported is True


def test_inject_consumes_first_then_drain_is_empty(tmp_path: Path) -> None:
    """Order B: another consumer (wait/check/inject all funnel through
    consume_notification) claims the task first; the mid-turn drain then finds it
    already consumed and surfaces nothing."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=None)
    with TestClient(app):
        parent = app.state.sessions.create(workspace_id="ws_default", title="p").id
        task = _seed_terminal_task(app, parent, task_id="task_seed")
        inbox_for(app, parent).put(InboxEvent(kind="child_completed", task_id=task.task_id))

        # A prior consumer claims the notification via the shared once-gate.
        claimed = consume_notification(app, task.task_id)
        assert claimed is not None and claimed.notify_pending is False

        with _active_turn(app, parent):
            block = drain_active_session_inbox(app)
        assert block == "", "an already-consumed completion must not be re-surfaced"


def test_drain_no_active_session_returns_empty(tmp_path: Path) -> None:
    """A drain with no active session (app-less boundary) returns "" and never raises."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=None)
    with TestClient(app):
        with _gact_app_context(app):
            assert drain_active_session_inbox(app) == ""


# --------------------------------------------------------------------------- #
# 3. Carrier — string return appends, raw path bypasses, cancel wins           #
# --------------------------------------------------------------------------- #


class _FakeClient:
    """Minimal async client shape used by the sync executor."""

    def __init__(self) -> None:
        self.entered = False
        self.exited = False
        self.started_call = False

    async def __aenter__(self) -> "_FakeClient":
        self.entered = True
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.exited = True

    async def list_tools(self) -> list[Any]:
        return [
            SimpleNamespace(
                name="fake_echo",
                description="Echo a value.",
                inputSchema={"properties": {"value": {"type": "string"}}},
            )
        ]

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        self.started_call = True
        return SimpleNamespace(data={"name": name, "args": args})

    async def read_resource(self, uri: str) -> Any:
        return [SimpleNamespace(uri=uri, mimeType="text/plain", text="resource")]


def test_carrier_appends_drain_block_to_model_string() -> None:
    """The string (model-observation) return has the drained block appended."""

    executor = SyncMCPToolExecutor(object(), timeout=1.0, client_factory=lambda _: _FakeClient())
    drain_calls = {"n": 0}

    def _drain() -> str:
        drain_calls["n"] += 1
        return "MID_TURN_WAKE_BLOCK"

    try:
        set_tool_runtime_fallback(ToolRuntimeHooks(loop_inbox_drain=_drain))
        result = executor.call_tool("fake_echo", {"value": "hi"})
        assert '"name": "fake_echo"' in result
        assert result.endswith("\n\nMID_TURN_WAKE_BLOCK")
        assert drain_calls["n"] == 1
    finally:
        set_tool_runtime_fallback(ToolRuntimeHooks())
        executor.close()


def test_carrier_no_drain_appended_when_empty() -> None:
    """A drain that returns "" leaves the observation string untouched (no
    trailing separator)."""

    executor = SyncMCPToolExecutor(object(), timeout=1.0, client_factory=lambda _: _FakeClient())
    try:
        set_tool_runtime_fallback(ToolRuntimeHooks(loop_inbox_drain=lambda: ""))
        result = executor.call_tool("fake_echo", {"value": "hi"})
        assert not result.endswith("\n\n")
        assert "MID_TURN" not in result
    finally:
        set_tool_runtime_fallback(ToolRuntimeHooks())
        executor.close()


def test_carrier_raw_path_does_not_append_or_drain() -> None:
    """The return_raw path (MCP Apps bridge) is NOT the model lane: no drain
    append, and the drain is not even invoked."""

    executor = SyncMCPToolExecutor(object(), timeout=1.0, client_factory=lambda _: _FakeClient())
    drain_calls = {"n": 0}

    def _drain() -> str:
        drain_calls["n"] += 1
        return "MID_TURN_WAKE_BLOCK"

    try:
        set_tool_runtime_fallback(ToolRuntimeHooks(loop_inbox_drain=_drain))
        raw = executor.call_tool_result("fake_echo", {"value": "hi"})
        # Raw object, not the appended model string.
        assert not isinstance(raw, str)
        assert drain_calls["n"] == 0
    finally:
        set_tool_runtime_fallback(ToolRuntimeHooks())
        executor.close()


def test_cancel_precedes_injection() -> None:
    """A cancellation after the tool returns raises BEFORE the drain runs — cancel
    always beats injection, and the drain is never invoked."""

    executor = SyncMCPToolExecutor(object(), timeout=1.0, client_factory=lambda _: _FakeClient())
    checks = iter([False, True])  # before-stage passes, after-stage cancels
    drain_calls = {"n": 0}

    def _drain() -> str:
        drain_calls["n"] += 1
        return "MID_TURN_WAKE_BLOCK"

    try:
        set_tool_runtime_fallback(
            ToolRuntimeHooks(
                cancellation_checker=lambda: next(checks, True),
                loop_inbox_drain=_drain,
            )
        )
        with pytest.raises(CancellationError, match="tool call cancelled"):
            executor.call_tool("fake_echo", {"value": "late-cancel"})
        assert drain_calls["n"] == 0, "cancel must short-circuit before the drain"
    finally:
        set_tool_runtime_fallback(ToolRuntimeHooks())
        executor.close()


# --------------------------------------------------------------------------- #
# 4. Producer A — enqueue only when busy, never raise                          #
# --------------------------------------------------------------------------- #


def _wake_app(*, parent_sid: str, busy: bool, get: Any = None) -> Any:
    task_current = SimpleNamespace(parent_session_id=parent_sid)
    registry = SimpleNamespace(get=get or (lambda _tid: task_current))
    runner = SimpleNamespace(busy=lambda _sid: busy)
    state = SimpleNamespace(agent_task_registry=registry, turn_runner=runner, loop_inboxes={})
    return SimpleNamespace(state=state)


def test_producer_a_enqueues_completed_when_busy() -> None:
    app = _wake_app(parent_sid="sess_p", busy=True)
    enqueue_completion_wake(app, SimpleNamespace(status=STATUS_COMPLETED, task_id="t1"))
    events = app.state.loop_inboxes["sess_p"].drain()
    assert [(e.kind, e.task_id) for e in events] == [("child_completed", "t1")]


def test_producer_a_enqueues_failed_when_busy() -> None:
    app = _wake_app(parent_sid="sess_p", busy=True)
    enqueue_completion_wake(app, SimpleNamespace(status=STATUS_FAILED, task_id="t2"))
    events = app.state.loop_inboxes["sess_p"].drain()
    assert [(e.kind, e.task_id) for e in events] == [("child_failed", "t2")]


def test_producer_a_skips_when_parent_idle() -> None:
    app = _wake_app(parent_sid="sess_p", busy=False)
    enqueue_completion_wake(app, SimpleNamespace(status=STATUS_COMPLETED, task_id="t1"))
    assert app.state.loop_inboxes == {}, "no mid-turn wake when the parent is idle"


def test_producer_a_skips_non_notify_terminal() -> None:
    """A non-completed/failed terminal (e.g. still running / cancelled) is not a
    mid-turn wake."""

    app = _wake_app(parent_sid="sess_p", busy=True)
    enqueue_completion_wake(app, SimpleNamespace(status=STATUS_RUNNING, task_id="t1"))
    enqueue_completion_wake(app, SimpleNamespace(status="cancelled", task_id="t2"))
    assert app.state.loop_inboxes == {}


def test_producer_a_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    """A registry fault in the wake path is caught + logged with a typed reason,
    never raised into the child completion callback."""

    def _boom(_tid: str) -> Any:
        raise RuntimeError("registry exploded")

    app = _wake_app(parent_sid="sess_p", busy=True, get=_boom)
    with caplog.at_level("WARNING", logger="clio_agent.gact.loop_inbox"):
        enqueue_completion_wake(app, SimpleNamespace(status=STATUS_COMPLETED, task_id="t1"))
    assert "reason=wake_enqueue_error" in caplog.text
