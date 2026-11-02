"""A child's report reaches its parent whole unless it cannot fit the parent's context."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from clio_agent.gact import child_report_fit, reminders
from clio_agent.gact.runtime import context_tokens


@pytest.fixture
def four_chars_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(context_tokens, "_estimate_text_tokens", lambda text: len(text) // 4)


@pytest.mark.usefixtures("four_chars_a_token")
def test_reports_spend_one_room_measured_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The room is the window up to the compaction threshold less the prompt already held;
    each report that fits spends from it. SABOTAGE: stop subtracting in ``report_fits``
    and the third report is let through."""
    monkeypatch.setattr("clio_agent.gact.context.active_react_context_window", lambda: 10_000)
    monkeypatch.setattr(context_tokens, "_autocompact_threshold", lambda: 0.9)
    monkeypatch.setattr(context_tokens, "_last_prompt_tokens", lambda: 3_000)
    with child_report_fit.report_room():  # 9,000 - 3,000 = 6,000 tokens
        assert child_report_fit.report_fits("x" * 12_000)  # 3,000 tokens
        assert child_report_fit.report_fits("x" * 12_000)  # 3,000 more: exactly full
        assert not child_report_fit.report_fits("x" * 4)  # nothing left


@pytest.mark.usefixtures("four_chars_a_token")
def test_an_unknown_window_is_no_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("clio_agent.gact.context.active_react_context_window", lambda: 0)
    with child_report_fit.report_room():
        assert child_report_fit.report_fits("x" * 10_000_000)


def test_only_a_child_is_told_who_reads_its_answer() -> None:
    """SABOTAGE: drop the ``parent_session_id`` check and the root session gets it too."""
    root = SimpleNamespace(parent_session_id="")
    child = SimpleNamespace(parent_session_id="sess_parent")
    assert child_report_fit.child_report_note(None, "sess_root", root) == ""
    note = child_report_fit.child_report_note(None, "sess_child", child)
    assert note.startswith("Your final answer is passed as written to the agent that started")
    assert "succinct" in note
    reminders.update(child_report_fit.CHILD_REPORT_SOURCE, active=False)
    assert child_report_fit.child_report_note(None, "sess_child", child) == ""
