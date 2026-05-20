"""
Tests for multi-provider LM configuration.

Tests LMProviderConfig, load_config_from_env, create_lm, and create_planner_lm.
"""

from unittest.mock import MagicMock, patch

import dspy
import pytest

from clio_agent.config import (
    LMProviderConfig,
    create_lm,
    create_planner_lm,
    create_router_lm,
    load_config_from_env,
)


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
        assert config.model == "ibm/granite-4-h-tiny"
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
        """Default temperature should be 1.0."""
        config = LMProviderConfig()
        assert config.temperature == 1.0

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

    def test_qwopus_profile_respects_explicit_planner_knobs(self):
        """Explicit planner settings should win over profile defaults."""
        config = LMProviderConfig(
            provider="lm_studio",
            model="qwopus3.5-9b-v3",
            max_tokens=1024,
            planner_temperature=0.2,
            planner_max_tokens=2048,
        )
        assert config.planner_temperature == 0.2
        assert config.planner_max_tokens == 2048

    def test_default_environment(self):
        """Default environment should be 'dev'."""
        config = LMProviderConfig()
        assert config.environment == "dev"

    def test_default_codex_transport(self):
        """Codex transport should default to exec."""
        config = LMProviderConfig(provider="codex")
        assert config.codex_transport == "exec"

    def test_invalid_codex_transport_rejected(self):
        """Invalid Codex transport should fail during config construction."""
        with pytest.raises(ValueError, match="codex_transport"):
            LMProviderConfig(provider="codex", codex_transport="telepathy")  # type: ignore[arg-type]


class TestLoadConfigFromEnv:
    """Test load_config_from_env function."""

    def test_default_returns_lm_studio(self):
        """With no env vars, default provider is lm_studio."""
        with patch.dict("os.environ", {}, clear=True):
            config = load_config_from_env()
            assert config.provider == "lm_studio"
            assert config.api_base == "http://127.0.0.1:1234/v1"

    def test_ollama_provider_from_env(self):
        """CLIO_LM_PROVIDER=ollama should configure ollama defaults."""
        with patch.dict("os.environ", {"CLIO_LM_PROVIDER": "ollama"}, clear=True):
            config = load_config_from_env()
            assert config.provider == "ollama"
            assert config.api_base == "http://127.0.0.1:11434/v1"
            assert config.model == "granite3.1-dense:8b"

    def test_env_model_overrides_provider_default(self):
        """CLIO_LM_MODEL should override provider's default model."""
        env = {"CLIO_LM_PROVIDER": "lm_studio", "CLIO_LM_MODEL": "custom/override"}
        with patch.dict("os.environ", env, clear=True):
            config = load_config_from_env()
            assert config.model == "custom/override"

    def test_env_api_base_override(self):
        """CLIO_LM_API_BASE should override provider's default api_base."""
        env = {"CLIO_LM_API_BASE": "http://remote:5000/v1"}
        with patch.dict("os.environ", env, clear=True):
            config = load_config_from_env()
            assert config.api_base == "http://remote:5000/v1"

    def test_env_api_key_override(self):
        """CLIO_LM_API_KEY should override provider's default api_key."""
        env = {"CLIO_LM_API_KEY": "my-secret-key"}
        with patch.dict("os.environ", env, clear=True):
            config = load_config_from_env()
            assert config.api_key == "my-secret-key"

    def test_env_temperature_override(self):
        """CLIO_LM_TEMPERATURE should override default temperature."""
        env = {"CLIO_LM_TEMPERATURE": "0.7"}
        with patch.dict("os.environ", env, clear=True):
            config = load_config_from_env()
            assert config.temperature == 0.7

    def test_env_max_tokens_override(self):
        """CLIO_LM_MAX_TOKENS should override default max_tokens."""
        env = {"CLIO_LM_MAX_TOKENS": "8192"}
        with patch.dict("os.environ", env, clear=True):
            config = load_config_from_env()
            assert config.max_tokens == 8192

    def test_env_planner_max_tokens_override(self):
        """CLIO_LM_PLANNER_MAX_TOKENS should override planner max tokens."""
        env = {"CLIO_LM_PLANNER_MAX_TOKENS": "2048"}
        with patch.dict("os.environ", env, clear=True):
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
        with patch.dict("os.environ", env, clear=True):
            config = load_config_from_env()
            assert config.planner_temperature == 0.0
            assert config.planner_max_tokens == 4096

    def test_env_environment(self):
        """CLIO_ENVIRONMENT should set environment field."""
        env = {"CLIO_ENVIRONMENT": "production"}
        with patch.dict("os.environ", env, clear=True):
            config = load_config_from_env()
            assert config.environment == "production"

    def test_openai_requires_api_key(self):
        """OpenAI provider without API key should raise ValueError."""
        env = {"CLIO_LM_PROVIDER": "openai"}
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(ValueError, match="requires an API key"):
                load_config_from_env()

    def test_anthropic_requires_api_key(self):
        """Anthropic provider without API key should raise ValueError."""
        env = {"CLIO_LM_PROVIDER": "anthropic"}
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(ValueError, match="requires an API key"):
                load_config_from_env()

    def test_openai_with_clio_api_key(self):
        """OpenAI with CLIO_LM_API_KEY should work."""
        env = {"CLIO_LM_PROVIDER": "openai", "CLIO_LM_API_KEY": "sk-from-clio"}
        with patch.dict("os.environ", env, clear=True):
            config = load_config_from_env()
            assert config.api_key == "sk-from-clio"

    def test_openai_with_native_env_key(self):
        """OpenAI with OPENAI_API_KEY (not CLIO_) should work via __post_init__."""
        env = {"CLIO_LM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-native"}
        with patch.dict("os.environ", env, clear=True):
            config = load_config_from_env()
            assert config.api_key == "sk-native"

    def test_codex_transport_from_env(self):
        """CLIO_CODEX_TRANSPORT should select the Codex transport mode."""
        env = {"CLIO_LM_PROVIDER": "codex", "CLIO_CODEX_TRANSPORT": "sdk"}
        with patch.dict("os.environ", env, clear=True):
            config = load_config_from_env()
            assert config.codex_transport == "sdk"


class TestCreateLM:
    """Test create_lm function."""

    def test_returns_dspy_lm(self):
        """create_lm should return a dspy.LM instance."""
        config = LMProviderConfig(provider="lm_studio")
        lm = create_lm(config)
        assert isinstance(lm, dspy.LM)

    def test_lm_studio_uses_openai_prefix(self):
        """LM Studio models should get openai/ prefix."""
        config = LMProviderConfig(provider="lm_studio")
        lm = create_lm(config)
        assert "openai/" in lm.model

    def test_ollama_uses_openai_prefix(self):
        """Ollama models should get openai/ prefix."""
        config = LMProviderConfig(provider="ollama")
        lm = create_lm(config)
        assert "openai/" in lm.model

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
        assert lm.kwargs["codex_transport"] == "exec"

    def test_codex_model_marker_is_not_doubled(self):
        """Codex should accept already-prefixed config values idempotently."""
        config = LMProviderConfig(provider="codex", model="codex/cdx-gpt-5.5")
        lm = create_lm(config)
        assert lm.model == "codex/cdx-gpt-5.5"

    def test_codex_sdk_transport_passes_litellm_kwarg(self):
        """Codex SDK transport should flow into dspy.LM kwargs."""
        config = LMProviderConfig(
            provider="codex",
            model="gpt-5.5",
            codex_transport="sdk",
        )
        lm = create_lm(config)
        assert lm.kwargs["codex_transport"] == "sdk"

    def test_each_provider_returns_lm(self):
        """All providers should produce valid dspy.LM instances."""
        for provider in ("lm_studio", "ollama"):
            config = LMProviderConfig(provider=provider)
            lm = create_lm(config)
            assert isinstance(lm, dspy.LM), f"Failed for {provider}"


class TestCreatePlannerLM:
    """Test create_planner_lm function."""

    def test_returns_dspy_lm(self):
        """create_planner_lm should return a dspy.LM instance."""
        config = LMProviderConfig(provider="lm_studio")
        lm = create_planner_lm(config)
        assert isinstance(lm, dspy.LM)

    def test_uses_planner_temperature(self):
        """Planner LM should use planner_temperature, not temperature."""
        config = LMProviderConfig(
            provider="lm_studio",
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

    def test_router_lm_alias(self):
        """create_router_lm remains as a compatibility alias."""
        config = LMProviderConfig(provider="lm_studio")
        lm = create_router_lm(config)
        assert isinstance(lm, dspy.LM)


class TestSetupDspy:
    """Test setup_dspy function."""

    def test_setup_returns_lm(self):
        """setup_dspy should return a dspy.LM instance."""
        from clio_agent.config import setup_dspy

        with patch.dict("os.environ", {}, clear=True):
            lm = setup_dspy(verbose=False)
            assert isinstance(lm, dspy.LM)

    def test_setup_with_model_override(self):
        """setup_dspy with model override should use specified model."""
        from clio_agent.config import setup_dspy

        with patch.dict("os.environ", {}, clear=True):
            lm = setup_dspy(model="custom/model", verbose=False)
            assert "custom/model" in lm.model

    def test_setup_verbose_prints(self, capsys):
        """setup_dspy with verbose=True should print config info."""
        from clio_agent.config import setup_dspy

        with patch.dict("os.environ", {}, clear=True):
            setup_dspy(verbose=True)
            captured = capsys.readouterr()
            assert "LM configured" in captured.out

    def test_setup_cloud_no_key_raises(self):
        """setup_dspy with cloud provider missing key should raise ValueError."""
        from clio_agent.config import setup_dspy

        env = {"CLIO_LM_PROVIDER": "openai"}
        with patch.dict("os.environ", env, clear=True):
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
        with patch.dict("os.environ", env, clear=True):
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
        with patch.dict("os.environ", env, clear=True):
            # config.py imports dspy lazily via _dspy() — patch the
            # underlying dspy.configure directly rather than the
            # (no-longer-existent) module-level alias.
            with patch("dspy.configure") as mock_configure:
                setup_dspy(verbose=False)

        adapter = mock_configure.call_args.kwargs["adapter"]
        assert adapter.use_json_adapter_fallback is True


class TestFetchLmStudioModels:
    """Test fetch_lm_studio_models with mocked HTTP."""

    def test_successful_fetch(self):
        """Should return model list on successful response."""
        from clio_agent.config import fetch_lm_studio_models

        mock_response = {"data": [{"id": "model-1"}, {"id": "model-2"}]}
        with patch("clio_agent.config.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.json.return_value = mock_response
            mock_resp.raise_for_status.return_value = None
            mock_get.return_value = mock_resp

            models = fetch_lm_studio_models(max_retries=1)
            assert models == ["model-1", "model-2"]

    def test_fetch_accepts_api_base_with_v1_suffix(self):
        """Configured LM Studio api_base values may already include /v1."""
        from clio_agent.config import fetch_lm_studio_models

        with patch("clio_agent.config.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"data": [{"id": "nemotron"}]}
            mock_resp.raise_for_status.return_value = None
            mock_get.return_value = mock_resp

            models = fetch_lm_studio_models(
                base_url="http://192.168.86.143:1234/v1",
                max_retries=1,
            )

        assert models == ["nemotron"]
        mock_get.assert_called_once_with("http://192.168.86.143:1234/v1/models", timeout=10)

    def test_empty_models_retries_then_surfaces_configuration_error(self):
        """Empty discovery should not collapse into a silent [] fallback."""
        from clio_agent.config import LMStudioDiscoveryError, fetch_lm_studio_models

        with patch("clio_agent.config.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"data": []}
            mock_resp.raise_for_status.return_value = None
            mock_get.return_value = mock_resp

            with patch("time.sleep"):
                with pytest.raises(LMStudioDiscoveryError, match="no loaded models"):
                    fetch_lm_studio_models(max_retries=2, retry_delay=0)
                assert mock_get.call_count == 2

    def test_connection_error_retries_then_surfaces_provider_error(self):
        """Connection exhaustion should preserve the endpoint error."""
        import requests as req

        from clio_agent.config import LMStudioDiscoveryError, fetch_lm_studio_models

        with patch("clio_agent.config.requests.get") as mock_get:
            mock_get.side_effect = req.exceptions.ConnectionError("refused")

            with patch("time.sleep"):
                with pytest.raises(LMStudioDiscoveryError, match="Could not connect"):
                    fetch_lm_studio_models(max_retries=2, retry_delay=0)
                assert mock_get.call_count == 2

    def test_http_error_surfaces_provider_error(self):
        """HTTP errors should not be reclassified as no loaded model."""
        import requests as req

        from clio_agent.config import LMStudioDiscoveryError, fetch_lm_studio_models

        with patch("clio_agent.config.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.raise_for_status.side_effect = req.exceptions.HTTPError("500 server")
            mock_get.return_value = mock_resp

            with pytest.raises(LMStudioDiscoveryError, match="500 server"):
                fetch_lm_studio_models(max_retries=1)

    def test_unexpected_error_surfaces_provider_error(self):
        """Unexpected client errors should not return an empty model list."""
        from clio_agent.config import LMStudioDiscoveryError, fetch_lm_studio_models

        with patch("clio_agent.config.requests.get") as mock_get:
            mock_get.side_effect = RuntimeError("weird error")

            with pytest.raises(LMStudioDiscoveryError, match="weird error"):
                fetch_lm_studio_models(max_retries=1)

    def test_invalid_json_surfaces_provider_error(self):
        """Malformed provider JSON should preserve the parsing failure."""
        from clio_agent.config import LMStudioDiscoveryError, fetch_lm_studio_models

        with patch("clio_agent.config.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.json.side_effect = ValueError("not json")
            mock_get.return_value = mock_resp

            with pytest.raises(LMStudioDiscoveryError, match="invalid JSON"):
                fetch_lm_studio_models(max_retries=1)

    def test_malformed_response_surfaces_provider_error(self):
        """A response without data[] should be reported as malformed."""
        from clio_agent.config import LMStudioDiscoveryError, fetch_lm_studio_models

        with patch("clio_agent.config.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.json.return_value = {"models": [{"id": "x"}]}
            mock_get.return_value = mock_resp

            with pytest.raises(LMStudioDiscoveryError, match="missing data"):
                fetch_lm_studio_models(max_retries=1)
