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


class _Recorder:
    def __init__(self) -> None:
        self.notes: list[tuple[str, str]] = []

    def injections(self, items: list[tuple[str, str]]) -> None:
        self.notes.extend(items)


def _loop(applies: bool = True) -> object:
    from types import SimpleNamespace

    return SimpleNamespace(recorder=_Recorder(), _clio_finish_check_applies=applies)


def test_the_finish_check_repeats_once_mid_turn() -> None:
    """SABOTAGE: drop the ``_clio_finish_checked`` check and a second note appears."""
    injections.update(finish_reminder.FINISH_CHECK_SOURCE, params={"after": 3})
    loop = _loop()
    finish_reminder.note_finish_check(loop, ["shell_bash", "shell_bash"])
    assert loop.recorder.notes == []
    finish_reminder.note_finish_check(loop, ["ocean_bloom_phenology"])
    [(source, text)] = loop.recorder.notes
    assert source == finish_reminder.FINISH_CHECK_SOURCE
    assert "3 calls" in text and "not rerun" in text and "ask_user" in text
    finish_reminder.note_finish_check(loop, ["shell_bash"] * 5)
    assert len(loop.recorder.notes) == 1


def test_the_finish_check_skips_sessions_it_does_not_reach() -> None:
    injections.update(finish_reminder.FINISH_CHECK_SOURCE, params={"after": 2})
    loop = _loop(applies=False)
    finish_reminder.note_finish_check(loop, ["shell_bash"] * 6)
    assert loop.recorder.notes == []


def test_the_finish_check_reads_its_providers(session: dict[str, object]) -> None:
    from clio_agent.gact import context as ctx

    loop = _loop()
    del loop._clio_finish_check_applies
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ctx, "active_app", lambda: None)
        mp.setattr(ctx, "active_session_id", lambda: "sess_a")
        assert finish_reminder._check_applies(loop)
        del loop._clio_finish_check_applies
        session["provider"] = "lm_studio"
        assert not finish_reminder._check_applies(loop)
