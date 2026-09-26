"""A Codex model reference keeps its transport (``sdk`` / ``direct``) end to end.

The composer's picker has an SDK half and a Direct half for the same model id,
and the message's model reference names the half as ``ModelRef.variant``. The
per-turn route, the expert's :class:`LMSpec`, the resolver, and the LiteLLM
provider name must all carry that variant: a turn picked on the SDK half must
run on the SDK transport even when Direct was never signed in (the owner-reported
``codex_credential_missing`` failure came from the variant being dropped and the
config defaulting to Direct).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.config import LMProviderConfig
from clio_agent.gact.types import AgentDef
from clio_agent.providers import resolver as resolver_mod
from clio_agent.providers.codex.constants import LITELLM_PROVIDER, LITELLM_PROVIDER_SDK
from clio_agent.providers.lm_spec import LMSpec, build_spec, spec_from_config


@pytest.fixture(autouse=True)
def _no_handshake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No network handshake, and no chance of touching a real Codex login."""

    monkeypatch.setattr(resolver_mod, "run_handshake_sync", lambda *_a, **_k: None)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex_home"))
    monkeypatch.setattr("clio_agent.gact.context.active_app", lambda: None)


def _codex_config(variant: str) -> LMProviderConfig:
    return LMProviderConfig(
        provider="codex",
        model="gpt-5.5",
        api_base="codex://direct",
        codex_variant=variant,  # type: ignore[arg-type]
    )


def test_spec_from_config_carries_the_codex_variant() -> None:
    assert spec_from_config(_codex_config("sdk")).variant == "sdk"
    assert spec_from_config(_codex_config("direct")).variant == "direct"


def test_spec_from_config_has_no_variant_for_single_transport_providers() -> None:
    cfg = LMProviderConfig(provider="lm_studio", model="qwen", api_key="lm-studio")
    assert spec_from_config(cfg).variant == ""


def test_build_spec_inherits_and_overrides_the_variant() -> None:
    default = spec_from_config(_codex_config("direct"))
    inherited = build_spec(AgentDef(id="a", title="A"), default)
    chosen = build_spec(AgentDef(id="a", title="A", variant="sdk"), default)
    assert inherited.variant == "direct"
    assert chosen.variant == "sdk"


@pytest.mark.parametrize(
    ("variant", "litellm_provider"),
    [("sdk", LITELLM_PROVIDER_SDK), ("direct", LITELLM_PROVIDER)],
)
def test_resolver_binds_the_named_codex_transport(variant: str, litellm_provider: str) -> None:
    from clio_agent.lm.factory import _resolve_model_name

    resolved = resolver_mod.resolve_endpoint_and_handshake(
        LMSpec(provider="codex", model="gpt-5.5", provider_id="codex", variant=variant)
    )
    config = resolved.materialize()
    assert config.codex_variant == variant
    assert _resolve_model_name(config) == f"{litellm_provider}/cg-gpt-5.5"


def _turn_state(effective_model: dict[str, str]) -> Any:
    return SimpleNamespace(
        user_msg=SimpleNamespace(
            metadata={"model_selection_source": "per_message", "effective_model": effective_model}
        )
    )


def test_turn_route_copies_the_variant_onto_the_turn_agent() -> None:
    from clio_agent.gact.turn_forward import _apply_turn_model_selection

    routed = _apply_turn_model_selection(
        _turn_state({"provider_id": "codex", "model_id": "gpt-5.5", "variant": "sdk"}),
        AgentDef(id="main", title="Main"),
    )
    assert (routed.default_provider, routed.default_model, routed.variant) == (
        "codex",
        "gpt-5.5",
        "sdk",
    )


def test_sdk_turn_runs_on_the_sdk_transport_without_direct_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full chain: message ref -> turn agent -> spec -> config -> LiteLLM -> SDK."""

    from clio_agent.gact.agents.builders import _dynamic_agent_lm_config
    from clio_agent.gact.turn_forward import _apply_turn_model_selection
    from clio_agent.lm.factory import create_lm
    from clio_agent.providers.codex import litellm_adapter, sdk_transport
    from clio_agent.providers.codex.credentials import CodexCredentialStore
    from clio_agent.providers.codex.sdk_client import _stream_chunk, usage_chunk

    # Direct is not signed in on this machine.
    assert CodexCredentialStore().load() is None
    sdk_calls: list[str] = []

    def _fake_run_sdk(**kwargs: Any) -> tuple[str, dict[str, int]]:
        sdk_calls.append(kwargs["model"])
        return "from the sdk", {"input_tokens": 11, "output_tokens": 3}

    async def _fake_astream_sdk(**kwargs: Any) -> Any:
        sdk_calls.append(kwargs["model"])
        yield _stream_chunk(text="from the sdk", is_finished=False)
        yield _stream_chunk(
            text="", is_finished=True, usage=usage_chunk({"input_tokens": 11, "output_tokens": 3})
        )

    def _direct_must_not_run(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the SDK selection reached the Direct transport")

    monkeypatch.setattr(sdk_transport, "run_sdk", _fake_run_sdk)
    monkeypatch.setattr(sdk_transport, "astream_sdk", _fake_astream_sdk)
    for name in ("completion", "acompletion", "streaming", "astreaming"):
        monkeypatch.setattr(litellm_adapter.CodexLLM, name, _direct_must_not_run)

    # The active (boot) model is a different provider entirely.
    base_agent = SimpleNamespace(
        _provider_config=LMProviderConfig(provider="lm_studio", model="qwen", api_key="lm-studio")
    )
    agent_def = _apply_turn_model_selection(
        _turn_state({"provider_id": "codex", "model_id": "gpt-5.5", "variant": "sdk"}),
        AgentDef(id="main", title="Main"),
    )
    config = _dynamic_agent_lm_config(base_agent, agent_def).materialize()
    assert config.codex_variant == "sdk"

    out = create_lm(config)("hello")

    assert sdk_calls == ["gpt-5.5"]
    assert out == ["from the sdk"]
