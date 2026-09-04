"""#1306: the model-facing digest for an oversize completed child output.

``digest_agent_task_output`` is the pure function ``digested_model_row`` (in
turn called from ``wait_agent_tasks``) uses to bound its model-facing row's
``output`` field; ``get_agent_task_output_impl`` is the recoverability half
(a completed task's full text -- or a failed one's stored material -- on
demand). All three are unit-tested directly here, independent of the
spawn-runtime tool/context machinery -- the tool-registration + wait-loop
integration tests live in ``test_spawn_runtime_s4.py`` beside their siblings.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from clio_agent.gact.agent_tasks import AgentTask, AgentTaskRegistry
from clio_agent.gact.agents.agent_task_output_digest import (
    AGENT_TASK_OUTPUT_OVERSIZE_REASON,
    agent_task_output_digest_chars,
    digest_agent_task_output,
    digested_model_row,
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
    result = digest_agent_task_output(
        text,
        task_id="task_1",
        child_session_id="child_1",
        message_ref="msg_1",
        answer_excerpt=text[:20],
    )
    assert result == text
    assert isinstance(result, str)


def test_output_one_char_over_cap_digests() -> None:
    """#1306 review round, finding 3: the digest is a nested dict, NOT a
    pre-encoded JSON string -- the caller's own outer json.dumps (the wait
    loop, via digested_model_row) encodes it exactly once."""

    set_config("limits", {"agent_task_output_digest_chars": 100})
    text = "y" * 101
    envelope = digest_agent_task_output(
        text,
        task_id="task_1",
        child_session_id="child_1",
        message_ref="msg_1",
        answer_excerpt="y" * 20,
    )
    assert envelope != text
    assert isinstance(envelope, dict)
    assert envelope["_clio"]["status"] == "digested"
    assert envelope["_clio"]["reason"] == AGENT_TASK_OUTPUT_OVERSIZE_REASON
    assert envelope["_clio"]["original_chars"] == 101


def test_digest_envelope_carries_durable_reference_and_excerpt() -> None:
    set_config("limits", {"agent_task_output_digest_chars": 50})
    envelope = digest_agent_task_output(
        "z" * 51,
        task_id="task_big",
        child_session_id="child_big",
        message_ref="msg_big",
        answer_excerpt="z" * 20,
    )
    assert isinstance(envelope, dict)
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


def test_digest_envelope_encodes_once_through_an_outer_dumps() -> None:
    """The crux of finding 3: json.dumps'ing a dict CONTAINING the envelope
    parses back to the SAME structure -- no double-escaped blob, and a
    quote/newline-dense excerpt does not inflate past the cap that produced
    it (the exact bounded_model_tool_result lesson)."""

    set_config("limits", {"agent_task_output_digest_chars": 20})
    dense_excerpt = 'quotes "like this" and\nnewlines\tand\\backslashes'
    envelope = digest_agent_task_output(
        "w" * 21,
        task_id="task_dense",
        child_session_id="child_dense",
        message_ref="msg_dense",
        answer_excerpt=dense_excerpt,
    )
    outer = json.dumps({"results": [{"output": envelope}]})
    round_tripped = json.loads(outer)
    assert round_tripped["results"][0]["output"] == envelope
    assert round_tripped["results"][0]["output"]["answer_excerpt"] == dense_excerpt


def test_digest_never_triggers_on_empty_or_short_answer_excerpt() -> None:
    """A short (earthscope-class) answer never digests -- the #1306 regression."""

    set_config("limits", {"agent_task_output_digest_chars": 8_000})
    short = "the staged CSV has 1024 rows across 4 stations."
    result = digest_agent_task_output(
        short,
        task_id="task_1",
        child_session_id="child_1",
        message_ref="msg_1",
        answer_excerpt=short,
    )
    assert result == short


def test_none_output_coerces_to_empty_string_not_crash() -> None:
    """#1306 review round, finding 5: AgentTask.from_session can rebuild a
    malformed/legacy record's answer_excerpt (and therefore, via
    _resolve_verbatim_output's fallback, output) as a literal None. A bare
    len(None) would kill the WHOLE wait_agent_tasks collect batch on one bad
    row -- coerce instead of crash."""

    set_config("limits", {"agent_task_output_digest_chars": 100})
    result = digest_agent_task_output(
        None,  # type: ignore[arg-type]
        task_id="task_1",
        child_session_id="child_1",
        message_ref="msg_1",
        answer_excerpt=None,  # type: ignore[arg-type]
    )
    assert result == ""


def test_none_answer_excerpt_coerces_to_empty_string_in_envelope() -> None:
    set_config("limits", {"agent_task_output_digest_chars": 10})
    envelope = digest_agent_task_output(
        "v" * 11,
        task_id="task_1",
        child_session_id="child_1",
        message_ref="msg_1",
        answer_excerpt=None,  # type: ignore[arg-type]
    )
    assert isinstance(envelope, dict)
    assert envelope["answer_excerpt"] == ""
    assert envelope["_clio"]["excerpt_chars"] == 0


# --------------------------------------------------------------------------- #
# digested_model_row -- the owner-module helper the wait loop calls (finding 8).
# --------------------------------------------------------------------------- #


def test_digested_model_row_passes_through_under_cap() -> None:
    set_config("limits", {"agent_task_output_digest_chars": 8_000})
    payload = {"output": "short answer", "message_ref": "msg_1", "task_id": "task_1"}
    task_result = SimpleNamespace(
        task_id="task_1",
        child_session_id="child_1",
        result={"answer_excerpt": "short answer"},
    )
    row = digested_model_row(payload, task_result)
    assert row["output"] == "short answer"
    assert row["message_ref"] == "msg_1"


def test_digested_model_row_digests_over_cap() -> None:
    set_config("limits", {"agent_task_output_digest_chars": 20})
    payload = {"output": "x" * 21, "message_ref": "msg_1", "task_id": "task_1"}
    task_result = SimpleNamespace(
        task_id="task_1",
        child_session_id="child_1",
        result={"answer_excerpt": "x" * 10},
    )
    row = digested_model_row(payload, task_result)
    assert isinstance(row["output"], dict)
    assert row["output"]["_clio"]["status"] == "digested"
    assert row["output"]["child_session_id"] == "child_1"


# --------------------------------------------------------------------------- #
# get_agent_task_output_impl -- the recoverability half.
# --------------------------------------------------------------------------- #


def _fake_app(
    registry: AgentTaskRegistry | None, messages: dict[str, list[Message]]
) -> SimpleNamespace:
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
    assert result["error_reason"] == ""


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


def test_get_agent_task_output_missing_registry_is_its_own_typed_reason() -> None:
    """#1306 review round, finding 6: a missing registry is an infrastructure
    gap, not "no such task" -- collapsing it into unknown_task would tell the
    model to give up on a reference that may well exist."""

    app = _fake_app(None, {})
    result = json.loads(get_agent_task_output_impl(app, "task_1"))
    assert result == {"error": "registry_unavailable", "task_id": "task_1"}


def test_get_agent_task_output_failed_task_returns_material_not_refusal() -> None:
    """#1306 review round, finding 9: the chosen contract is FULL stored
    material (whatever answer text existed, plus the typed error_reason) for
    a FAILED task -- never a refusal. Only unknown/non-terminal refuse."""

    registry = AgentTaskRegistry()
    registry.register(
        AgentTask(
            task_id="task_bad",
            parent_session_id="sess_x",
            child_session_id="child_1",
            agent_ref={"expert_id": "researcher", "requesting_expert_id": "main"},
            status="failed",
            error_reason="agent_error",
            result={
                "answer_excerpt": "partial draft before it failed",
                "workflow_state": {},
                "message_ref": "msg_1",
            },
        )
    )
    app = _fake_app(
        registry,
        {"child_1": [_assistant_message("msg_1", "child_1", "partial draft before it failed")]},
    )
    result = json.loads(get_agent_task_output_impl(app, "task_bad"))
    assert "error" not in result
    assert result["status"] == "failed"
    assert result["output"] == "partial draft before it failed"
    assert result["error_reason"] == "agent_error"


def test_error_responses_do_not_escape_non_ascii() -> None:
    """#1306 review round, finding 4: ensure_ascii=False on every json.dumps in
    this module -- the default \\uXXXX-escapes non-ASCII up to 6x on the
    largest payload in the system."""

    registry = AgentTaskRegistry()
    registry.register(
        AgentTask(
            task_id="task_unicode",
            parent_session_id="sess_x",
            child_session_id="child_1",
            agent_ref={"expert_id": "researcher", "requesting_expert_id": "main"},
            status="completed",
            result={"answer_excerpt": "", "workflow_state": {}, "message_ref": "msg_1"},
        )
    )
    text = "éèê 中文 \U0001f680"
    app = _fake_app(registry, {"child_1": [_assistant_message("msg_1", "child_1", text)]})
    raw = get_agent_task_output_impl(app, "task_unicode")
    assert "\\u" not in raw
    assert text in raw
