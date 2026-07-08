"""#771 Slice C: ONE conversation-history channel through the gact turn path.

gact owns the conversation transcript — ``_compile_session_conversation_history``
prepends every prior turn's Q/A to the next turn's prompt (turn.py). The ARC
``ContextCompiler`` used to ALSO fold the same live turns into a ``[Session
Context]`` block inside ``ClioAgent.forward`` (via ``_get_session_context``), so a
follow-up turn paid for its predecessor's answer TWICE.

Slice C threads ``include_conversation=False`` from ``ClioAgent.forward`` through
``retrieval.compile_expert_context`` into ``ContextCompiler.compile``: the
compiled context stops echoing turns and the transcript prepend is THE channel.

These tests drive a REAL ``ClioAgent`` through the REAL gact turn path with the
LM loop stubbed at ``_run_agent_loop`` (so no model is hit) and capture the exact
composed prompt inputs (``question`` + ``session_context``) the agent would have
sent. They assert:

* a prior turn's answer appears EXACTLY ONCE across the composed inputs;
* the compiled context never consults ``get_live_context`` on the shipped path;
* the 3-turn composed length is STRICTLY LOWER than the pre-change behavior
  (recreated in-process by forcing ``include_conversation=True``) — a recorded
  comparison, not a hard-coded golden.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app

# Distinctive, collision-free answer markers so substring counts are unambiguous.
_ANSWERS = ["ALPHAANSWERZZZ", "BETAANSWERZZZ", "GAMMAANSWERZZZ"]
_QUESTIONS = [
    "QONE what is in run.h5",
    "QTWO now summarise it",
    "QTHREE and plot the result",
]


async def _no_stream(app: Any, enriched_text: str, sid: str, emit_chunk: Any, **kwargs: Any) -> Any:
    """Force the synchronous forward path so ``_run_agent_loop`` runs once/turn."""
    del app, enriched_text, sid, emit_chunk, kwargs
    return None


def _run_three_turns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    pre_change: bool,
) -> list[str]:
    """Drive three gact turns; return the composed prompt inputs per turn.

    ``pre_change=True`` recreates the double-channel behavior by forcing
    ``include_conversation=True`` inside ``_get_session_context`` so the compiled
    context echoes the live turns again (what the fix removes).
    """
    from clio_agent.agent import ClioAgent
    from clio_agent.harness import RouteDecision

    agent = ClioAgent(data_dir=str(tmp_path / "clio"))

    composed: list[str] = []
    turns_seed: list[dict[str, str]] = []

    def _wrapped_loop(
        *,
        question: str,
        session_context: str,
        file_context: str,
        trace: Any,
        images: Any = None,
        routing_mode: str = "auto",
    ) -> tuple[str, str, Any, Any, RouteDecision]:
        # The full composed prompt inputs the agent would send: the (transcript-
        # prepended) question plus the compiled ARC context.
        composed.append(f"{question}\n{session_context}")
        answer = _ANSWERS[len(composed) - 1]
        route = RouteDecision(target="chat", source="dspy", reason="test", confidence=0.0)
        return ("chat", answer, None, None, route)

    monkeypatch.setattr(agent, "_run_agent_loop", _wrapped_loop)

    # A live view that reflects the PRIOR turns of the session — this is what the
    # compiled [Session Context] would fold in when the channel is on.
    def _fake_live(session_id: str, *, max_turns: int | None = None) -> dict[str, Any]:
        del session_id, max_turns
        return {"turns": list(turns_seed)}

    monkeypatch.setattr(agent.arc, "get_live_context", _fake_live)

    if pre_change:
        real_get_ctx = agent._get_session_context

        def _forced_ctx(
            question: str,
            session_id: str,
            tier: int = 2,
            tool_scope: str = "none",
            include_conversation: bool = False,
        ) -> str:
            return real_get_ctx(
                question,
                session_id,
                tier=tier,
                tool_scope=tool_scope,
                include_conversation=True,
            )

        monkeypatch.setattr(agent, "_get_session_context", _forced_ctx)

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", _no_stream)

    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    try:
        from .conftest import complete_turn

        with TestClient(app) as client:
            sid = client.post("/v1/sessions", json={"title": "history"}).json()["id"]
            for i, question in enumerate(_QUESTIONS):
                complete_turn(client, sid, question)
                turns_seed.append(
                    {
                        "question": question,
                        "answer": _ANSWERS[i],
                        "selected_expert": "chat",
                    }
                )
    finally:
        agent.shutdown()

    assert len(composed) == 3, f"expected one composed prompt per turn, got {len(composed)}"
    return composed


def test_prior_turn_answer_appears_exactly_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On the shipped path the prior answer is carried ONCE — by the transcript."""
    composed = _run_three_turns(tmp_path, monkeypatch, pre_change=False)

    # Turn 2 carries turn 1's answer; turn 3 carries turns 1 and 2's answers.
    assert composed[1].count(_ANSWERS[0]) == 1
    assert composed[2].count(_ANSWERS[0]) == 1
    assert composed[2].count(_ANSWERS[1]) == 1
    # And the single copy rides the transcript, never a [Session Context] echo.
    assert "[Session Context]" not in composed[1]
    assert "[Session Context]" not in composed[2]


def test_shipped_path_never_reads_live_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """include_conversation=False must skip the get_live_context branch entirely."""
    from clio_agent.agent import ClioAgent
    from clio_agent.harness import RouteDecision

    agent = ClioAgent(data_dir=str(tmp_path / "clio"))
    live_calls: list[str] = []

    def _wrapped_loop(
        *,
        question: str,
        session_context: str,
        file_context: str,
        trace: Any,
        images: Any = None,
        routing_mode: str = "auto",
    ) -> tuple[str, str, Any, Any, RouteDecision]:
        del question, session_context, file_context, trace, images, routing_mode
        route = RouteDecision(target="chat", source="dspy", reason="test", confidence=0.0)
        return ("chat", "ONLYANSWER", None, None, route)

    def _tripwire_live(session_id: str, *, max_turns: int | None = None) -> dict[str, Any]:
        live_calls.append(session_id)
        return {"turns": [{"question": "q", "answer": "a", "selected_expert": "chat"}]}

    monkeypatch.setattr(agent, "_run_agent_loop", _wrapped_loop)
    monkeypatch.setattr(agent.arc, "get_live_context", _tripwire_live)
    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", _no_stream)

    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    try:
        from .conftest import complete_turn

        with TestClient(app) as client:
            sid = client.post("/v1/sessions", json={"title": "no-live"}).json()["id"]
            complete_turn(client, sid, "hello")
            complete_turn(client, sid, "again")
    finally:
        agent.shutdown()

    assert live_calls == [], f"get_live_context was consulted despite the flag: {live_calls}"


def test_three_turn_prompt_strictly_shorter_than_pre_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The compiled context no longer echoes turns -> strictly fewer prompt bytes."""
    shipped = _run_three_turns(tmp_path / "shipped", monkeypatch, pre_change=False)
    pre_change = _run_three_turns(tmp_path / "prechange", monkeypatch, pre_change=True)

    shipped_len = sum(len(c) for c in shipped)
    pre_change_len = sum(len(c) for c in pre_change)

    assert shipped_len < pre_change_len, (
        f"expected the single-channel prompt to be strictly shorter: "
        f"shipped={shipped_len} pre_change={pre_change_len}"
    )
    # The savings come from the dropped [Session Context] echo, present ONLY in
    # the pre-change composition.
    assert any("[Session Context]" in c for c in pre_change)
    assert all("[Session Context]" not in c for c in shipped)
    # Under the double channel the prior answer is duplicated on later turns.
    assert pre_change[1].count(_ANSWERS[0]) == 2
