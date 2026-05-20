"""
Tests for clio_agent.agent module.

Tests ClioAgent Router + ChatAgent + Expert dispatch architecture.
"""

from contextlib import contextmanager
from unittest.mock import patch

import dspy

from clio_agent.agent import ClioAgent


class TestClioAgent:
    """Test ClioAgent agent functionality."""

    def test_initialization(self):
        """Test ClioAgent agent can be initialized."""
        agent = ClioAgent()
        assert agent is not None
        assert hasattr(agent, "forward")
        agent.shutdown()

    def test_has_router(self):
        """Test ClioAgent has router component."""
        agent = ClioAgent()
        assert hasattr(agent, "router")
        agent.shutdown()

    def test_has_chat_agent(self):
        """Test ClioAgent has chat_agent component."""
        agent = ClioAgent()
        assert hasattr(agent, "chat_agent")
        agent.shutdown()

    def test_has_data_expert(self):
        """Test ClioAgent has data_expert component."""
        agent = ClioAgent()
        assert hasattr(agent, "data_expert")
        agent.shutdown()

    def test_has_arc_memory(self):
        """Test ClioAgent has ARC memory."""
        agent = ClioAgent()
        assert hasattr(agent, "arc")
        assert agent.arc is not None
        agent.shutdown()

    def test_has_lsm_tree(self):
        """Test ClioAgent has LSM tree."""
        agent = ClioAgent()
        assert hasattr(agent, "lsm")
        assert agent.lsm is not None
        agent.shutdown()

    def test_has_registry(self):
        """Test ClioAgent has agent registry."""
        agent = ClioAgent()
        assert hasattr(agent, "registry")
        agent.shutdown()

    def test_expert_registry_all(self):
        """Test that all 3 experts are registered."""
        agent = ClioAgent()
        agents = agent.registry.list_agents()
        assert "data" in agents
        assert "analysis" in agents
        assert "visualization" in agents
        assert len(agents) == 3
        agent.shutdown()

    def test_expert_capabilities(self):
        """Test expert capabilities are loaded."""
        agent = ClioAgent()
        cap = agent.registry.get_capabilities("data")
        assert cap is not None
        assert cap.description is not None
        assert "hdf5" in cap.keywords
        agent.shutdown()

    def test_arc_stats(self):
        """Test ARC stats are retrievable."""
        agent = ClioAgent()
        stats = agent.get_arc_stats()
        assert "hit_rate" in stats
        assert "size" in stats
        assert "capacity" in stats
        agent.shutdown()

    def test_lsm_stats(self):
        """Test LSM stats are retrievable."""
        agent = ClioAgent()
        stats = agent.get_lsm_stats()
        assert "write_count" in stats
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
        assert agent._router_lm is agent._planner_lm
        agent.shutdown()

    def test_lm_studio_explicit_model_skips_model_discovery(self, tmp_path, monkeypatch):
        """Explicit CLIO_LM_MODEL should keep lm_studio pinned to one model."""
        monkeypatch.setenv("CLIO_LM_PROVIDER", "lm_studio")
        monkeypatch.setenv("CLIO_LM_API_BASE", "http://192.168.86.143:1234/v1")
        monkeypatch.setenv("CLIO_LM_MODEL", "nemotron-cascade-2-30b-a3b-i1")

        with patch(
            "clio_agent.agent.fetch_lm_studio_models",
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

    def test_planner_context_pins_provider_adapter(self, tmp_path, monkeypatch):
        """Planner calls must use the agent-owned adapter, not DSPy's default."""
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
            agent.action_planner = lambda **_: dspy.Prediction(
                action_json='{"action":"answer","answer":"ok"}'
            )
            with patch("clio_agent.agent.dspy.context", side_effect=fake_context):
                action = agent._plan_next_action(
                    question="hi",
                    session_context="",
                    file_context="",
                    capabilities="",
                    observations=[],
                )

            assert action["answer"] == "ok"
            assert calls[0]["lm"] is agent._planner_lm
            assert calls[0]["adapter"] is agent._dspy_adapter
            assert agent._dspy_adapter.use_json_adapter_fallback is False
        finally:
            agent.shutdown()

    def test_lm_studio_discovery_uses_configured_api_base(self, tmp_path, monkeypatch):
        """LM Studio discovery should use the configured remote endpoint."""
        monkeypatch.setenv("CLIO_LM_PROVIDER", "lm_studio")
        monkeypatch.setenv("CLIO_LM_API_BASE", "http://192.168.86.143:1234/v1")
        monkeypatch.delenv("CLIO_LM_MODEL", raising=False)

        seen: dict[str, str] = {}

        def fake_fetch(*, base_url: str, **_: object):
            seen["base_url"] = base_url
            return ["nemotron-cascade-2-30b-a3b-i1"]

        with patch("clio_agent.agent.fetch_lm_studio_models", side_effect=fake_fetch):
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

    def test_get_session_context_empty(self):
        """Test session context retrieval with no history."""
        agent = ClioAgent()
        ctx = agent._get_session_context("test question", "empty_session")
        assert isinstance(ctx, str)
        agent.shutdown()
