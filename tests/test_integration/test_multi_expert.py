"""
Multi-expert integration tests for CLIO Agent.

Tests one-pass planner dispatch, expert registration, routing decision storage,
cross-expert dataset profile sharing, and gateway tool listing.
"""

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from clio_agent.agent import ClioAgent
from clio_agent.arc.schema import DatasetProfile
from clio_agent.signatures.main_agent_sig import AgentActionSignature

pytestmark = pytest.mark.integration


class TestPlannerActionContract:
    """Test the one-pass planner action contract is documented in the signature."""

    def test_planner_doc_lists_action_kinds(self):
        """Planner instructions should list every accepted action kind."""
        instructions = AgentActionSignature.instructions
        assert '{"action":"tool"' in instructions
        assert '{"action":"expert"' in instructions
        assert '{"action":"answer"' in instructions
        assert '{"action":"none"' in instructions

    def test_planner_doc_lists_experts(self):
        """Planner expert action should be constrained to the registered expert ids."""
        assert '"expert":"data|analysis|visualization"' in AgentActionSignature.instructions


class TestClioAgentExperts:
    """Test ClioAgent has all 3 experts instantiated and registered."""

    def test_clioagent_has_three_experts(self):
        """ClioAgent must have data, analysis, visualization expert attributes."""
        agent = ClioAgent()
        assert hasattr(agent, "data_expert")
        assert hasattr(agent, "analysis_expert")
        assert hasattr(agent, "visualization_expert")
        assert agent.data_expert is not None
        assert agent.analysis_expert is not None
        assert agent.visualization_expert is not None
        agent.shutdown()

    def test_clioagent_registers_three_experts(self):
        """Registry should contain exactly 3 experts."""
        agent = ClioAgent()
        assert agent.registry.get_agent_count() == 3
        agent.shutdown()

    def test_clioagent_registry_ids(self):
        """Registry should have the correct agent IDs."""
        agent = ClioAgent()
        ids = agent.registry.list_agents()
        assert set(ids) == {"data", "analysis", "visualization"}
        agent.shutdown()


def _make_mock_planner(selected_expert: str):
    """Create a mock planner returning the requested one-pass action."""
    mock_planner = MagicMock()
    if selected_expert in {"data", "analysis", "visualization"}:
        action = {"action": "expert", "expert": selected_expert, "question": "test query"}
    elif selected_expert == "none":
        action = {
            "action": "none",
            "answer": "CLIO is specialized in scientific data; no suitable CLIO action.",
        }
    else:
        action = {"action": "answer", "answer": "Hello! I am CLIO."}
    mock_planner.return_value = MagicMock(action_json=json.dumps(action))
    return mock_planner


class TestDispatch:
    """Test dispatch to different experts based on planner output."""

    def test_dispatch_data_query(self, tmp_path):
        """Planner selects data, verify data_expert called."""
        agent = ClioAgent(data_dir=str(tmp_path / "clio"))

        agent.action_planner = _make_mock_planner("data")

        # Mock data_expert forward
        mock_result = MagicMock()
        mock_result.analysis = "HDF5 analysis results"
        mock_result.recommendations = "Use gzip compression"

        with patch.object(agent, "data_expert", return_value=mock_result) as mock_expert:
            result = agent(question="Analyze my HDF5 file")
            mock_expert.assert_called_once()
            assert "HDF5 analysis results" in result.answer
            assert result.selected_expert == "data"
        agent.shutdown()

    def test_dispatch_analysis_query(self, tmp_path):
        """Planner selects analysis, verify analysis_expert called."""
        agent = ClioAgent(data_dir=str(tmp_path / "clio"))

        agent.action_planner = _make_mock_planner("analysis")

        mock_result = MagicMock()
        mock_result.analysis = "Parquet schema has 3 columns"
        mock_result.recommendations = "Check null rates"

        with patch.object(agent, "analysis_expert", return_value=mock_result) as mock_expert:
            result = agent(question="Analyze parquet schema")
            mock_expert.assert_called_once()
            assert "Parquet schema" in result.answer
            assert result.selected_expert == "analysis"
        agent.shutdown()

    def test_dispatch_visualization_query(self, tmp_path):
        """Planner selects visualization, verify visualization_expert called."""
        agent = ClioAgent(data_dir=str(tmp_path / "clio"))

        agent.action_planner = _make_mock_planner("visualization")

        mock_result = MagicMock()
        mock_result.visualization_description = "Histogram of temperature"
        mock_result.file_path = "/tmp/histogram.png"

        with patch.object(agent, "visualization_expert", return_value=mock_result) as mock_expert:
            result = agent(question="Plot the distribution of temperature")
            mock_expert.assert_called_once()
            assert "Histogram of temperature" in result.answer
            assert result.selected_expert == "visualization"
        agent.shutdown()

    def test_dispatch_none_query(self, tmp_path):
        """Planner selects 'none', verify its explicit message is returned."""
        agent = ClioAgent(data_dir=str(tmp_path / "clio"))
        agent.action_planner = _make_mock_planner("none")

        result = agent(question="What is the weather today?")
        assert "CLIO" in result.answer
        assert "scientific data" in result.answer
        assert result.selected_expert == "none"
        agent.shutdown()

    def test_dispatch_chat_query(self, tmp_path):
        """Planner selects direct chat answer without an implicit fallback call."""
        agent = ClioAgent(data_dir=str(tmp_path / "clio"))

        agent.action_planner = _make_mock_planner("chat")

        mock_result = MagicMock()
        mock_result.answer = "fallback should not run"

        with patch.object(agent, "chat_agent", return_value=mock_result) as mock_chat:
            result = agent(question="Hello, who are you?")
            mock_chat.assert_not_called()
            assert "CLIO" in result.answer
            assert result.selected_expert == "chat"
        agent.shutdown()


class TestRoutingDecisionStorage:
    """Test that routing decisions are stored in ARC."""

    def test_routing_decision_stored_in_arc(self, tmp_path):
        """After a query, routing decision should appear in conversation."""
        agent = ClioAgent(data_dir=str(tmp_path / "clio"))
        session_id = "test_routing_session"

        agent.action_planner = _make_mock_planner("none")
        agent(question="What is quantum physics?", session_id=session_id)

        # Check conversation has routing decision
        conv = agent.arc.get_conversation(session_id)
        assert conv is not None
        assert len(conv.routing_decisions) >= 1
        rd = conv.routing_decisions[-1]
        assert rd.selected_agent == "none"
        assert rd.query == "What is quantum physics?"
        agent.shutdown()

    def test_multiple_routing_decisions_accumulate(self, tmp_path):
        """Multiple queries should accumulate routing decisions."""
        agent = ClioAgent(data_dir=str(tmp_path / "clio"))
        session_id = "test_multi_routing"

        queries_and_experts = [
            ("Hello", "chat"),
            ("Analyze HDF5", "data"),
            ("Plot distribution", "visualization"),
        ]

        for question, expert in queries_and_experts:
            agent.action_planner = _make_mock_planner(expert)

            # Mock all possible targets to avoid LM errors
            mock_result = MagicMock()
            mock_result.answer = "mock answer"
            mock_result.analysis = "mock analysis"
            mock_result.recommendations = "mock recs"
            mock_result.visualization_description = "mock viz"
            mock_result.file_path = "/tmp/mock.png"

            with patch.object(agent, "chat_agent", return_value=mock_result):
                with patch.object(agent, "data_expert", return_value=mock_result):
                    with patch.object(agent, "visualization_expert", return_value=mock_result):
                        agent(question=question, session_id=session_id)

        conv = agent.arc.get_conversation(session_id)
        assert conv is not None
        assert len(conv.routing_decisions) == 3
        assert conv.routing_decisions[0].selected_agent == "chat"
        assert conv.routing_decisions[1].selected_agent == "data"
        assert conv.routing_decisions[2].selected_agent == "visualization"
        agent.shutdown()


class TestDatasetProfileSharing:
    """Test dataset profiles are shared across experts via ARC."""

    def test_dataset_profile_passed_to_analysis(self, tmp_path):
        """Store a profile, query analysis, verify file_context populated."""
        agent = ClioAgent(data_dir=str(tmp_path / "clio"))
        session_id = "test_profile_sharing"

        # Store a dataset profile as if DataExpert created it
        profile = DatasetProfile(
            session_id=session_id,
            filepath="/data/test.parquet",
            file_format="parquet",
            created_by="data",
            created_at=time.time(),
            schema_info={"columns": ["temp", "pressure"], "rows": 1000},
            statistics={"temp": {"mean": 24.5, "std": 3.2}},
        )
        agent.arc.store_dataset_profile(profile)

        # Mock router to route to analysis
        agent.action_planner = _make_mock_planner("analysis")

        # Track what file_context the analysis_expert receives
        received_contexts = []

        mock_result = MagicMock()
        mock_result.analysis = "Analysis with profile context"
        mock_result.recommendations = "Based on stored profile"

        def capture_call(self_inner, **kwargs):
            received_contexts.append(kwargs.get("file_context", ""))
            return mock_result

        with patch.object(agent.analysis_expert.__class__, "__call__", capture_call):
            agent(question="Analyze parquet stats", session_id=session_id)

        # Verify file_context was passed with profile data
        assert len(received_contexts) == 1
        file_context = received_contexts[0]
        assert file_context != ""
        parsed = json.loads(file_context)
        assert len(parsed) == 1
        assert parsed[0]["filepath"] == "/data/test.parquet"
        assert parsed[0]["schema"]["columns"] == ["temp", "pressure"]
        agent.shutdown()


class TestGateway:
    """Test gateway tool listing."""

    def test_gateway_lists_all_tools(self):
        """Gateway should have both hdf5_* and parquet_* tools."""
        import asyncio

        from fastmcp import Client

        from clio_agent.tools.gateway import gateway

        async def _list():
            async with Client(gateway) as c:
                return await c.list_tools()

        tools = asyncio.run(_list())
        names = [t.name for t in tools]

        # HDF5 tools
        assert any(n.startswith("hdf5_") for n in names)
        # Parquet tools
        assert any(n.startswith("parquet_") for n in names)
        # At least 8 total (5 HDF5 + 3 Parquet)
        assert len(names) >= 8

    def test_list_capabilities_lightweight(self):
        """list_capabilities should return compact summaries."""
        from clio_agent.tools.gateway import list_capabilities

        caps = list_capabilities()
        assert isinstance(caps, list)
        assert len(caps) >= 8

        # Each entry has name, description, server
        for cap in caps:
            assert "name" in cap
            assert "description" in cap
            assert "server" in cap
            assert cap["server"] in ("hdf5", "parquet", "fs", "unknown")

        # Check compact format (description should be single sentence)
        for cap in caps:
            desc = cap["description"]
            # Should end with period and be relatively short
            assert desc.endswith(".")
            assert len(desc) < 200  # compact, not full schema
