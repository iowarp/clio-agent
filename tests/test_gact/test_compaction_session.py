"""A compaction and the session around it: the stored usage and the record's coverage.

On the real composition (``test_compaction_visible``): a real gact turn runs the real
loop on real clio-core; only the model replies, the measured prompt size and the
summarizer are scripted. The default keep rule applies.

SABOTAGE (each turns the named test red):

* (the busy slot: ``test_compaction_visible::test_a_manual_compaction_during_a_running_turn_is_refused_busy``)
* ``compaction.compact_session_context``: drop ``usage_after_fold`` ->
  ``test_a_manual_compaction_does_not_leave_a_stale_usage_that_compacts_again``.
* ``compaction._covered_rows``: skip every turn with a kept segment (the old rule) ->
  ``test_the_record_covers_the_rows_whose_segments_it_folded``.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import clio_agent.gact.compaction as compaction
import clio_agent.gact.runtime.context_tokens as context_tokens
from clio_agent.arc.memory import ARCMemory
from clio_agent.gact import context as ctx
from clio_agent.gact.app import build_app
from clio_agent.gact.summarization_record import as_summarization
from tests._scripted_engine import ScriptedEngine
from tests.test_gact.test_compaction_visible import (
    SCOPE,
    SummarizerAgent,
    _install_loop,
    _patch_tokens,
    _three_step_turn,
    _turn,
)


@pytest.fixture(autouse=True)
def _fixed_result_room(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests._result_room import pin_result_room

    pin_result_room(monkeypatch, 100_000)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[tuple[Any, TestClient]]:
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    app = build_app(sessions_path=tmp_path / "s.json", agent=SummarizerAgent(), arc=arc)
    with TestClient(app) as client:
        yield app, client


def _session_with_turns(
    app: Any, client: TestClient, monkeypatch: pytest.MonkeyPatch, questions: list[str]
) -> str:
    sid = str(client.post("/v1/sessions", json={"title": "c"}).json()["id"])
    engines: list[ScriptedEngine] = []
    _install_loop(monkeypatch, engines, [_three_step_turn() for _ in questions])
    _patch_tokens(monkeypatch, high_on=set())  # no auto compaction in these turns
    for question in questions:
        _turn(client, sid, question)
    _settled(app, sid)
    return sid


def _settled(app: Any, sid: str) -> None:
    """Wait until the last turn has released the session's slot (it retires after idle)."""
    deadline = time.monotonic() + 10
    while app.state.turn_runner.busy(sid):
        assert time.monotonic() < deadline, "the turn never released the session"
        time.sleep(0.01)


def test_a_manual_compaction_does_not_leave_a_stale_usage_that_compacts_again(
    env: tuple[Any, TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stored usage measured the context before the fold; the next turn's first step
    boundary read it (no measurement of its own yet) and compacted again at once."""
    app, client = env
    sid = _session_with_turns(app, client, monkeypatch, ["which stations are near LA?"])
    measured = {SCOPE: {"used_tokens": 950, "source": "provider", "model": "m"}}
    app.state.sessions.update(sid, metadata_patch={"context_usage_by_scope": measured})
    r = client.post(f"/v1/sessions/{sid}/compact", params={"scope": SCOPE}, json={})
    assert r.status_code == 200 and r.json()["compacted"], r.text
    row = app.state.sessions.get(sid).metadata["context_usage_by_scope"][SCOPE]
    assert row["source"] == "estimated" and row["used_tokens"] < 950

    compactions: list[Any] = []

    def record(*args: Any, **_kwargs: Any) -> dict[str, Any]:
        compactions.append(args)
        return {"compacted": False}

    monkeypatch.setattr(compaction, "compact_session_context", record)
    monkeypatch.setattr(context_tokens, "_last_prompt_tokens", lambda: 0)  # step 0
    tokens = [
        ctx.set_app(app),
        ctx.set_react_scope(SCOPE),
        ctx.set_react_session(sid),
        ctx.set_react_window(1000),
    ]
    try:
        compaction.maybe_autocompact(compaction.AutoCompactionGuard(summarize=lambda p: "S"))
    finally:
        for token in reversed(tokens):
            ctx.reset(token)
    assert compactions == []


def test_the_record_covers_the_rows_whose_segments_it_folded(
    env: tuple[Any, TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every typed message stays verbatim, so every turn has a kept segment; coverage by
    "the turn kept nothing" named no row at all. The first turn's steps are folded, so
    its assistant row is covered; the user rows stay (their messages are kept), and so
    does the last turn's reply (its newest step is kept)."""
    app, client = env
    sid = _session_with_turns(app, client, monkeypatch, ["near LA?", "and their elevations?"])
    rows = list(app.state.messages[sid])
    r = client.post(f"/v1/sessions/{sid}/compact", params={"scope": SCOPE}, json={})
    assert r.status_code == 200 and r.json()["compacted"], r.text
    [record] = [
        found
        for message in app.state.messages[sid]
        for part in message.parts
        if (found := as_summarization(part)) is not None
    ]
    first_reply = next(m for m in rows if m.role == "assistant")
    assert list(record.compacted_message_ids) == [first_reply.id]
