"""An auto-compaction setting that cannot be used is traced, never ignored silently.

``autocompact.pct`` and a session's ``autocompact_pct`` / ``automatic_compaction`` fall
back so a typo or an edited session record cannot wedge a turn; the fallback is traced
once per source and value (``autocompact_pct_invalid``, ``automatic_compaction_invalid``).

SABOTAGE: drop the ``_report_invalid`` calls -> both tests see no trace.
"""

from __future__ import annotations

import pytest

from clio_agent.gact.context_preferences_types import DEFAULT_AUTOCOMPACT_PCT
from clio_agent.gact.runtime import context_tokens


@pytest.fixture
def traced(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from clio_agent.runtime import trace

    events: list[str] = []
    monkeypatch.setattr(trace, "event", lambda tag, msg, *args: events.append(msg % args))
    monkeypatch.setattr(context_tokens, "_REPORTED_INVALID", set())
    return events


def test_an_invalid_configured_threshold_is_traced_once(
    monkeypatch: pytest.MonkeyPatch, traced: list[str]
) -> None:
    monkeypatch.setenv("CLIO_AUTOCOMPACT_PCT", "90")  # a percent, not a fraction
    assert context_tokens._autocompact_threshold() == DEFAULT_AUTOCOMPACT_PCT
    assert context_tokens._autocompact_threshold() == DEFAULT_AUTOCOMPACT_PCT
    [event] = traced
    assert event.startswith("autocompact_pct_invalid source=autocompact.pct value=")
    monkeypatch.setenv("CLIO_AUTOCOMPACT_PCT", "0.7")
    assert context_tokens._autocompact_threshold() == 0.7 and len(traced) == 1


def test_an_invalid_session_preference_is_traced_and_an_unset_one_is_not(
    monkeypatch: pytest.MonkeyPatch, traced: list[str]
) -> None:
    monkeypatch.setenv("CLIO_AUTOCOMPACT_PCT", "0.6")
    unset = {"context_preferences": {"automatic_compaction": False}}
    assert context_tokens._session_autocompact_preferences(unset) == (False, 0.6)
    assert traced == []
    bad = {"context_preferences": {"automatic_compaction": "no", "autocompact_pct": "high"}}
    assert context_tokens._session_autocompact_preferences(bad) == (True, 0.6)
    assert sorted(e.split(" ")[0] for e in traced) == [
        "autocompact_pct_invalid",
        "automatic_compaction_invalid",
    ]
    assert all("source=session" in e for e in traced)
