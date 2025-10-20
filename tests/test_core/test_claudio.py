"""
Tests for claudio.claudio module.

Tests ClaudIO agent routing and expert selection.
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from claudio.claudio import ClaudIO


class TestClaudIO:
    """Test ClaudIO agent functionality."""

    def test_claudio_initialization(self):
        """Test ClaudIO agent can be initialized."""
        # Note: This requires DSPy to be configured, but we can test basic structure
        agent = ClaudIO()
        
        assert agent is not None
        assert hasattr(agent, 'forward')
        assert hasattr(agent, 'experts')
        assert hasattr(agent, 'expert_capabilities')

    def test_expert_registry(self):
        """Test that only DataExpert is in the registry."""
        agent = ClaudIO()
        
        # Should only have 'data' expert
        assert 'data' in agent.experts
        assert len(agent.experts) == 1

    def test_expert_capabilities(self):
        """Test expert capabilities are loaded."""
        agent = ClaudIO()
        
        caps = agent.expert_capabilities
        assert 'data' in caps
        assert len(caps) == 1
        
        # Check data expert capabilities
        data_caps = caps['data']
        assert 'name' in data_caps
        assert 'description' in data_caps
        assert 'keywords' in data_caps

    def test_format_capabilities(self):
        """Test capability formatting for router."""
        agent = ClaudIO()
        
        formatted = agent._format_capabilities()
        
        assert isinstance(formatted, str)
        assert 'data' in formatted.lower()
        assert 'hdf5' in formatted.lower() or 'adios' in formatted.lower()

    # TODO: Add tests for:
    # - forward() method with mock DSPy predictions
    # - Routing logic validation
    # - Error handling
    # These require mocking DSPy components
