"""
Tests for clio_agent.agent module.

Tests ClioAgent agent routing and expert selection.
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from clio_agent.agent import ClioAgent


class TestClioAgent:
    """Test ClioAgent agent functionality."""

    def test_clio_agent_initialization(self):
        """Test ClioAgent agent can be initialized."""
        # Note: This requires DSPy to be configured, but we can test basic structure
        agent = ClioAgent()
        
        assert agent is not None
        assert hasattr(agent, 'forward')
        assert hasattr(agent, 'registry')
        
    def test_expert_registry(self):
        """Test that only DataExpert is in the registry."""
        agent = ClioAgent()
        
        # Check registry contents
        agents = agent.registry.list_agents()
        assert 'data' in agents
        assert len(agents) == 1

    def test_expert_capabilities(self):
        """Test expert capabilities are loaded."""
        agent = ClioAgent()
        
        # Use registry to get capabilities
        cap = agent.registry.get_capabilities('data')
        assert cap is not None
        assert cap.description is not None
        assert 'hdf5' in cap.keywords

    # TODO: Add tests for:
    # - forward() method with mock DSPy predictions
    # - Routing logic validation
    # - Error handling
    # These require mocking DSPy components
