"""Tests for the terminal-responder settle logic (#736).

The delegation settle loop must END the turn at the declared final responder
(``structured_outputs.final_responder``): that child's answer is the user
deliverable, and the parent is NOT re-invoked. These tests exercise the extracted
owner-module predicates and the settle decision function directly, spying the
parent re-invoke (``run_dynamic_agent_sync``) to prove the post-terminal round is
gone while the non-terminal common case is untouched.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import dspy
import pytest

from clio_agent.gact import turn_terminal
from clio_agent.gact.turn_terminal import (
    adopt_final_responder_answer,
    final_responder_ids,
    is_final_responder,
    settle_parent_next_pred,
)
from clio_agent.gact.types import AgentDef
from clio_agent.gact.workflow_state.schema import WorkflowStateSchema


def _agent(agent_id: str, **structured: Any) -> AgentDef:
    return AgentDef(id=agent_id, title=agent_id, structured_outputs=dict(structured))


def _state() -> SimpleNamespace:
    return SimpleNamespace(sid="sess_1", turn_id="turn_1", app=None)


def _completed_row(agent_id: str, **overrides: Any) -> dict[str, Any]:
    # #880: ONE output channel — ``output`` IS the child's answer, verbatim. The
    # retired output_raw/output_summary keys do not exist on the row shape.
    row = {
        "agent_id": agent_id,
        "status": "completed",
        "stage": "delegate.completed",
        "output": "",
        "workflow_state": {},
    }
    row.update(overrides)
    return row


# --------------------------------------------------------------------------- #
# Predicates (declarative-data reads, never prose/id/order)                    #
# --------------------------------------------------------------------------- #


def test_is_final_responder_reads_structured_outputs() -> None:
    for truthy in (True, "true", "yes", "on", 1):
        assert is_final_responder(_agent("synthesis", final_responder=truthy)) is True
    for falsy in (False, None):
        assert is_final_responder(_agent("synthesis", final_responder=falsy)) is False
    # Flag entirely absent.
    assert is_final_responder(_agent("synthesis")) is False
    # Non-mapping structured_outputs is tolerated (never raises).
    weird = _agent("x")
    object.__setattr__(weird, "structured_outputs", None)
    assert is_final_responder(weird) is False


def test_is_final_responder_quoted_false_string_is_disabled() -> None:
    """(#736/C) A QUOTED author error (``final_responder: "no"`` / ``"false"``) must be
    treated as DISABLED, not silently truthy. ``bool("no")`` is ``True`` — the read
    must route through the shared structured-flag truthiness helper so a quoted
    string cannot short-circuit a turn."""

    for quoted_off in ("false", "False", "0", "no", "off", "disabled"):
        assert is_final_responder(_agent("synthesis", final_responder=quoted_off)) is False


def test_final_responder_ids_returns_only_flagged_children() -> None:
    children = [
        _agent("data"),
        _agent("analysis"),
        _agent("synthesis", final_responder=True),
    ]
    assert final_responder_ids(children) == frozenset({"synthesis"})
    assert final_responder_ids([_agent("data"), _agent("analysis")]) == frozenset()


# --------------------------------------------------------------------------- #
# adopt_final_responder_answer: the child's answer becomes the deliverable      #
# --------------------------------------------------------------------------- #


def test_adopt_carries_child_answer_and_attributes_to_child() -> None:
    schema = WorkflowStateSchema()
    parent_pred = dspy.Prediction(
        answer="MAIN RESTATEMENT",
        reasoning="main routing rationale",
        selected_expert="main",
        next_expert="synthesis",
        execution_path="main>synthesis",
    )
    row = _completed_row("synthesis", output="The synthesis deliverable.")

    deliverable = adopt_final_responder_answer(parent_pred, row, "synthesis", schema=schema)

    assert deliverable.answer == "The synthesis deliverable."
    # Load-bearing: attribution flips to the child so finalize's op-identity dedup
    # collapses the server-side double to exactly one answer part.
    assert deliverable.selected_expert == "synthesis"
    assert deliverable.next_expert == "finish"
    # Main-only envelope fields are preserved (copied, not dropped).
    assert deliverable.execution_path == "main>synthesis"
    # (#736/D) The parent's routing rationale is DROPPED, not carried: the deliverable
    # is attributed to the child, and finalize would otherwise relabel main's reasoning
    # as the CHILD's `thinking` part. See test_adopt_drops_parent_reasoning below.
    assert deliverable.reasoning == ""
    # The parent's restatement never becomes the deliverable.
    assert deliverable.answer != "MAIN RESTATEMENT"


def test_adopt_drops_parent_reasoning_to_avoid_child_mislabel() -> None:
    """(#736/D) ``adopt`` sets ``selected_expert=child_id`` so finalize attributes the
    turn to the child; ``turn.py`` then sets ``state.thinking_text = pred.reasoning`` and
    ``turn_finalize`` appends it as a ``thinking`` Part under ``responder_agent_id`` (the
    child). Carrying the PARENT's routing rationale ("route to synthesis") would surface
    main's disclosure MISLABELED as the child's reasoning — so adopt drops it. main's
    routing rationale still appears once, as the delegate.started handoff thought."""

    schema = WorkflowStateSchema()
    parent_pred = dspy.Prediction(
        answer="restate",
        reasoning="I will route to synthesis to write the final answer.",
        selected_expert="main",
        next_expert="synthesis",
        trajectory={"step_0_thought": "main plans the route"},
    )
    row = _completed_row("synthesis", output="the child's deliverable")

    deliverable = adopt_final_responder_answer(parent_pred, row, "synthesis", schema=schema)

    # Neither the parent's reasoning nor its trajectory may ride the child-attributed
    # deliverable (both feed ``state.thinking_text`` and would mislabel).
    assert deliverable.reasoning == ""
    assert not getattr(deliverable, "trajectory", None)


def test_adopt_carries_structured_answer_verbatim_on_output() -> None:
    # #880: a structured (JSON) final-responder answer rides ``output`` byte-for-byte
    # (no output_raw channel); adopt carries it verbatim as the deliverable.
    schema = WorkflowStateSchema()
    parent_pred = dspy.Prediction(answer="restate", selected_expert="main", next_expert="synthesis")
    row = _completed_row("synthesis", output='{"result": 1}')

    deliverable = adopt_final_responder_answer(parent_pred, row, "synthesis", schema=schema)

    assert deliverable.answer == '{"result": 1}'


def test_final_responder_workflow_state_merges() -> None:
    from clio_agent.gact.delegation import _prediction_workflow_state

    schema = WorkflowStateSchema()
    parent_pred = dspy.Prediction(
        answer="restate",
        selected_expert="main",
        next_expert="synthesis",
        workflow_state={"b": 2},
    )
    row = _completed_row("synthesis", output="done", workflow_state={"a": 1})

    deliverable = adopt_final_responder_answer(parent_pred, row, "synthesis", schema=schema)

    merged = _prediction_workflow_state(deliverable, schema=schema)
    assert merged == {"a": 1, "b": 2}


# --------------------------------------------------------------------------- #
# settle_parent_next_pred: the decision — end at terminal, else re-invoke       #
# --------------------------------------------------------------------------- #


def test_final_responder_child_ends_turn_without_parent_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed final responder ENDS the settle: parent is NOT re-invoked, and
    the returned prediction is the child's answer (not a main restatement)."""

    calls: list[Any] = []

    async def _spy_resume(state: Any, parent: Any, prompt: str) -> Any:
        calls.append((parent.id, prompt))
        return dspy.Prediction(answer="MAIN RESTATEMENT AFTER SYNTHESIS", next_expert="finish")

    monkeypatch.setattr(
        "clio_agent.gact.turn_delegation.run_dynamic_agent_sync", _spy_resume, raising=True
    )

    schema = WorkflowStateSchema()
    parent = _agent("main")
    latest_pred = dspy.Prediction(
        answer="", selected_expert="main", next_expert="synthesis", reasoning="route"
    )
    completed = [_completed_row("synthesis", output="THE FINAL SYNTHESIS ANSWER.")]

    pred, stop = asyncio.run(
        settle_parent_next_pred(
            _state(),
            parent,
            "source",
            list(completed),
            completed,
            {"synthesis"},
            frozenset({"synthesis"}),
            latest_pred,
            schema=schema,
        )
    )

    assert stop is True
    assert calls == [], "parent must NOT be re-invoked after the final responder"
    assert pred.answer == "THE FINAL SYNTHESIS ANSWER."
    assert pred.selected_expert == "synthesis"


def test_final_responder_deliverable_is_child_not_parent_restatement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The would-be parent restatement never becomes the deliverable."""

    restatement = "PARENT RESTATEMENT — SHOULD NEVER SURFACE"

    async def _spy_resume(state: Any, parent: Any, prompt: str) -> Any:
        return dspy.Prediction(answer=restatement, next_expert="finish")

    monkeypatch.setattr(
        "clio_agent.gact.turn_delegation.run_dynamic_agent_sync", _spy_resume, raising=True
    )

    schema = WorkflowStateSchema()
    latest_pred = dspy.Prediction(answer="", selected_expert="main", next_expert="synthesis")
    completed = [_completed_row("synthesis", output="child deliverable")]

    pred, stop = asyncio.run(
        settle_parent_next_pred(
            _state(),
            _agent("main"),
            "source",
            list(completed),
            completed,
            {"synthesis"},
            frozenset({"synthesis"}),
            latest_pred,
            schema=schema,
        )
    )

    assert pred.answer == "child deliverable"
    assert pred.answer != restatement


def test_no_final_responder_parent_is_resumed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A completed child WITHOUT the flag → the parent IS re-invoked (common case)."""

    calls: list[Any] = []

    async def _spy_resume(state: Any, parent: Any, prompt: str) -> Any:
        calls.append(parent.id)
        return dspy.Prediction(answer="", next_expert="analysis")

    def _fake_resume_prompt(source_text: str, parent: Any, all_rows: Any, **kw: Any) -> str:
        return f"resume::{parent.id}"

    monkeypatch.setattr(
        "clio_agent.gact.turn_delegation.run_dynamic_agent_sync", _spy_resume, raising=True
    )
    monkeypatch.setattr(
        "clio_agent.gact.delegation._dynamic_parent_resume_prompt",
        _fake_resume_prompt,
        raising=True,
    )

    schema = WorkflowStateSchema()
    latest_pred = dspy.Prediction(answer="", selected_expert="data", next_expert="data")
    completed = [_completed_row("data", output="data returned")]

    pred, stop = asyncio.run(
        settle_parent_next_pred(
            _state(),
            _agent("main"),
            "source",
            list(completed),
            completed,
            {"data", "analysis", "synthesis"},
            frozenset({"synthesis"}),
            latest_pred,
            schema=schema,
        )
    )

    assert stop is False
    assert calls == ["main"], "parent must be re-invoked after a non-terminal child"
    assert pred.next_expert == "analysis"


# --------------------------------------------------------------------------- #
# Error path (no-silent-fallback): empty terminal answer emits a reason         #
# --------------------------------------------------------------------------- #


def test_final_responder_empty_answer_emits_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty terminal answer STILL ends the settle (no papering-over by re-invoke)
    and emits a structured stream_audit reason."""

    audits: list[tuple[str, dict[str, Any]]] = []

    def _capture(stage: str, **fields: Any) -> None:
        audits.append((stage, fields))

    async def _spy_resume(state: Any, parent: Any, prompt: str) -> Any:  # pragma: no cover
        raise AssertionError("parent must not be re-invoked for an empty final responder")

    monkeypatch.setattr(turn_terminal, "stream_audit", _capture, raising=True)
    monkeypatch.setattr(
        "clio_agent.gact.turn_delegation.run_dynamic_agent_sync", _spy_resume, raising=True
    )

    schema = WorkflowStateSchema()
    latest_pred = dspy.Prediction(answer="", selected_expert="main", next_expert="synthesis")
    completed = [_completed_row("synthesis")]  # all output channels empty

    pred, stop = asyncio.run(
        settle_parent_next_pred(
            _state(),
            _agent("main"),
            "source",
            list(completed),
            completed,
            {"synthesis"},
            frozenset({"synthesis"}),
            latest_pred,
            schema=schema,
        )
    )

    assert stop is True
    assert pred.selected_expert == "synthesis"
    reasons = [f.get("reason") for _stage, f in audits]
    assert "final_responder_empty_answer" in reasons


def test_final_responder_empty_answer_records_always_on_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No-silent-fallback: with ``CLIO_STREAM_AUDIT_LOG`` UNSET (so ``stream_audit``
    writes NOTHING in a default deployment), an empty terminal answer STILL lands a
    typed reason in the always-on, per-session unified ledger
    (``app.state.turn_degradations``).

    The pre-existing ``..._emits_reason`` test above monkeypatches ``stream_audit`` and
    therefore passes even if the reason reaches no real sink. This test uses a REAL app
    stub and the real ``stream_audit`` (env unset -> no-op) to prove the degradation is
    queryable after the fact regardless of the audit-log gate."""

    monkeypatch.delenv("CLIO_STREAM_AUDIT_LOG", raising=False)

    async def _spy_resume(state: Any, parent: Any, prompt: str) -> Any:  # pragma: no cover
        raise AssertionError("parent must not be re-invoked for an empty final responder")

    monkeypatch.setattr(
        "clio_agent.gact.turn_delegation.run_dynamic_agent_sync", _spy_resume, raising=True
    )

    app = SimpleNamespace(state=SimpleNamespace())
    state = SimpleNamespace(sid="sess_ledger", turn_id="turn_1", app=app)

    schema = WorkflowStateSchema()
    latest_pred = dspy.Prediction(answer="", selected_expert="main", next_expert="synthesis")
    completed = [_completed_row("synthesis")]  # all output channels empty

    pred, stop = asyncio.run(
        settle_parent_next_pred(
            state,
            _agent("main"),
            "source",
            list(completed),
            completed,
            {"synthesis"},
            frozenset({"synthesis"}),
            latest_pred,
            schema=schema,
        )
    )

    assert stop is True
    assert pred.selected_expert == "synthesis"
    ledger = getattr(app.state, "turn_degradations", {})
    entries = ledger.get("sess_ledger", [])
    reasons = [p.get("reason") for p in entries]
    assert "final_responder_empty_answer" in reasons
    # Typed payload from the unified turn-degradation catalog (not a bare string).
    payload = next(p for p in entries if p.get("reason") == "final_responder_empty_answer")
    assert payload["category"] == "delegation_degradation"
    assert payload["recovery_actions"]
    assert "main" in payload.get("message", "")


def test_delegation_reason_absent_from_audited_streaming_capability_set() -> None:
    """The delegation degradation reason lives in the unified turn-degradation catalog,
    NOT the audited client-facing stream_fallback capability set (a closed set)."""

    from clio_agent.gact.runtime.capabilities import _stream_fallback_reason_capabilities
    from clio_agent.gact.turn_degradation import _TURN_DEGRADATION_REASON_DEFINITIONS

    assert "final_responder_empty_answer" in _TURN_DEGRADATION_REASON_DEFINITIONS
    assert "final_responder_empty_answer" not in _stream_fallback_reason_capabilities()


def test_delegation_fallback_payload_rejects_unknown_reason() -> None:
    """Like the stream_fallback payload, an unknown reason is rejected (no bare fallback)."""

    from clio_agent.gact.turn_degradation import _turn_degradation_payload

    with pytest.raises(ValueError, match="Unknown turn degradation reason"):
        _turn_degradation_payload("not_a_real_reason")


def test_final_responder_empty_answer_surfaces_delegation_evidence() -> None:
    """finalize's fallback surfaces the child's delegation evidence when the terminal
    answer is empty (the no-silent-fallback contract's downstream half)."""

    from clio_agent.gact.delegation import _fallback_answer_from_delegation

    handoffs = [
        {
            "stage": "parent.resumed",
            "status": "completed",
            "output": "evidence the child returned",
        }
    ]
    assert _fallback_answer_from_delegation(handoffs) == "evidence the child returned"


def test_final_responder_structured_answer_carried_verbatim_and_settled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#880: a structured (JSON) terminal answer rides ``output`` byte-for-byte and is
    adopted verbatim as the deliverable. The former output/output_raw split — and its
    cosmetic ``final_responder_structured_answer`` reason — are GONE: any non-empty
    answer settles with the single ``final_responder_settled`` reason (the only real
    decision is empty-vs-nonempty, a structural fact of ``output``)."""

    audits: list[dict[str, Any]] = []
    monkeypatch.setattr(
        turn_terminal, "stream_audit", lambda stage, **f: audits.append(f), raising=True
    )

    schema = WorkflowStateSchema()
    latest_pred = dspy.Prediction(answer="", selected_expert="main", next_expert="synthesis")
    completed = [_completed_row("synthesis", output='{"answer": "x"}')]

    pred, stop = asyncio.run(
        settle_parent_next_pred(
            _state(),
            _agent("main"),
            "source",
            list(completed),
            completed,
            {"synthesis"},
            frozenset({"synthesis"}),
            latest_pred,
            schema=schema,
        )
    )

    assert stop is True
    # The JSON answer is carried BYTE-FOR-BYTE (no summary, no blanking).
    assert pred.answer == '{"answer": "x"}'
    reasons = [f.get("reason") for f in audits]
    assert "final_responder_settled" in reasons
    # The retired content-sniff reason no longer exists on any path.
    assert "final_responder_structured_answer" not in reasons


def test_final_responder_reason_is_structural_not_prose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(#880) The stream_audit reason LABEL is decided by the row's STRUCTURE — the only
    real decision is empty-vs-nonempty ``output`` — NEVER by sniffing whether the answer
    text looks like JSON. An answer whose content merely STARTS with '{' settles with the
    single ``final_responder_settled`` reason (the retired ``final_responder_structured_answer``
    content-sniff reason no longer exists on any path), proving no prose-content heuristic
    drives the label."""

    audits: list[dict[str, Any]] = []
    monkeypatch.setattr(
        turn_terminal, "stream_audit", lambda stage, **f: audits.append(f), raising=True
    )

    schema = WorkflowStateSchema()
    latest_pred = dspy.Prediction(answer="", selected_expert="main", next_expert="synthesis")
    # Answer content LOOKS structured (leading brace) but rides the PROSE channel.
    completed = [
        _completed_row("synthesis", output="{this is prose that happens to open with a brace}")
    ]

    pred, stop = asyncio.run(
        settle_parent_next_pred(
            _state(),
            _agent("main"),
            "source",
            list(completed),
            completed,
            {"synthesis"},
            frozenset({"synthesis"}),
            latest_pred,
            schema=schema,
        )
    )

    assert stop is True
    reasons = [f.get("reason") for f in audits]
    assert "final_responder_settled" in reasons
    assert "final_responder_structured_answer" not in reasons
