"""The turn-start reminder to carry a request through reaches only the providers it names."""

from __future__ import annotations

import pytest

from clio_agent import injections
from clio_agent.gact import finish_reminder
from clio_agent.gact.agents import resolution


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    state: dict[str, object] = {"provider": "claude_code", "delegated": False}
    monkeypatch.setattr(resolution, "session_provider_id", lambda _a, _s: state["provider"])
    monkeypatch.setattr(resolution, "session_is_delegated", lambda _a, _s: state["delegated"])
    return state


def test_a_claude_root_session_gets_the_reminder(session: dict[str, object]) -> None:
    text = finish_reminder.finish_work_reminder(None, "sess_a")
    assert text.startswith("Carry this request through before you answer.")
    assert "ask_user" in text


def test_other_providers_and_delegated_sessions_do_not(session: dict[str, object]) -> None:
    session["provider"] = "lm_studio"
    assert finish_reminder.finish_work_reminder(None, "sess_a") == ""
    session["provider"] = "anthropic"
    session["delegated"] = True
    assert finish_reminder.finish_work_reminder(None, "sess_a") == ""


def test_the_providers_are_the_reminders_own_setting(session: dict[str, object]) -> None:
    injections.update(finish_reminder.FINISH_WORK_SOURCE, params={"providers": "lm_studio"})
    try:
        assert finish_reminder.finish_work_reminder(None, "sess_a") == ""
        session["provider"] = "lm_studio"
        assert finish_reminder.finish_work_reminder(None, "sess_a")
    finally:
        injections.update(
            finish_reminder.FINISH_WORK_SOURCE, params={"providers": "claude_code,anthropic"}
        )
