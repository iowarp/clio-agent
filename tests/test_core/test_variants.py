"""Tests for VariantManager -- save, load, deploy, rollback, compare.

Tests cover:
- save_variant creates file on disk and stores VariantRecord in ARC
- load_variant calls .load() on existing module instance (not new instance)
- deploy sets is_active=True on target, False on previously active
- rollback restores previous variant
- rollback returns None when no previous variant exists
- compare returns all variants sorted by created_at desc
- variant_id generation is sequential (data_v1, data_v2, ...)
- list_agents_with_variants scans disk for unique agent_ids
"""

import time
from pathlib import Path

import dspy
import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.schema import VariantRecord
from clio_agent.optimizer.variants import VariantManager


class MockModule(dspy.Module):
    """Mock dspy.Module for testing save/load."""

    def __init__(self):
        super().__init__()
        self._saved_path = None
        self._loaded_path = None

    def save(self, path: str, *args, **kwargs) -> None:
        self._saved_path = path
        # Write a dummy file so file exists checks pass
        Path(path).write_text('{"mock": true}')

    def load(self, path: str, *args, **kwargs) -> None:
        self._loaded_path = path

    def forward(self, **kwargs):
        pass


@pytest.fixture
def arc(tmp_path):
    """Create an ARCMemory instance with tmp_path."""
    return ARCMemory(data_dir=str(tmp_path / "arc"))


@pytest.fixture
def vm(arc, tmp_path):
    """Create a VariantManager with tmp_path."""
    return VariantManager(arc, variants_dir=str(tmp_path / "variants"))


@pytest.fixture
def mock_module():
    """Create a mock dspy.Module."""
    return MockModule()


class TestSaveVariant:
    """Tests for VariantManager.save_variant."""

    def test_save_creates_file_on_disk(self, vm, mock_module, tmp_path):
        """save_variant creates a JSON file on disk."""
        vm.save_variant(
            mock_module, "data", 0.6, 0.85, 50, 0.003, True
        )

        variant_path = tmp_path / "variants" / "data_v1.json"
        assert variant_path.exists()
        assert mock_module._saved_path == str(variant_path)

    def test_save_stores_variant_record_in_arc(self, vm, arc, mock_module):
        """save_variant stores VariantRecord in ARC memory."""
        vm.save_variant(
            mock_module, "data", 0.6, 0.85, 50, 0.003, True
        )

        stored = arc.get_variant_records("data")
        assert len(stored) == 1
        assert stored[0].variant_id == "data_v1"
        assert stored[0].before_score == 0.6
        assert stored[0].after_score == 0.85
        assert stored[0].improvement_delta == pytest.approx(0.25)
        assert stored[0].p_value == 0.003
        assert stored[0].is_significant is True
        assert stored[0].training_examples == 50
        assert stored[0].dspy_version == dspy.__version__

    def test_save_returns_variant_record(self, vm, mock_module):
        """save_variant returns a VariantRecord with correct metadata."""
        record = vm.save_variant(
            mock_module, "data", 0.6, 0.85, 50, 0.003, True
        )

        assert isinstance(record, VariantRecord)
        assert record.variant_id == "data_v1"
        assert record.agent_id == "data"
        assert record.is_active is False  # not auto-deployed

    def test_sequential_variant_ids(self, vm, mock_module):
        """Variant IDs are sequential: data_v1, data_v2, data_v3."""
        r1 = vm.save_variant(mock_module, "data", 0.5, 0.6, 30, 0.04, True)
        r2 = vm.save_variant(mock_module, "data", 0.6, 0.7, 40, 0.02, True)
        r3 = vm.save_variant(mock_module, "data", 0.7, 0.8, 50, 0.01, True)

        assert r1.variant_id == "data_v1"
        assert r2.variant_id == "data_v2"
        assert r3.variant_id == "data_v3"


class TestLoadVariant:
    """Tests for VariantManager.load_variant."""

    def test_load_calls_load_on_existing_instance(self, vm, mock_module, tmp_path):
        """load_variant calls .load() on the existing module, not a new one."""
        # Save first
        vm.save_variant(mock_module, "data", 0.6, 0.85, 50, 0.003, True)

        # Load into the same instance
        another_module = MockModule()
        result = vm.load_variant(another_module, "data_v1")

        # Must be the SAME instance (not a new one)
        assert result is another_module
        expected_path = str(tmp_path / "variants" / "data_v1.json")
        assert another_module._loaded_path == expected_path

    def test_load_raises_for_missing_file(self, vm, mock_module):
        """load_variant raises FileNotFoundError for missing variant."""
        with pytest.raises(FileNotFoundError, match="Variant file not found"):
            vm.load_variant(mock_module, "data_v99")


class TestDeploy:
    """Tests for VariantManager.deploy."""

    def test_deploy_activates_target(self, vm, arc, mock_module):
        """deploy sets is_active=True on the target variant."""
        vm.save_variant(mock_module, "data", 0.5, 0.6, 30, 0.04, True)
        vm.deploy("data_v1", "data")

        active = vm.get_active_variant("data")
        assert active is not None
        assert active.variant_id == "data_v1"
        assert active.is_active is True

    def test_deploy_deactivates_previous(self, vm, arc, mock_module):
        """deploy deactivates previously active variant."""
        vm.save_variant(mock_module, "data", 0.5, 0.6, 30, 0.04, True)
        vm.save_variant(mock_module, "data", 0.6, 0.75, 40, 0.02, True)

        vm.deploy("data_v1", "data")
        vm.deploy("data_v2", "data")

        records = arc.get_variant_records("data")
        active_count = sum(1 for r in records if r.is_active)
        assert active_count == 1

        active = vm.get_active_variant("data")
        assert active.variant_id == "data_v2"

    def test_deploy_raises_for_unknown_variant(self, vm, mock_module):
        """deploy raises ValueError for unknown variant_id."""
        with pytest.raises(ValueError, match="not found"):
            vm.deploy("data_v99", "data")


class TestRollback:
    """Tests for VariantManager.rollback."""

    def test_rollback_restores_previous(self, vm, mock_module):
        """rollback activates the previous variant and deactivates current."""
        vm.save_variant(mock_module, "data", 0.5, 0.6, 30, 0.04, True)
        vm.save_variant(mock_module, "data", 0.6, 0.75, 40, 0.02, True)

        vm.deploy("data_v1", "data")
        vm.deploy("data_v2", "data")

        restored = vm.rollback("data")
        assert restored == "data_v1"

        active = vm.get_active_variant("data")
        assert active.variant_id == "data_v1"

    def test_rollback_returns_none_no_previous(self, vm, mock_module):
        """rollback returns None when only one variant exists."""
        vm.save_variant(mock_module, "data", 0.5, 0.6, 30, 0.04, True)
        vm.deploy("data_v1", "data")

        result = vm.rollback("data")
        assert result is None

    def test_rollback_returns_none_no_variants(self, vm):
        """rollback returns None when no variants exist."""
        result = vm.rollback("data")
        assert result is None


class TestCompare:
    """Tests for VariantManager.compare."""

    def test_compare_returns_all_sorted(self, vm, mock_module):
        """compare returns all variants sorted by created_at descending."""
        vm.save_variant(mock_module, "data", 0.5, 0.6, 30, 0.04, True)
        time.sleep(0.01)  # ensure different created_at
        vm.save_variant(mock_module, "data", 0.6, 0.75, 40, 0.02, True)

        variants = vm.compare("data")
        assert len(variants) == 2
        # Most recent first (created_at desc)
        assert variants[0].variant_id == "data_v2"
        assert variants[1].variant_id == "data_v1"

    def test_compare_empty_for_unknown_agent(self, vm):
        """compare returns empty list for agent with no variants."""
        assert vm.compare("unknown") == []


class TestListAgents:
    """Tests for VariantManager.list_agents_with_variants."""

    def test_list_agents(self, vm, mock_module):
        """list_agents_with_variants returns sorted unique agent_ids."""
        vm.save_variant(mock_module, "data", 0.5, 0.6, 30, 0.04, True)
        vm.save_variant(mock_module, "analysis", 0.5, 0.7, 30, 0.03, True)

        agents = vm.list_agents_with_variants()
        assert agents == ["analysis", "data"]

    def test_list_agents_empty(self, vm):
        """list_agents_with_variants returns empty list when no variants."""
        assert vm.list_agents_with_variants() == []
