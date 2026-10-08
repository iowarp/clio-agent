"""The health/doctor LM probe checks the provider CLIO actually uses.

Found live: with Codex configured in ``config.yaml`` and no agent built yet, ``/v1/health``
probed LM Studio (the env-only default) and answered 503 "unavailable". The probe now
takes the live bound LM, else the boot config the agent would be built from.
"""

from __future__ import annotations

import pytest

from clio_agent.config import LMProviderConfig
from clio_agent.gact.routes import provider_probe_env
from clio_agent.gact.routes.provider_probe_env import runtime_provider_probe_env


def _boot(monkeypatch: pytest.MonkeyPatch, provider: str, model: str, api_base: str) -> None:
    monkeypatch.setattr(
        provider_probe_env,
        "load_config_from_env",
        lambda: LMProviderConfig(provider=provider, model=model, api_base=api_base),
    )


def test_no_bound_lm_probes_the_boot_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLIO_LM_PROVIDER", raising=False)
    _boot(monkeypatch, "codex", "gpt-6-sol", "codex://direct")

    env = runtime_provider_probe_env({})

    assert (env["CLIO_LM_PROVIDER"], env["CLIO_LM_MODEL"], env["CLIO_LM_API_BASE"]) == (
        "codex",
        "gpt-6-sol",
        "codex://direct",
    )


def test_the_bound_lm_wins_over_the_boot_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _boot(monkeypatch, "codex", "gpt-6-sol", "codex://direct")

    env = runtime_provider_probe_env(
        {"provider": "ollama", "model": "llama3", "api_base": "http://127.0.0.1:11434"}
    )

    assert (env["CLIO_LM_PROVIDER"], env["CLIO_LM_MODEL"]) == ("ollama", "llama3")


def test_a_boot_config_error_is_left_for_the_probe_to_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misconfigured provider is the probe's typed MISCONFIGURED row, never a 500."""

    def _bad() -> LMProviderConfig:
        raise ValueError("Unsupported CLIO_LM_PROVIDER 'nope'")

    monkeypatch.setattr(provider_probe_env, "load_config_from_env", _bad)
    monkeypatch.setattr(provider_probe_env.conf, "resolve", lambda *_a, **_k: "nope")

    env = runtime_provider_probe_env({})

    assert env["CLIO_LM_PROVIDER"] == "nope"


def test_a_bound_provider_with_a_stored_key_is_not_reported_keyless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F024: a managed vLLM bind (kind openai, key in CLIO's store) is not "requires a key"."""
    from clio_agent.runtime.status import RuntimeProbe

    monkeypatch.delenv("CLIO_LM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(provider_probe_env, "_has_saved_key", lambda provider_id: True)
    bound = {
        "provider_id": "vllm",
        "provider": "openai",
        "api_base": "http://127.0.0.1:37153/v1",
        "model": "/models/qwen3-4b",
    }
    env = runtime_provider_probe_env(bound)
    assert env["CLIO_LM_KEY_SOURCE"] == "store:vllm"
    assert "CLIO_LM_API_KEY" not in env  # the secret never enters the probe env
    config, _source = RuntimeProbe(env=env)._load_lm_config()
    assert config.provider == "openai"

    monkeypatch.setattr(provider_probe_env, "_has_saved_key", lambda provider_id: False)
    env = runtime_provider_probe_env(bound)
    assert "CLIO_LM_KEY_SOURCE" not in env
    with pytest.raises(ValueError, match="requires CLIO_LM_API_KEY"):
        RuntimeProbe(env=env)._load_lm_config()
