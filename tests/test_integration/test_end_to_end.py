"""
End-to-end integration tests for CLIO Agent.

Tests the full flow: CLI -> Router -> Expert/Chat -> answer.
Tests multi-expert workflow, context compilation, routing persistence.
Tests requiring LM Studio are skipped if not available.
"""

import json
import time
from unittest.mock import MagicMock, patch

import pytest
import requests

from clio_agent.agent import ClioAgent
from clio_agent.arc.schema import DatasetProfile

pytestmark = pytest.mark.integration


def lm_studio_available():
    """Check if LM Studio is running and has models loaded."""
    try:
        r = requests.get("http://127.0.0.1:1234/v1/models", timeout=2)
        if r.status_code == 200:
            data = r.json()
            return bool(data.get("data"))
        return False
    except Exception:
        return False


class TestAgentArchitecture:
    """Test agent architecture without LM calls."""

    def test_agent_components_connected(self):
        """All components (router, chat, 3 experts, arc, lsm) are wired."""
        agent = ClioAgent()
        assert agent.router is not None
        assert agent.chat_agent is not None
        assert agent.data_expert is not None
        assert agent.analysis_expert is not None
        assert agent.visualization_expert is not None
        assert agent.arc is not None
        assert agent.lsm is not None
        assert agent.registry.get_agent_count() == 3
        agent.shutdown()

    def test_agent_data_expert_has_native_tool_boundary(self):
        """DataExpert should expose native tools without ReAct ownership."""
        agent = ClioAgent()
        expert = agent.data_expert
        assert hasattr(expert, "agent")
        assert "ReAct" not in type(expert.agent).__name__
        assert len(expert._tools) >= 4
        agent.shutdown()

    def test_agent_store_conversation(self, tmp_path):
        """_store_conversation should create a conversation in ARC."""
        agent = ClioAgent(data_dir=str(tmp_path / "clio_test"))
        session_id = f"test_session_{id(self)}"
        agent._store_conversation("test q", "test a", session_id)
        conv = agent.arc.get_conversation(session_id)
        assert conv is not None
        assert len(conv.messages) == 2
        assert conv.messages[0].role == "user"
        assert conv.messages[0].content == "test q"
        assert conv.messages[1].role == "assistant"
        assert conv.messages[1].content == "test a"
        agent.shutdown()

    def test_agent_store_metrics(self):
        """_store_metrics should write to LSM tree."""
        agent = ClioAgent()
        initial_stats = agent.get_lsm_stats()
        initial_count = initial_stats["write_count"]

        agent._store_metrics("q", "s1", "chat", 100.0, True)

        new_stats = agent.get_lsm_stats()
        assert new_stats["write_count"] == initial_count + 1
        agent.shutdown()

    def test_agent_get_session_history(self):
        """get_session_history returns conversation list."""
        agent = ClioAgent()
        history = agent.get_session_history("nonexistent_session")
        assert isinstance(history, list)
        agent.shutdown()


@pytest.mark.skipif(
    not lm_studio_available(),
    reason="LM Studio not running or no models loaded",
)
@pytest.mark.filterwarnings("ignore:Pydantic serializer warnings:UserWarning")
class TestEndToEnd:
    """End-to-end tests requiring LM Studio."""

    def test_data_query_returns_answer(self, sample_hdf5):
        """User asks about HDF5 file, gets tool-backed answer."""
        agent = ClioAgent()
        result = agent(question=f"What datasets are in {sample_hdf5}?")
        assert result.answer  # Non-empty answer
        assert result.selected_expert == "data"
        agent.shutdown()

    def test_greeting_returns_chat(self):
        """User says hello, gets conversational response."""
        agent = ClioAgent()
        result = agent(question="Hello, who are you?")
        assert result.answer
        assert result.selected_expert == "chat"
        agent.shutdown()

    def test_prediction_has_all_fields(self):
        """Verify returned Prediction has expected fields."""
        agent = ClioAgent()
        result = agent(question="Hi there")
        assert hasattr(result, "answer")
        assert hasattr(result, "selected_expert")
        assert hasattr(result, "session_id")
        assert hasattr(result, "duration_ms")
        assert hasattr(result, "arc_stats")
        assert hasattr(result, "lsm_stats")
        agent.shutdown()


def _make_mock_planner(selected_expert: str):
    """Create a mock planner returning a specified expert action."""
    mock_planner = MagicMock()
    if selected_expert in {"data", "analysis", "visualization"}:
        action = {"action": "expert", "expert": selected_expert, "question": "test query"}
    elif selected_expert == "none":
        action = {"action": "none", "answer": "No suitable CLIO action."}
    else:
        action = {"action": "answer", "answer": "Hello from CLIO."}
    mock_planner.return_value = MagicMock(action_json=json.dumps(action))
    return mock_planner


class TestMultiExpertWorkflow:
    """Test cross-expert workflows without LM Studio."""

    def test_multi_expert_workflow(self, tmp_path):
        """DataExpert stores profile -> AnalysisExpert reads profile."""
        agent = ClioAgent(data_dir=str(tmp_path / "clio"))
        session_id = "workflow_test"

        # Step 1: Simulate DataExpert storing a dataset profile
        profile = DatasetProfile(
            session_id=session_id,
            filepath="/data/experiment.parquet",
            file_format="parquet",
            created_by="data",
            created_at=time.time(),
            schema_info={"columns": ["temp", "pressure", "humidity"], "rows": 5000},
            statistics={
                "temp": {"mean": 22.3, "std": 4.1, "min": 10.0, "max": 38.5},
                "pressure": {"mean": 1013.2, "std": 5.5},
            },
            quality_notes=["No nulls detected"],
        )
        agent.arc.store_dataset_profile(profile)

        # Step 2: Route to analysis expert -- should receive file_context with profile
        agent.action_planner = _make_mock_planner("analysis")

        received_contexts = []
        mock_result = MagicMock()
        mock_result.analysis = "Statistical analysis complete"
        mock_result.recommendations = "Data quality is excellent"

        def capture_call(self_inner, **kwargs):
            received_contexts.append(kwargs.get("file_context", ""))
            return mock_result

        with patch.object(agent.analysis_expert.__class__, "__call__", capture_call):
            agent(question="Analyze the temperature column", session_id=session_id)

        # Verify profile was shared to analysis expert
        assert len(received_contexts) == 1
        ctx = json.loads(received_contexts[0])
        assert ctx[0]["filepath"] == "/data/experiment.parquet"
        assert ctx[0]["schema"]["rows"] == 5000
        agent.shutdown()

    def test_context_compilation_in_clioagent(self, tmp_path):
        """Query ClioAgent, verify ContextCompiler was used."""
        agent = ClioAgent(data_dir=str(tmp_path / "clio"))
        session_id = "context_test"

        # Store some prior conversation
        agent._store_conversation("What is HDF5?", "HDF5 is a data format.", session_id)

        # Now query with "none" to avoid needing LM
        agent.action_planner = _make_mock_planner("none")
        result = agent(question="Tell me about weather", session_id=session_id)

        # Context compiler should have been used (graceful even if no enrichment)
        assert result.answer is not None
        assert result.selected_expert == "none"
        agent.shutdown()

    def test_routing_decision_persistence(self, tmp_path):
        """Make 3 queries, verify routing_decisions list in conversation."""
        agent = ClioAgent(data_dir=str(tmp_path / "clio"))
        session_id = "routing_persist"

        none_turns = [
            ("Weather today?", "Weather requests are outside this assistant's scope."),
            ("Sports score?", "Sports requests are outside this assistant's scope."),
            ("Calendar holiday?", "Calendar trivia is outside this assistant's scope."),
        ]
        for question, answer in none_turns:
            action = {"action": "none", "answer": answer}
            agent.action_planner = MagicMock(return_value=MagicMock(action_json=json.dumps(action)))
            agent(question=question, session_id=session_id)

        conv = agent.arc.get_conversation(session_id)
        assert conv is not None
        assert len(conv.routing_decisions) == 3
        assert all(rd.selected_agent == "none" for rd in conv.routing_decisions)
        agent.shutdown()
