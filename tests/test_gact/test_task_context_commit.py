"""Observability must reflect the completion text actually committed to a model."""

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import context
from clio_agent.gact.agent_tasks import AgentTaskRegistry
from clio_agent.gact.enrichment import _estimate_context_tokens, _record_context_frame
from clio_agent.gact.events import EventBus
from clio_agent.gact.mcp_task_store import SessionMetadataTaskStore
from clio_agent.gact.sessions import SessionStore
from clio_agent.gact.task_controls import wait_tasks
from clio_agent.gact.task_delivery import commit_staged_completions, pending_completions
from clio_agent.gact.types import Message
from clio_agent.tools.mcp_tasks import utcnow_iso
from tests.test_gact.test_shared_task_controls import record


@pytest.fixture
def scoped_app(tmp_path: Path) -> Iterator[tuple[Any, str, SessionMetadataTaskStore]]:
    """Keep real-store fixture ownership local under full-suite/sharded collection."""
    sessions = SessionStore(path=tmp_path / "sessions.json")
    sid = sessions.create(workspace_id="ws", title="parent").id
    store = SessionMetadataTaskStore(sessions)
    app = SimpleNamespace(
        state=SimpleNamespace(
            sessions=sessions,
            agent_task_registry=AgentTaskRegistry(),
            mcp_task_store=store,
            bus=EventBus(),
            cancel_flags=set(),
        )
    )
    app_token, sid_token = context.set_app(app), context.set_session_id(sid)
    try:
        yield app, sid, store
    finally:
        context.reset(sid_token)
        context.reset(app_token)


@pytest.mark.parametrize("collected", [True, False], ids=["collected", "delivered"])
def test_context_frame_matches_completion_commit(
    scoped_app: tuple[Any, str, SessionMetadataTaskStore], collected: bool
) -> None:
    """Real staging/collection retains other context and refreshes its task-only estimate."""
    app, sid, store = scoped_app
    record(store, sid, "staged", status="completed")
    block, handles = pending_completions(app, sid)
    now = utcnow_iso()
    user = Message(id="user", session_id=sid, role="user", created_at=now, updated_at=now)
    app.state.messages = {sid: [user]}
    app.state.context_files = {}
    app.state.context_frames = {}
    frame = _record_context_frame(
        app,
        sid,
        app.state.sessions.get(sid),
        user,
        user_text="Continue",
        enriched_text="Continue",
        context_error=None,
        injections=[("todos", "keep"), ("task_results", block)],
    )
    identity, original_tokens = frame["id"], frame["tokens_estimated"]
    state = SimpleNamespace(
        app=app,
        sid=sid,
        pending_notification_task_ids=handles,
        injections=[("todos", "keep"), ("task_results", block)],
        context_frame=frame,
    )
    if collected:
        assert wait_tasks("staged", timeout_s=0)["results"]
    commit_staged_completions(state)
    assert frame is app.state.context_frames[sid][0] and frame["id"] == identity
    assert any(
        item["source_id"] == "todos" and item["metadata"]["text"] == "keep"
        for item in frame["items"]
    )
    task_items = [item for item in frame["items"] if item["source_id"] == "task_results"]
    if collected:
        assert not task_items
        assert state.injections == [("todos", "keep")]
        assert frame["tokens_estimated"] == original_tokens - _estimate_context_tokens(block)
    else:
        assert len(task_items) == 1
        actual = dict(state.injections)["task_results"]
        assert task_items[0]["metadata"]["text"] == actual
        assert task_items[0]["tokens_estimated"] == _estimate_context_tokens(actual)
    assert frame["metadata"]["task_delivery_phase"] == "committed"
