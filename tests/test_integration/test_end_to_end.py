"""
End-to-end integration tests for CLIO Agent.

Tests the full flow: CLI -> Router -> Expert/Chat -> answer.
Tests requiring LM Studio are skipped if not available.
"""

import pytest
import requests

from clio_agent.agent import ClioAgent


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

    def test_agent_data_expert_has_react(self):
        """DataExpert should use ReAct with real MCP tools."""
        agent = ClioAgent()
        expert = agent.data_expert
        assert hasattr(expert, "agent")
        assert hasattr(expert.agent, "tools")
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
