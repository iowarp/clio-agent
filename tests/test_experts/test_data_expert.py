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
        assert hasattr(expert, 'agent')

    def test_expert_chainofthought_mode(self):
        """Test expert in ChainOfThought mode (no tools)."""
        expert = DataExpert(use_tools=False)

        assert expert is not None
        assert expert.use_tools is False
        assert hasattr(expert, 'agent')

    def test_expert_tools_mode_uses_cot(self):
        """Test expert in tools mode still uses ChainOfThought (Plan 02 will add ReAct)."""
        expert = DataExpert(use_tools=True, arc_memory=None)

        assert expert is not None
        assert expert.use_tools is True
        assert hasattr(expert, 'agent')

    def test_expert_with_arc_memory(self):
        """Test expert with ARC memory integration."""
        mock_arc = Mock()
        expert = DataExpert(use_tools=True, arc_memory=mock_arc)

        assert expert is not None
        assert expert.arc_memory is mock_arc
