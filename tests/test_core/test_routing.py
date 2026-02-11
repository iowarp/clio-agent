"""
Tests for Router dispatch logic.

Tests RouterSignature and ChatAgentSignature field definitions,
Literal type constraints, and routing behavior at the ClioAgent level.
"""

from typing import get_args, get_type_hints

import dspy

from clio_agent.signatures.main_agent_sig import ChatAgentSignature, RouterSignature


class TestRouterSignature:
    """Test RouterSignature field definitions and Literal output."""

    def test_has_question_input(self):
        """Verify RouterSignature has a 'question' input field."""
        assert "question" in RouterSignature.input_fields

    def test_has_selected_expert_output(self):
        """Verify RouterSignature has 'selected_expert' output field."""
        assert "selected_expert" in RouterSignature.output_fields

    def test_selected_expert_is_literal(self):
        """Verify selected_expert uses Literal['data', 'chat'] annotation."""
        hints = get_type_hints(RouterSignature)
        annotation = hints["selected_expert"]
        # Extract Literal args
        args = get_args(annotation)
        assert "data" in args
        assert "chat" in args

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
