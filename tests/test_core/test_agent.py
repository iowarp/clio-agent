"""
Tests for clio_agent.agent module.

Tests ClioAgent Router + ChatAgent + Expert dispatch architecture.
"""

from contextlib import contextmanager
from unittest.mock import patch

import dspy
import pytest

from clio_agent.agent import ClioAgent


class TestClioAgent:
    """Test ClioAgent agent functionality."""

    def test_initialization(self):
        """Test ClioAgent agent can be initialized."""
        agent = ClioAgent()
        assert agent is not None
        agent.shutdown()

    def test_planner_surface_deleted(self):
        """#948 S4b: the Tier-1 planner half is DELETED — no forward, no planner
        predicts. ClioAgent is a host; blueprint react mains are the only mains."""
        agent = ClioAgent()
        try:
            assert not hasattr(agent, "forward")
            assert not hasattr(agent, "action_planner")
            assert not hasattr(agent, "answer_synthesizer")
        finally:
            agent.shutdown()

    def test_run_chat_agent_single_shot_synthesis(self):
        """_run_chat_agent is plain single-shot LM synthesis (no tool loop).

        The session-compaction summarizer (routes/sessions.py) depends on this
        host helper folding a prompt into text via one chat_agent call.
        """
        agent = ClioAgent()
        try:
            calls: list[dict[str, object]] = []

            def fake_chat_agent(**kwargs: object):
                calls.append(kwargs)
                return dspy.Prediction(answer="compact summary")

            agent.chat_agent = fake_chat_agent
            result = agent._run_chat_agent("summarise this transcript", "")
            assert result == "compact summary"
            # Single-shot: exactly one predictor call, no tool loop.
            assert len(calls) == 1
        finally:
            agent.shutdown()

    def test_has_chat_agent(self):
        """Test ClioAgent has chat_agent component."""
        agent = ClioAgent()
        assert hasattr(agent, "chat_agent")
        agent.shutdown()

    def test_does_not_have_native_domain_experts(self):
        """Test ClioAgent does not expose native domain expert components."""
        agent = ClioAgent()
        assert not hasattr(agent, "data_expert")
        assert not hasattr(agent, "analysis_expert")
        assert not hasattr(agent, "visualization_expert")
        assert not hasattr(agent, "ndp_catalog_expert")
        assert not hasattr(agent, "sac_format_expert")
        agent.shutdown()

    def test_has_arc_memory(self):
        """Test ClioAgent has ARC memory."""
        agent = ClioAgent()
        assert hasattr(agent, "arc")
        assert agent.arc is not None
        agent.shutdown()

    def test_has_registry(self):
        """Test ClioAgent has agent registry."""
        agent = ClioAgent()
        assert hasattr(agent, "registry")
        agent.shutdown()

    def test_core_registry_has_no_domain_experts(self):
        """Core registers no agents at construction (#948 S4b).

        The legacy 'utility' self-registration is deleted with the planner;
        blueprint experts register through the pack bootstrap, not __init__.
        """
        agent = ClioAgent()
        agents = agent.registry.list_agents()
        assert "utility" not in agents
        assert "data" not in agents
        assert "analysis" not in agents
        assert "visualization" not in agents
        agent.shutdown()

    def test_arc_stats(self):
        """Test ARC stats are retrievable."""
        agent = ClioAgent()
        stats = agent.get_arc_stats()
        assert "hit_rate" in stats
        assert "size" in stats
        assert "capacity" in stats
        agent.shutdown()

    def test_shutdown(self):
        """Test clean shutdown does not raise."""
        agent = ClioAgent()
        agent.shutdown()  # Should not raise

    def test_verbose_mode(self):
        """Test verbose mode can be enabled."""
        agent = ClioAgent(verbose=True)
        assert agent.verbose is True
        agent.shutdown()

    def test_custom_data_dir(self, tmp_path):
        """Test custom data directory for ARC/LSM storage."""
        data_dir = str(tmp_path / "clio_test")
        agent = ClioAgent(data_dir=data_dir)
        assert agent is not None
        agent.shutdown()

    def test_planner_lm_configured(self):
        """Test that planner LM is configured separately."""
        agent = ClioAgent()
        assert agent._planner_lm is not None
        agent.shutdown()

    def test_rebind_lms_swaps_entire_lm_surface(self):
        """rebind_lms rebuilds the whole LM surface from a new provider config.

        Guards the partial-write regression class: all four LM-surface fields
        (_provider_config / _main_lm / _planner_lm / _dspy_adapter) must be
        rebuilt together, never a torn subset.
        """
        import dataclasses

        agent = ClioAgent()
        before_main = agent._main_lm
        before_planner = agent._planner_lm
        before_adapter = agent._dspy_adapter
        cfg2 = dataclasses.replace(agent._provider_config, model="rebind-depth-model")
        agent.rebind_lms(cfg2)
        assert agent._provider_config is cfg2
        assert agent._main_lm is not before_main
        assert agent._planner_lm is not before_planner
        assert agent._dspy_adapter is not before_adapter
        assert "rebind-depth-model" in agent._main_lm.model
        assert "rebind-depth-model" in agent._planner_lm.model
        agent.shutdown()

    def test_lm_studio_explicit_model_skips_model_discovery(self, tmp_path, monkeypatch):
        """An explicit ``lm.model`` should keep lm_studio pinned to one model."""
        from tests._config_layer import set_config

        monkeypatch.setenv("CLIO_LM_PROVIDER", "lm_studio")
        monkeypatch.setenv("CLIO_LM_API_BASE", "http://192.168.86.143:1234/v1")
        # ``lm.model`` lives in the config FILE (file > env); pin it there so the
        # explicit-override contract (skip discovery) is exercised (#985 residual).
        set_config("lm.model", "nemotron-cascade-2-30b-a3b-i1")

        with patch(
            "clio_agent.agent.list_lm_studio_models",
            side_effect=AssertionError("model discovery should be skipped"),
        ):
            agent = ClioAgent(data_dir=str(tmp_path / "clio"))

        try:
            assert agent._provider_config.model == "nemotron-cascade-2-30b-a3b-i1"
            assert agent._planner_lm.model == "openai/nemotron-cascade-2-30b-a3b-i1"
        finally:
            agent.shutdown()

    def test_direct_lm_studio_agent_uses_text_chat_adapter(self, tmp_path, monkeypatch):
        """Direct ClioAgent construction should not leak DSPy's JSON fallback."""
        monkeypatch.setenv("CLIO_LM_PROVIDER", "lm_studio")
        monkeypatch.setenv("CLIO_LM_API_BASE", "http://127.0.0.1:1234/v1")
        monkeypatch.setenv("CLIO_LM_MODEL", "ibm/granite-4-h-tiny")

        agent = ClioAgent(data_dir=str(tmp_path / "clio"))

        try:
            assert agent._main_lm.model == "openai/ibm/granite-4-h-tiny"
            assert agent._planner_lm.model == "openai/ibm/granite-4-h-tiny"
            assert agent._dspy_adapter.use_json_adapter_fallback is False
        finally:
            agent.shutdown()

    def test_chat_context_pins_provider_adapter(self, tmp_path, monkeypatch):
        """Chat synthesis must use the agent-owned adapter, not DSPy's default."""
        monkeypatch.setenv("CLIO_LM_PROVIDER", "lm_studio")
        monkeypatch.setenv("CLIO_LM_API_BASE", "http://127.0.0.1:1234/v1")
        monkeypatch.setenv("CLIO_LM_MODEL", "ibm/granite-4-h-tiny")

        agent = ClioAgent(data_dir=str(tmp_path / "clio"))
        calls: list[dict[str, object]] = []

        @contextmanager
        def fake_context(**kwargs: object):
            calls.append(kwargs)
            yield

        try:
            agent.chat_agent = lambda **_: dspy.Prediction(answer="ok")
            with patch("clio_agent.agent.dspy.context", side_effect=fake_context):
                answer = agent._run_chat_agent("hi", "")

            assert answer == "ok"
            assert calls[0]["lm"] is agent._main_lm
            assert calls[0]["adapter"] is agent._dspy_adapter
            assert agent._dspy_adapter.use_json_adapter_fallback is False
        finally:
            agent.shutdown()

    def test_lm_studio_discovery_uses_configured_api_base(self, tmp_path, monkeypatch):
        """LM Studio discovery should use the configured remote endpoint."""
        from tests._config_layer import delete_config

        monkeypatch.setenv("CLIO_LM_PROVIDER", "lm_studio")
        monkeypatch.setenv("CLIO_LM_API_BASE", "http://192.168.86.143:1234/v1")
        # Drop the fixture's file-pinned ``lm.model`` so no explicit override exists
        # and discovery triggers (the file-layer analogue of delenv; #985 residual).
        delete_config("lm.model")

        seen: dict[str, str] = {}

        def fake_fetch(*, base_url: str, **_: object):
            seen["base_url"] = base_url
            return ["nemotron-cascade-2-30b-a3b-i1"]

        with patch("clio_agent.agent.list_lm_studio_models", side_effect=fake_fetch):
            with patch(
                "clio_agent.agent.select_models_for_agents",
                return_value=("nemotron-cascade-2-30b-a3b-i1", "nemotron-cascade-2-30b-a3b-i1"),
            ):
                agent = ClioAgent(data_dir=str(tmp_path / "clio"))

        try:
            assert seen["base_url"] == "http://192.168.86.143:1234/v1"
            assert agent._provider_config.model == "nemotron-cascade-2-30b-a3b-i1"
        finally:
            agent.shutdown()

    def test_lm_studio_empty_discovery_surfaces_configuration_error(self, tmp_path, monkeypatch):
        """No discovered LM Studio models should fail before creating a guessed LM."""
        from tests._config_layer import delete_config

        monkeypatch.setenv("CLIO_LM_PROVIDER", "lm_studio")
        monkeypatch.setenv("CLIO_LM_API_BASE", "http://127.0.0.1:1234/v1")
        delete_config("lm.model")

        with patch("clio_agent.agent.list_lm_studio_models", return_value=[]):
            with patch(
                "clio_agent.agent.create_lm",
                side_effect=AssertionError("create_lm should not be called"),
            ):
                with pytest.raises(ValueError, match="reported no loaded models"):
                    ClioAgent(data_dir=str(tmp_path / "clio"))

    def test_lm_studio_discovery_exception_surfaces_original_error(self, tmp_path, monkeypatch):
        """Discovery transport/schema errors should not become generic no-model errors."""
        from clio_agent.config import LMStudioDiscoveryError
        from tests._config_layer import delete_config

        monkeypatch.setenv("CLIO_LM_PROVIDER", "lm_studio")
        monkeypatch.setenv("CLIO_LM_API_BASE", "http://127.0.0.1:1234/v1")
        delete_config("lm.model")

        with patch(
            "clio_agent.agent.list_lm_studio_models",
            side_effect=LMStudioDiscoveryError("LM Studio invalid JSON from /models"),
        ):
            with patch(
                "clio_agent.agent.create_lm",
                side_effect=AssertionError("create_lm should not be called"),
            ):
                with pytest.raises(LMStudioDiscoveryError, match="invalid JSON"):
                    ClioAgent(data_dir=str(tmp_path / "clio"))

    def test_no_session_context_helper(self):
        """#948 S4b: the planner's session/file context helpers are deleted with
        the loop. ClioAgent no longer exposes _get_session_context."""
        agent = ClioAgent()
        try:
            assert not hasattr(agent, "_get_session_context")
            assert not hasattr(agent, "_store_expert_invocation")
        finally:
            agent.shutdown()
