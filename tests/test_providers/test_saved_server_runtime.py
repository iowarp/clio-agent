"""A saved custom model server runs a session, its SPOTTER watcher and the memory pass.

The server (``server-<slug>``, preset ``vllm``) is reached at its OWN address with its
OWN key along every path that builds an LM from a model reference -- never the
preset's default address or key, and never LM Studio.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent import config
from clio_agent.gact import context, local_server_store
from clio_agent.gact.local_server_store import LocalServerEntry
from clio_agent.providers import api_key_store, resolver

SERVER = LocalServerEntry(
    id="server-vllm-spotter-2",
    preset_id="vllm",
    label="vllm-spotter-2",
    address="http://10.0.0.7:35507/v1",
    credential_ref="managed-server:local:vllm@spotter-2",
)
KEY = "spotter-deployment-key"


@pytest.fixture(autouse=True)
def _saved_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        local_server_store, "get_server", lambda sid: SERVER if sid == SERVER.id else None
    )
    monkeypatch.setattr(
        api_key_store, "stored_api_key", lambda pid: KEY if pid == SERVER.id else ""
    )
    monkeypatch.delenv("CLIO_LM_API_KEY", raising=False)

    def _no_lm_studio(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("LM Studio discovery must not run for a saved server")

    monkeypatch.setattr(config, "list_lm_studio_models", _no_lm_studio)
    # The resolver's handshake is passive; keep it off the network here.
    monkeypatch.setattr(resolver, "run_handshake_sync", lambda *_a, **_k: None)


def _assert_server(cfg: Any) -> None:
    assert cfg.provider_id == "vllm"
    assert cfg.server_id == SERVER.id
    assert cfg.api_base == SERVER.address
    assert cfg.api_key == KEY


def test_the_config_runs_at_the_servers_address_with_its_key() -> None:
    cfg = config.LMProviderConfig(provider_id=SERVER.id, model="granite")
    _assert_server(cfg)
    # A dataclasses.replace (as create_lm's identity tagging does) keeps it.
    _assert_server(replace(cfg, provider_options={}))


def test_an_unknown_provider_fails_fast_with_a_typed_error() -> None:
    with pytest.raises(config.UnknownLMProviderError, match="Unknown LM provider"):
        config.LMProviderConfig(provider_id="server-missing", model="m")
    with pytest.raises(config.UnknownLMProviderError):
        config.LMProviderConfig(provider_id="vllm", server_id="server-missing", model="m")


def test_the_session_turn_lm_resolves_to_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.gact.agents.lm_resolution import _dynamic_agent_lm_config
    from clio_agent.gact.types import AgentDef

    monkeypatch.setattr(context, "active_app", lambda: None)
    # The host (boot/default profile) is a different provider entirely.
    host = SimpleNamespace(
        _provider_config=config.LMProviderConfig(
            provider_id="lm_studio", model="local", api_key="lm-studio"
        )
    )
    agent_def = AgentDef(
        id="chat", title="Chat", default_provider=SERVER.id, default_model="granite"
    )
    resolved = _dynamic_agent_lm_config(host, agent_def)
    assert resolved.spec.provider_id == SERVER.id
    cfg = resolved.materialize()
    _assert_server(cfg)
    assert cfg.model == "granite"

    lm = config.create_lm(cfg)
    assert lm.kwargs["api_base"] == SERVER.address
    assert lm.kwargs["api_key"] == KEY
    assert lm.model == "hosted_vllm/granite"


def test_a_host_built_on_the_server_keeps_it_for_inheriting_experts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.gact.agents.lm_resolution import _dynamic_agent_lm_config
    from clio_agent.gact.types import AgentDef

    monkeypatch.setattr(context, "active_app", lambda: None)
    host = SimpleNamespace(
        _provider_config=config.LMProviderConfig(provider_id=SERVER.id, model="granite")
    )
    resolved = _dynamic_agent_lm_config(host, AgentDef(id="expert", title="Expert"))
    assert resolved.spec.server_id == SERVER.id
    _assert_server(resolved.materialize())


def _capture_route(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    from clio_agent.lm import hooked_lm

    built: list[Any] = []
    monkeypatch.setattr(hooked_lm, "create_hooked_lm", lambda cfg: built.append(cfg) or "lm")
    monkeypatch.setattr(config, "create_chat_adapter", lambda cfg: "adapter")
    return built


def test_a_fleet_model_route_on_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.gact.fleet_settings import model_route

    built = _capture_route(monkeypatch)
    assert model_route(SERVER.id, "granite") == ("lm", "adapter")
    _assert_server(built[0])


def test_a_route_with_no_or_an_unknown_provider_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.gact.fleet_settings import ModelRouteUnavailable, model_route

    built = _capture_route(monkeypatch)
    with pytest.raises(ModelRouteUnavailable):
        model_route("", "granite")
    with pytest.raises(ModelRouteUnavailable):
        model_route("server-missing", "granite")
    assert built == []


def test_the_memory_model_on_a_server_session_host(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.gact.memory import models

    built = _capture_route(monkeypatch)
    host = SimpleNamespace(
        _provider_config=config.LMProviderConfig(provider_id=SERVER.id, model="granite")
    )
    app = SimpleNamespace(state=SimpleNamespace(agent=host, sessions={}))
    monkeypatch.setattr(context, "active_app", lambda: app)
    monkeypatch.setattr(models, "fleet_settings", lambda: {"judges": {}})
    lm, adapter, identity = models.memory_model("memory_extract")
    assert (lm, adapter) == ("lm", "adapter")
    assert identity == {
        "provider_id": SERVER.id,
        "model_id": "granite",
        "source": "server_default",
    }
    _assert_server(built[0])


def test_an_unconfigured_memory_model_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.gact.memory import models
    from clio_agent.gact.providers import boot_selection

    built = _capture_route(monkeypatch)
    monkeypatch.setattr(context, "active_app", lambda: None)
    monkeypatch.setattr(models, "fleet_settings", lambda: {"judges": {}})
    monkeypatch.setattr(boot_selection, "explicit_lm_provider", lambda: "")
    with pytest.raises(models.MemoryModelUnconfigured):
        models.memory_model("memory_extract")
    assert built == []
