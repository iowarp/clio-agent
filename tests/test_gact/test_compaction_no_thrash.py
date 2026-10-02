"""Auto compaction does not thrash when what is left after it is still over the threshold.

Found live: with a threshold below the post-compaction floor (system prompt + tools +
summary + the question kept verbatim), every later step compacted again -- one
summarizer call and a stateful provider restart per step, and the turn never finished.
Each test drives the real :class:`ClioReAct` loop inside a real gact turn on real
clio-core (the composition of ``test_compaction_visible``); only the measured prompt
size and the summarizer LM are scripted.

SABOTAGE (run manually, each turns the named test red):

* ``compaction.maybe_autocompact``: drop the ``guard.above_threshold`` skip -> a
  compaction at every step: ``test_a_compaction_that_leaves_the_context_over_...``.
* ``compaction.maybe_autocompact``: set ``above_threshold`` without checking the
  measurement -> the second compaction never runs:
  ``test_a_compaction_that_gets_under_the_threshold_...``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import clio_agent.gact.compaction as compaction
from clio_agent.arc.memory import ARCMemory
from clio_agent.gact.app import build_app
from clio_agent.gact.protocol.v3.message import part_to_v3_block
from tests._scripted_engine import Reply, ScriptedEngine, calls
from tests.test_gact.test_compaction_visible import (
    SCOPE,
    SummarizerAgent,
    _compaction_events,
    _install_loop,
    _kinds,
    _patch_tokens,
    _reload,
    _texts,
    _transcript,
    _turn,
)


@pytest.fixture
def env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Any, TestClient, SummarizerAgent, list[Any], list[dict[str, Any]]]]:
    """A real app on real clio-core; its semantic events and compaction audits kept."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    agent = SummarizerAgent()
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent, arc=arc)
    events: list[Any] = []
    real_emit = app.state.semantic_event_sink.emit
    app.state.semantic_event_sink.emit = lambda e: (events.append(e), real_emit(e))[1]
    audits: list[dict[str, Any]] = []
    real_audit = compaction.stream_audit

    def audit(stage: str, **fields: Any) -> None:
        audits.append({"stage": stage, **fields})
        real_audit(stage, **fields)

    monkeypatch.setattr(compaction, "stream_audit", audit)
    with TestClient(app) as client:
        yield app, client, agent, events, audits


def _search_turn(steps: int) -> list[Reply]:
    searches = [calls(("search", {"q": f"q{i}"}), text=f"look up q{i}") for i in range(steps)]
    return [*searches, Reply(text="FINAL: the answer")]


def _completed(events: list[Any]) -> list[dict[str, Any]]:
    return [e.payload for e in events if e.event_type == "compaction.completed"]


def test_a_compaction_that_leaves_the_context_over_the_threshold_is_not_repeated(
    env: tuple[Any, TestClient, SummarizerAgent, list[Any], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client, summarizer, events, audits = env
    sid = client.post("/v1/sessions", json={"title": "thrash"}).json()["id"]
    engines: list[ScriptedEngine] = []
    _install_loop(monkeypatch, engines, [_search_turn(5)])
    # Every measurement from the step-1 boundary on is over the threshold.
    _patch_tokens(monkeypatch, high_on=set(range(1, 100)))
    _turn(client, sid, "which stations are near LA?")  # the turn finishes

    # Exactly one compaction (at the step-1 boundary).
    [engine] = engines
    assert len(engine.requests) == 6
    seq = _compaction_events(events)
    assert [e.event_type for e in seq] == ["compaction.started", "compaction.completed"]
    [completed] = _completed(events)
    assert len(summarizer.prompts) == 1
    # After it the context only grows: no later request starts from a new summary.
    after = [_texts(r.messages) for r in engine.requests[2:]]
    for earlier, later in zip(after, after[1:], strict=False):
        assert later[: len(earlier)] == earlier

    # The skip is typed and audited at every later crossing (steps 2..5).
    skips = [
        a
        for a in audits
        if a["stage"] == compaction.AUDIT_AUTO_SKIPPED
        and a["reason"] == compaction.SKIP_ABOVE_THRESHOLD_AFTER_COMPACTION
    ]
    assert len(skips) == 4
    assert {(a["scope"], a["compaction_id"]) for a in skips} == {
        (SCOPE, completed["compaction_id"])
    }

    # ... and visible once: a notice right after the step that measured it, live and
    # reloaded the same, tied to the compaction that did not get the context under.
    step = ["text", "tool_call", "tool_result"]
    for served in (_transcript(client, sid), _reload(app, client, sid)):
        [assistant] = [m for m in served if m["role"] == "assistant"]
        assert _kinds(assistant) == [
            *step,
            "injection:summarization",
            *step,
            "notice",
            *step,
            *step,
            *step,
            "text",
        ], _kinds(assistant)
        [notice] = [p for p in assistant["parts"] if p["type"] == "notice"]
        assert {k: notice[k] for k in ("source", "code", "trigger", "compaction_id")} == {
            "source": "compaction_skipped",
            "code": "above_threshold_after_compaction",
            "trigger": "auto",
            "compaction_id": completed["compaction_id"],
        }
        assert notice["text"] == compaction.ABOVE_THRESHOLD_NOTICE
        assert assistant["parts"][-1]["text"] == "FINAL: the answer"
    assert part_to_v3_block(notice)["source"] == "compaction_skipped"
    # The model is never told.
    sent = json.dumps([_texts(r.messages) for r in engine.requests])
    assert compaction.ABOVE_THRESHOLD_NOTICE not in sent


def test_a_compaction_that_gets_under_the_threshold_leaves_auto_compaction_on(
    env: tuple[Any, TestClient, SummarizerAgent, list[Any], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client, summarizer, events, audits = env
    sid = client.post("/v1/sessions", json={"title": "regrow"}).json()["id"]
    engines: list[ScriptedEngine] = []
    _install_loop(monkeypatch, engines, [_search_turn(4)])
    # Over at the step-1 and step-3 boundaries only: the first compaction got the
    # context under (step 2 measured low), so the regrown context compacts again.
    _patch_tokens(monkeypatch, high_on={1, 3})
    _turn(client, sid, "which stations are near LA?")

    assert len(_completed(events)) == 2
    assert len(summarizer.prompts) == 2
    assert not [a for a in audits if a.get("reason") == "above_threshold_after_compaction"]
    [assistant] = [m for m in _transcript(client, sid) if m["role"] == "assistant"]
    assert "notice" not in _kinds(assistant)
