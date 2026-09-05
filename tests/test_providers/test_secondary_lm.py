"""Caller-relative repairer and summarizer identity regressions (#1322)."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import dspy
import pytest

from clio_agent import conf
from clio_agent.config import LMProviderConfig, create_chat_adapter, create_lm
from clio_agent.lm.secondary import resolve_secondary_lm


def _caller(model: str, endpoint: str, key: str, *, stamp_option: bool = False) -> tuple[Any, Any]:
    config = LMProviderConfig(
        provider="vllm",
        model=model,
        api_base=endpoint,
        api_key=key,
    )
    lm = create_lm(config)
    # Stamp a provider-scoped option after construction so the switch assertion
    # proves it is dropped without asking the vLLM catalog to accept a fake key.
    if stamp_option:
        lm._clio_provider_config.provider_options = {"foreign_option": model}
    return lm, create_chat_adapter(config)


@pytest.mark.parametrize("role", ["repairer", "summarizer"])
def test_empty_role_reuses_effective_caller_exactly(role: str) -> None:
    lm, adapter = _caller("session-model", "http://session.test/v1", "session-key")
    route = resolve_secondary_lm(role, caller_lm=lm, caller_adapter=adapter)  # type: ignore[arg-type]
    assert route.inherited is True
    assert route.lm is lm
    assert route.adapter is adapter


def test_model_only_repair_override_keeps_caller_endpoint_and_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIO_REPAIRER_MODEL", "repair-model")
    conf.reload()
    lm, adapter = _caller("acting-model", "http://session.test/v1", "session-key")
    route = resolve_secondary_lm("repairer", caller_lm=lm, caller_adapter=adapter)
    config = route.lm._clio_provider_config
    assert config.model == "repair-model"
    assert config.api_base == "http://session.test/v1"
    assert config.api_key == "session-key"


def test_provider_change_drops_foreign_endpoint_credential_and_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIO_REPAIRER_PROVIDER", "ollama")
    conf.reload()
    lm, adapter = _caller(
        "acting-model", "http://foreign.test/v1", "foreign-key", stamp_option=True
    )
    route = resolve_secondary_lm("repairer", caller_lm=lm, caller_adapter=adapter)
    config = route.lm._clio_provider_config
    assert config.provider == "ollama"
    assert config.api_base != "http://foreign.test/v1"
    assert config.api_key != "foreign-key"
    assert config.provider_options == {}


def test_concurrent_sessions_cannot_cross_talk() -> None:
    callers = [_caller(f"model-{i}", f"http://session-{i}.test/v1", f"key-{i}") for i in range(8)]

    def resolve(pair: tuple[Any, Any]) -> tuple[str, str, str]:
        lm, adapter = pair
        route = resolve_secondary_lm("repairer", caller_lm=lm, caller_adapter=adapter)
        return route.lm.model, route.lm.kwargs["api_base"], route.lm.kwargs["api_key"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        actual = list(pool.map(resolve, callers))
    assert actual == [
        (f"hosted_vllm/model-{i}", f"http://session-{i}.test/v1", f"key-{i}") for i in range(8)
    ]


@pytest.mark.parametrize("site", ["goal", "review"])
def test_out_of_band_inference_prefers_bound_session_over_app_default(site: str) -> None:
    default_lm, default_adapter = _caller("app-default", "http://default.test/v1", "default")
    app = SimpleNamespace(
        state=SimpleNamespace(
            agent=SimpleNamespace(_main_lm=default_lm, _dspy_adapter=default_adapter)
        )
    )

    def resolve(index: int) -> tuple[tuple[Any, Any], tuple[Any, Any]]:
        lm, adapter = _caller(f"session-{index}", f"http://session-{index}.test/v1", f"key-{index}")
        with dspy.context(lm=lm, adapter=adapter):
            if site == "goal":
                from clio_agent.gact.goal import _judge_route

                selected = _judge_route(app)
            else:
                from clio_agent.gact.runtime.ai_review import _resolve_reviewer_lm

                selected = _resolve_reviewer_lm(app)
        return selected, (lm, adapter)

    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(resolve, range(2)))
    assert all(selected == expected for selected, expected in rows)
