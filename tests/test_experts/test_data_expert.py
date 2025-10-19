"""
Tests for Data Expert module.
"""

import pytest
from unittest.mock import Mock, patch
from claudio.experts.data_expert import DataExpert


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
        assert hasattr(expert, 'generate')

    # TODO: Add tests for:
    # - forward() method with mock DSPy predictions
    # - Tool integration when tools are available
    # - Error handling
