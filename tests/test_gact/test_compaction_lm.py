"""F011c: a pinned session's compaction summary runs on the session's own model."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import compaction_lm
from clio_agent.gact.types import ModelRef

_ACTIVE = {"provider_id": "vllm", "model": "/models/qwen3-1.7b"}


def _app(model: ModelRef | None) -> Any:
    return SimpleNamespace(
        state=SimpleNamespace(
            sessions={"sess_1": SimpleNamespace(model=model)},
            lm_config=dict(_ACTIVE),
            agent=SimpleNamespace(_provider_config=None),
        )
    )


@pytest.fixture
def resolved(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    from clio_agent import config
    from clio_agent.gact.agents import builders

    seen: list[tuple[str, str]] = []

    def _resolve(base_agent: Any, selection: Any) -> Any:
        seen.append((selection.default_provider, selection.default_model))
        return SimpleNamespace(materialize=lambda: SimpleNamespace(model=selection.default_model))

    monkeypatch.setattr(builders, "_dynamic_agent_lm_config", _resolve)
    monkeypatch.setattr(config, "create_lm", lambda cfg: ("lm", cfg.model))
    return seen


def test_a_pinned_session_resolves_its_own_model(resolved: list) -> None:
    app = _app(ModelRef(provider_id="vllm", model_id="/models/qwen3-4b"))

    lm = compaction_lm.pinned_summary_lm(app, "sess_1")

    assert resolved == [("vllm", "/models/qwen3-4b")]
    assert lm == ("lm", "/models/qwen3-4b")


@pytest.mark.parametrize(
    "model", [None, ModelRef(), ModelRef(provider_id="vllm", model_id=_ACTIVE["model"])]
)
def test_unpinned_or_active_pin_keeps_the_host_lm(resolved: list, model: ModelRef | None) -> None:
    assert compaction_lm.pinned_summary_lm(_app(model), "sess_1") is None
    assert resolved == []


def test_summary_passes_the_pinned_lm_and_fails_typed_when_unresolvable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.gact import compaction, compaction_summarizer

    calls: list[Any] = []

    def _scope_summarizer(app: Any, sid: str, scope: str, lm: Any) -> Any:
        calls.append(lm)
        return lambda prompt: "summary"

    app = _app(ModelRef(provider_id="vllm", model_id="/models/qwen3-4b"))
    app.state.agent = SimpleNamespace(_main_lm="HOST_LM")
    monkeypatch.setattr(compaction, "_context_file_inventory", lambda app, sid: [])
    monkeypatch.setattr(compaction_summarizer, "scope_summarizer", _scope_summarizer)
    monkeypatch.setattr(compaction_lm, "pinned_summary_lm", lambda app, sid: "PINNED_LM")
    assert compaction._summary(app, "sess_1", _plan("user: hi"), "", None).startswith("summary")
    monkeypatch.setattr(compaction_lm, "pinned_summary_lm", lambda app, sid: None)
    assert compaction._summary(app, "sess_1", _plan("user: hi"), "", None).startswith("summary")
    assert calls == ["PINNED_LM", "HOST_LM"]

    def _boom(app: Any, sid: str) -> Any:
        raise RuntimeError("no endpoint")

    monkeypatch.setattr(compaction_lm, "pinned_summary_lm", _boom)
    with pytest.raises(compaction.CompactionError) as info:
        compaction._summary(app, "sess_1", _plan("user: hi"), "", None)
    assert (info.value.status, info.value.error) == (502, "pinned_model_unresolved")


def _plan(transcript: str) -> Any:
    from clio_agent.gact import compaction

    return compaction._ScopePlan("main", (), frozenset(), transcript)
