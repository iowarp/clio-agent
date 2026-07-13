"""
Tests for clio_agent.registry.registry module.

Tests AgentRegistry registration, discovery, and thread safety.
"""

import pytest

from clio_agent.registry.registry import AgentCapability, AgentRegistry


class TestAgentCapability:
    """Test AgentCapability dataclass."""

    def test_create_capability(self):
        """Test creating a capability with required fields."""
        cap = AgentCapability(
            keywords=["hdf5", "data"],
            description="Data expert",
            tools=["analyze"],
            specialization="data_io",
        )
        assert cap.keywords == ["hdf5", "data"]
        assert cap.priority == 5  # default

    def test_custom_priority(self):
        """Test custom priority."""
        cap = AgentCapability(
            keywords=["hdf5"],
            description="Data expert",
            tools=["analyze"],
            specialization="data_io",
            priority=1,
        )
        assert cap.priority == 1

    def test_metadata_default(self):
        """Test metadata defaults to empty dict."""
        cap = AgentCapability(
            keywords=["hdf5"],
            description="Data expert",
            tools=[],
            specialization="data_io",
        )
        assert cap.metadata == {}


class TestAgentRegistry:
    """Test AgentRegistry core operations."""

    def test_empty_registry(self):
        """New registry should be empty."""
        reg = AgentRegistry()
        assert reg.get_agent_count() == 0
        assert reg.list_agents() == []

    def test_register_agent(self):
        """Test registering an agent."""
        reg = AgentRegistry()
        cap = AgentCapability(
            keywords=["hdf5"],
            description="Data expert",
            tools=["analyze"],
            specialization="data_io",
        )
        reg.register_agent("data", object(), cap)
        assert reg.get_agent_count() == 1
        assert "data" in reg.list_agents()

    def test_register_duplicate_raises(self):
        """Registering same agent_id twice should raise ValueError."""
        reg = AgentRegistry()
        cap = AgentCapability(
            keywords=["hdf5"],
            description="Data expert",
            tools=[],
            specialization="data_io",
        )
        reg.register_agent("data", object(), cap)
        with pytest.raises(ValueError, match="already registered"):
            reg.register_agent("data", object(), cap)

    def test_register_invalid_id(self):
        """Registering with empty/None id should raise ValueError."""
        reg = AgentRegistry()
        cap = AgentCapability(
            keywords=["hdf5"],
            description="Data expert",
            tools=[],
            specialization="data_io",
        )
        with pytest.raises(ValueError, match="Invalid agent_id"):
            reg.register_agent("", object(), cap)

    def test_get_agent(self):
        """Test retrieving agent by ID."""
        reg = AgentRegistry()
        sentinel = object()
        cap = AgentCapability(
            keywords=["hdf5"],
            description="Data expert",
            tools=[],
            specialization="data_io",
        )
        reg.register_agent("data", sentinel, cap)
        assert reg.get_agent("data") is sentinel

    def test_get_agent_not_found(self):
        """Getting non-existent agent returns None."""
        reg = AgentRegistry()
        assert reg.get_agent("nonexistent") is None

    def test_unregister_agent(self):
        """Test unregistering an agent."""
        reg = AgentRegistry()
        cap = AgentCapability(
            keywords=["hdf5"],
            description="Data expert",
            tools=[],
            specialization="data_io",
        )
        reg.register_agent("data", object(), cap)
        assert reg.unregister_agent("data") is True
        assert reg.get_agent_count() == 0

    def test_unregister_nonexistent(self):
        """Unregistering non-existent agent returns False."""
        reg = AgentRegistry()
        assert reg.unregister_agent("nonexistent") is False

    def test_get_capabilities(self):
        """Test retrieving agent capabilities."""
        reg = AgentRegistry()
        cap = AgentCapability(
            keywords=["hdf5", "data"],
            description="Data expert",
            tools=["analyze"],
            specialization="data_io",
        )
        reg.register_agent("data", object(), cap)
        result = reg.get_capabilities("data")
        assert result is not None
        assert result.keywords == ["hdf5", "data"]

    def test_get_capabilities_not_found(self):
        """Getting capabilities for non-existent agent returns None."""
        reg = AgentRegistry()
        assert reg.get_capabilities("nonexistent") is None

    def test_get_all_capabilities(self):
        """Test getting all capabilities returns deep copy."""
        reg = AgentRegistry()
        cap = AgentCapability(
            keywords=["hdf5"],
            description="Data expert",
            tools=[],
            specialization="data_io",
        )
        reg.register_agent("data", object(), cap)
        all_caps = reg.get_all_capabilities()
        assert "data" in all_caps
        # Verify it's a copy
        all_caps["data"].keywords.append("modified")
        original = reg.get_capabilities("data")
        assert "modified" not in original.keywords

    def test_clear(self):
        """Test clearing all agents."""
        reg = AgentRegistry()
        cap = AgentCapability(
            keywords=["hdf5"],
            description="Data expert",
            tools=[],
            specialization="data_io",
        )
        reg.register_agent("data", object(), cap)
        reg.clear()
        assert reg.get_agent_count() == 0

    def test_list_agents_sorted(self):
        """list_agents should return sorted IDs."""
        reg = AgentRegistry()
        for name in ["charlie", "alice", "bob"]:
            cap = AgentCapability(
                keywords=[name],
                description=name,
                tools=[],
                specialization=name,
            )
            reg.register_agent(name, object(), cap)
        assert reg.list_agents() == ["alice", "bob", "charlie"]

    def test_lists_root_and_child_agents(self):
        """Registry should expose hierarchy without hardcoded agent IDs."""
        reg = AgentRegistry()
        reg.register_agent(
            "data",
            object(),
            AgentCapability(
                keywords=["data"],
                description="Root data manager",
                tools=[],
                specialization="data_io",
            ),
        )
        reg.register_agent(
            "ndp_catalog",
            object(),
            AgentCapability(
                keywords=["ndp"],
                description="Nested NDP catalog expert",
                tools=["ndp_search_datasets"],
                specialization="data_catalog",
                parent_id="data",
                source="builtin_nested",
            ),
        )

        assert reg.list_root_agents() == ["data"]
        assert reg.list_child_agents("data") == ["ndp_catalog"]
