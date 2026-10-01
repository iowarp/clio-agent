"""Codex has ONE transport: direct. The removed SDK path is refused, never remapped.

A Codex model selection runs on the direct engine end to end (turn route -> spec ->
resolver -> engine LM). Anything still naming the removed Codex SDK path -- a
persisted ``lm.codex_variant`` (the config file the selection store writes, or the
``CLIO_CODEX_VARIANT`` environment variable), a bind request's ``variant``, a
message's model reference -- is a typed, plain-language refusal that says what to
delete or redo, never silently bound to direct.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from clio_agent.config import LMProviderConfig, load_config_from_env
from clio_agent.errors import RemovedConfigKeyError
from clio_agent.gact.types import AgentDef
from clio_agent.providers import resolver as resolver_mod
from clio_agent.providers.codex.constants import (
    LITELLM_PROVIDER,
    TRANSPORT_API_BASES,
    TRANSPORT_DIRECT,
    TRANSPORT_LABELS,
)
from clio_agent.providers.lm_spec import LMSpec
from tests._config_layer import set_config, user_config_path


@pytest.fixture(autouse=True)
def _no_handshake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No network handshake, and no chance of touching a real Codex login."""

    monkeypatch.setattr(resolver_mod, "run_handshake_sync", lambda *_a, **_k: None)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex_home"))
    monkeypatch.delenv("CLIO_CODEX_VARIANT", raising=False)
    monkeypatch.setattr("clio_agent.gact.context.active_app", lambda: None)


# --------------------------------------------------------------------------- #
# a persisted removed key: a typed config error naming where it lives         #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", ["sdk", "direct", ""])
def test_the_removed_key_in_the_config_file_is_a_typed_error(value: str) -> None:
    set_config("lm.provider", "codex")
    set_config("lm.codex_variant", value)

    with pytest.raises(RemovedConfigKeyError) as err:
        load_config_from_env()

    message = str(err.value)
    assert message.startswith("The Codex SDK path was removed")
    assert "delete lm.codex_variant from" in message
    assert str(user_config_path()) in message
    assert err.value.error_type == "config_key_removed"
    assert err.value.locations == [str(user_config_path())]
    assert isinstance(err.value, ValueError)  # every invalid-config handler reports it


def test_the_removed_key_in_the_environment_is_a_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIO_CODEX_VARIANT", "sdk")

    with pytest.raises(RemovedConfigKeyError) as err:
        load_config_from_env()

    assert "the environment variable CLIO_CODEX_VARIANT" in str(err.value)
    assert err.value.locations == ["the environment variable CLIO_CODEX_VARIANT"]


def test_without_the_removed_key_codex_config_loads() -> None:
    set_config("lm.provider", "codex")
    set_config("lm.model", "gpt-5.5")

    config = load_config_from_env()

    assert (config.provider, config.model) == ("codex", "gpt-5.5")
    assert not hasattr(config, "codex_variant")


def test_the_selection_store_refuses_to_rewrite_around_the_removed_key() -> None:
    from clio_agent.gact.providers import selection_store

    set_config("lm.codex_variant", "sdk")
    codex = SimpleNamespace(
        provider="codex",
        provider_id="codex",
        model="gpt-5.5",
        codex_transport="websocket",
        claude_code_transport="sdk",
    )

    outcome = selection_store.persist_lm_selection(codex, requested_api_base="")

    assert outcome.persisted is False
    assert outcome.reason == selection_store.LM_SELECTION_NOT_PERSISTED
    assert outcome.detail.startswith("The Codex SDK path was removed")
    assert "delete lm.codex_variant" in outcome.detail
    # The stale key is left for the user to delete, never silently dropped.
    from tests._config_layer import read_config

    assert read_config()["lm"]["codex_variant"] == "sdk"


def test_a_written_selection_carries_no_transport_choice() -> None:
    from clio_agent.gact.providers import selection_store

    codex = SimpleNamespace(
        provider="codex",
        provider_id="codex",
        model="gpt-5.5",
        codex_transport="websocket",
        claude_code_transport="sdk",
    )
    assert selection_store.selection_entries(codex, requested_api_base="") == {
        "provider": "codex",
        "model": "gpt-5.5",
        "codex_transport": "websocket",
    }


# --------------------------------------------------------------------------- #
# the bind request and the transport catalog                                   #
# --------------------------------------------------------------------------- #
def test_codex_has_only_the_direct_transport() -> None:
    assert TRANSPORT_LABELS == {TRANSPORT_DIRECT: "Direct"}
    assert TRANSPORT_API_BASES == {TRANSPORT_DIRECT: "codex://direct"}


def test_a_bind_naming_the_removed_sdk_transport_is_refused_in_plain_language() -> None:
    from clio_agent.gact.lm_provider_types import LMProviderRequest

    body = {"provider": "codex", "api_base": "", "model": "gpt-5.5"}
    with pytest.raises(ValidationError) as err:
        LMProviderRequest(**body, variant="sdk")
    assert "The Codex SDK path was removed" in str(err.value)
    # The catalog's one transport row id (what clients echo back) binds as before.
    assert LMProviderRequest(**body, variant="direct").model == "gpt-5.5"
    assert LMProviderRequest(**body).model == "gpt-5.5"


def test_the_config_has_no_transport_choice_field() -> None:
    with pytest.raises(TypeError):
        LMProviderConfig(provider="codex", model="gpt-5.5", codex_variant="sdk")  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# a codex selection runs direct end to end                                     #
# --------------------------------------------------------------------------- #
def test_the_resolver_binds_codex_direct() -> None:
    from clio_agent.lm.factory import _resolve_model_name

    resolved = resolver_mod.resolve_endpoint_and_handshake(
        LMSpec(provider="codex", model="gpt-5.5", provider_id="codex")
    )
    assert _resolve_model_name(resolved.materialize()) == f"{LITELLM_PROVIDER}/gpt-5.5"


def _turn_state(effective_model: dict[str, str]) -> Any:
    return SimpleNamespace(
        user_msg=SimpleNamespace(
            metadata={"model_selection_source": "per_message", "effective_model": effective_model}
        )
    )


def test_a_codex_turn_runs_on_the_direct_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """The full chain: message ref -> turn agent -> spec -> config -> engine LM -> direct."""

    from dspy.lm15 import OpenAICodexLM

    from clio_agent.gact.agents.builders import _dynamic_agent_lm_config
    from clio_agent.gact.turn_forward import _apply_turn_model_selection
    from clio_agent.lm.factory import create_lm
    from clio_agent.providers.codex import direct_engine

    monkeypatch.setattr(
        direct_engine, "default_wire", lambda: OpenAICodexLM(api_key="t", account_id="a")
    )
    base_agent = SimpleNamespace(
        _provider_config=LMProviderConfig(provider="lm_studio", model="qwen", api_key="lm-studio")
    )
    agent_def = _apply_turn_model_selection(
        _turn_state({"provider_id": "codex", "model_id": "gpt-5.5", "variant": "direct"}),
        AgentDef(id="main", title="Main"),
    )
    assert (agent_def.default_provider, agent_def.default_model) == ("codex", "gpt-5.5")

    lm = create_lm(_dynamic_agent_lm_config(base_agent, agent_def).materialize())

    assert lm.model == f"{LITELLM_PROVIDER}/gpt-5.5"
    assert isinstance(lm._engine_spec, direct_engine.CodexDirectEngine)
