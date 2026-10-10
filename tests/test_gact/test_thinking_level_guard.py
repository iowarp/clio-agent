"""F039 / DIRECTIVES 15: an unsupported thinking level gets a typed 422."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from clio_agent.gact import thinking_level_guard as guard
from clio_agent.gact.types import ModelRef

MODEL = ModelRef(provider_id="ollama", model_id="qwen3:4b")


def _app() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(lm_handshake_report=None))


def test_unknown_thinking_passes_through() -> None:
    assert guard.catalog_reasoning_levels(_app(), MODEL) is None  # no handshake
    assert guard.thinking_level_error(_app(), MODEL, "high", session_id="s") is None


@pytest.mark.parametrize("levels", [["off", "high"], ["low", "medium", "high"]])
def test_a_listed_level_and_no_level_pass(monkeypatch, levels: list[str]) -> None:
    monkeypatch.setattr(guard, "catalog_reasoning_levels", lambda app, model: levels)
    assert guard.thinking_level_error(_app(), MODEL, "high", session_id="s") is None
    assert guard.thinking_level_error(_app(), MODEL, None, session_id="s") is None


def test_an_unlisted_level_is_a_typed_error(monkeypatch) -> None:
    monkeypatch.setattr(guard, "catalog_reasoning_levels", lambda app, model: ["off", "high"])
    error = guard.thinking_level_error(_app(), MODEL, "low", session_id="s1")
    assert error is not None
    assert error.error.error == "thinking_level_unsupported"
    assert error.error.details["supported_levels"] == ["off", "high"]
    assert error.error.recoverable is True


def test_a_model_without_thinking_control_refuses_every_level(monkeypatch) -> None:
    monkeypatch.setattr(guard, "catalog_reasoning_levels", lambda app, model: [])
    monkeypatch.setattr(
        "clio_agent.gact.model_selection.surrogate_selection_error", lambda *a: None
    )
    with pytest.raises(HTTPException) as raised:
        guard.raise_if_selection_unusable(_app(), MODEL, "high", "s1")
    assert raised.value.status_code == 422
    assert raised.value.detail["error"]["error"] == "thinking_level_unsupported"
    guard.raise_if_selection_unusable(_app(), MODEL, None, "s1")  # no level: accepted
