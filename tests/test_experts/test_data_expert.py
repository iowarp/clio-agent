"""
Tests for Data Expert module.
"""

import pytest
from unittest.mock import Mock, patch
from clio_agent.experts.data_expert import DataExpert


class TestDataExpert:
    """Test Data Expert functionality."""

    def test_capabilities(self):
        """Test expert capabilities metadata."""
        caps = DataExpert.get_capabilities()

        assert caps['name'] == "Data Expert"
        assert 'hdf5' in caps['keywords']
        assert 'adios' in caps['keywords']
        assert caps['priority'] == 1

    def test_expert_initialization(self):
        """Test expert can be initialized."""
        expert = DataExpert()

        assert expert is not None
        assert hasattr(expert, 'forward')
        assert hasattr(expert, 'agent')  # ReAct agent attribute

    def test_expert_chainofthought_mode(self):
        """Test expert in ChainOfThought mode (no tools)."""
        expert = DataExpert(use_tools=False)

        assert expert is not None
        assert expert.use_tools is False
        assert hasattr(expert, 'agent')

    def test_expert_react_mode_with_tools(self):
        """Test expert in ReAct mode with IOWarp MCP tools."""
        expert = DataExpert(use_tools=True, arc_memory=None)

        assert expert is not None
        assert expert.use_tools is True
        assert hasattr(expert, 'mcp_connector')
        assert hasattr(expert, 'tools')
        # Should have 10 IOWarp tools + 2 legacy mock tools
        assert len(expert.tools) == 12

    def test_expert_react_mode_with_arc(self):
        """Test expert with ARC memory integration."""
        mock_arc = Mock()
        expert = DataExpert(use_tools=True, arc_memory=mock_arc)

        assert expert is not None
        assert expert.arc_memory is mock_arc
        assert hasattr(expert, 'mcp_connector')

    # TODO: Add tests for:
    # - forward() method with mock DSPy predictions
    # - Tool calling with mock IOWarp servers
    # - ARC caching of tool results
    # - Error handling
