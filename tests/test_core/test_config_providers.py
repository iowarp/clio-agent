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
        """Ollama defaults should match PROVIDER_DEFAULTS.

        No trailing ``/v1``: LiteLLM's native ``ollama_chat`` provider appends
        its own ``/api/chat`` to this base (#1413) — a ``/v1`` suffix here
        would double into ``/v1/api/chat`` and 404.
        """
        # model-capabilities brief 9.1: no compiled-in suggested model id --
        # discovery (/api/tags) supplies it; the catalog default is "".
        config = LMProviderConfig(provider="ollama")
        assert config.api_base == "http://127.0.0.1:11434"
        assert config.model == ""
        assert config.api_key == "ollama"

    def test_openai_defaults(self):
        """OpenAI defaults should load correct api_base; model is discovery-only now."""
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test-123"}, clear=False):
            config = LMProviderConfig(provider="openai")
            assert config.api_base == "https://api.openai.com/v1"
            assert config.model == ""
            assert config.api_key == "sk-test-123"

    def test_anthropic_defaults(self):
        """Anthropic defaults should load correct api_base; model is discovery-only now."""
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=False):
            config = LMProviderConfig(provider="anthropic")
            assert config.api_base == "https://api.anthropic.com/v1"
            assert config.model == ""
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
        """Default temperature is unset (model-capabilities brief Part 7 item 1):
        with no value, the provider/model's own sampling default applies instead
        of clio forcing temp-0 on every model."""
        config = LMProviderConfig()
        assert config.temperature is None

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
        """Default max_tokens leaves the output budget to the provider (#1323)."""
        config = LMProviderConfig()
        assert config.max_tokens == 0
        assert config.planner_max_tokens == 0

    def test_qwopus_profile_keeps_exact_inherited_cap(self):
        # model-capabilities brief 9.1: the qwen-name heuristic
        # (_uses_local_reasoning_model_profile / _apply_model_profile_defaults)
        # that used to force planner_temperature/router_temperature to 0.0 for a
        # "qwopus"-named model is deleted -- no per-model-name matching in code.
        # planner_temperature keeps its own explicit default (0.3) regardless of
        # the model name; only the inherited max_tokens cap is asserted here.
        config = LMProviderConfig(
            provider="lm_studio",
            model="qwopus3.5-9b-v3",
            max_tokens=1024,
        )
        assert config.max_tokens == 1024
        assert config.planner_temperature == 0.3
        assert config.router_temperature == 0.3
        assert config.planner_max_tokens == 1024

    def test_qwopus_profile_respects_exact_explicit_planner_cap(self):
        config = LMProviderConfig(
            provider="lm_studio",
            model="qwopus3.5-9b-v3",
            max_tokens=1024,
            planner_temperature=0.2,
            planner_max_tokens=2048,
        )
        assert config.planner_temperature == 0.2
        assert config.planner_max_tokens == 2048

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
        """Codex transport defaults to the direct websocket transport (A.6)."""
        config = LMProviderConfig(provider="codex")
        assert config.codex_transport == "websocket"
        assert config.parse_retry_capability == "single_attempt"

    def test_invalid_codex_transport_rejected(self):
        """Invalid Codex transport should fail during config construction."""
        with pytest.raises(ValueError, match="codex_transport"):
            LMProviderConfig(provider="codex", codex_transport="telepathy")  # type: ignore[arg-type]

    def test_claude_code_defaults(self):
        """Claude Code needs no API key and has no synthetic model default."""
        config = LMProviderConfig(provider="claude_code")
        assert config.api_base == "claude-code://sdk"
        assert config.model == ""
        assert config.api_key == ""
        assert config.claude_code_transport == "sdk"  # sdk is the default (best config)

    def test_invalid_claude_code_transport_rejected(self):
        """Invalid Claude Code transport should fail during config construction.

        'exec' and 'sdk' are both valid now; only a genuinely unknown value is rejected.
        """
        with pytest.raises(ValueError, match="claude_code_transport"):
            LMProviderConfig(provider="claude_code", claude_code_transport="bogus")  # type: ignore[arg-type]


class TestProviderIdIsAnIdentityNotAKind:
    """provider_id names a real preset; it gets no kind_default fallback (Part 3).

    Only the no-provider_id convenience form (``LMProviderConfig(provider=...)``)
    resolves through the kind default -- an explicit provider_id is an identity
    claim and a bogus one is a typed error, never a silent redirect to
    whichever preset happens to share that kind.
    """

    def test_unknown_provider_id_is_a_typed_error(self):
        with pytest.raises(ValueError, match="Unknown LM provider"):
            LMProviderConfig(provider_id="not-a-real-provider")

    def test_a_bare_kind_string_as_provider_id_is_a_typed_error(self):
        """"argonne" is a kind, not a preset id (the presets are argonne_sophia /
        argonne_metis); passed explicitly as provider_id it must error rather
        than silently resolve to the kind's default preset."""
        with pytest.raises(ValueError, match="Unknown LM provider"):
            LMProviderConfig(provider_id="argonne")

    def test_bare_kind_construction_still_resolves_via_kind_default(self):
        """With no provider_id at all, ``provider="argonne"`` is still a valid
        convenience construction (used throughout the test suite) and resolves
        to the kind's designated default preset."""
        config = LMProviderConfig(provider="argonne")
        assert config.provider_id == "argonne_sophia"

    def test_explicit_provider_id_is_preserved_exactly(self):
        config = LMProviderConfig(provider_id="argonne_metis")
        assert config.provider_id == "argonne_metis"
        assert config.provider == "argonne"


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
        """CLIO_LM_PROVIDER=ollama should configure ollama defaults (no /v1, #1413)."""
        with isolated_environ({"CLIO_LM_PROVIDER": "ollama"}):
            config = load_config_from_env()
            assert config.provider == "ollama"
            assert config.api_base == "http://127.0.0.1:11434"
            assert config.model == ""

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
            assert config.max_tokens == 0
            assert config.planner_max_tokens == 2048

    def test_env_qwopus_profile_without_manual_planner_tuning(self):
        """No per-model-name planner profile any more (brief 9.1) -- planner_temperature
        keeps its own explicit default regardless of the configured model's name."""
        env = {
            "CLIO_LM_PROVIDER": "lm_studio",
            "CLIO_LM_MODEL": "qwopus3.5-9b-v3",
            "CLIO_LM_MAX_TOKENS": "1024",
        }
        with isolated_environ(env):
            config = load_config_from_env()
            assert config.planner_temperature == 0.3
            assert config.planner_max_tokens == 1024

    def test_env_qwopus_profile_preserves_small_manual_planner_cap(self):
        """Explicit positive planner caps are sent exactly."""
        env = {
            "CLIO_LM_PROVIDER": "lm_studio",
            "CLIO_LM_MODEL": "qwopus3.5-9b-v3",
            "CLIO_LM_MAX_TOKENS": "8192",
            "CLIO_LM_PLANNER_MAX_TOKENS": "1024",
        }
        with isolated_environ(env):
            config = load_config_from_env()
            assert config.max_tokens == 8192
            assert config.planner_max_tokens == 1024

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
        """CLIO_CODEX_TRANSPORT accepts websocket (default, A.6) or sse."""
        env = {"CLIO_LM_PROVIDER": "codex", "CLIO_CODEX_TRANSPORT": "sse"}
        with isolated_environ(env):
            config = load_config_from_env()
            assert config.codex_transport == "sse"

    def test_codex_invalid_transport_from_env_raises(self):
        """An invalid transport in the env is a loud config error, not a silent downgrade."""
        env = {"CLIO_LM_PROVIDER": "codex", "CLIO_CODEX_TRANSPORT": "exec"}
        with isolated_environ(env):
            with pytest.raises(ValueError, match="codex_transport"):
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

    def test_ollama_uses_native_litellm_prefix(self):
        """Ollama chat models use LiteLLM's explicit Ollama chat route."""
        # model-capabilities brief 9.1: no compiled-in suggested model any
        # more, so an explicit model is required to actually construct an LM.
        config = LMProviderConfig(provider="ollama", model="llama3.2")
        lm = create_lm(config)
        assert lm.model.startswith("ollama_chat/")

    def test_argonne_sophia_preserves_openai_prefixed_model_ids(self):
        """Sophia GPT-OSS ids include openai/ as part of the served model id."""
        config = LMProviderConfig(
            provider="argonne",
            api_base="https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1",
            model="openai/gpt-oss-120b",
            api_key="token",
        )

        lm = create_lm(config)

        assert lm.model == "hosted_vllm/openai/gpt-oss-120b"

    def test_argonne_metis_keeps_single_openai_provider_prefix(self):
        """Metis GPT-OSS ids do not need Sophia's double-prefix workaround."""
        config = LMProviderConfig(
            provider="argonne",
            api_base="https://inference-api.alcf.anl.gov/resource_server/metis/api/v1",
            model="openai/gpt-oss-120b",
            api_key="token",
        )

        lm = create_lm(config)

        assert lm.model == "hosted_vllm/openai/gpt-oss-120b"

    def test_argonne_sophia_huggingface_ids_keep_single_provider_prefix(self):
        """Sophia non-openai model ids still use the normal LiteLLM provider prefix."""
        config = LMProviderConfig(
            provider="argonne",
            api_base="https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1",
            model="meta-llama/Llama-4-Scout-17B-16E-Instruct",
            api_key="token",
        )

        lm = create_lm(config)

        assert lm.model == "hosted_vllm/meta-llama/Llama-4-Scout-17B-16E-Instruct"

    def test_openai_uses_native_prefix(self):
        """OpenAI models should get openai/ prefix (native)."""
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}, clear=False):
            config = LMProviderConfig(provider="openai", model="gpt-4o-mini")
            lm = create_lm(config)
            assert lm.model.startswith("openai/")

    def test_anthropic_uses_native_prefix(self):
        """Anthropic models should get anthropic/ prefix."""
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant"}, clear=False):
            config = LMProviderConfig(provider="anthropic", model="claude-haiku-4-5")
            lm = create_lm(config)
            assert lm.model.startswith("anthropic/")

    def test_codex_uses_custom_provider_prefix_with_internal_marker(self):
        """Codex should keep user-facing model ids clean and mark internally.

        The litellm-facing prefix is "codex_direct" (never bare "codex" --
        litellm ships its own native "codex" provider; see
        providers.codex.constants.LITELLM_PROVIDER).
        """
        config = LMProviderConfig(provider="codex", model="gpt-5.5")
        lm = create_lm(config)
        assert lm.model == "codex_direct/cg-gpt-5.5"
        assert lm.kwargs["codex_transport"] == "websocket"

    def test_codex_model_marker_is_not_doubled(self):
        """Codex should accept already-prefixed config values idempotently."""
        config = LMProviderConfig(provider="codex", model="codex_direct/cg-gpt-5.5")
        lm = create_lm(config)
        assert lm.model == "codex_direct/cg-gpt-5.5"

    def test_codex_legacy_prefix_is_stripped_defensively(self):
        """A config persisted before the litellm-prefix rename (bare 'codex/')
        still resolves to the current 'codex_direct/' wire prefix, never doubled."""
        config = LMProviderConfig(provider="codex", model="codex/cg-gpt-5.5")
        lm = create_lm(config)
        assert lm.model == "codex_direct/cg-gpt-5.5"

    def test_codex_transport_passes_litellm_kwarg(self):
        """The codex transport should flow into dspy.LM kwargs."""
        config = LMProviderConfig(
            provider="codex",
            model="gpt-5.5",
            codex_transport="sse",
        )
        lm = create_lm(config)
        assert lm.kwargs["codex_transport"] == "sse"

    def test_codex_thinking_level_passes_codex_reasoning_effort_kwarg(self):
        """SEAM (#896): the #895 thinking level survives the factory into the LM
        kwargs as codex_reasoning_effort — the same optional_params lane
        codex_transport already proves reaches the CustomLLM. off → codex's
        explicit 'none' (never omit-and-inherit-ambient)."""
        from clio_agent.providers.capabilities import invalidation
        from clio_agent.providers.capabilities.records import (
            DeploymentCapabilities,
            EndpointCapabilities,
            Fact,
            ModelCapabilities,
            ThinkingSpec,
        )

        invalidation.clear_all()
        now = "2026-01-01T00:00:00+00:00"
        invalidation.record_endpoint_capabilities(
            EndpointCapabilities(
                provider_id="codex",
                api_base="codex://direct",
                dialect="codex",
                thinking_controls=Fact(frozenset({"reasoning_effort"}), "dialect", now),
            )
        )
        invalidation.record_model_capabilities(
            ModelCapabilities(
                model_key="test:codex:gpt-5.5",
                thinking=Fact(
                    ThinkingSpec(
                        mechanism="effort_levels", levels=("high",), effort_by_level={}
                    ),
                    "server_report",
                    now,
                ),
            )
        )
        invalidation.record_deployment_capabilities(
            DeploymentCapabilities(
                provider_id="codex",
                api_base="codex://direct",
                model_id="gpt-5.5",
                model_key=Fact("test:codex:gpt-5.5", "server_report", now),
            )
        )

        config = LMProviderConfig(provider="codex", model="gpt-5.5", thinking_level="high")
        lm = create_lm(config)
        assert lm.kwargs["codex_reasoning_effort"] == "high"

        config_off = LMProviderConfig(provider="codex", model="gpt-5.5", thinking_level="off")
        lm_off = create_lm(config_off)
        assert lm_off.kwargs["codex_reasoning_effort"] == "none"

        # Unset level on a model with NO linked evidence yet (no handshake has
        # run for this identity) → no effort kwarg at all, never a guess
        # (fail closed; model-capabilities brief 5.5). A model WITH known
        # evidence sends codex's explicit 'none' even when unset (dialect_wire
        # docstring: never omit-and-inherit-ambient) -- this is the distinct
        # "nothing is known yet" case, covered with its own, unseeded model id.
        config_default = LMProviderConfig(provider="codex", model="gpt-5.5-unseeded")
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
            model = "loaded-model" if provider == "lm_studio" else "llama3.2"
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
            model="llama3.2",
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
            # model-capabilities brief 9.1: no compiled-in suggested model any
            # more, so an explicit model is required to construct an LM.
            "CLIO_LM_MODEL": "gpt-4o-mini",
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

    @pytest.fixture(autouse=True)
    def _reset_shutdown_signal(self):
        """Keep the process-global shutdown signal isolated between tests."""
        from clio_agent.providers.lmstudio_discovery import reset_discovery_shutdown

        reset_discovery_shutdown()
        yield
        reset_discovery_shutdown()

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

    def test_shutdown_interrupts_discovery_before_another_probe(self):
        """Desktop Quit stops an in-progress retry loop without waiting its deadline."""
        from clio_agent.providers.lmstudio_discovery import (
            LMStudioDiscoveryCancelled,
            list_lm_studio_models,
            request_discovery_shutdown,
        )

        rep = self._report(ok=True, models=())

        def request_shutdown(_delay: float) -> bool:
            request_discovery_shutdown()
            return True

        with (
            patch(self._PATCH, return_value=rep) as mock_hs,
            patch(
                "clio_agent.providers.lmstudio_discovery._shutdown_requested.wait",
                side_effect=request_shutdown,
            ),
            pytest.raises(LMStudioDiscoveryCancelled, match="cancelled during CLIO shutdown"),
        ):
            list_lm_studio_models(max_retries=10, retry_delay=30)

        assert mock_hs.call_count == 1
