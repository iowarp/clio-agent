"""Tests for agent.py dispatch with mocked experts and planner.

Tests forward() dispatch, expert invocation instrumentation, variant loading
on __init__, and error handling paths -- all without requiring LM Studio.
"""

import json
from unittest.mock import MagicMock, patch

import dspy
import pytest

from clio_agent.agent import ClioAgent, cancellation_checker, routing_mode_override
from clio_agent.harness import ToolObservation


@pytest.fixture
def agent(tmp_path):
    """Create a ClioAgent with isolated data dir."""
    a = ClioAgent(data_dir=str(tmp_path / "clio"), verbose=False)
    yield a
    a.shutdown()


def _plan_action(action: dict[str, object]) -> dspy.Prediction:
    return dspy.Prediction(action_json=json.dumps(action))


def _set_planner(agent: ClioAgent, action: dict[str, object]) -> None:
    agent.action_planner = MagicMock(return_value=_plan_action(action))


class TestForwardDispatch:
    """Test forward() dispatch to experts with mocked planner."""

    @staticmethod
    def _plan_action(action: dict[str, object]) -> dspy.Prediction:
        return dspy.Prediction(action_json=json.dumps(action))

    def _set_planner(self, agent: ClioAgent, action: dict[str, object]) -> None:
        _set_planner(agent, action)

    def test_incompatible_file_expert_replans_without_synthesis(self, agent, tmp_path):
        """An HDF5 follow-up must not let the analysis expert synthesize fake file facts."""
        hdf5_path = tmp_path / "run.h5"
        hdf5_path.touch()
        agent._store_conversation(
            f"Inspect {hdf5_path}",
            f"Inspected HDF5 file {hdf5_path}.",
            "guard-hdf5",
        )
        agent.action_planner = MagicMock(
            side_effect=[
                _plan_action(
                    {
                        "action": "expert",
                        "expert": "analysis",
                        "question": "analyze it",
                        "reason": "incorrect expert choice",
                    }
                ),
                _plan_action(
                    {
                        "action": "expert",
                        "expert": "data",
                        "question": "analyze it",
                        "reason": "compatible expert choice",
                    }
                ),
            ]
        )
        agent.analysis_expert = MagicMock(
            return_value=dspy.Prediction(analysis="fake analysis", recommendations="fake rec")
        )
        agent.data_expert = MagicMock(
            return_value=dspy.Prediction(analysis="native hdf5 facts", recommendations="native rec")
        )

        result = agent.forward(question="analyze it", session_id="guard-hdf5")

        assert result.selected_expert == "data"
        assert "native hdf5 facts" in result.answer
        agent.analysis_expert.assert_not_called()
        agent.data_expert.assert_called_once()

    def test_dispatch_data_expert(self, agent):
        """Test routing to data expert stores tier-2 invocation."""
        self._set_planner(
            agent,
            {"action": "expert", "expert": "data", "question": "Optimize my HDF5 file"},
        )

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

    def test_data_handoff_continues_to_analysis_and_visualization(self, agent, tmp_path):
        """Staged data should continue to downstream experts when the prompt asks."""
        staged = tmp_path / "waveforms.tar"
        staged.write_bytes(b"placeholder")
        self._set_planner(
            agent,
            {
                "action": "expert",
                "expert": "data",
                "question": "find seismic data, analyze it, and plot traces",
            },
        )
        data_result = dspy.Prediction(
            analysis="staged waveform archive",
            recommendations="pass downstream",
            tool_provenance=[
                ToolObservation(
                    tool="ndp_stage_resource",
                    params={},
                    result={"staged": True, "path": str(staged)},
                    duration_ms=1.0,
                    ok=True,
                )
            ],
        )
        analysis_result = dspy.Prediction(
            analysis="computed SAC statistics",
            recommendations="plot representative traces",
            tool_provenance=[
                ToolObservation(
                    tool="sac_compute_trace_statistics",
                    params={"filepath": str(staged)},
                    result={"ok": True},
                    duration_ms=1.0,
                    ok=True,
                )
            ],
        )
        visualization_result = dspy.Prediction(
            visualization_description="Plotted 3 SAC traces",
            file_path=str(tmp_path / "plot.png"),
            tool_provenance=[
                ToolObservation(
                    tool="sac_plot_traces",
                    params={"filepath": str(staged)},
                    result={"ok": True},
                    duration_ms=1.0,
                    ok=True,
                )
            ],
        )
        agent.data_expert = MagicMock(return_value=data_result)
        agent.analysis_expert = MagicMock(return_value=analysis_result)
        agent.visualization_expert = MagicMock(return_value=visualization_result)

        result = agent.forward(
            question="Find seismic data, analyze representative traces, and produce a plot.",
            session_id="handoff-session",
        )

        assert result.selected_expert == "visualization"
        assert "Data stage:" in result.answer
        assert "Analysis stage:" in result.answer
        assert "Visualization stage:" in result.answer
        assert [tool.tool for tool in result.tools_called] == [
            "ndp_stage_resource",
            "sac_compute_trace_statistics",
            "sac_plot_traces",
        ]
        agent.analysis_expert.assert_called_once()
        agent.visualization_expert.assert_called_once()

    def test_dispatch_analysis_expert(self, agent):
        """Test routing to analysis expert."""
        self._set_planner(
            agent,
            {"action": "expert", "expert": "analysis", "question": "Analyze parquet schema"},
        )

        expert_result = dspy.Prediction(
            analysis="Parquet schema analysis",
            recommendations="Add partitioning",
        )
        agent.analysis_expert = MagicMock(return_value=expert_result)

        result = agent.forward(question="Analyze parquet schema", session_id="test_session")

        assert result.selected_expert == "analysis"
        assert "Parquet schema" in result.answer

    def test_dispatch_analysis_expert_propagates_nanoagent_spawns(self, agent):
        """Expert nanoagent spawns should reach GACT through the top-level prediction."""
        self._set_planner(
            agent,
            {
                "action": "expert",
                "expert": "analysis",
                "question": "Validate schema and statistics",
            },
        )

        spawns = [
            {
                "agent_id": "analysis_validator",
                "input": {"question": "Validate: schema"},
                "answer": "schema ok",
                "duration_ms": 12.5,
            },
            {
                "agent_id": "analysis_validator",
                "input": {"question": "Validate: statistics"},
                "answer": "statistics ok",
                "duration_ms": 7.0,
            },
        ]
        expert_result = dspy.Prediction(
            analysis="Parallel validation complete",
            recommendations="No changes",
            nanoagents_spawned=spawns,
        )
        agent.analysis_expert = MagicMock(return_value=expert_result)

        result = agent.forward(question="Validate in parallel", session_id="test_session")

        assert result.selected_expert == "analysis"
        assert result.nanoagents_spawned == spawns
        expert_invocations = agent.arc.get_invocations_by_agent("analysis")
        assert expert_invocations[0].nanoagents_spawned[0].nanoagent_id == "analysis_validator"
        main_invocations = agent.arc.get_session_invocations("test_session")
        assert any(inv.nanoagents_spawned for inv in main_invocations)

    def test_dispatch_visualization_expert(self, agent):
        """Test routing to visualization expert."""
        self._set_planner(
            agent,
            {"action": "expert", "expert": "visualization", "question": "Plot a histogram"},
        )

        expert_result = dspy.Prediction(
            visualization_description="Histogram of temperature",
            file_path="/tmp/chart.png",
        )
        agent.visualization_expert = MagicMock(return_value=expert_result)

        result = agent.forward(question="Plot a histogram", session_id="test_session")

        assert result.selected_expert == "visualization"
        assert "Histogram" in result.answer

    def test_dispatch_chat(self, agent):
        """Test direct planner answers remain chat-routed without fallback calls."""
        self._set_planner(
            agent,
            {"action": "answer", "answer": "I can help with data analysis."},
        )
        agent.chat_agent = MagicMock(return_value=dspy.Prediction(answer="fallback should not run"))

        result = agent.forward(question="Hello!", session_id="test_session")

        assert result.selected_expert == "chat"
        assert "help with data" in result.answer
        agent.chat_agent.assert_not_called()

    def test_forward_accepts_session_knobs(self, agent):
        """GACT session mode/edit mode kwargs should reach real ClioAgent.forward."""
        self._set_planner(agent, {"action": "answer", "answer": "ok"})

        result = agent.forward(
            question="Hello!",
            session_id="test_session",
            session_mode="edit",
            session_edit_mode="patch",
        )

        assert result.answer == "ok"

    def test_routing_mode_chat_forces_chat_without_planner(self, agent):
        """routing_mode=chat should bypass planner classification for this turn."""
        agent._routing_mode_override = "chat"
        agent.action_planner = MagicMock()
        agent._run_chat_agent = MagicMock(return_value="chat override answer")

        result = agent.forward(question="Hello!", session_id="test_session")

        assert result.selected_expert == "chat"
        assert result.answer == "chat override answer"
        assert "routing_mode='chat'" in result.route_reason
        agent.action_planner.assert_not_called()

    def test_routing_mode_context_override_is_scoped(self, agent):
        """Context-scoped routing overrides should not mutate legacy state."""
        agent._routing_mode_override = "experts"

        with routing_mode_override("chat"):
            assert agent._effective_routing_mode() == "chat"

        assert agent._effective_routing_mode() == "experts"

    def test_agent_loop_cooperative_cancel_after_planner(self, agent):
        """A cancel observed after planning should not become a normal answer."""
        cancelled = False

        def plan_then_cancel(**_: object) -> dspy.Prediction:
            nonlocal cancelled
            cancelled = True
            return _plan_action({"action": "answer", "answer": "late normal answer"})

        agent.action_planner = MagicMock(side_effect=plan_then_cancel)

        with cancellation_checker(lambda: cancelled):
            result = agent.forward(question="cancel me", session_id="test_session")

        assert result.answer == ""
        assert result.error_info is not None
        assert result.error_info["error"] == "cancelled"
        assert result.error_info["details"]["execution_cancellation"] == "cooperative"
        assert result.error_info["details"]["executor_work_may_continue"] is False
        assert result.error_info["details"]["stage"] == "planner_after"

    def test_routing_mode_experts_rejects_direct_answer(self, agent):
        """routing_mode=experts should surface a route error instead of chatting."""
        agent._routing_mode_override = "experts"
        self._set_planner(agent, {"action": "answer", "answer": "plain chat"})

        result = agent.forward(question="Hello!", session_id="test_session")

        assert result.answer == ""
        assert result.error_info is not None
        assert result.error_info["error"] == "routing_error"
        assert result.error_info["details"]["recovery_actions"] == [
            "retry",
            "reconfigure_provider",
            "exit",
        ]

    def test_hdf5_file_followup_reuses_last_session_path(self, agent, tmp_path):
        """Pathless HDF5 follow-ups should stay on the native data expert path."""
        hdf5_path = tmp_path / "run.h5"
        agent._store_conversation(
            f"Inspect {hdf5_path}",
            f"Inspected HDF5 file {hdf5_path}.",
            "followup-hdf5",
        )
        agent.data_expert = MagicMock(
            return_value=dspy.Prediction(analysis="native hdf5 facts", recommendations="native rec")
        )
        self._set_planner(
            agent,
            {"action": "expert", "expert": "data", "question": "summarize the full file"},
        )

        result = agent.forward(
            question="summarize the full file",
            session_id="followup-hdf5",
        )

        assert result.selected_expert == "data"
        assert result.route_source == "dspy"
        assert "planner" in result.route_reason.lower()
        call = agent.data_expert.call_args.kwargs
        assert str(hdf5_path) in call["question"]
        assert str(hdf5_path) in call["file_context"]

    def test_parquet_file_followup_reuses_last_session_path(self, agent, tmp_path):
        """Pathless tabular follow-ups should stay on the native analysis path."""
        parquet_path = tmp_path / "run.parquet"
        agent._store_conversation(
            f"Inspect {parquet_path}",
            f"Inspected Parquet file {parquet_path}.",
            "followup-parquet",
        )
        agent.analysis_expert = MagicMock(
            return_value=dspy.Prediction(
                analysis="native parquet facts",
                recommendations="native rec",
            )
        )
        self._set_planner(
            agent,
            {"action": "expert", "expert": "analysis", "question": "summarize the full file"},
        )

        result = agent.forward(
            question="summarize the full file",
            session_id="followup-parquet",
        )

        assert result.selected_expert == "analysis"
        assert result.route_source == "dspy"
        assert "planner" in result.route_reason.lower()
        call = agent.analysis_expert.call_args.kwargs
        assert str(parquet_path) in call["question"]
        assert str(parquet_path) in call["file_context"]

    def test_dispatch_empty_direct_answer_surfaces_error_without_provider_bypass(self, agent):
        """Malformed planner answer routes must surface instead of using raw fallback."""
        self._set_planner(agent, {"action": "answer", "answer": ""})

        agent.chat_agent = MagicMock(return_value="fallback should not run")
        agent._provider_config.provider = "lm_studio"
        agent._provider_config.api_base = "http://192.168.86.143:1234/v1"
        agent._provider_config.api_key = "lm-studio"
        agent._provider_config.model = "nemotron-cascade-2-30b-a3b-i1"

        result = agent.forward(
            question="Tell me about your capabilities",
            session_id="test_session",
        )

        assert result.selected_expert == "chat"
        assert result.answer == ""
        assert result.error_info is not None
        assert result.error_info["error"] == "routing_error"
        assert "did not provide usable text" in result.error_info["message"]
        assert result.error_info["details"]["planner_action"]["action"] == "answer"
        assert result.error_info["details"]["recovery_actions"] == [
            "retry",
            "reconfigure_provider",
            "exit",
        ]
        agent.chat_agent.assert_not_called()
        assert not hasattr(ClioAgent, "_direct_chat_completion")
        assert not hasattr(ClioAgent, "_direct_action_completion")

    def test_dispatch_none_out_of_scope(self, agent):
        """Test routing to 'none' returns out-of-scope message."""
        self._set_planner(
            agent,
            {
                "action": "none",
                "answer": (
                    "I'm CLIO, specialized in scientific data. Could you rephrase "
                    "your question in terms of data analysis?"
                ),
            },
        )

        result = agent.forward(question="What is the meaning of life?", session_id="test_session")

        assert result.selected_expert == "none"
        assert "specialized in scientific data" in result.answer

    def test_expert_failure_logs_status(self, agent):
        """Test that expert failure results in structured error."""
        self._set_planner(agent, {"action": "expert", "expert": "data", "question": "Analyze HDF5"})

        agent.data_expert = MagicMock(side_effect=RuntimeError("MCP tool failed"))

        result = agent.forward(question="Analyze HDF5", session_id="test_session")

        assert result.selected_expert == "data"
        assert result.answer == ""
        # Structured error_info should be present
        assert result.error_info is not None
        assert result.error_info["error"] == "expert_error"
        assert result.error_info["details"]["expert"] == "data"
        assert result.error_info["details"]["recovery_actions"] == [
            "retry",
            "reconfigure_provider",
            "exit",
        ]

    def test_planner_failure_returns_structured_error(self, agent):
        """Planner failures should be structured instead of faking an answer."""
        agent.action_planner = MagicMock(side_effect=RuntimeError("Planner failed"))
        agent._provider_config.provider = "anthropic"

        result = agent.forward(question="Test query", session_id="test_session")

        assert result.selected_expert == "chat"
        assert result.answer == ""
        assert result.error_info is not None
        assert result.error_info["error"] == "routing_error"
        assert result.error_info["details"]["recovery_actions"] == [
            "retry",
            "reconfigure_provider",
            "exit",
        ]

    def test_planner_error_step_limit_returns_structured_error(self, agent, monkeypatch, tmp_path):
        """Repeated planner errors should not become normal assistant text."""
        monkeypatch.setenv("CLIO_AGENT_MAX_STEPS", "2")
        hdf5_path = tmp_path / "run.h5"
        hdf5_path.touch()
        agent._store_conversation(
            f"Inspect {hdf5_path}",
            f"Inspected HDF5 file {hdf5_path}.",
            "test_session",
        )
        self._set_planner(
            agent,
            {"action": "expert", "expert": "analysis", "question": "analyze it"},
        )

        result = agent.forward(question="Test query", session_id="test_session")

        assert result.selected_expert == "chat"
        assert result.answer == ""
        assert result.error_info is not None
        assert result.error_info["error"] == "routing_error"
        assert "without producing a valid action" in result.error_info["message"]
        assert result.error_info["details"]["recovery_actions"] == [
            "retry",
            "reconfigure_provider",
            "exit",
        ]

    def test_stores_expert_invocation_in_arc(self, agent):
        """Test that expert dispatch stores invocation in ARC."""
        self._set_planner(agent, {"action": "expert", "expert": "data", "question": "Test query"})

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
        _set_planner(agent, {"action": "answer", "answer": "Hello!"})

        agent.forward(question="Hi", session_id="conv_test")

        conv = agent.arc.get_conversation("conv_test")
        assert conv is not None
        assert len(conv.messages) == 2  # user + assistant
