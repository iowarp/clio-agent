"""#1306: the model-facing digest for an oversize completed child output.

``digest_agent_task_output`` is the pure function ``wait_agent_tasks`` calls
on its model-facing row's ``output`` field; ``get_agent_task_output_impl`` is
the recoverability half (a completed task's full text on demand). Both are
unit-tested directly here, independent of the spawn-runtime tool/context
machinery -- the tool-registration + wait-loop integration tests live in
``test_spawn_runtime_s4.py`` beside their siblings.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from clio_agent.gact.agent_tasks import AgentTask, AgentTaskRegistry
from clio_agent.gact.agents.agent_task_output_digest import (
    AGENT_TASK_OUTPUT_OVERSIZE_REASON,
    agent_task_output_digest_chars,
    digest_agent_task_output,
    get_agent_task_output_impl,
)
from clio_agent.gact.types import Message, Part
from tests._config_layer import set_config


def test_default_digest_cap_is_8000_chars() -> None:
    """The in-code default (also what config.defaults.yaml pins)."""

    assert agent_task_output_digest_chars() == 8_000


def test_digest_cap_is_configurable() -> None:
    set_config("limits", {"agent_task_output_digest_chars": 500})
    assert agent_task_output_digest_chars() == 500


def test_output_at_or_under_cap_passes_through_byte_identical() -> None:
    set_config("limits", {"agent_task_output_digest_chars": 100})
    text = "x" * 100
    assert (
        digest_agent_task_output(
            text,
            task_id="task_1",
            child_session_id="child_1",
            message_ref="msg_1",
            answer_excerpt=text[:20],
        )
        == text
    )


def test_output_one_char_over_cap_digests() -> None:
    set_config("limits", {"agent_task_output_digest_chars": 100})
    text = "y" * 101
    result = digest_agent_task_output(
        text,
        task_id="task_1",
        child_session_id="child_1",
        message_ref="msg_1",
        answer_excerpt="y" * 20,
    )
    assert result != text
    envelope = json.loads(result)
    assert envelope["_clio"]["status"] == "digested"
    assert envelope["_clio"]["reason"] == AGENT_TASK_OUTPUT_OVERSIZE_REASON
    assert envelope["_clio"]["original_chars"] == 101


def test_digest_envelope_carries_durable_reference_and_excerpt() -> None:
    set_config("limits", {"agent_task_output_digest_chars": 50})
    result = digest_agent_task_output(
        "z" * 51,
        task_id="task_big",
        child_session_id="child_big",
        message_ref="msg_big",
        answer_excerpt="z" * 20,
    )
    envelope = json.loads(result)
    # The full text is genuinely ABSENT (never a truncated prefix of the raw text
    # hiding under a different key) -- only the already-bounded excerpt rides along.
    assert "z" * 51 not in json.dumps(envelope)
    assert envelope["answer_excerpt"] == "z" * 20
    assert envelope["task_id"] == "task_big"
    assert envelope["child_session_id"] == "child_big"
    assert envelope["message_ref"] == "msg_big"
    assert envelope["fetch_full_output"] == {
        "tool": "get_agent_task_output",
        "args": {"task_id": "task_big"},
    }


def test_digest_never_triggers_on_empty_or_short_answer_excerpt() -> None:
    """A short (earthscope-class) answer never digests -- the #1306 regression."""

    set_config("limits", {"agent_task_output_digest_chars": 8_000})
    short = "the staged CSV has 1024 rows across 4 stations."
    assert (
        digest_agent_task_output(
            short,
            task_id="task_1",
            child_session_id="child_1",
            message_ref="msg_1",
            answer_excerpt=short,
        )
        == short
    )


# --------------------------------------------------------------------------- #
# get_agent_task_output_impl -- the recoverability half.
# --------------------------------------------------------------------------- #


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


def test_get_agent_task_output_returns_full_stored_output() -> None:
    registry = AgentTaskRegistry()
    registry.register(
        AgentTask(
            task_id="task_done",
            parent_session_id="sess_x",
            child_session_id="child_1",
            agent_ref={"expert_id": "researcher", "requesting_expert_id": "main"},
            status="completed",
            result={
                "answer_excerpt": "short excerpt",
                "workflow_state": {},
                "message_ref": "msg_1",
            },
        )
    )
    # No trailing whitespace: _message_text strips it (as it does when minting the
    # durable excerpt), so the verbatim contract is byte-for-byte on the stripped body.
    big = " | ".join(f"line-{i:04d} the full multi-page report" for i in range(500))
    app = _fake_app(registry, {"child_1": [_assistant_message("msg_1", "child_1", big)]})

    result = json.loads(get_agent_task_output_impl(app, "task_done"))

    assert result["task_id"] == "task_done"
    assert result["status"] == "completed"
    assert result["output"] == big


def test_get_agent_task_output_unknown_task_returns_typed_error() -> None:
    app = _fake_app(AgentTaskRegistry(), {})
    result = json.loads(get_agent_task_output_impl(app, "task_ghost"))
    assert result == {"error": "unknown_task", "task_id": "task_ghost"}


def test_get_agent_task_output_non_terminal_task_returns_typed_error() -> None:
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
    result = json.loads(get_agent_task_output_impl(app, "task_running"))
    assert result == {"error": "task_not_terminal", "task_id": "task_running", "status": "running"}
