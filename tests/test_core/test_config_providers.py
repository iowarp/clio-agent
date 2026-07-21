"""
Tests for multi-provider LM configuration.

Tests LMProviderConfig, load_config_from_env, create_lm, and create_planner_lm.
"""

from pathlib import Path
from unittest.mock import patch

import dspy
import pytest

from clio_agent import conf
from clio_agent.config import (
    LMProviderConfig,
    create_lm,
    create_planner_lm,
    load_config_from_env,
)
from tests.env_isolation import isolated_environ


@pytest.fixture(autouse=True)
def _clean_model_file_layer(allow_pytest_tmp_path):
    """Resolve this module's LM config from a clean file layer w.r.t. ``lm.model``.

    #985 residual: the autouse ``allow_pytest_tmp_path`` fixture pins
    ``lm.model: ibm/granite-4-h-tiny`` in the per-test config FILE (file > env) to
    suppress LM discovery in agent-construction tests. This module, by contrast,
    exercises ``load_config_from_env`` / ``has_explicit_model_override`` *resolution*
    from a clean slate (env-layer and provider-default subjects). Dropping the
    fixture's ``lm.model`` file value restores exactly the pre-residual behaviour
    these tests assert — a config file without a pinned model — so their ENV and
    provider-default expectations resolve as they always did. Depends on
    ``allow_pytest_tmp_path`` so ``XDG_CONFIG_HOME`` is set before we edit the file.
    """
    from tests._config_layer import delete_config

    delete_config("lm.model")
    yield


class TestLMProviderConfig:
    """Test LMProviderConfig dataclass."""

    def test_default_provider_is_lm_studio(self):
        """Default provider should be lm_studio."""
        config = LMProviderConfig()
        assert config.provider == "lm_studio"

    def test_lm_studio_defaults(self):
        """LM Studio defaults should match PROVIDER_DEFAULTS."""
        config = LMProviderConfig(provider="lm_studio")
        assert config.api_base == "http://127.0.0.1:1234/v1"
        assert config.model == ""
        assert config.api_key == "lm-studio"

    def test_ollama_defaults(self):
        """Ollama defaults should match PROVIDER_DEFAULTS."""
        config = LMProviderConfig(provider="ollama")
        assert config.api_base == "http://127.0.0.1:11434/v1"
        assert config.model == "granite3.1-dense:8b"
        assert config.api_key == "ollama"

    def test_openai_defaults(self):
        """OpenAI defaults should load correct api_base and model."""
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test-123"}, clear=False):
            config = LMProviderConfig(provider="openai")
            assert config.api_base == "https://api.openai.com/v1"
            assert config.model == "gpt-4o-mini"
            assert config.api_key == "sk-test-123"

    def test_anthropic_defaults(self):
        """Anthropic defaults should load correct api_base and model."""
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=False):
            config = LMProviderConfig(provider="anthropic")
            assert config.api_base == "https://api.anthropic.com/v1"
            assert config.model == "claude-sonnet-4-20250514"
            assert config.api_key == "sk-ant-test"

    def test_explicit_values_override_defaults(self):
        """Explicitly provided values should not be overwritten by defaults."""
        config = LMProviderConfig(
            provider="lm_studio",
            api_base="http://custom:9999/v1",
            model="custom/model",
            api_key="custom-key",
        )
        assert config.api_base == "http://custom:9999/v1"
        assert config.model == "custom/model"
        assert config.api_key == "custom-key"

    def test_default_temperature(self):
        """Default agentic temperature should be 0.0 (deterministic structured output)."""
        config = LMProviderConfig()
        assert config.temperature == 0.0

    def test_default_planner_temperature(self):
        """Default planner temperature should be 0.3."""
        config = LMProviderConfig()
        assert config.planner_temperature == 0.3
        assert config.router_temperature == 0.3

    def test_router_temperature_alias(self):
        """Legacy router_temperature constructor arg should still configure the planner."""
        config = LMProviderConfig(router_temperature=0.2)
        assert config.planner_temperature == 0.2
        assert config.router_temperature == 0.2

    def test_default_max_tokens(self):
        """Default max_tokens should be 32000."""
        config = LMProviderConfig()
        assert config.max_tokens == 32000
        assert config.planner_max_tokens == 32000

    def test_qwopus_profile_hardens_planner_defaults(self):
        """Qwopus should get deterministic planning and a planner token floor."""
        config = LMProviderConfig(
            provider="lm_studio",
            model="qwopus3.5-9b-v3",
            max_tokens=1024,
        )
        assert config.max_tokens == 1024
        assert config.planner_temperature == 0.0
        assert config.router_temperature == 0.0
        assert config.planner_max_tokens == 4096

    def test_qwopus_profile_respects_temperature_but_enforces_planner_token_floor(self):
        """Qwopus should keep manual temperature but reject too-small planner caps."""
        config = LMProviderConfig(
            provider="lm_studio",
            model="qwopus3.5-9b-v3",
            max_tokens=1024,
            planner_temperature=0.2,
            planner_max_tokens=2048,
        )
        assert config.planner_temperature == 0.2
        assert config.planner_max_tokens == 4096

    def test_qwopus_profile_respects_explicit_planner_cap_above_floor(self):
        """Explicit planner caps above the local reasoning floor should win."""
        config = LMProviderConfig(
            provider="lm_studio",
            model="qwopus3.5-9b-v3",
            max_tokens=1024,
            planner_max_tokens=8192,
        )
        assert config.planner_max_tokens == 8192

    def test_default_environment(self):
        """Default environment should be 'dev'."""
        config = LMProviderConfig()
        assert config.environment == "dev"

    def test_default_codex_transport(self):
        """Codex transport defaults to app_server (native JSON-RPC/stdio, #896)."""
        config = LMProviderConfig(provider="codex")
        assert config.codex_transport == "app_server"

    def test_invalid_codex_transport_rejected(self):
        """Invalid Codex transport should fail during config construction."""
        with pytest.raises(ValueError, match="codex_transport"):
            LMProviderConfig(provider="codex", codex_transport="telepathy")  # type: ignore[arg-type]

    def test_claude_code_defaults(self):
        """Claude Code should not require an API key."""
        config = LMProviderConfig(provider="claude_code")
        assert config.api_base == "claude-code://sdk"
        assert config.model == "sonnet"
        assert config.api_key == ""
        assert config.claude_code_transport == "sdk"  # sdk is the default (best config)

    def test_invalid_claude_code_transport_rejected(self):
        """Invalid Claude Code transport should fail during config construction.

        'exec' and 'sdk' are both valid now; only a genuinely unknown value is rejected.
        """
        with pytest.raises(ValueError, match="claude_code_transport"):
            LMProviderConfig(provider="claude_code", claude_code_transport="bogus")  # type: ignore[arg-type]


class TestLoadConfigFromEnv:
    """Test load_config_from_env function."""

    def test_default_returns_lm_studio(self):
        """With no env vars, default provider is lm_studio."""
        with isolated_environ():
            config = load_config_from_env()
            assert config.provider == "lm_studio"
            assert config.api_base == "http://127.0.0.1:1234/v1"

    def test_cold_conf_cache_without_home_dir(self, monkeypatch):
        """Regression (#769 Slice 2): with the conf file-layer cache cold AND no
        resolvable home directory (on Windows ``Path.home()`` raises when the
        scrubbed environment lacks USERPROFILE/HOME), ``load_config_from_env``
        must degrade to env/default tiers instead of crashing with RuntimeError.
        ``Path.home`` is forced to raise so the Windows chain is exercised
        deterministically on every platform."""

        def _no_home() -> Path:
            raise RuntimeError("Could not determine home directory.")

        monkeypatch.setattr(Path, "home", staticmethod(_no_home))
        conf.reload()  # force the next resolve to hit ConfigStore._load
        try:
            with isolated_environ():
                config = load_config_from_env()
        finally:
            conf.reload()  # drop the degraded cache so later tests re-read fresh
        assert config.provider == "lm_studio"

    def test_ollama_provider_from_env(self):
        """CLIO_LM_PROVIDER=ollama should configure ollama defaults."""
        with isolated_environ({"CLIO_LM_PROVIDER": "ollama"}):
            config = load_config_from_env()
            assert config.provider == "ollama"
            assert config.api_base == "http://127.0.0.1:11434/v1"
            assert config.model == "granite3.1-dense:8b"

    def test_env_model_overrides_provider_default(self):
        """CLIO_LM_MODEL should override provider's default model."""
        env = {"CLIO_LM_PROVIDER": "lm_studio", "CLIO_LM_MODEL": "custom/override"}
        with isolated_environ(env):
            config = load_config_from_env()
            assert config.model == "custom/override"

    def test_env_api_base_override(self):
        """CLIO_LM_API_BASE should override provider's default api_base."""
        env = {"CLIO_LM_API_BASE": "http://remote:5000/v1"}
        with isolated_environ(env):
            config = load_config_from_env()
            assert config.api_base == "http://remote:5000/v1"

    def test_env_api_key_override(self):
        """CLIO_LM_API_KEY should override provider's default api_key."""
        env = {"CLIO_LM_API_KEY": "my-secret-key"}
        with isolated_environ(env):
            config = load_config_from_env()
            assert config.api_key == "my-secret-key"

    def test_env_temperature_override(self):
        """CLIO_LM_TEMPERATURE should override default temperature."""
        env = {"CLIO_LM_TEMPERATURE": "0.7"}
        with isolated_environ(env):
            config = load_config_from_env()
            assert config.temperature == 0.7

    def test_env_max_tokens_override(self):
        """CLIO_LM_MAX_TOKENS should override default max_tokens."""
        env = {"CLIO_LM_MAX_TOKENS": "8192"}
        with isolated_environ(env):
            config = load_config_from_env()
            assert config.max_tokens == 8192

    def test_env_planner_max_tokens_override(self):
        """CLIO_LM_PLANNER_MAX_TOKENS should override planner max tokens."""
        env = {"CLIO_LM_PLANNER_MAX_TOKENS": "2048"}
        with isolated_environ(env):
            config = load_config_from_env()
            assert config.max_tokens == 32000
            assert config.planner_max_tokens == 2048

    def test_env_qwopus_profile_without_manual_planner_tuning(self):
        """Qwopus via LM Studio should apply its planner profile from env config."""
        env = {
            "CLIO_LM_PROVIDER": "lm_studio",
            "CLIO_LM_MODEL": "qwopus3.5-9b-v3",
            "CLIO_LM_MAX_TOKENS": "1024",
        }
        with isolated_environ(env):
            config = load_config_from_env()
            assert config.planner_temperature == 0.0
            assert config.planner_max_tokens == 4096

    def test_env_qwopus_profile_raises_too_small_manual_planner_cap(self):
        """Qwopus planner caps below the known reliable floor are raised."""
        env = {
            "CLIO_LM_PROVIDER": "lm_studio",
            "CLIO_LM_MODEL": "qwopus3.5-9b-v3",
            "CLIO_LM_MAX_TOKENS": "8192",
            "CLIO_LM_PLANNER_MAX_TOKENS": "1024",
        }
        with isolated_environ(env):
            config = load_config_from_env()
            assert config.max_tokens == 8192
            assert config.planner_max_tokens == 4096

    def test_env_environment(self):
        """CLIO_ENVIRONMENT should set environment field."""
        env = {"CLIO_ENVIRONMENT": "production"}
        with isolated_environ(env):
            config = load_config_from_env()
            assert config.environment == "production"

    def test_openai_requires_api_key(self):
        """OpenAI provider without API key should raise ValueError."""
        env = {"CLIO_LM_PROVIDER": "openai"}
        with isolated_environ(env):
            with pytest.raises(ValueError, match="requires an API key"):
                load_config_from_env()

    def test_anthropic_requires_api_key(self):
        """Anthropic provider without API key should raise ValueError."""
        env = {"CLIO_LM_PROVIDER": "anthropic"}
        with isolated_environ(env):
            with pytest.raises(ValueError, match="requires an API key"):
                load_config_from_env()

    def test_openai_with_clio_api_key(self):
        """OpenAI with CLIO_LM_API_KEY should work."""
        env = {"CLIO_LM_PROVIDER": "openai", "CLIO_LM_API_KEY": "sk-from-clio"}
        with isolated_environ(env):
            config = load_config_from_env()
            assert config.api_key == "sk-from-clio"

    def test_openai_with_native_env_key(self):
        """OpenAI with OPENAI_API_KEY (not CLIO_) should work via __post_init__."""
        env = {"CLIO_LM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-native"}
        with isolated_environ(env):
            config = load_config_from_env()
            assert config.api_key == "sk-native"

    def test_codex_transport_from_env(self):
        """CLIO_CODEX_TRANSPORT accepts only app_server (v0.8.0 single transport)."""
        env = {"CLIO_LM_PROVIDER": "codex", "CLIO_CODEX_TRANSPORT": "app_server"}
        with isolated_environ(env):
            config = load_config_from_env()
            assert config.codex_transport == "app_server"

    def test_codex_removed_transport_from_env_raises(self):
        """A deleted transport in the env is a loud config error, not a downgrade."""
        env = {"CLIO_LM_PROVIDER": "codex", "CLIO_CODEX_TRANSPORT": "exec"}
        with isolated_environ(env):
            with pytest.raises(ValueError, match="removed in the v0.8.0 cleanup"):
                load_config_from_env()

    def test_claude_code_transport_from_env(self):
        """CLIO_CLAUDE_CODE_TRANSPORT accepts only sdk (v0.8.0 single transport)."""
        env = {"CLIO_LM_PROVIDER": "claude_code", "CLIO_CLAUDE_CODE_TRANSPORT": "sdk"}
        with isolated_environ(env):
            config = load_config_from_env()
            assert config.claude_code_transport == "sdk"

    def test_claude_code_removed_transport_from_env_raises(self):
        """A deleted transport in the env is a loud config error, not a downgrade."""
        env = {"CLIO_LM_PROVIDER": "claude_code", "CLIO_CLAUDE_CODE_TRANSPORT": "exec"}
        with isolated_environ(env):
            with pytest.raises(ValueError, match="removed in the v0.8.0 cleanup"):
                load_config_from_env()


class TestLoadConfigFileLayerWins:
    """Slice 2: LM boot config resolves file → env → default.

    A committed ``.clio``/user ``config.yaml`` ``lm.*`` key wins over the matching
    ``CLIO_LM_*`` environment variable; the secret ``CLIO_LM_API_KEY`` stays env-only.
    """

    @pytest.fixture(autouse=True)
    def _fresh_store(self):
        from clio_agent import conf

        conf.reload()
        yield
        conf.reload()

    @staticmethod
    def _write_user_config(body: str) -> None:
        import os
        from pathlib import Path

        from clio_agent import conf

        xdg = os.environ["XDG_CONFIG_HOME"]  # per-test tmp dir from conftest
        target = Path(xdg) / "clio-agent" / "config.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        conf.reload()

    def test_provider_file_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("CLIO_LM_PROVIDER", "openai")
        monkeypatch.setenv("CLIO_LM_API_KEY", "sk-x")  # openai would need a key
        self._write_user_config("lm:\n  provider: ollama\n")
        config = load_config_from_env()
        assert config.provider == "ollama"

    def test_model_file_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("CLIO_LM_PROVIDER", "lm_studio")
        monkeypatch.setenv("CLIO_LM_MODEL", "env/model")
        self._write_user_config("lm:\n  model: file/model\n")
        config = load_config_from_env()
        assert config.model == "file/model"

    def test_max_tokens_file_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("CLIO_LM_PROVIDER", "lm_studio")
        monkeypatch.setenv("CLIO_LM_MAX_TOKENS", "1234")
        self._write_user_config("lm:\n  max_tokens: 4321\n")
        config = load_config_from_env()
        assert config.max_tokens == 4321

    def test_environment_file_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("CLIO_ENVIRONMENT", "staging")
        self._write_user_config("runtime:\n  environment: production\n")
        config = load_config_from_env()
        assert config.environment == "production"

    def test_router_temperature_legacy_env_alias_is_retired(self, monkeypatch):
        # SABOTAGE twin (#985 move 1): the CLIO_LM_ROUTER_TEMPERATURE env alias was a
        # pure fall-through to the migrated lm.planner_temperature and is now deleted.
        # Setting it must be INERT — planner_temperature falls to its normal default,
        # never 0.42, so the retired alias can never silently re-acquire a reader.
        monkeypatch.setenv("CLIO_LM_PROVIDER", "lm_studio")
        monkeypatch.setenv("CLIO_LM_MODEL", "plain/model")  # avoid a profile override
        monkeypatch.delenv("CLIO_LM_PLANNER_TEMPERATURE", raising=False)
        monkeypatch.setenv("CLIO_LM_ROUTER_TEMPERATURE", "0.42")
        config = load_config_from_env()
        assert config.planner_temperature == 0.3

    def test_api_key_stays_env_only(self, monkeypatch):
        # A config file must NOT be able to supply the secret API key.
        monkeypatch.setenv("CLIO_LM_PROVIDER", "openai")
        monkeypatch.delenv("CLIO_LM_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        self._write_user_config("lm:\n  api_key: sk-from-file\n")
        with pytest.raises(ValueError, match="requires an API key"):
            load_config_from_env()


class TestHasExplicitModelOverride:
    """Slice 2: ``has_explicit_model_override`` honors the file layer."""

    @pytest.fixture(autouse=True)
    def _fresh_store(self):
        from clio_agent import conf

        conf.reload()
        yield
        conf.reload()

    def test_env_sets_override(self, monkeypatch):
        from clio_agent.config import has_explicit_model_override

        monkeypatch.setenv("CLIO_LM_MODEL", "some/model")
        assert has_explicit_model_override() is True

    def test_unset_is_false(self, monkeypatch):
        from clio_agent.config import has_explicit_model_override

        monkeypatch.delenv("CLIO_LM_MODEL", raising=False)
        assert has_explicit_model_override(env={}) is False

    def test_file_layer_counts_as_override(self, monkeypatch):
        import os
        from pathlib import Path

        from clio_agent import conf
        from clio_agent.config import has_explicit_model_override

        xdg = os.environ["XDG_CONFIG_HOME"]
        target = Path(xdg) / "clio-agent" / "config.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("lm:\n  model: file/model\n", encoding="utf-8")
        conf.reload()
        # Even with the env var absent, the pinned file model is an override.
        assert has_explicit_model_override(env={}) is True


class TestCreateLM:
    """Test create_lm function."""

    def test_returns_dspy_lm(self):
        """create_lm should return a dspy.LM instance."""
        config = LMProviderConfig(provider="lm_studio", model="loaded-model")
        lm = create_lm(config)
        assert isinstance(lm, dspy.LM)

    def test_lm_studio_uses_openai_prefix(self):
        """LM Studio models should get openai/ prefix."""
        config = LMProviderConfig(provider="lm_studio", model="loaded-model")
        lm = create_lm(config)
        assert "openai/" in lm.model

    def test_lm_studio_empty_model_discovers_loaded_model(self):
        """Blank LM Studio model means use the currently loaded model."""
        config = LMProviderConfig(provider="lm_studio")
        with patch("clio_agent.config.list_lm_studio_models", return_value=["qwopus3.5-9b-v3"]):
            lm = create_lm(config)
        assert lm.model == "openai/qwopus3.5-9b-v3"

    def test_ollama_uses_openai_prefix(self):
        """Ollama models should get openai/ prefix."""
        config = LMProviderConfig(provider="ollama")
        lm = create_lm(config)
        assert "openai/" in lm.model

    def test_argonne_sophia_preserves_openai_prefixed_model_ids(self):
        """Sophia GPT-OSS ids include openai/ as part of the served model id."""
        config = LMProviderConfig(
            provider="argonne",
            api_base="https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1",
            model="openai/gpt-oss-120b",
            api_key="token",
        )

        lm = create_lm(config)

        assert lm.model == "openai/openai/gpt-oss-120b"

    def test_argonne_metis_keeps_single_openai_provider_prefix(self):
        """Metis GPT-OSS ids do not need Sophia's double-prefix workaround."""
        config = LMProviderConfig(
            provider="argonne",
            api_base="https://inference-api.alcf.anl.gov/resource_server/metis/api/v1",
            model="openai/gpt-oss-120b",
            api_key="token",
        )

        lm = create_lm(config)

        assert lm.model == "openai/gpt-oss-120b"

    def test_argonne_sophia_huggingface_ids_keep_single_provider_prefix(self):
        """Sophia non-openai model ids still use the normal LiteLLM provider prefix."""
        config = LMProviderConfig(
            provider="argonne",
            api_base="https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1",
            model="meta-llama/Llama-4-Scout-17B-16E-Instruct",
            api_key="token",
        )

        lm = create_lm(config)

        assert lm.model == "openai/meta-llama/Llama-4-Scout-17B-16E-Instruct"

    def test_openai_uses_native_prefix(self):
        """OpenAI models should get openai/ prefix (native)."""
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}, clear=False):
            config = LMProviderConfig(provider="openai")
            lm = create_lm(config)
            assert lm.model.startswith("openai/")

    def test_anthropic_uses_native_prefix(self):
        """Anthropic models should get anthropic/ prefix."""
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant"}, clear=False):
            config = LMProviderConfig(provider="anthropic")
            lm = create_lm(config)
            assert lm.model.startswith("anthropic/")

    def test_codex_uses_custom_provider_prefix_with_internal_marker(self):
        """Codex should keep user-facing model ids clean and mark internally."""
        config = LMProviderConfig(provider="codex", model="gpt-5.5")
        lm = create_lm(config)
        assert lm.model == "codex/cdx-gpt-5.5"
        assert lm.kwargs["codex_transport"] == "app_server"

    def test_codex_model_marker_is_not_doubled(self):
        """Codex should accept already-prefixed config values idempotently."""
        config = LMProviderConfig(provider="codex", model="codex/cdx-gpt-5.5")
        lm = create_lm(config)
        assert lm.model == "codex/cdx-gpt-5.5"

    def test_codex_transport_passes_litellm_kwarg(self):
        """The codex transport should flow into dspy.LM kwargs."""
        config = LMProviderConfig(
            provider="codex",
            model="gpt-5.5",
            codex_transport="app_server",
        )
        lm = create_lm(config)
        assert lm.kwargs["codex_transport"] == "app_server"

    def test_codex_thinking_level_passes_codex_reasoning_effort_kwarg(self):
        """SEAM (#896): the #895 thinking level survives the factory into the LM
        kwargs as codex_reasoning_effort — the same optional_params lane
        codex_transport already proves reaches the CustomLLM. off → codex's
        explicit 'none' (never omit-and-inherit-ambient)."""
        config = LMProviderConfig(provider="codex", model="gpt-5.5", thinking_level="high")
        lm = create_lm(config)
        assert lm.kwargs["codex_reasoning_effort"] == "high"

        config_off = LMProviderConfig(provider="codex", model="gpt-5.5", thinking_level="off")
        lm_off = create_lm(config_off)
        assert lm_off.kwargs["codex_reasoning_effort"] == "none"

        # Unset level → no effort kwarg at all (codex's own default governs).
        config_default = LMProviderConfig(provider="codex", model="gpt-5.5")
        lm_default = create_lm(config_default)
        assert "codex_reasoning_effort" not in lm_default.kwargs

    def test_claude_code_uses_custom_provider_prefix(self):
        """Claude Code should keep user-facing model ids clean and mark internally."""
        config = LMProviderConfig(provider="claude_code", model="sonnet")
        lm = create_lm(config)
        assert lm.model == "claude_code/cc-sonnet"
        assert lm.kwargs["claude_code_transport"] == "sdk"  # sdk is the default

    def test_claude_code_model_marker_is_not_doubled(self):
        """Claude Code should accept already-prefixed config values idempotently."""
        config = LMProviderConfig(provider="claude_code", model="claude_code/cc-sonnet")
        lm = create_lm(config)
        assert lm.model == "claude_code/cc-sonnet"

    def test_each_provider_returns_lm(self):
        """All providers should produce valid dspy.LM instances."""
        for provider in ("lm_studio", "ollama"):
            model = "loaded-model" if provider == "lm_studio" else ""
            config = LMProviderConfig(provider=provider, model=model)
            lm = create_lm(config)
            assert isinstance(lm, dspy.LM), f"Failed for {provider}"


class TestCreatePlannerLM:
    """Test create_planner_lm function."""

    def test_returns_dspy_lm(self):
        """create_planner_lm should return a dspy.LM instance."""
        config = LMProviderConfig(provider="lm_studio", model="loaded-model")
        lm = create_planner_lm(config)
        assert isinstance(lm, dspy.LM)

    def test_uses_planner_temperature(self):
        """Planner LM should use planner_temperature, not temperature."""
        config = LMProviderConfig(
            provider="lm_studio",
            model="loaded-model",
            temperature=1.0,
            planner_temperature=0.3,
        )
        lm = create_planner_lm(config)
        # The temperature is set on the LM kwargs
        assert lm.kwargs.get("temperature") == 0.3

    def test_uses_planner_max_tokens(self):
        """Planner LM should use planner_max_tokens, not answer max_tokens."""
        config = LMProviderConfig(
            provider="lm_studio",
            model="loaded-model",
            max_tokens=1024,
            planner_max_tokens=4096,
        )
        lm = create_planner_lm(config)
        assert lm.kwargs.get("max_tokens") == 4096

    def test_custom_planner_temperature(self):
        """Planner LM should respect custom planner_temperature."""
        config = LMProviderConfig(
            provider="ollama",
            planner_temperature=0.1,
        )
        lm = create_planner_lm(config)
        assert lm.kwargs.get("temperature") == 0.1


class TestSetupDspy:
    """Test setup_dspy function."""

    def test_setup_returns_lm(self):
        """setup_dspy should return a dspy.LM instance."""
        from clio_agent.config import setup_dspy

        with isolated_environ({"CLIO_LM_MODEL": "loaded-model"}):
            lm = setup_dspy(verbose=False)
            assert isinstance(lm, dspy.LM)

    def test_setup_with_model_override(self):
        """setup_dspy with model override should use specified model."""
        from clio_agent.config import setup_dspy

        with isolated_environ():
            lm = setup_dspy(model="custom/model", verbose=False)
            assert "custom/model" in lm.model

    def test_setup_verbose_prints(self, capsys):
        """setup_dspy with verbose=True should print config info."""
        from clio_agent.config import setup_dspy

        with isolated_environ({"CLIO_LM_MODEL": "loaded-model"}):
            setup_dspy(verbose=True)
            captured = capsys.readouterr()
            assert "LM configured" in captured.out

    def test_setup_cloud_no_key_raises(self):
        """setup_dspy with cloud provider missing key should raise ValueError."""
        from clio_agent.config import setup_dspy

        env = {"CLIO_LM_PROVIDER": "openai"}
        with isolated_environ(env):
            with pytest.raises(ValueError, match="requires an API key"):
                setup_dspy(verbose=False)

    def test_setup_local_openai_compatible_endpoint_disables_json_fallback(self):
        """Pinned local LM Studio via OpenAI-compatible API should use text chat mode."""
        from clio_agent.config import setup_dspy

        env = {
            "CLIO_LM_PROVIDER": "openai",
            "CLIO_LM_API_BASE": "http://192.168.86.143:1234/v1",
            "CLIO_LM_API_KEY": "lm-studio",
            "CLIO_LM_MODEL": "nemotron-cascade-2-30b-a3b-i1",
        }
        with isolated_environ(env):
            # config.py imports dspy lazily via _dspy() — patch the
            # underlying dspy.configure directly rather than the
            # (no-longer-existent) module-level alias.
            with patch("dspy.configure") as mock_configure:
                setup_dspy(verbose=False)

        adapter = mock_configure.call_args.kwargs["adapter"]
        assert adapter.use_json_adapter_fallback is False

    def test_setup_cloud_openai_keeps_json_fallback(self):
        """Real OpenAI API should retain DSPy's JSON adapter fallback."""
        from clio_agent.config import setup_dspy

        env = {
            "CLIO_LM_PROVIDER": "openai",
            "CLIO_LM_API_KEY": "sk-test",
        }
        with isolated_environ(env):
            # config.py imports dspy lazily via _dspy() — patch the
            # underlying dspy.configure directly rather than the
            # (no-longer-existent) module-level alias.
            with patch("dspy.configure") as mock_configure:
                setup_dspy(verbose=False)

        adapter = mock_configure.call_args.kwargs["adapter"]
        assert adapter.use_json_adapter_fallback is True


class TestListLmStudioModels:
    """``list_lm_studio_models`` is the single CLI discovery path: it delegates to
    the unified :class:`LMStudioHandshake` (so there is no second HTTP probe to
    rot) while preserving the one CLI-specific behaviour — *retry while LM Studio
    is still loading a model*, and hard-fail (never a silent ``[]``) when nothing
    ever loads. The low-level ``/api/v0`` parsing is the handshake's job and is
    covered in ``tests/test_providers``; here we pin the wrapper contract against
    mocked handshake reports. The wrapper imports ``run_handshake_sync`` from the
    handshake package, so we patch it at its source module."""

    _PATCH = "clio_agent.providers.handshake.run_handshake_sync"

    @staticmethod
    def _report(*, ok: bool, models: tuple[str, ...] = (), error: str | None = None):
        """Minimal stand-in for a HandshakeReport (only the fields the wrapper reads)."""
        from types import SimpleNamespace

        return SimpleNamespace(
            ok=ok,
            models=tuple(SimpleNamespace(id=m) for m in models),
            error=error,
        )

    def test_returns_loaded_model_ids(self):
        """A reachable backend with loaded models yields their ids in one probe."""
        from clio_agent.config import list_lm_studio_models

        rep = self._report(ok=True, models=("model-1", "model-2"))
        with patch(self._PATCH, return_value=rep) as mock_hs:
            models = list_lm_studio_models(max_retries=1)
        assert models == ["model-1", "model-2"]
        assert mock_hs.call_count == 1

    def test_recovers_after_a_loading_delay(self):
        """Empty first probe, loaded second -> returns once the model loads."""
        from clio_agent.config import list_lm_studio_models

        reports = [
            self._report(ok=True, models=()),
            self._report(ok=True, models=("granite",)),
        ]
        with patch(self._PATCH, side_effect=reports), patch("time.sleep"):
            models = list_lm_studio_models(max_retries=5, retry_delay=0)
        assert models == ["granite"]

    def test_empty_models_retries_then_surfaces_configuration_error(self):
        """A persistently empty (but reachable) backend must not collapse to []."""
        from clio_agent.config import LMStudioDiscoveryError, list_lm_studio_models

        rep = self._report(ok=True, models=())
        with patch(self._PATCH, return_value=rep) as mock_hs, patch("time.sleep"):
            with pytest.raises(LMStudioDiscoveryError, match="no loaded models"):
                list_lm_studio_models(max_retries=3, retry_delay=0)
        assert mock_hs.call_count == 3

    def test_unreachable_surfaces_endpoint_error(self):
        """An unreachable backend preserves the handshake's connectivity error."""
        from clio_agent.config import LMStudioDiscoveryError, list_lm_studio_models

        rep = self._report(ok=False, models=(), error="ConnectionError: refused")
        with patch(self._PATCH, return_value=rep) as mock_hs, patch("time.sleep"):
            with pytest.raises(LMStudioDiscoveryError, match="refused"):
                list_lm_studio_models(max_retries=2, retry_delay=0)
        assert mock_hs.call_count == 2

    def test_discovery_is_names_only_offline(self):
        """Model discovery must not trigger the external context cascade."""
        from clio_agent.config import list_lm_studio_models

        rep = self._report(ok=True, models=("nemotron",))
        with patch(self._PATCH, return_value=rep) as mock_hs:
            models = list_lm_studio_models(base_url="http://192.168.86.143:1234/v1", max_retries=1)
        assert models == ["nemotron"]
        ctx = mock_hs.call_args.args[0]
        assert ctx.provider_kind == "lm_studio"
        assert ctx.api_base == "http://192.168.86.143:1234/v1"
        assert ctx.allow_external_sources is False
