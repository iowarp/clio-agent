"""Tests for agent.py dispatch with mocked experts and router.

Tests forward() dispatch, expert invocation instrumentation, variant loading
on __init__, and error handling paths -- all without requiring LM Studio.
"""

import time
from unittest.mock import MagicMock, patch

import dspy
import pytest

from clio_agent.agent import ClioAgent


@pytest.fixture
def agent(tmp_path):
    """Create a ClioAgent with isolated data dir."""
    a = ClioAgent(data_dir=str(tmp_path / "clio"), verbose=False)
    yield a
    a.shutdown()


class TestForwardDispatch:
    """Test forward() dispatch to experts with mocked router."""

    def test_dispatch_data_expert(self, agent):
        """Test routing to data expert stores tier-2 invocation."""
        # Mock router to return "data"
        mock_prediction = MagicMock()
        mock_prediction.selected_expert = "data"
        agent.router = MagicMock(return_value=mock_prediction)

        # Mock data expert
        expert_result = dspy.Prediction(
            analysis="HDF5 analysis result",
            recommendations="Use gzip compression",
        )
        agent.data_expert = MagicMock(return_value=expert_result)

        result = agent.forward(question="Optimize my HDF5 file", session_id="test_session")

        assert result.selected_expert == "data"
        assert "HDF5 analysis result" in result.answer
        assert "gzip compression" in result.answer

    def test_dispatch_analysis_expert(self, agent):
        """Test routing to analysis expert."""
        mock_prediction = MagicMock()
        mock_prediction.selected_expert = "analysis"
        agent.router = MagicMock(return_value=mock_prediction)

        expert_result = dspy.Prediction(
            analysis="Parquet schema analysis",
            recommendations="Add partitioning",
        )
        agent.analysis_expert = MagicMock(return_value=expert_result)

        result = agent.forward(question="Analyze parquet schema", session_id="test_session")

        assert result.selected_expert == "analysis"
        assert "Parquet schema" in result.answer

    def test_dispatch_visualization_expert(self, agent):
        """Test routing to visualization expert."""
        mock_prediction = MagicMock()
        mock_prediction.selected_expert = "visualization"
        agent.router = MagicMock(return_value=mock_prediction)

        expert_result = dspy.Prediction(
            visualization_description="Histogram of temperature",
            file_path="/tmp/chart.png",
        )
        agent.visualization_expert = MagicMock(return_value=expert_result)

        result = agent.forward(question="Plot a histogram", session_id="test_session")

        assert result.selected_expert == "visualization"
        assert "Histogram" in result.answer

    def test_dispatch_chat(self, agent):
        """Test routing to chat agent."""
        mock_prediction = MagicMock()
        mock_prediction.selected_expert = "chat"
        agent.router = MagicMock(return_value=mock_prediction)

        chat_result = dspy.Prediction(answer="I can help with data analysis.")
        agent.chat_agent = MagicMock(return_value=chat_result)

        result = agent.forward(question="Hello!", session_id="test_session")

        assert result.selected_expert == "chat"
        assert "help with data" in result.answer

    def test_dispatch_chat_falls_back_to_direct_local_completion(self, agent, monkeypatch):
        """Local chat should recover from DSPy parse failures with the same model."""
        mock_prediction = MagicMock()
        mock_prediction.selected_expert = "chat"
        agent.router = MagicMock(return_value=mock_prediction)

        agent.chat_agent = MagicMock(
            side_effect=RuntimeError("Adapter ChatAdapter failed to parse the LM response")
        )
        agent._provider_config.provider = "lm_studio"
        agent._provider_config.api_base = "http://192.168.86.143:1234/v1"
        agent._provider_config.api_key = "lm-studio"
        agent._provider_config.model = "nemotron-cascade-2-30b-a3b-i1"

        captured: dict[str, object] = {}

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "choices": [
                        {"message": {"content": "Hello from the local direct fallback."}}
                    ]
                }

        def fake_post(url, json, headers, timeout):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            captured["timeout"] = timeout
            return FakeResponse()

        monkeypatch.setattr("clio_agent.agent.requests.post", fake_post)

        result = agent.forward(
            question="Tell me about your capabilities",
            session_id="test_session",
        )

        assert result.selected_expert == "chat"
        assert result.answer == "Hello from the local direct fallback."
        assert result.error_info is None
        assert captured["url"] == "http://192.168.86.143:1234/v1/chat/completions"
        assert captured["json"]["model"] == "nemotron-cascade-2-30b-a3b-i1"
        assert captured["json"]["messages"][-1]["content"] == "Tell me about your capabilities"

    def test_dispatch_none_out_of_scope(self, agent):
        """Test routing to 'none' returns out-of-scope message."""
        mock_prediction = MagicMock()
        mock_prediction.selected_expert = "none"
        agent.router = MagicMock(return_value=mock_prediction)

        result = agent.forward(question="What is the meaning of life?", session_id="test_session")

        assert result.selected_expert == "none"
        assert "specialized in scientific data" in result.answer

    def test_expert_failure_logs_status(self, agent):
        """Test that expert failure results in structured error."""
        mock_prediction = MagicMock()
        mock_prediction.selected_expert = "data"
        agent.router = MagicMock(return_value=mock_prediction)

        agent.data_expert = MagicMock(side_effect=RuntimeError("MCP tool failed"))

        result = agent.forward(question="Analyze HDF5", session_id="test_session")

        assert result.selected_expert == "data"
        # User-facing answer should be friendly (no raw traceback)
        assert "issue" in result.answer.lower()
        # Structured error_info should be present
        assert result.error_info is not None
        assert result.error_info["error"] == "expert_error"
        assert result.error_info["details"]["expert"] == "data"

    def test_router_failure_falls_back_to_chat(self, agent):
        """Test that router failure falls back to chat."""
        agent.router = MagicMock(side_effect=RuntimeError("Router failed"))

        chat_result = dspy.Prediction(answer="Fallback response")
        agent.chat_agent = MagicMock(return_value=chat_result)

        result = agent.forward(question="Test query", session_id="test_session")

        assert result.selected_expert == "chat"

    def test_stores_expert_invocation_in_arc(self, agent):
        """Test that expert dispatch stores invocation in ARC."""
        mock_prediction = MagicMock()
        mock_prediction.selected_expert = "data"
        agent.router = MagicMock(return_value=mock_prediction)

        expert_result = dspy.Prediction(
            analysis="Result",
            recommendations="Rec",
        )
        agent.data_expert = MagicMock(return_value=expert_result)

        agent.forward(question="Test query", session_id="test_session")

        # Check invocations stored in ARC
        invocations = agent.arc.get_invocations_by_agent("data")
        assert len(invocations) >= 1
        assert invocations[0].agent_id == "data"
        assert invocations[0].status == "success"


class TestVariantLoading:
    """Test variant loading on __init__."""

    def test_variant_loading_on_init(self, tmp_path):
        """Test that active variants are loaded on agent init."""
        # Create agent, store a variant record
        agent = ClioAgent(data_dir=str(tmp_path / "clio"), verbose=True)

        from clio_agent.arc.schema import VariantRecord
        from clio_agent.optimizer.variants import VariantManager

        vm = VariantManager(agent.arc)
        record = VariantRecord(
            variant_id="data_v1",
            agent_id="data",
            is_active=True,
            file_path=str(tmp_path / "nonexistent.json"),
        )
        agent.arc.store_variant_record(record)
        agent.shutdown()

        # Now create a new agent; it should try to load the variant but
        # gracefully handle the missing file
        agent2 = ClioAgent(data_dir=str(tmp_path / "clio"), verbose=True)
        assert agent2 is not None  # Did not crash
        agent2.shutdown()

    def test_variant_loading_failure_doesnt_crash_init(self, tmp_path):
        """Test that variant loading failure doesn't crash agent init."""
        # Patch VariantManager at its source module to raise an exception
        with patch(
            "clio_agent.optimizer.variants.VariantManager.__init__",
            side_effect=RuntimeError("Broken"),
        ):
            agent = ClioAgent(data_dir=str(tmp_path / "clio"), verbose=False)
            assert agent is not None
            agent.shutdown()

    def test_forward_stores_conversation(self, agent):
        """Test that forward() stores conversation in ARC."""
        mock_prediction = MagicMock()
        mock_prediction.selected_expert = "chat"
        agent.router = MagicMock(return_value=mock_prediction)

        chat_result = dspy.Prediction(answer="Hello!")
        agent.chat_agent = MagicMock(return_value=chat_result)

        agent.forward(question="Hi", session_id="conv_test")

        conv = agent.arc.get_conversation("conv_test")
        assert conv is not None
        assert len(conv.messages) == 2  # user + assistant
