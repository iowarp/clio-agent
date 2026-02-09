"""Agent Registry - Capability-based agent discovery and routing (v0.2.0)

The registry module provides:
- AgentRegistry: Thread-safe registry for managing agents
- AgentCapability: Metadata for agent capabilities
- RoutingDecision: Result of routing queries to agents
- A2A Protocol: Adapters for external agent integration (LangChain, CrewAI, AutoGen)

Example:
    >>> from clio_agent.registry import AgentRegistry, AgentCapability
    >>> registry = AgentRegistry()
    >>> caps = AgentCapability(
    ...     keywords=["hdf5", "data"],
    ...     description="Data expert",
    ...     tools=["hdf5_analyze"],
    ...     specialization="data_io"
    ... )
    >>> registry.register_agent("data_expert", expert, caps)
"""

from clio_agent.registry.registry import (
    AgentRegistry,
    AgentCapability,
    RoutingDecision,
)

from clio_agent.registry.a2a_adapter import (
    A2ARequest,
    A2AResponse,
    A2AAdapter,
    A2AProtocolHandler,
    LangChainAdapter,
    CrewAIAdapter,
    AutoGenAdapter,
)

__all__ = [
    "AgentRegistry",
    "AgentCapability",
    "RoutingDecision",
    "A2ARequest",
    "A2AResponse",
    "A2AAdapter",
    "A2AProtocolHandler",
    "LangChainAdapter",
    "CrewAIAdapter",
    "AutoGenAdapter",
]
