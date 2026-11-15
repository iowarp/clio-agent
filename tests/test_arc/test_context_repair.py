"""FN-16: a scope whose tool call has no recorded result repairs at the next fold.

A step records its calls and results as one write, so a call without a result is
damage (a torn write, a deleted result). The fold refused such a scope
(``unanswered_call``) on every later turn, restart or not, and the conversation was
stuck. The next forward now records the truth for each such call
(``operation_outcome_uncertain``, nothing replayed) before it folds, and reports it
(``context_repaired_unanswered_calls``). Malformed records are never guessed at.

Sabotage tripwires:
* remove the ``repair_unanswered_calls`` call in ``ClioReAct.run`` →
  ``test_the_next_forward_repairs_and_the_model_is_told_the_outcome_is_unknown`` fails
  with ``context_fold_failed``;
* drop the ``_report`` call → ``test_a_repair_is_reported_on_the_trace_and_transcript``.
"""

from __future__ import annotations

import json
import types
from typing import Any

import dspy
import pytest

import clio_agent.gact.runtime.globals as runtime_globals
from clio_agent.arc.memory import ARCMemory
from clio_agent.gact import context as ctx
from clio_agent.gact import injection_parts
from clio_agent.gact.agents import context_repair
from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.gact.agents.clio_react_record import fold_steps
from clio_agent.gact.agents.context_errors import ContextFoldError
from clio_agent.gact.agents.context_repair import (
    CONTEXT_REPAIRED,
    OUTCOME_UNCERTAIN,
    repair_unanswered_calls,
)
from tests._scripted_engine import calls, scripted_lm, wire

from .conftest import live_plane_context

SID, SCOPE = "fn15-s1", "agentA"


def _damaged(arc: ARCMemory, *, malformed: bool = False) -> None:
    """A turn whose step kept its call but lost the result, then a later question."""
    arc.append_segment(SID, SCOPE, "user", {"text": "list the files"}, turn_id="t1")
    arc.append_segment(SID, SCOPE, "thought", {"text": "", "thinking": []}, step=0, turn_id="t1")
    arc.append_segment(
        SID,
        SCOPE,
        "tool_call",
        {"id": "call_lost", "name": "shell", "args": {"command": "ls"}},
        step=0,
        turn_id="t1",
    )
    if malformed:
        arc.append_segment(SID, SCOPE, "thought", {"text": "x", "thinking": "not-a-list"})
    arc.append_segment(SID, SCOPE, "user", {"text": "what was the second file?"}, turn_id="t2")


def test_unanswered_calls_get_an_interrupted_result_and_the_context_folds(tmp_path) -> None:
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    _damaged(arc)
    with pytest.raises(ContextFoldError, match="unanswered_call"):
        fold_steps(arc.context_view(SID, SCOPE).segments)

    assert repair_unanswered_calls(arc, SID, SCOPE, turn_id="t3") == ["call_lost"]

    live = arc.context_view(SID, SCOPE).segments
    assert [s.kind for s in live] == ["user", "thought", "tool_call", "observation", "user"]
    observation = live[3]
    assert observation.content["call_id"] == "call_lost" and observation.content["is_error"]
    assert observation.turn_id == "t1", "recorded in the call's own turn (a rewind takes both)"
    body = json.loads(observation.content["text"])
    assert body["error"] == OUTCOME_UNCERTAIN and body["resubmitted"] is False
    assert "outcome is unknown" in body["message"]
    messages = fold_steps(live)  # folds now
    assert [m.role for m in messages] == ["user", "assistant", "tool", "user"]
    # Idempotent: nothing is missing any more.
    assert repair_unanswered_calls(arc, SID, SCOPE) == []


def test_malformed_content_is_not_touched(tmp_path) -> None:
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    arc.append_segment(SID, SCOPE, "user", {"text": "q"})
    arc.append_segment(SID, SCOPE, "thought", {"text": "x", "thinking": "not-a-list"})
    arc.append_segment(SID, SCOPE, "tool_call", {"name": "shell", "args": {}})  # no id
    before = [(s.id, s.kind) for s in arc.context_view(SID, SCOPE).segments]

    assert repair_unanswered_calls(arc, SID, SCOPE) == []

    assert [(s.id, s.kind) for s in arc.context_view(SID, SCOPE).segments] == before
    with pytest.raises(ContextFoldError, match="malformed"):
        fold_steps(arc.context_view(SID, SCOPE).segments)


def test_a_malformed_record_still_fails_typed_after_the_call_is_repaired(tmp_path) -> None:
    """Only the missing result is supplied; the malformed thought stays a typed refusal."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    _damaged(arc, malformed=True)

    assert repair_unanswered_calls(arc, SID, SCOPE) == ["call_lost"]

    with pytest.raises(ContextFoldError, match="malformed_thinking"):
        fold_steps(arc.context_view(SID, SCOPE).segments)


def test_a_repair_is_reported_on_the_trace_and_transcript(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    _damaged(arc)
    events: list[tuple[str, dict[str, Any]]] = []
    injections: list[tuple[str, str]] = []
    monkeypatch.setattr(
        runtime_globals,
        "_emit_semantic_event",
        lambda _app, _sid, event_type, **kw: events.append((event_type, kw)),
    )
    monkeypatch.setattr(
        injection_parts,
        "emit_injection",
        lambda source, text, **_: injections.append((source, text)),
    )
    token = ctx.set_app(types.SimpleNamespace(state=types.SimpleNamespace(arc=arc)))
    try:
        repair_unanswered_calls(arc, SID, SCOPE, turn_id="t3")
    finally:
        ctx.reset(token)

    [(event_type, fields)] = events
    assert event_type == "context.repaired"
    assert fields["payload"] == {
        "reason": CONTEXT_REPAIRED,
        "scope": SCOPE,
        "call_ids": ["call_lost"],
    }
    [(source, text)] = injections
    assert source == "context_repair" and OUTCOME_UNCERTAIN in text


def test_the_next_forward_repairs_and_the_model_is_told_the_outcome_is_unknown(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    _damaged(arc)
    monkeypatch.setattr(runtime_globals, "_emit_react_step_event", lambda **kw: None)
    monkeypatch.setattr(runtime_globals, "_emit_expert_lifecycle_event", lambda *a, **kw: None)
    monkeypatch.setattr(runtime_globals, "_emit_semantic_event", lambda *a, **kw: None)
    lm, engine = scripted_lm([calls(("submit", {"answer": "unknown, I will rerun ls"}))])
    agent = ClioReAct("question -> answer", tools=[], max_iters=3)
    with live_plane_context(arc, session=SID, scope=SCOPE), dspy.context(lm=lm):
        prediction = agent(question="what was the second file?")

    assert prediction.answer == "unknown, I will rerun ls"
    [request] = engine.requests
    results = [part for _, parts in wire(request) for part in parts if part[0] == "result"]
    assert results[0][1] == "call_lost" and results[0][3] is True
    assert json.loads(results[0][2])["error"] == OUTCOME_UNCERTAIN


def test_started_tasks_are_named_in_the_result() -> None:
    row = types.SimpleNamespace(
        handle="task_1", kind="Subagent", label="council flow", status="active"
    )
    body = json.loads(context_repair.interrupted_observation("call_x", [row])["text"])
    assert body["started_tasks"][0]["handle"] == "task_1"
    assert "not started again" in body["started_note"]
