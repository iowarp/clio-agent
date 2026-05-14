"""Tests for agent.py dispatch with mocked experts and router.

Tests forward() dispatch, expert invocation instrumentation, variant loading
on __init__, and error handling paths -- all without requiring LM Studio.
"""

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
        """Test routing to chat agent.

        Mocks _direct_chat_completion (the canonical chat path for any
        backend with an api_base configured) instead of agent.chat_agent
        (the DSPy/litellm fallback used only for native anthropic).
        Previously this test depended on the silent-fallback-to-DSPy
        path that was removed because it masked real upstream errors
        with confusing AnyIO worker-thread crashes.
        """
        mock_prediction = MagicMock()
        mock_prediction.selected_expert = "chat"
        agent.router = MagicMock(return_value=mock_prediction)

        agent._direct_chat_completion = MagicMock(
            return_value="I can help with data analysis."
        )

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
        # Out-of-scope answer should explicitly say so and name the available
        # experts so the user knows how to rephrase. (Message rewritten in
        # commit 6b01c39 to surface the routing decision visibly.)
        assert "out-of-scope" in result.answer.lower()
        assert "experts" in result.answer.lower()

    def test_expert_failure_logs_status(self, agent):
        """Test that expert failure results in structured error."""
        mock_prediction = MagicMock()
        mock_prediction.selected_expert = "data"
        agent.router = MagicMock(return_value=mock_prediction)

        agent.data_expert = MagicMock(side_effect=RuntimeError("MCP tool failed"))

        result = agent.forward(question="Analyze HDF5", session_id="test_session")

        assert result.selected_expert == "data"
        # User-facing answer should be friendly (no raw traceback) and
        # name which expert tripped so the user can decide whether to retry.
        assert "data expert" in result.answer.lower()
        assert "Traceback" not in result.answer
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


# iowarp/clio-agent#25 — hybrid routing inside the data branch must
# pick "fast" for inspection questions, "expert_loop" for reasoning
# questions, and let session.routing_mode="reasoning_only" override
# everything to expert_loop.


class TestDataIntentDispatch:
    """forward() must select the right execution path inside the data
    branch and surface it via Prediction.execution_path."""

    def test_inspect_prompt_runs_fast_path(self, agent, tmp_path):
        """An inspect-verb question against a real .h5 path must use
        _direct_hdf5_answer and never call data_expert.forward."""
        # Heuristic router will pick "data" because the question
        # contains ".h5"; no LM router invocation required.
        h5 = tmp_path / "test.h5"
        import h5py

        with h5py.File(h5, "w") as f:
            f.create_dataset("temperature", data=[1.0, 2.0, 3.0])

        agent.data_expert = MagicMock()  # would raise if called
        result = agent.forward(
            question=f"inspect {h5}",
            session_id="t1",
        )

        agent.data_expert.assert_not_called()
        assert result.selected_expert == "data"
        assert getattr(result, "execution_path", "") == "fast"

    def test_reason_prompt_runs_expert_loop(self, agent, tmp_path):
        """A reason-verb question against a real .h5 path must reach
        the DataExpert ReAct loop, not the deterministic template."""
        h5 = tmp_path / "test.h5"
        import h5py

        with h5py.File(h5, "w") as f:
            f.create_dataset("a", data=[1.0])
            f.create_dataset("b", data=[2.0])

        expert_result = dspy.Prediction(
            analysis="Compared a vs b",
            recommendations="None",
        )
        agent.data_expert = MagicMock(return_value=expert_result)

        result = agent.forward(
            question=f"compare a and b from {h5}",
            session_id="t2",
        )

        agent.data_expert.assert_called_once()
        assert result.selected_expert == "data"
        assert getattr(result, "execution_path", "") == "expert_loop"
        assert "Compared a vs b" in result.answer

    def test_reasoning_only_mode_bypasses_fast_path(self, agent, tmp_path):
        """When session.routing_mode = 'reasoning_only', even an
        inspect-verb question must reach data_expert.forward."""
        h5 = tmp_path / "test.h5"
        import h5py

        with h5py.File(h5, "w") as f:
            f.create_dataset("x", data=[1.0])

        expert_result = dspy.Prediction(
            analysis="x has shape (1,)",
            recommendations="None",
        )
        agent.data_expert = MagicMock(return_value=expert_result)
        agent._routing_mode_override = "reasoning_only"
        try:
            result = agent.forward(
                question=f"inspect {h5}",  # inspect verb → would normally fast-path
                session_id="t3",
            )
        finally:
            agent._routing_mode_override = "auto"

        agent.data_expert.assert_called_once()
        assert result.selected_expert == "data"
        assert getattr(result, "execution_path", "") == "expert_loop"

    def test_ambiguous_prompt_calls_lm_resolver(self, agent, tmp_path):
        """An ambiguous prompt (neither inspect nor reason verb)
        must invoke the LM intent classifier exactly once. We mock
        the classifier to return 'reason' and verify data_expert ran."""
        h5 = tmp_path / "test.h5"
        import h5py

        with h5py.File(h5, "w") as f:
            f.create_dataset("d", data=[1.0])

        resolver_pred = MagicMock()
        resolver_pred.intent = "reason"
        agent.data_intent_classifier = MagicMock(return_value=resolver_pred)

        expert_result = dspy.Prediction(analysis="ok", recommendations="ok")
        agent.data_expert = MagicMock(return_value=expert_result)

        result = agent.forward(
            question=f"tell me about {h5}",  # neither bucket matches
            session_id="t4",
        )

        agent.data_intent_classifier.assert_called_once()
        agent.data_expert.assert_called_once()
        assert getattr(result, "execution_path", "") == "expert_loop"
