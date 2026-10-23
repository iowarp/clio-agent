"""A person's model choice against the conversation agent's requirements (M5, person side)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from clio_agent.gact.fleet import child_model, session_check
from clio_agent.gact.fleet.entries import FleetEntry
from clio_agent.gact.fleet.session_check import entry_fit, model_requirement_error
from clio_agent.sdk.model import Requires

ENTRIES = [
    FleetEntry("vllm", "text-only", tool_calling=True, vision=False, available=True),
    FleetEntry("claude_code", "haiku", tool_calling=True, vision=True, available=True),
]


@pytest.fixture(autouse=True)
def _fleet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(child_model, "fleet_entries", lambda _app, needed: ENTRIES)
    monkeypatch.setattr(
        session_check,
        "session_requirements",
        lambda _app, _sid: (Requires(vision=True), {"vision": ["pdf"]}),
    )


def test_a_model_without_what_the_agent_needs_is_refused_with_the_models_that_fit() -> None:
    error = model_requirement_error(SimpleNamespace(), "vllm", "text-only", "s1")
    assert error is not None
    assert error.error.error == "model_requirement_unmet"
    assert "image input (required by pdf)" in error.error.message
    assert error.error.details["fitting"] == ["claude_code/haiku"]


def test_a_fitting_or_unlisted_model_is_not_refused() -> None:
    assert model_requirement_error(SimpleNamespace(), "claude_code", "haiku", "s1") is None
    assert model_requirement_error(SimpleNamespace(), "lm_studio", "local", "s1") is None


def test_the_picker_fit_marks_each_entry() -> None:
    fit = entry_fit(ENTRIES, Requires(vision=True), {"vision": ["pdf"]})
    assert fit["claude_code/haiku"]["meets"] is True
    assert fit["vllm/text-only"]["unmet"] == ["image input (required by pdf)"]
