"""Focused contracts for the live vLLM qualification helper."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from clio_agent.config import LMProviderConfig

ROOT = Path(__file__).resolve().parents[2]


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "qualify_vllm_local", ROOT / "scripts/qualify_vllm_local.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_call_model_distinguishes_omitted_and_positive_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The helper records Agent factory kwargs rather than inferring wire behavior."""
    module = _module()

    class FakeLM:
        def __init__(self, kwargs: dict[str, Any]) -> None:
            self.kwargs = kwargs
            self.history: list[dict[str, Any]] = []

        def __call__(self, **_kwargs: Any) -> list[str]:
            self.history.append({"response": "OK"})
            return ["OK"]

    def fake_create(config: LMProviderConfig) -> FakeLM:
        kwargs = {} if config.max_tokens == 0 else {"max_tokens": config.max_tokens}
        return FakeLM(kwargs)

    monkeypatch.setattr(module, "create_lm", fake_create)
    omitted = module.call_model(api_base="http://local/v1", model="alias", max_tokens=0)
    capped = module.call_model(api_base="http://local/v1", model="alias", max_tokens=16)
    assert omitted["wire_max_tokens_present"] is False
    assert capped["wire_max_tokens"] == 16
    assert omitted["history_entries"] == capped["history_entries"] == 1
