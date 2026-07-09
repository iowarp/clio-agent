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
        assert hasattr(agent, "forward")
        agent.shutdown()

    def test_has_action_planner(self):
        """Test ClioAgent has an action planner component."""
        agent = ClioAgent()
        assert hasattr(agent, "action_planner")
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
        """Test that built-in domain experts are not registered by core."""
        agent = ClioAgent()
        agents = agent.registry.list_agents()
        assert "utility" in agents
        assert "data" not in agents
        assert "analysis" not in agents
        assert "visualization" not in agents
        agent.shutdown()

    def test_utility_capabilities_are_loaded(self):
        """Test runtime utility capabilities are loaded."""
        agent = ClioAgent()
        cap = agent.registry.get_capabilities("utility")
        assert cap is not None
        assert cap.description is not None
        assert "shell" in cap.keywords
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
        """Explicit CLIO_LM_MODEL should keep lm_studio pinned to one model."""
        monkeypatch.setenv("CLIO_LM_PROVIDER", "lm_studio")
        monkeypatch.setenv("CLIO_LM_API_BASE", "http://192.168.86.143:1234/v1")
        monkeypatch.setenv("CLIO_LM_MODEL", "nemotron-cascade-2-30b-a3b-i1")

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
        monkeypatch.setenv("CLIO_LM_PROVIDER", "lm_studio")
        monkeypatch.setenv("CLIO_LM_API_BASE", "http://127.0.0.1:1234/v1")
        monkeypatch.delenv("CLIO_LM_MODEL", raising=False)

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

        monkeypatch.setenv("CLIO_LM_PROVIDER", "lm_studio")
        monkeypatch.setenv("CLIO_LM_API_BASE", "http://127.0.0.1:1234/v1")
        monkeypatch.delenv("CLIO_LM_MODEL", raising=False)

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

    def test_get_session_context_empty(self):
        """Test session context retrieval with no history."""
        agent = ClioAgent()
        ctx = agent._get_session_context("test question", "empty_session")
        assert isinstance(ctx, str)
        agent.shutdown()


class TestStoreExpertInvocation:
    """#801: the per-turn invocation write is the future optimizer training
    corpus (and feeds /metrics) — pin that it still persists to ARC."""

    def test_store_expert_invocation_persists_training_corpus(self, tmp_path):
        """A successful expert turn must land as a tier-2 ARC invocation."""
        agent = ClioAgent(data_dir=str(tmp_path / "clio"))
        try:
            prediction = dspy.Prediction(
                analysis="mean displacement is 4.2 mm",
                recommendations="plot the north component",
            )
            agent._store_expert_invocation(
                question="what is the mean displacement?",
                file_context="station MTA1 csv",
                selected="data",
                session_id="s-corpus",
                expert_result=prediction,
                success=True,
                error_msg=None,
                duration_ms=12.5,
                trace=None,
            )

            invocations = agent.arc.get_invocations_by_agent("data", status="success")
            assert len(invocations) == 1
            inv = invocations[0]
            assert inv.tier == 2
            assert inv.agent_id == "data"
            assert inv.session_id == "s-corpus"
            assert inv.input["question"] == "what is the mean displacement?"
            assert inv.input["file_context"] == "station MTA1 csv"
            assert inv.output["analysis"] == "mean displacement is 4.2 mm"
            assert inv.performance["success"] is True
        finally:
            agent.shutdown()

    def test_store_expert_invocation_records_failures_too(self, tmp_path):
        """Failed turns are corpus signal as well — status + error persist."""
        agent = ClioAgent(data_dir=str(tmp_path / "clio"))
        try:
            agent._store_expert_invocation(
                question="broken question",
                file_context="",
                selected="data",
                session_id="s-corpus",
                expert_result=None,
                success=False,
                error_msg="expert exploded",
                duration_ms=3.0,
                trace=None,
            )

            invocations = agent.arc.get_invocations_by_agent("data", status="failure")
            assert len(invocations) == 1
            assert invocations[0].output["error"] == "expert exploded"
        finally:
            agent.shutdown()
