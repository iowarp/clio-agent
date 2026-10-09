"""Observability must reflect the completion text actually committed to a model."""

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.enrichment import _estimate_context_tokens, _record_context_frame
from clio_agent.gact.mcp_task_store import SessionMetadataTaskStore
from clio_agent.gact.task_controls import wait_tasks
from clio_agent.gact.task_delivery import commit_staged_completions, pending_completions
from clio_agent.gact.types import Message
from clio_agent.tools.mcp_tasks import utcnow_iso
from tests.test_gact.test_shared_task_controls import record

pytest_plugins = ("tests.test_gact.test_shared_task_controls",)


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
