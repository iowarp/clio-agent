"""A continuation turn keeps the behavior of the request it continues.

After a server restart, a Deep research conversation resumed as the conversation's
default agent: the recovery turn is a system continuation message with no
``behavior``, and only a Deep research *message* selects the deep-researcher agent.
A continuation now carries the person's latest message behavior.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI

from clio_agent.gact.types import Message, Part
from tests.test_gact.task_records import record_assignment_fixture
from tests.test_gact.test_finalize_error_envelope import finalize_app  # noqa: F401

pytestmark = pytest.mark.usefixtures("host_agent_executor")

_NOW = "2026-11-15T00:00:00+00:00"


def _message(role: str, mid: str, behavior: dict | None = None) -> Message:
    return Message(
        id=mid,
        session_id="sess_x",
        role=role,
        created_at=_NOW,
        updated_at=_NOW,
        parts=[Part(id=mid + ":text", type="text", text="hello")],
        metadata={"behavior": behavior} if behavior is not None else {},
    )


def test_the_latest_user_message_behavior_is_continued() -> None:
    from clio_agent.gact.message_contract import continued_behavior  # after types: import cycle

    rows = [
        _message("user", "u1", {"execution_mode": "plan"}),
        _message("assistant", "a1"),
        _message("user", "u2", {"execution_mode": "deep_research", "confirmation_policy": "ask"}),
        _message("assistant", "a2"),
        _message("system", "s1"),
    ]
    assert continued_behavior(rows) == {
        "execution_mode": "deep_research",
        "confirmation_policy": "ask",
    }
    # The latest request chose nothing: nothing is carried from an older one.
    assert continued_behavior([*rows, _message("user", "u3")]) == {}
    assert continued_behavior([]) == {}


def test_a_continuation_of_a_deep_research_request_runs_the_deep_researcher(
    finalize_app: Callable[[], FastAPI],  # noqa: F811
) -> None:
    """SABOTAGE: return ``{}`` from ``continued_behavior`` and the continuation runs
    the conversation's default agent (the live restart of a deep-research run)."""
    from clio_agent.gact.message_contract import DEEP_RESEARCH_AGENT, continued_behavior
    from clio_agent.gact.turn_state import new_turn_state

    app = finalize_app()
    sid = app.state.sessions.create(workspace_id="ws_default", title="t").id
    record_assignment_fixture(app, sid)
    asked = _message("user", "u1", {"execution_mode": "deep_research"})
    continuation = Message(
        id="turn_recovery",
        session_id=sid,
        role="system",
        created_at=_NOW,
        updated_at=_NOW,
        parts=[Part(id="turn_recovery:continuation", type="injection", text="restarted")],
        metadata={"system_injection": True, "behavior": continued_behavior([asked])},
    )
    sess = app.state.sessions.get(sid)
    state = new_turn_state(app, sid, "restarted", continuation, "", sess=sess, bus=app.state.bus)
    assert state.turn_agent_id == DEEP_RESEARCH_AGENT
