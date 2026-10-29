"""An idle session the resident set evicts no longer stays in ARC's process memory.

ARC writes every segment through to clio-core and reloads a scope from it on the next
access. Its in-process copy of a session was never dropped, so a long-running server
held every session it had served (#1846). The resident transcript set now releases it
when it evicts the session; the data is still read back, from clio-core.

This drives real ClioReAct turns (``POST /messages``) over a scripted engine, with the
resident set capped at one session so the second session's turn evicts the first.

SABOTAGE: drop the ``_release_arc_session`` call in
``evicted_session.forget_evicted_session`` -> the first session's scopes stay loaded.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import dspy
import pytest
from fastapi.testclient import TestClient

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact import context as ctx
from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.gact.app import build_app
from tests._harness import install_scripted_module
from tests._scripted_engine import Reply, scripted_lm
from tests.turn_signals import post_turn_and_wait

ANSWER = "The run finished; its result is in the workspace."


class _HostAgent:
    """The app's host agent: unused, the turn runs the scripted ClioReAct module."""

    def forward(self, question: str, session_id: str) -> Any:  # pragma: no cover - unused
        raise AssertionError("the turn runs the scripted ClioReAct module")


@pytest.fixture
def arc(tmp_path: Path) -> ARCMemory:
    return ARCMemory(data_dir=str(tmp_path / "arc"))


@pytest.fixture
def client(tmp_path: Path, arc: ARCMemory, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CLIO_RESIDENT_LEDGERS_MAX", "1")

    def run(question: str, session_id: str, **_kwargs: Any) -> Any:
        lm, _engine = scripted_lm([Reply(text=ANSWER)])
        tokens = [ctx.set_react_scope("standard"), ctx.set_react_session(session_id)]
        try:
            ctx.set_primary_agent_id("standard")
            with dspy.context(lm=lm):
                return ClioReAct("question -> answer", tools=[])(question=question)
        finally:
            for token in reversed(tokens):
                ctx.reset(token)

    install_scripted_module(monkeypatch, run, commit_input=False)
    app = build_app(sessions_path=tmp_path / "s.json", agent=_HostAgent(), arc=arc)
    with TestClient(app) as test_client:
        yield test_client


def _loaded_scopes(arc: ARCMemory, sid: str) -> list[str]:
    return sorted(scope for session, scope in arc._segments._scopes if session == sid)


def _turn(client: TestClient, sid: str) -> None:
    body = {"parts": [{"type": "text", "text": "Is the run done?"}]}
    assert post_turn_and_wait(client, sid, body) == "idle"


def test_an_evicted_idle_session_is_released_from_arc_and_read_back_from_clio_core(
    client: TestClient, arc: ARCMemory
) -> None:
    first, second = (
        str(client.post("/v1/sessions", json={"title": title}).json()["id"])
        for title in ("first", "second")
    )
    _turn(client, first)
    scopes = _loaded_scopes(arc, first)
    assert scopes, "the first session's turn must have written ARC segments"
    before = {scope: arc._segments.render(first, scope) for scope in scopes}

    _turn(client, second)  # the resident set holds one session: this evicts the first

    deadline = time.monotonic() + 30
    while _loaded_scopes(arc, first) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _loaded_scopes(arc, first) == [], "ARC kept the evicted session in memory"
    assert _loaded_scopes(arc, second), "the active session's scopes stay loaded"

    after = {scope: arc._segments.render(first, scope) for scope in scopes}
    assert after == before, "a released session reads back from clio-core unchanged"
