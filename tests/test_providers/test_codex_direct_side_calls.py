"""Side calls of an agent forward never touch its kept Codex conversation.

Found live (2026-10-02, early adopters' data run): after an agent's final answer,
DSPy's extract (``ClioReAct`` filling the signature outputs the loop did not produce)
ran ONE message on the agent's own Codex direct conversation key, audited ``full
prefix_mismatch sent 1/1``. It replaced the kept response chain, so the agent's next
real call was a full resend instead of a delta -- eight resets in a three-turn run.

The real composition: a real ``ClioReAct`` over the real ``create_lm`` Codex LM and
the real direct engine, the WebSocket faked at the network boundary (the direct engine
suite's harness). Pins: the extract is a stateless one-shot call of its own; the
agent's kept conversation still holds its own final answer, and its next turn
continues it as a delta. Sabotage: drop ``outside_stateful_scope`` from the extract ->
the extract replaces the kept chain -> the next turn is ``prefix_mismatch`` -> red.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import dspy
import pytest

from clio_agent.gact import context as gact_context
from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.providers.codex import direct_engine
from tests.test_providers import test_codex_direct_engine as base
from tests.test_providers.test_codex_direct_engine import Harness, _calling, _codex_lm, _stateful

# The direct engine suite's fixtures: the faked socket and the audit rows.
harness = base.harness
audit = base.audit

EXTRACTED = (
    "[[ ## reasoning ## ]]\nFrom the trajectory.\n\n"
    "[[ ## summary ## ]]\nFour searches.\n\n[[ ## completed ## ]]"
)


def search(q: str) -> str:
    """Search."""
    return f"hits for {q}"


@pytest.fixture
def session(clio_core_plane: Any) -> Iterator[None]:
    """A GACT session around the plane: the engine keys its conversations on it."""
    token = gact_context.set_session_id("sess-side")
    try:
        yield
    finally:
        gact_context.reset(token)


@pytest.fixture
def extract_after_three(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_REACT_EXTRACT_ENABLED", "true")
    monkeypatch.setenv("CLIO_REACT_EXTRACT_AFTER_STEPS", "3")


@pytest.mark.usefixtures("session", "extract_after_three")
def test_the_extract_after_the_answer_never_replaces_the_agents_conversation(
    harness: Harness, audit: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    steps = [_calling(f"resp_{i}", f"call_{i}") for i in range(4)]
    harness.script[:] = [*steps, "The answer.", EXTRACTED, "Second answer."]
    lm = _codex_lm(monkeypatch)
    agent = ClioReAct("question -> answer, summary", tools=[search], max_iters=8)

    with dspy.context(lm=lm):
        first = agent(question="what is in the file?")
        [(key, kept)] = direct_engine._CONVERSATIONS.items()
        kept_response = kept.response_id
        second = agent(question="and the units?")

    assert (first.answer, first.summary) == ("The answer.", "Four searches.")
    assert second.answer == "Second answer."
    # Turn 1: the agent's five steps; the extract has no row on the agent's key; turn
    # 2's first call continues the agent's conversation.
    assert _stateful(audit) == [("full", "first_call")] + [("delta", None)] * 5
    assert {row["conversation"] for row in audit if row["stage"] == "provider.stateful"} == {
        "::".join(key[:2])
    }
    agent_socket, extract_socket = harness.sockets
    assert kept_response == "resp_5", "the kept chain ends at the agent's own final answer"
    assert len(agent_socket.frames) == 6 and not agent_socket.closed
    assert agent_socket.frames[-1]["previous_response_id"] == "resp_5"
    # The extract ran as a one-shot call of its own: a fresh socket, closed after it.
    assert len(extract_socket.frames) == 1 and extract_socket.closed
    assert "previous_response_id" not in extract_socket.frames[0]
