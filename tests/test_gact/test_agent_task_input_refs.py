"""#1306 review round, finding 1 (the crux): forwarding prior task output as a
NEW child's input evidence, via ``spawn_agent_task``/``spawn_agents_parallel``'s
``input_task_ids``.

``resolve_input_task_evidence`` is the pure validate-then-build function; the
tool-level integration (spawn refused typed, child briefing carries the
labeled evidence) lives in ``test_spawn_runtime_s4.py`` beside its siblings.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from clio_agent.gact.agent_tasks import AgentTask, AgentTaskRegistry
from clio_agent.gact.agents.agent_task_input_refs import resolve_input_task_evidence
from clio_agent.gact.agents.invoker import SpawnError
from clio_agent.gact.types import Message, Part


def _fake_app(registry: AgentTaskRegistry, messages: dict[str, list[Message]]) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(agent_task_registry=registry, messages=messages))


def _assistant_message(msg_id: str, session_id: str, text: str) -> Message:
    return Message(
        id=msg_id,
        session_id=session_id,
        role="assistant",
        created_at="2026-09-03T00:00:00+00:00",
        updated_at="2026-09-03T00:00:00+00:00",
        parts=[Part(type="text", text=text)],
    )


def _completed_task(task_id: str, parent_session_id: str, child_session_id: str) -> AgentTask:
    return AgentTask(
        task_id=task_id,
        parent_session_id=parent_session_id,
        child_session_id=child_session_id,
        agent_ref={"expert_id": "researcher", "requesting_expert_id": "main"},
        status="completed",
        result={"answer_excerpt": "excerpt only", "workflow_state": {}, "message_ref": "msg_1"},
    )


def test_no_input_task_ids_returns_task_text_unchanged() -> None:
    app = _fake_app(AgentTaskRegistry(), {})
    assert resolve_input_task_evidence(app, "sess_x", "do the review", None) == "do the review"
    assert resolve_input_task_evidence(app, "sess_x", "do the review", []) == "do the review"


def test_appends_full_output_as_a_labeled_evidence_block() -> None:
    registry = AgentTaskRegistry()
    registry.register(_completed_task("task_r1", "sess_x", "child_r1"))
    app = _fake_app(
        registry,
        {"child_r1": [_assistant_message("msg_1", "child_r1", "the FULL researcher answer")]},
    )

    result = resolve_input_task_evidence(app, "sess_x", "review this", ["task_r1"])

    assert result.startswith("review this")
    assert "the FULL researcher answer" in result
    assert "task_r1" in result
    assert "child_r1" in result
    assert "researcher" in result


def test_appends_one_block_per_referenced_task_in_order() -> None:
    registry = AgentTaskRegistry()
    registry.register(_completed_task("task_r1", "sess_x", "child_r1"))
    registry.register(_completed_task("task_r2", "sess_x", "child_r2"))
    app = _fake_app(
        registry,
        {
            "child_r1": [_assistant_message("msg_1", "child_r1", "researcher one's full answer")],
            "child_r2": [_assistant_message("msg_1", "child_r2", "researcher two's full answer")],
        },
    )

    result = resolve_input_task_evidence(app, "sess_x", "synthesize both", ["task_r1", "task_r2"])

    assert "researcher one's full answer" in result
    assert "researcher two's full answer" in result
    # Order preserved: task_r1's block precedes task_r2's.
    assert result.index("researcher one's full answer") < result.index(
        "researcher two's full answer"
    )


def test_unknown_task_id_refuses_typed() -> None:
    app = _fake_app(AgentTaskRegistry(), {})
    with pytest.raises(SpawnError) as exc_info:
        resolve_input_task_evidence(app, "sess_x", "review this", ["task_ghost"])
    assert exc_info.value.reason == "task_ref_unknown"


def test_foreign_task_id_refuses_typed() -> None:
    """A task that exists but was spawned by a DIFFERENT session."""

    registry = AgentTaskRegistry()
    registry.register(_completed_task("task_other", "sess_someone_else", "child_1"))
    app = _fake_app(registry, {})
    with pytest.raises(SpawnError) as exc_info:
        resolve_input_task_evidence(app, "sess_x", "review this", ["task_other"])
    assert exc_info.value.reason == "task_ref_not_yours"


def test_incomplete_task_id_refuses_typed() -> None:
    registry = AgentTaskRegistry()
    registry.register(
        AgentTask(
            task_id="task_running",
            parent_session_id="sess_x",
            child_session_id="child_1",
            agent_ref={"expert_id": "researcher", "requesting_expert_id": "main"},
            status="running",
        )
    )
    app = _fake_app(registry, {})
    with pytest.raises(SpawnError) as exc_info:
        resolve_input_task_evidence(app, "sess_x", "review this", ["task_running"])
    assert exc_info.value.reason == "task_ref_not_terminal"


def test_one_bad_id_among_good_ones_refuses_the_whole_batch() -> None:
    """All-or-nothing: a broken reference must not spawn with PARTIAL evidence."""

    registry = AgentTaskRegistry()
    registry.register(_completed_task("task_r1", "sess_x", "child_r1"))
    app = _fake_app(
        registry, {"child_r1": [_assistant_message("msg_1", "child_r1", "full answer")]}
    )
    with pytest.raises(SpawnError) as exc_info:
        resolve_input_task_evidence(app, "sess_x", "review this", ["task_r1", "task_ghost"])
    assert exc_info.value.reason == "task_ref_unknown"


def test_failed_referenced_task_is_terminal_so_its_material_still_forwards() -> None:
    """A failed sibling is still a legitimate reference (terminal, not just
    completed) -- whatever material it produced is still evidence."""

    registry = AgentTaskRegistry()
    registry.register(
        AgentTask(
            task_id="task_failed",
            parent_session_id="sess_x",
            child_session_id="child_1",
            agent_ref={"expert_id": "researcher", "requesting_expert_id": "main"},
            status="failed",
            error_reason="agent_error",
            result={"answer_excerpt": "partial", "workflow_state": {}, "message_ref": "msg_1"},
        )
    )
    app = _fake_app(registry, {"child_1": [_assistant_message("msg_1", "child_1", "partial")]})
    result = resolve_input_task_evidence(app, "sess_x", "review this", ["task_failed"])
    assert "partial" in result
