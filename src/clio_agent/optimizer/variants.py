"""Variant management for optimized expert modules.

Provides VariantManager for saving, loading, deploying, rolling back,
and comparing optimized expert variants. Each variant is stored as a
JSON file on disk with metadata tracked in ARC memory via VariantRecord.

Key design decisions:
- load_variant reuses existing module instance to avoid MCPToolBridge
  constructor side effects (Pitfall 5 from research).
- Only one variant per agent is active at any time.
- Variant IDs are sequential: {agent_id}_v1, {agent_id}_v2, etc.
"""

from pathlib import Path
from typing import Any

import dspy

from clio_agent.arc.schema import VariantRecord


class VariantManager:
    """Manages optimized expert variant lifecycle.

    Saves optimized modules to disk, tracks metadata in ARC, and manages
    deployment/rollback so that exactly one variant per agent is active.

    Args:
        arc_memory: ARCMemory instance for variant record storage
        variants_dir: Directory for saving variant JSON files

    Example:
        >>> from clio_agent.arc.memory import ARCMemory
        >>> arc = ARCMemory(data_dir="/tmp/arc")
        >>> vm = VariantManager(arc, variants_dir="/tmp/variants")
        >>> record = vm.save_variant(module, "data", 0.6, 0.85, 50, 0.003, True)
        >>> vm.deploy(record.variant_id, "data")
    """

    def __init__(
        self,
        arc_memory: Any,
        variants_dir: str = ".clio_agent/variants",
    ) -> None:
        """Initialize VariantManager.

        Args:
            arc_memory: ARCMemory instance for variant record storage
            variants_dir: Directory for saving variant JSON files
        """
        self._arc = arc_memory
        self._variants_dir = Path(variants_dir)
        self._variants_dir.mkdir(parents=True, exist_ok=True)

    def save_variant(
        self,
        module: dspy.Module,
        agent_id: str,
        before_score: float,
        after_score: float,
        training_examples: int,
        p_value: float,
        is_significant: bool,
    ) -> VariantRecord:
        """Save an optimized module variant to disk and ARC.

        Generates a sequential variant_id, calls module.save() to persist
        the module state, and stores a VariantRecord in ARC with all metadata.

        Args:
            module: Optimized dspy.Module to save
            agent_id: Expert identifier (e.g., "data", "analysis")
            before_score: Score before optimization
            after_score: Score after optimization
            training_examples: Number of training examples used
            p_value: Statistical significance p-value
            is_significant: Whether improvement passed significance test

        Returns:
            VariantRecord with all metadata

        Example:
            >>> record = vm.save_variant(optimized, "data", 0.6, 0.85, 50, 0.003, True)
            >>> print(f"Saved: {record.variant_id}")
        """
        # Determine next sequential variant number
        existing = self._arc.get_variant_records(agent_id)
        next_n = len(existing) + 1
        variant_id = f"{agent_id}_v{next_n}"

        # Save module to disk
        variant_path = self._variants_dir / f"{variant_id}.json"
        module.save(str(variant_path))

        # Create and store VariantRecord
        record = VariantRecord(
            variant_id=variant_id,
            agent_id=agent_id,
            training_examples=training_examples,
            before_score=before_score,
            after_score=after_score,
            improvement_delta=after_score - before_score,
            p_value=p_value,
            is_significant=is_significant,
            is_active=False,
            file_path=str(variant_path),
            dspy_version=dspy.__version__,
        )
        self._arc.store_variant_record(record)

        return record

    def load_variant(
        self,
        existing_module: dspy.Module,
        variant_id: str,
    ) -> dspy.Module:
        """Load a variant's state into an existing module instance.

        Reuses the existing module instance to avoid MCPToolBridge
        constructor side effects. Calls existing_module.load() which
        loads saved state into the same object.

        Args:
            existing_module: Existing dspy.Module instance to load state into
            variant_id: Variant identifier to load

        Returns:
            The same module instance with loaded state

        Raises:
            FileNotFoundError: If variant file does not exist

        Example:
            >>> loaded = vm.load_variant(expert_module, "data_v2")
            >>> assert loaded is expert_module  # same instance
        """
        variant_path = self._variants_dir / f"{variant_id}.json"
        if not variant_path.exists():
            raise FileNotFoundError(
                f"Variant file not found: {variant_path}"
            )

        existing_module.load(path=str(variant_path))
        return existing_module

    def deploy(self, variant_id: str, agent_id: str) -> None:
        """Deploy a variant as the active variant for an agent.

        Sets the specified variant as active and deactivates any
        previously active variant for the same agent. Exactly one
        variant per agent is active at a time.

        Args:
            variant_id: Variant identifier to deploy
            agent_id: Agent identifier

        Raises:
            ValueError: If variant_id not found for agent_id

        Example:
            >>> vm.deploy("data_v2", "data")
        """
        records = self._arc.get_variant_records(agent_id)

        found = False
        for record in records:
            if record.variant_id == variant_id:
                found = True

        if not found:
            raise ValueError(
                f"Variant '{variant_id}' not found for agent '{agent_id}'"
            )

        # Deactivate all, then activate target
        for record in records:
            if record.is_active and record.variant_id != variant_id:
                record.is_active = False
                self._arc.store_variant_record(record)
            elif record.variant_id == variant_id and not record.is_active:
                record.is_active = True
                self._arc.store_variant_record(record)

    def rollback(self, agent_id: str) -> str | None:
        """Rollback to the previous active variant for an agent.

        Finds the currently active variant, deactivates it, and
        activates the second most recent variant (by created_at).
        Returns None if no previous variant exists.

        Args:
            agent_id: Agent identifier

        Returns:
            Restored variant_id, or None if no previous variant exists

        Example:
            >>> restored = vm.rollback("data")
            >>> if restored:
            ...     print(f"Rolled back to {restored}")
        """
        records = self._arc.get_variant_records(agent_id)

        if len(records) < 2:
            return None

        # Records are sorted by created_at descending (most recent first)
        # Find currently active
        active_record = None
        for record in records:
            if record.is_active:
                active_record = record
                break

        if active_record is None:
            return None

        # Find the previous variant (next one in the list after active,
        # or the second record if active is the first)
        previous_record = None
        found_active = False
        for record in records:
            if record.variant_id == active_record.variant_id:
                found_active = True
                continue
            if found_active:
                previous_record = record
                break

        if previous_record is None:
            return None

        # Deactivate current, activate previous
        active_record.is_active = False
        self._arc.store_variant_record(active_record)

        previous_record.is_active = True
        self._arc.store_variant_record(previous_record)

        return previous_record.variant_id

    def get_active_variant(self, agent_id: str) -> VariantRecord | None:
        """Get the currently active variant for an agent.

        Args:
            agent_id: Agent identifier

        Returns:
            Active VariantRecord, or None if no variant is active

        Example:
            >>> active = vm.get_active_variant("data")
            >>> if active:
            ...     print(f"Active: {active.variant_id}")
        """
        records = self._arc.get_variant_records(agent_id)
        for record in records:
            if record.is_active:
                return record
        return None

    def compare(self, agent_id: str) -> list[VariantRecord]:
        """Get all variants for an agent for comparison.

        Returns all VariantRecords sorted by created_at descending
        (most recent first), suitable for CLI comparison table display.

        Args:
            agent_id: Agent identifier

        Returns:
            List of VariantRecord objects, most recent first

        Example:
            >>> variants = vm.compare("data")
            >>> for v in variants:
            ...     print(f"{v.variant_id}: {v.before_score} -> {v.after_score}")
        """
        return self._arc.get_variant_records(agent_id)

    def list_agents_with_variants(self) -> list[str]:
        """List agent IDs that have at least one variant.

        Scans variant files on disk to find unique agent_ids.

        Returns:
            Sorted list of agent_id strings

        Example:
            >>> agents = vm.list_agents_with_variants()
            >>> print(agents)  # ['analysis', 'data']
        """
        agent_ids: set[str] = set()

        for fpath in self._variants_dir.glob("*.json"):
            # variant_id format: {agent_id}_v{N}
            name = fpath.stem  # e.g., "data_v1"
            parts = name.rsplit("_v", 1)
            if len(parts) == 2:
                agent_ids.add(parts[0])

        return sorted(agent_ids)
