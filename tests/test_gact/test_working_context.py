"""Model semantics: a working context for a model CLIO binds but cannot configure."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.providers import working_context
from clio_agent.gact.routes.working_context import register_working_context_routes
from clio_agent.gact.runtime.context_tokens import _resolve_expert_context_window
from clio_agent.user_config_document import read_document, user_config_path
from tests._config_layer import set_config


def _cfg(**fields) -> SimpleNamespace:
    base = {
        "provider_id": "openai",
        "provider": "openai",
        "model": "gpt-5",
        "api_base": "https://api.openai.com/v1",
        "context_window": 400_000,
        "native_context_window": 400_000,
        "chosen_context": 400_000,
    }
    return SimpleNamespace(**{**base, **fields})


def test_a_saved_choice_persists_in_the_user_config_and_clears_back_to_max() -> None:
    assert working_context.saved_working_context("openai", "gpt-5") is None
    working_context.save_working_context("openai", "gpt-5", 128_000)
    working_context.save_working_context("vllm", "Qwen/Qwen3-4B", 32_768)
    assert working_context.saved_working_context("openai", "gpt-5") == 128_000
    document = read_document(user_config_path())
    assert {"provider": "openai", "model": "gpt-5", "tokens": 128_000} in document["providers"][
        "working_context"
    ]
    working_context.save_working_context("openai", "gpt-5", None)
    assert working_context.saved_working_context("openai", "gpt-5") is None
    assert working_context.saved_working_context("vllm", "Qwen/Qwen3-4B") == 32_768


def test_a_choice_below_the_minimum_is_refused() -> None:
    with pytest.raises(working_context.WorkingContextStoreError, match="at least 1024"):
        working_context.save_working_context("openai", "gpt-5", 100)


def test_the_choice_is_bounded_by_the_reported_maximum() -> None:
    working_context.save_working_context("openai", "gpt-5", 128_000)
    assert working_context.working_context_for(_cfg()) == 128_000
    assert working_context.working_context_for(_cfg(context_window=64_000)) == 64_000
    assert working_context.working_context_for(_cfg(model="gpt-4o")) is None


def test_the_agent_loop_budgets_against_the_working_context() -> None:
    # The auto-compaction denominator (builders set it from this resolver every turn).
    assert _resolve_expert_context_window(_cfg()) == 400_000
    working_context.save_working_context("openai", "gpt-5", 100_000)
    assert _resolve_expert_context_window(_cfg()) == 100_000
    # A config with no provider identity (a stub) keeps its own window.
    assert _resolve_expert_context_window(SimpleNamespace(chosen_context=4000, model="m")) == 4000


def test_an_unreadable_config_never_fails_a_turn() -> None:
    set_config("providers.working_context", "not-a-list")
    assert working_context.saved_working_contexts() == {}
    assert _resolve_expert_context_window(_cfg()) == 400_000


def test_model_controls_offer_max_and_a_number_but_never_fit_to_gpu() -> None:
    controls = working_context.model_context_controls("openai", "gpt-5", maximum=400_000)
    assert controls.semantics == "model"
    assert (controls.current, controls.current_choice) == (400_000, "max")
    assert not controls.fit_to_gpu.available
    assert "does not run this model's server" in controls.fit_to_gpu.reason
    assert controls.fit_to_gpu.strategies[0].id == "fit_to_gpu"
    working_context.save_working_context("openai", "gpt-5", 500_000)
    bounded = working_context.model_context_controls("openai", "gpt-5", maximum=400_000)
    assert (bounded.current, bounded.current_choice) == (400_000, "number")
    assert "bounded by the model's maximum 400000" in bounded.current_reason


def _client(bound: SimpleNamespace | None) -> tuple[TestClient, FastAPI]:
    app = FastAPI()
    app.state.agent = SimpleNamespace(_provider_config=bound) if bound is not None else None
    app.state.lm_config = {"provider_id": "openai", "model": "gpt-5", "chosen_context": 400_000}
    register_working_context_routes(app)
    return TestClient(app), app


def test_the_route_reads_and_saves_the_bound_models_working_context() -> None:
    bound = _cfg()
    client, app = _client(bound)
    url = "/v1/providers/openai/working-context"
    shown = client.get(url, params={"model": "gpt-5"}).json()
    assert shown["maximum"] == 400_000 and shown["current_choice"] == "max"

    saved = client.put(url, json={"model": "gpt-5", "choice": "number", "tokens": 64_000})
    assert saved.status_code == 200 and saved.json()["current"] == 64_000
    assert bound.chosen_context == 64_000  # the bound model applies it from its next turn
    assert app.state.lm_config["chosen_context"] == 64_000

    too_big = client.put(url, json={"model": "gpt-5", "choice": "number", "tokens": 10**7})
    assert too_big.status_code == 422 and "above the model's maximum" in too_big.json()["detail"]

    cleared = client.put(url, json={"model": "gpt-5", "choice": "max"})
    assert cleared.json()["current_choice"] == "max"
    assert bound.chosen_context == 400_000


def test_an_unbound_model_with_no_known_maximum_still_takes_a_number() -> None:
    client, _app = _client(None)
    url = "/v1/providers/openai/working-context"
    shown = client.get(url, params={"model": "unknown-model-xyz"}).json()
    assert shown["maximum"] is None and shown["fit_to_gpu"]["available"] is False
    saved = client.put(url, json={"model": "unknown-model-xyz", "choice": "number", "tokens": 8192})
    assert saved.json()["current"] == 8192
    assert working_context.saved_working_context("openai", "unknown-model-xyz") == 8192
