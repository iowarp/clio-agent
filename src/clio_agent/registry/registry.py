"""Core Agent Registry.

The AgentRegistry provides thread-safe agent registration and discovery
for ClioAgent's 3-tier agent hierarchy.

Example:
    >>> from clio_agent.registry import AgentRegistry, AgentCapability
    >>>
    >>> registry = AgentRegistry()
    >>> blueprint_agent = object()
    >>>
    >>> capabilities = AgentCapability(
    ...     keywords=["hdf5", "parquet", "compression"],
    ...     description="Scientific data optimization expert",
    ...     tools=["hdf5_analyze", "hdf5_optimize"],
    ...     specialization="data_io"
    ... )
    >>>
    >>> registry.register_agent("data_blueprint", blueprint_agent, capabilities)
    >>> agent = registry.get_agent("data_blueprint")
"""

import copy
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AgentCapability:
    """Agent capability metadata for routing.

    Attributes:
        keywords: Keywords that trigger this agent (e.g., ["hdf5", "compression"])
        description: Human-readable description of agent's role
        tools: List of tool names this agent can use
        specialization: Domain specialization (e.g., "data_io", "scheduling")
        priority: Routing priority (1=highest, 10=lowest). Default: 5
        parent_id: Optional parent agent ID when this is a nested expert
        source: Capability source such as builtin, user, skill, or builtin_nested
        planner_visible: Whether planner-facing catalogs should expose this agent
        metadata: Additional agent-specific metadata
    """

    keywords: List[str]
    description: str
    tools: List[str]
    specialization: str
    priority: int = 5
    parent_id: Optional[str] = None
    source: str = "builtin"
    planner_visible: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)


class AgentRegistry:
    """Thread-safe registry for managing agents and their capabilities.

    The registry supports:
    - Registration of DSPy agents (Tier 2 experts, Tier 3 nanoagents)
    - Registration of external A2A agents (LangChain, CrewAI, AutoGen)
    - Thread-safe operations for multi-agent coordination

    Example:
        >>> registry = AgentRegistry()
        >>> blueprint_agent = object()
        >>> caps = AgentCapability(
        ...     keywords=["hdf5", "data"],
        ...     description="Data I/O expert",
        ...     tools=["hdf5_analyze"],
        ...     specialization="data_io"
        ... )
        >>> registry.register_agent("data_blueprint", blueprint_agent, caps)
        >>> agents = registry.list_agents()
        >>> ['data_blueprint']
    """

    def __init__(self):
        """Initialize empty registry with thread lock."""
        self._agents: Dict[str, Any] = {}
        self._capabilities: Dict[str, AgentCapability] = {}
        self._lock = threading.Lock()

    def register_agent(self, agent_id: str, agent: Any, capabilities: AgentCapability) -> None:
        """Register an agent with its capabilities.

        Thread-safe registration of agents. Supports both DSPy modules
        and external A2A agents.

        Args:
            agent_id: Unique identifier for this agent
            agent: Agent instance (DSPy module or A2A adapter)
            capabilities: Agent capability metadata

        Raises:
            ValueError: If agent_id already exists or is invalid

        Example:
            >>> blueprint_agent = object()
            >>> caps = AgentCapability(
            ...     keywords=["hdf5"],
            ...     description="HDF5 expert",
            ...     tools=["hdf5_analyze"],
            ...     specialization="data_io"
            ... )
            >>> registry.register_agent("data_blueprint", blueprint_agent, caps)
        """
        if not agent_id or not isinstance(agent_id, str):
            raise ValueError(f"Invalid agent_id: {agent_id}")

        with self._lock:
            if agent_id in self._agents:
                raise ValueError(f"Agent '{agent_id}' already registered")

            self._agents[agent_id] = agent
            self._capabilities[agent_id] = capabilities

    def unregister_agent(self, agent_id: str) -> bool:
        """Remove agent from registry.

        Thread-safe unregistration. Used for ephemeral nanoagents
        or when agents are disabled.

        Args:
            agent_id: ID of agent to remove

        Returns:
            True if agent was removed, False if not found

        Example:
            >>> registry.unregister_agent("data_expert")
            True
            >>> registry.unregister_agent("nonexistent")
            False
        """
        with self._lock:
            if agent_id not in self._agents:
                return False

            del self._agents[agent_id]
            del self._capabilities[agent_id]
            return True

    def get_agent(self, agent_id: str) -> Optional[Any]:
        """Retrieve agent by ID.

        Thread-safe agent retrieval. Returns None if not found.

        Args:
            agent_id: ID of agent to retrieve

        Returns:
            Agent instance or None if not found

        Example:
            >>> expert = registry.get_agent("data_expert")
            >>> if expert:
            ...     result = expert.forward(query)
        """
        with self._lock:
            return self._agents.get(agent_id)

    def list_agents(self) -> List[str]:
        """List all registered agent IDs.

        Thread-safe retrieval of all agent IDs.

        Returns:
            List of agent IDs sorted alphabetically

        Example:
            >>> registry.list_agents()
            ['data_expert', 'slurm_expert', 'storage_expert']
        """
        with self._lock:
            return sorted(self._agents.keys())

    def list_child_agents(self, parent_id: str) -> List[str]:
        """List registered child agent IDs for a parent agent."""

        with self._lock:
            return sorted(
                agent_id
                for agent_id, caps in self._capabilities.items()
                if caps.parent_id == parent_id
            )

    def list_root_agents(self, *, planner_visible_only: bool = False) -> List[str]:
        """List registered agents without a parent."""

        with self._lock:
            return sorted(
                agent_id
                for agent_id, caps in self._capabilities.items()
                if caps.parent_id is None and (caps.planner_visible or not planner_visible_only)
            )

    def get_capabilities(self, agent_id: str) -> Optional[AgentCapability]:
        """Get agent capabilities.

        Thread-safe capability retrieval.

        Args:
            agent_id: ID of agent

        Returns:
            AgentCapability or None if agent not found

        Example:
            >>> caps = registry.get_capabilities("data_expert")
            >>> if caps:
            ...     print(caps.description)
            ...     print(caps.keywords)
        """
        with self._lock:
            return self._capabilities.get(agent_id)

    def get_all_capabilities(self) -> Dict[str, AgentCapability]:
        """Get capabilities for all registered agents.

        Thread-safe retrieval of complete capability mapping.

        Returns:
            Dictionary mapping agent_id -> AgentCapability

        Example:
            >>> all_caps = registry.get_all_capabilities()
            >>> for agent_id, caps in all_caps.items():
            ...     print(f"{agent_id}: {caps.description}")
        """
        with self._lock:
            return copy.deepcopy(self._capabilities)

    def get_agent_count(self) -> int:
        """Get total number of registered agents.

        Thread-safe count of registered agents.

        Returns:
            Number of registered agents

        Example:
            >>> registry.get_agent_count()
            3
        """
        with self._lock:
            return len(self._agents)

    def clear(self) -> None:
        """Clear all registered agents.

        Thread-safe removal of all agents. Use with caution.
        Primarily for testing or system reset.

        Example:
            >>> registry.clear()
            >>> registry.get_agent_count()
            0
        """
        with self._lock:
            self._agents.clear()
            self._capabilities.clear()
