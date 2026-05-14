"""
Tests for Router dispatch logic.

Tests RouterSignature and ChatAgentSignature field definitions,
Literal type constraints, and routing behavior at the ClioAgent level.
"""

from typing import get_args, get_type_hints

import dspy

import pytest

from clio_agent.signatures.main_agent_sig import (
    ChatAgentSignature,
    DataIntentSig,
    RouterSignature,
)


class TestRouterSignature:
    """Test RouterSignature field definitions and Literal output."""

    def test_has_question_input(self):
        """Verify RouterSignature has a 'question' input field."""
        assert "question" in RouterSignature.input_fields

    def test_has_selected_expert_output(self):
        """Verify RouterSignature has 'selected_expert' output field."""
        assert "selected_expert" in RouterSignature.output_fields

    def test_selected_expert_is_literal_with_five_targets(self):
        """Verify selected_expert uses Literal with all 5 routing targets."""
        hints = get_type_hints(RouterSignature)
        annotation = hints["selected_expert"]
        args = get_args(annotation)
        assert "chat" in args
        assert "data" in args
        assert "analysis" in args
        assert "visualization" in args
        assert "none" in args
        assert len(args) == 5

    def test_no_extra_input_fields(self):
        """Router should only have 'question' as input."""
        assert len(RouterSignature.input_fields) == 1

    def test_no_extra_output_fields(self):
        """Router should only have 'selected_expert' as output."""
        assert len(RouterSignature.output_fields) == 1

    def test_docstring_mentions_routing(self):
        """Signature docstring should describe routing behavior."""
        doc = RouterSignature.__doc__
        assert doc is not None
        assert "route" in doc.lower() or "router" in doc.lower()

    def test_docstring_mentions_analysis(self):
        """Signature docstring should mention analysis routing."""
        doc = RouterSignature.__doc__
        assert doc is not None
        assert "analysis" in doc.lower()

    def test_docstring_mentions_visualization(self):
        """Signature docstring should mention visualization routing."""
        doc = RouterSignature.__doc__
        assert doc is not None
        assert "visualization" in doc.lower()

    def test_can_create_chain_of_thought(self):
        """Verify ChainOfThought can be instantiated with RouterSignature."""
        router = dspy.ChainOfThought(RouterSignature)
        assert router is not None


class TestChatAgentSignature:
    """Test ChatAgentSignature field definitions."""

    def test_has_question_input(self):
        """Verify ChatAgentSignature has 'question' input."""
        assert "question" in ChatAgentSignature.input_fields

    def test_has_session_context_input(self):
        """Verify ChatAgentSignature has 'session_context' input."""
        assert "session_context" in ChatAgentSignature.input_fields

    def test_has_answer_output(self):
        """Verify ChatAgentSignature has 'answer' output."""
        assert "answer" in ChatAgentSignature.output_fields

    def test_input_count(self):
        """ChatAgentSignature should have exactly 2 inputs."""
        assert len(ChatAgentSignature.input_fields) == 2

    def test_output_count(self):
        """ChatAgentSignature should have exactly 1 output."""
        assert len(ChatAgentSignature.output_fields) == 1

    def test_docstring_mentions_clio(self):
        """Signature docstring should describe CLIO identity."""
        doc = ChatAgentSignature.__doc__
        assert doc is not None
        assert "CLIO" in doc

    def test_docstring_mentions_all_experts(self):
        """ChatAgentSignature docstring should mention all 3 experts."""
        doc = ChatAgentSignature.__doc__
        assert doc is not None
        assert "AnalysisExpert" in doc
        assert "VisualizationExpert" in doc

    def test_can_create_chain_of_thought(self):
        """Verify ChainOfThought can be instantiated with ChatAgentSignature."""
        chat = dspy.ChainOfThought(ChatAgentSignature)
        assert chat is not None


class TestClioAgentRouting:
    """Test routing at the ClioAgent orchestrator level."""

    def test_agent_has_router(self):
        """ClioAgent should have a router attribute."""
        from clio_agent.agent import ClioAgent

        agent = ClioAgent()
        assert hasattr(agent, "router")
        agent.shutdown()

    def test_agent_has_chat_agent(self):
        """ClioAgent should have a chat_agent attribute."""
        from clio_agent.agent import ClioAgent

        agent = ClioAgent()
        assert hasattr(agent, "chat_agent")
        agent.shutdown()

    def test_agent_has_router_lm(self):
        """ClioAgent should configure a separate LM for routing."""
        from clio_agent.agent import ClioAgent

        agent = ClioAgent()
        assert hasattr(agent, "_router_lm")
        assert agent._router_lm is not None
        agent.shutdown()

    def test_registry_has_data_expert(self):
        """Registry should have 'data' expert registered."""
        from clio_agent.agent import ClioAgent

        agent = ClioAgent()
        agents = agent.registry.list_agents()
        assert "data" in agents
        agent.shutdown()

    def test_registry_has_analysis_expert(self):
        """Registry should have 'analysis' expert registered."""
        from clio_agent.agent import ClioAgent

        agent = ClioAgent()
        agents = agent.registry.list_agents()
        assert "analysis" in agents
        agent.shutdown()

    def test_registry_has_visualization_expert(self):
        """Registry should have 'visualization' expert registered."""
        from clio_agent.agent import ClioAgent

        agent = ClioAgent()
        agents = agent.registry.list_agents()
        assert "visualization" in agents
        agent.shutdown()

    def test_registry_data_capability_keywords(self):
        """Data expert capability should include key routing keywords."""
        from clio_agent.agent import ClioAgent

        agent = ClioAgent()
        cap = agent.registry.get_capabilities("data")
        assert cap is not None
        assert "hdf5" in cap.keywords
        assert "compression" in cap.keywords
        assert "chunking" in cap.keywords
        agent.shutdown()

    def test_registry_data_capability_tools(self):
        """Data expert capability should list real MCP tool names."""
        from clio_agent.agent import ClioAgent

        agent = ClioAgent()
        cap = agent.registry.get_capabilities("data")
        assert cap is not None
        assert "hdf5_analyze_file" in cap.tools
        assert "hdf5_list_datasets" in cap.tools
        agent.shutdown()

    def test_registry_analysis_capability_keywords(self):
        """Analysis expert capability should include parquet/statistics keywords."""
        from clio_agent.agent import ClioAgent

        agent = ClioAgent()
        cap = agent.registry.get_capabilities("analysis")
        assert cap is not None
        assert "parquet" in cap.keywords
        assert "statistics" in cap.keywords
        agent.shutdown()

    def test_registry_visualization_capability_keywords(self):
        """Visualization expert capability should include plot/chart keywords."""
        from clio_agent.agent import ClioAgent

        agent = ClioAgent()
        cap = agent.registry.get_capabilities("visualization")
        assert cap is not None
        assert "plot" in cap.keywords
        assert "chart" in cap.keywords
        agent.shutdown()


# iowarp/clio-agent#25 — DataIntentSig + _classify_data_intent tests.


class TestDataIntentSignature:
    """DataIntentSig must declare a Literal[inspect, reason] output."""

    def test_has_question_input(self):
        assert "question" in DataIntentSig.input_fields

    def test_has_intent_output(self):
        assert "intent" in DataIntentSig.output_fields

    def test_intent_is_literal_with_two_targets(self):
        hints = get_type_hints(DataIntentSig)
        annotation = hints["intent"]
        args = get_args(annotation)
        assert set(args) == {"inspect", "reason"}

    def test_docstring_mentions_both_paths(self):
        doc = DataIntentSig.__doc__ or ""
        assert "inspect" in doc.lower()
        assert "reason" in doc.lower()


class TestClassifyDataIntent:
    """_classify_data_intent must return inspect / reason / ambiguous
    based on the wording, never call an LM."""

    @pytest.mark.parametrize(
        "question",
        [
            "list datasets in data/atmospheric.h5",
            "inspect /tmp/x.h5",
            "what's in /tmp/x.h5",
            "show me the schema of /tmp/x.h5",
            "analyze data/atmospheric.h5",
            "describe the file data/atmospheric.h5",
            "preview /tmp/x.h5",
            "schema of /tmp/x.parquet",
        ],
    )
    def test_inspect_verbs(self, question):
        from clio_agent.agent import ClioAgent

        assert ClioAgent._classify_data_intent(question) == "inspect"

    @pytest.mark.parametrize(
        "question",
        [
            "compare pressure and temperature from data/atmospheric.h5",
            "why is /tmp/x.h5 so large?",
            "recommend a compression strategy for /tmp/x.h5",
            "is there an anomaly in /tmp/x.h5",
            "explain the trend in data/atmospheric.h5",
            "optimize the chunking of /tmp/x.h5",
            "which is larger, temperature or pressure, in /tmp/x.h5?",
            "distribution of pressure in data/atmospheric.h5",
        ],
    )
    def test_reason_verbs(self, question):
        from clio_agent.agent import ClioAgent

        assert ClioAgent._classify_data_intent(question) == "reason"

    @pytest.mark.parametrize(
        "question",
        [
            "/tmp/x.h5",
            "tell me about /tmp/x.h5",
            "data/atmospheric.h5",
            "see /tmp/x.h5",
        ],
    )
    def test_ambiguous_returns_ambiguous(self, question):
        from clio_agent.agent import ClioAgent

        assert ClioAgent._classify_data_intent(question) == "ambiguous"

    def test_inspect_and_reason_together_is_ambiguous(self):
        """If both verb buckets match the same question, treat it as
        ambiguous so the LM resolver gets the final say."""
        from clio_agent.agent import ClioAgent

        q = "list datasets and compare temperature with pressure in /tmp/x.h5"
        assert ClioAgent._classify_data_intent(q) == "ambiguous"
