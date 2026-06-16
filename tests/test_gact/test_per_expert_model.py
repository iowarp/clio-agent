"""Capability (a): per-expert model/provider selection (epic #667, #668).

An expert declares ``(provider, model)`` in its ``.md``; the dynamic-agent LM
config must resolve it — including registry PRESET ids that share a wire kind
(``argonne_sophia`` vs ``argonne_metis``) to DISTINCT endpoints. Passing a preset
id straight to ``LMProviderConfig`` would miss the kind-keyed defaults and fall
back to LM Studio, which is the bug this guards.

Offline + hermetic: the argonne Globus token is stubbed (no network, no auth).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from clio_agent.config import LMProviderConfig
from clio_agent.gact.app import _dynamic_agent_lm_config


@pytest.fixture(autouse=True)
def _stub_globus(monkeypatch):
    # Keep offline: don't mint a real Globus token when building argonne configs.
    monkeypatch.setattr(
        "clio_agent.config._resolve_argonne_api_key", lambda: "test-token"
    )


def _agent_def(provider: str = "", model: str = "", **params):
    return SimpleNamespace(
        default_provider=provider, default_model=model, parameters=params
    )


def _base(provider="lm_studio", model="base-model", api_base="http://127.0.0.1:1234/v1"):
    cfg = LMProviderConfig(provider=provider, model=model, api_base=api_base)
    return SimpleNamespace(_provider_config=cfg)


def test_preset_ids_sharing_a_kind_resolve_to_distinct_endpoints():
    """The two-ALCF-providers contract: sophia and metis are both kind 'argonne'
    but must reach their own api_base."""
    base = _base()
    sophia = _dynamic_agent_lm_config(base, _agent_def("argonne_sophia"))
    metis = _dynamic_agent_lm_config(base, _agent_def("argonne_metis"))
    assert sophia.provider == "argonne" and metis.provider == "argonne"
    assert sophia.api_base.endswith("/sophia/vllm/v1")
    assert metis.api_base.endswith("/metis/api/v1")
    assert sophia.api_base != metis.api_base


def test_preset_id_does_not_fall_back_to_lm_studio():
    """Regression: a preset id must NOT collapse to the LM Studio default endpoint
    or key (which would also point at the user's local GPU)."""
    base = _base()
    cfg = _dynamic_agent_lm_config(base, _agent_def("argonne_sophia"))
    assert "127.0.0.1:1234" not in cfg.api_base
    # argonne kind -> Globus auth path (stubbed), not the lm_studio default key
    assert cfg.api_key == "test-token"


def test_declared_model_is_honored_cross_provider():
    base = _base()
    cfg = _dynamic_agent_lm_config(
        base, _agent_def("argonne_sophia", "openai/gpt-oss-120b")
    )
    assert cfg.model == "openai/gpt-oss-120b"


def test_preset_supplies_its_own_suggested_model_when_unset():
    base = _base()
    metis = _dynamic_agent_lm_config(base, _agent_def("argonne_metis"))
    assert metis.model == "gpt-oss-120b"  # metis preset's suggested model


def test_kind_declaration_still_works():
    """Declaring the bare wire kind keeps working (sophia is the argonne default)."""
    base = _base()
    cfg = _dynamic_agent_lm_config(base, _agent_def("argonne"))
    assert cfg.provider == "argonne"
    assert cfg.api_base.endswith("/sophia/vllm/v1")


def test_same_provider_no_override_inherits_base_endpoint():
    """The common path is untouched: same provider, no override -> inherit base."""
    base = _base(api_base="http://127.0.0.1:4321/v1")
    cfg = _dynamic_agent_lm_config(base, _agent_def(model="other-model"))
    assert cfg.provider == "lm_studio"
    assert cfg.api_base == "http://127.0.0.1:4321/v1"  # not clobbered
    assert cfg.model == "other-model"


def test_no_override_uses_base_verbatim():
    base = _base(provider="lm_studio", model="base-model")
    cfg = _dynamic_agent_lm_config(base, _agent_def())
    assert cfg.provider == "lm_studio"
    assert cfg.model == "base-model"


def test_unknown_preset_raises_not_silent_lmstudio():
    """A typo'd provider must fail loudly, not silently route to the local LM Studio
    endpoint (the bug an ALCF-intended expert would hit)."""
    base = _base()
    with pytest.raises(ValueError, match="unknown provider"):
        _dynamic_agent_lm_config(base, _agent_def("argonne_sohpia"))  # typo of _sophia
    with pytest.raises(ValueError, match="unknown provider"):
        _dynamic_agent_lm_config(base, _agent_def("not_a_provider"))


def test_per_expert_params_are_honored():
    """temperature/max_tokens/thinking_budget from the expert's parameters reach the
    config (previously parsed but never asserted)."""
    base = _base()
    cfg = _dynamic_agent_lm_config(
        base, _agent_def(temperature="0.7", max_tokens="1234", thinking_budget="555")
    )
    assert cfg.temperature == 0.7
    assert cfg.max_tokens == 1234
    assert cfg.thinking_budget == 555


def test_invalid_param_value_raises():
    base = _base()
    with pytest.raises(ValueError):
        _dynamic_agent_lm_config(base, _agent_def(temperature="not-a-number"))
