"""Tests for the serializable provider identity ``LMSpec`` (design §3.1).

Covers: the spec round-trips through serialize/deserialize (``asdict`` + JSON);
it carries no secret field (there is no ``api_key`` attribute); ``spec_from_config``
projects a live config down without the resolved key; and ``build_spec`` inherits
default-spec fields when the ``AgentDef`` declares none and overrides them when
they are set.
"""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

from clio_agent.config import LMProviderConfig
from clio_agent.gact.types import AgentDef
from clio_agent.providers.lm_spec import LMSpec, build_spec, spec_from_config


def _sample_spec() -> LMSpec:
    return LMSpec(
        provider="openai",
        model="gpt-4o",
        api_base="https://api.example.com/v1",
        credential_ref="openai:acctB",
        transport="",
        temperature=0.2,
        max_tokens=8192,
        thinking_budget=1024,
        top_p=0.95,
        top_k=20,
        min_p=0.05,
        presence_penalty=0.1,
    )


def test_is_frozen() -> None:
    """LMSpec is immutable — assigning a field raises."""
    spec = _sample_spec()
    try:
        spec.provider = "argonne"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("LMSpec must be a frozen dataclass")


def test_roundtrip_asdict() -> None:
    """dataclasses.asdict → LMSpec(**d) reconstructs an equal spec."""
    spec = _sample_spec()
    restored = LMSpec(**dataclasses.asdict(spec))
    assert restored == spec


def test_roundtrip_json() -> None:
    """The spec survives a JSON serialize/deserialize round-trip."""
    spec = _sample_spec()
    blob = json.dumps(dataclasses.asdict(spec))
    restored = LMSpec(**json.loads(blob))
    assert restored == spec


def test_carries_no_secret_field() -> None:
    """A spec must never carry an inline secret — no api_key attribute."""
    spec = _sample_spec()
    assert not hasattr(spec, "api_key")
    field_names = {f.name for f in dataclasses.fields(spec)}
    assert "api_key" not in field_names
    # The only credential surface is a reference, never a secret.
    assert "credential_ref" in field_names


def test_defaults_are_empty_or_none() -> None:
    """An all-defaults spec means 'inherit the default profile'."""
    spec = LMSpec(provider="lm_studio", model="local-model")
    assert spec.api_base == ""
    assert spec.credential_ref == ""
    assert spec.transport == ""
    assert spec.temperature is None
    assert spec.max_tokens is None
    assert spec.thinking_budget is None
    assert spec.top_p is None
    assert spec.top_k is None
    assert spec.min_p is None
    assert spec.presence_penalty is None


def test_spec_from_config_drops_secret() -> None:
    """spec_from_config keeps identity/params but never the resolved api_key."""
    cfg = LMProviderConfig(
        provider="openai",
        api_base="https://api.example.com/v1",
        model="gpt-4o",
        api_key="sk-super-secret",
        temperature=0.3,
        max_tokens=4096,
    )
    spec = spec_from_config(cfg)
    assert spec.provider == "openai"
    assert spec.model == "gpt-4o"
    assert spec.api_base == "https://api.example.com/v1"
    assert spec.credential_ref == ""
    # The secret must not leak into the spec anywhere.
    assert "sk-super-secret" not in json.dumps(dataclasses.asdict(spec))


def test_spec_from_config_transport() -> None:
    """Transport is projected from the provider-specific transport field."""
    codex_cfg = LMProviderConfig(provider="codex", codex_transport="app_server")
    assert spec_from_config(codex_cfg).transport == "app_server"

    cc_cfg = LMProviderConfig(provider="claude_code", claude_code_transport="sdk")
    assert spec_from_config(cc_cfg).transport == "sdk"

    # A provider with no transport choice yields an empty transport.
    plain = LMProviderConfig(provider="openai", api_key="x")
    assert spec_from_config(plain).transport == ""


def _default_spec() -> LMSpec:
    return LMSpec(
        provider="argonne",
        model="default-model",
        api_base="https://default.example/v1",
        credential_ref="argonne:default",
        transport="sdk",
        temperature=0.0,
        max_tokens=32000,
        thinking_budget=0,
        top_p=0.9,
    )


def test_build_spec_inherits_all_when_agent_declares_nothing() -> None:
    """An undeclared AgentDef resolves to exactly the default spec."""
    agent = AgentDef(id="a1", title="Undeclared")
    spec = build_spec(agent, _default_spec())
    assert spec == _default_spec()


def test_build_spec_inherits_identity_fields_on_empty() -> None:
    """Empty provider/model/api_base/credential_ref inherit the default."""
    # A stub carrying explicit-but-empty identity fields (as AgentDef will after
    # design §9 step 5 adds them) plus empty parameters.
    agent = SimpleNamespace(
        default_provider="",
        default_model="",
        api_base="",
        credential_ref="",
        transport="",
        parameters={},
    )
    default = _default_spec()
    spec = build_spec(agent, default)
    assert spec.provider == default.provider
    assert spec.model == default.model
    assert spec.api_base == default.api_base
    assert spec.credential_ref == default.credential_ref
    assert spec.transport == default.transport


def test_build_spec_overrides_when_set() -> None:
    """Declared identity fields + parameter overrides win over the default."""
    agent = SimpleNamespace(
        default_provider="openai",
        default_model="gpt-4o",
        api_base="https://acctB.example/v1",
        credential_ref="openai:acctB",
        transport="exec",
        parameters={
            "temperature": 0.7,
            "max_tokens": 2048,
            "thinking_budget": 512,
            "top_p": 0.5,
            "top_k": 10,
            "min_p": 0.01,
            "presence_penalty": 0.2,
        },
    )
    spec = build_spec(agent, _default_spec())
    assert spec.provider == "openai"
    assert spec.model == "gpt-4o"
    assert spec.api_base == "https://acctB.example/v1"
    assert spec.credential_ref == "openai:acctB"
    assert spec.transport == "exec"
    assert spec.temperature == 0.7
    assert spec.max_tokens == 2048
    assert spec.thinking_budget == 512
    assert spec.top_p == 0.5
    assert spec.top_k == 10
    assert spec.min_p == 0.01
    assert spec.presence_penalty == 0.2


def test_build_spec_partial_override_inherits_rest() -> None:
    """A partial declaration overrides only what it sets; the rest inherit."""
    agent = SimpleNamespace(
        default_provider="openai",
        default_model="",
        parameters={"temperature": 0.9},
    )
    default = _default_spec()
    spec = build_spec(agent, default)
    assert spec.provider == "openai"  # overridden
    assert spec.model == default.model  # inherited
    assert spec.api_base == default.api_base  # inherited (attr absent → getattr "")
    assert spec.credential_ref == default.credential_ref  # inherited
    assert spec.transport == default.transport  # inherited
    assert spec.temperature == 0.9  # overridden
    assert spec.max_tokens == default.max_tokens  # inherited
    assert spec.top_p == default.top_p  # inherited


def test_build_spec_uses_real_agentdef() -> None:
    """A real AgentDef (no api_base/credential_ref attrs yet) still resolves."""
    agent = AgentDef(
        id="e1",
        title="Cross-provider expert",
        default_provider="openai",
        default_model="gpt-4o-mini",
        parameters={"temperature": 0.4},
    )
    default = _default_spec()
    spec = build_spec(agent, default)
    assert spec.provider == "openai"
    assert spec.model == "gpt-4o-mini"
    # Fields AgentDef does not yet declare fall back to the default spec.
    assert spec.api_base == default.api_base
    assert spec.credential_ref == default.credential_ref
    assert spec.temperature == 0.4
