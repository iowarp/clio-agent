"""Which model a delegated Clio Coder instance runs on."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from clio_agent.gact import clio_coder_route as route

MAIN = {"provider_id": "openai", "model_id": "mini/qwen3.8-27b", "variant": ""}


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A conversation running on one local OpenAI-protocol endpoint booted from env."""
    for name in (
        "CLIO_FLEET_AGENTS",
        "CLIO_FLEET_DEFAULT",
        route.MODEL_ENV,
        route.HOME_TEMPLATE_ENV,
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("clio_agent.gact.providers.config._active_lm_model_ref", lambda _app: MAIN)
    monkeypatch.setattr("clio_agent.gact.fleet.child_model.caller_model", lambda _a, _s: MAIN)
    live = SimpleNamespace(api_base="http://mini:4000/v1", api_key="sk-local")
    return SimpleNamespace(state=SimpleNamespace(agent=SimpleNamespace(_provider_config=live)))


def _ref(provider_id: str, model_id: str) -> str:
    return json.dumps({"provider_id": provider_id, "model_id": model_id})


def test_with_nothing_configured_every_layer_collapses_to_the_conversations_model(app) -> None:
    resolved = route.resolve_route(app, None)
    assert (resolved.source, resolved.provider_id, resolved.model_id) == (
        "conversation",
        "openai",
        "mini/qwen3.8-27b",
    )
    target = resolved.target
    assert (target.runtime, target.url, target.model) == (
        "openai-compat",
        "http://mini:4000/v1",
        "mini/qwen3.8-27b",
    )
    # The settings name a variable; the key itself reaches only the child's environment.
    assert target.api_key_env == route.DELEGATED_KEY_ENV
    assert "sk-local" not in json.dumps(target.settings())
    assert resolved.env == {route.DELEGATED_KEY_ENV: "sk-local"}


def test_a_key_already_in_its_well_known_variable_is_referenced_not_copied(
    app, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-local")
    resolved = route.resolve_route(app, None)
    assert resolved.target.api_key_env == "OPENAI_API_KEY" and resolved.env == {}


def test_precedence_is_call_then_binding_then_fleet_default_then_conversation(
    app, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_FLEET_DEFAULT", _ref("anthropic", "claude-x"))
    resolved = route.resolve_route(app, None)
    assert (resolved.source, resolved.target.runtime) == ("fleet-default", "anthropic")

    monkeypatch.setenv(route.MODEL_ENV, "operator-model")
    resolved = route.resolve_route(app, None)
    assert (resolved.source, resolved.target.model) == ("agent-binding", "operator-model")

    monkeypatch.setenv(
        "CLIO_FLEET_AGENTS", json.dumps({"clio-coder": json.loads(_ref("claude_code", "opus"))})
    )
    resolved = route.resolve_route(app, None)
    assert (resolved.source, resolved.target.runtime, resolved.target.url) == (
        "agent-binding",
        "claude-code",
        None,
    )

    resolved = route.resolve_route(app, None, model="big-coder")
    assert (resolved.source, resolved.provider_id, resolved.model_id) == (
        "call",
        "openai",
        "big-coder",
    )
    resolved = route.resolve_route(app, None, model="sonnet", provider="anthropic")
    assert (resolved.provider_id, resolved.target.target_id) == ("anthropic", "clio-anthropic")


def test_a_home_template_binding_runs_on_the_profile_it_seeds(
    app, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv(route.HOME_TEMPLATE_ENV, str(tmp_path))
    resolved = route.resolve_route(app, None)
    assert (resolved.source, resolved.target, resolved.home_template) == (
        "agent-binding",
        None,
        tmp_path,
    )


def test_routes_clio_coder_cannot_reach_are_refused_with_the_way_to_bind_one(app) -> None:
    with pytest.raises(ValueError, match="bind it a route"):
        route.resolve_route(app, None, model="m", provider="bedrock")
    with pytest.raises(ValueError, match="Unknown provider"):
        route.resolve_route(app, None, model="m", provider="nope")
    with pytest.raises(ValueError, match="provider needs a model"):
        route.resolve_route(app, None, provider="anthropic")


def test_a_malformed_binding_is_an_error_not_a_silent_fallback(
    app, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_FLEET_AGENTS", json.dumps({"clio-coder": {"provider_id": "openai"}}))
    with pytest.raises(ValueError, match="fleet.agents.clio-coder"):
        route.resolve_route(app, None)
