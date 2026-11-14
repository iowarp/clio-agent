"""Started work is linked to the model tool call that started it (stop-and-restart.md)."""

from __future__ import annotations

from types import SimpleNamespace

from clio_agent.gact.artifacts.observer_bridge import MODEL_CALL_SLOT
from clio_agent.gact.conversation_recovery import _started_by_call
from clio_agent.gact.spawn_invocation import spawn_invocation_id, started_by


def test_the_invocation_carries_the_observer_and_model_call_ids() -> None:
    token = MODEL_CALL_SLOT.set({"call_id": "call_aaa", "model_call_id": "toolu_1"})
    try:
        assert spawn_invocation_id() == "call_aaa~toolu_1"
    finally:
        MODEL_CALL_SLOT.reset(token)


def test_a_reused_provider_call_id_still_gets_its_own_handle() -> None:
    ids = []
    for observer in ("call_aaa", "call_bbb"):
        token = MODEL_CALL_SLOT.set({"call_id": observer, "model_call_id": "call_0"})
        try:
            ids.append(spawn_invocation_id())
        finally:
            MODEL_CALL_SLOT.reset(token)
    assert ids[0] != ids[1]
    assert all(started_by(value, "call_0") for value in ids)


def test_without_a_model_call_the_invocation_is_unlinked_and_unique() -> None:
    first, second = spawn_invocation_id(), spawn_invocation_id()
    assert first != second
    assert "~" not in first


def test_started_by_reads_spawn_prefixes_and_batch_suffixes() -> None:
    assert started_by("flow:call_a~toolu_9", "toolu_9")
    assert started_by("call_a~toolu_9:2", "toolu_9")
    assert not started_by("call_a~toolu_90", "toolu_9")
    assert not started_by("flow:toolu_9", "toolu_9")  # no model part: never guessed
    assert not started_by("call_a~toolu_9", "")


def test_the_uncertainty_note_names_each_started_task() -> None:
    assert _started_by_call([]) == {}
    row = SimpleNamespace(handle="task_1", kind="Subagent", label="council flow", status="active")
    note = _started_by_call([row])
    assert note["started_tasks"] == [
        {"handle": "task_1", "kind": "Subagent", "label": "council flow", "status": "active"}
    ]
    assert "task_1" in note["started_note"] and "not started again" in note["started_note"]


def test_recovery_names_the_flow_an_interrupted_call_started(monkeypatch) -> None:
    """A crash after run_flow accepted, before its observation: the uncertainty names it."""
    import json

    from clio_agent.arc.task_types import ContextBoundary
    from clio_agent.gact import conversation_recovery

    monkeypatch.setattr(
        "clio_agent.gact.agents.clio_react_record._token_estimate", lambda _value: 1
    )
    call = SimpleNamespace(status="live", kind="tool_call", content={"id": "toolu_7"})
    flow = SimpleNamespace(
        handle="task_flow",
        kind="Subagent",
        label="deep-research flow",
        status="active",
        invocation_id="flow:call_x~toolu_7",
        created_at="1",
        owner=SimpleNamespace(session_id="child", parent_handle="task_parent"),
    )
    other = SimpleNamespace(
        handle="task_other",
        kind="MCP",
        label="search",
        status="completed",
        invocation_id="call_y~toolu_8",
        created_at="2",
        owner=SimpleNamespace(session_id="parent", parent_handle="task_parent"),
    )
    inserted: list[dict] = []

    class Arc:
        def context_view(self, _sid, _scope):
            return SimpleNamespace(segments=[call])

        def insert_segment(self, _sid, _scope, _position, _kind, observation, **_):
            inserted.append(observation)

    rows = {"task_flow": flow, "task_other": other}
    history = SimpleNamespace(
        arc=Arc(),
        session_handles=lambda _sid: {"task_parent", "task_other"},
        tree_handles=lambda _sid: set(rows),
        task_snapshot=rows.get,
        confirm_write=lambda write: write(),
    )
    owner = SimpleNamespace(history=history)
    assignment = SimpleNamespace(handle="task_parent", owner=SimpleNamespace(session_id="parent"))
    boundary = ContextBoundary(session_id="parent", scope="s", logical_time=0)
    conversation_recovery._record_interrupted_calls(owner, assignment, boundary)

    body = json.loads(inserted[0]["text"])
    assert body["error"] == "operation_outcome_uncertain" and body["resubmitted"] is False
    assert [row["handle"] for row in body["started_tasks"]] == ["task_flow"]
    assert "deep-research flow" in body["started_note"]
