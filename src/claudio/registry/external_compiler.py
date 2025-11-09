"""External Agent Compiler for ClaudIO

Compiles external agents (LangChain, CrewAI, AutoGen) into ClaudIO-compatible format.
External agents are wrapped with A2A protocol and exposed as DSPy-like modules
for transparent integration into the AgentRegistry.

Example:
    >>> from claudio.registry.external_compiler import ExternalAgentCompiler, ExternalAgentDefinition
    >>>
    >>> compiler = ExternalAgentCompiler()
    >>> definition = ExternalAgentDefinition(
    ...     agent_id="langchain_sql",
    ...     framework="langchain",
    ...     description="SQL query optimizer using LangChain",
    ...     keywords=["sql", "database", "query"],
    ...     tools=["sql_analyzer", "query_optimizer"],
    ...     config={"model": "gpt-4", "temperature": 0.0}
    ... )
    >>>
    >>> wrapper = compiler.compile_agent(definition)
    >>> response = wrapper.forward("Optimize this SQL query: SELECT * FROM users")
"""

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from claudio.registry.registry import AgentCapability


@dataclass
class A2ARequest:
    """A2A Protocol request message.

    A2A Protocol: https://a2a-protocol.org/latest/specification/

    Attributes:
        agent_id: Target agent identifier
        task: Task description/query
        context: Optional context from previous interactions
        session_id: Session identifier for conversation tracking
        metadata: Additional request metadata
    """
    agent_id: str
    task: str
    context: Optional[Dict[str, Any]] = None
    session_id: str = "default"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class A2AResponse:
    """A2A Protocol response message.

    Attributes:
        agent_id: Responding agent identifier
        result: Agent's response/result
        success: Whether task completed successfully
        error: Error message if success=False
        metadata: Response metadata (timing, tokens, etc.)
    """
    agent_id: str
    result: str
    success: bool = True
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class A2AProtocolHandler:
    """Handler for A2A protocol communication.

    Manages request/response serialization and communication with external agents.
    Currently implements local simulation for external agents.
    Full HTTP/WebSocket implementation will be in a2a_adapter.py (Task 1.3).
    """

    def __init__(self):
        """Initialize A2A protocol handler."""
        self._external_agents: Dict[str, Any] = {}

    def send_request(self, request: A2ARequest) -> A2AResponse:
        """Send A2A request to external agent.

        Args:
            request: A2A request message

        Returns:
            A2A response from external agent

        Raises:
            ValueError: If agent not found or request fails
        """
        # TODO: Full implementation in a2a_adapter.py (Task 1.3)
        # For now, simulate external agent response
        if request.agent_id not in self._external_agents:
            return A2AResponse(
                agent_id=request.agent_id,
                result="",
                success=False,
                error=f"External agent '{request.agent_id}' not configured",
                metadata={"timestamp": time.time()}
            )

        # Simulate external agent execution
        # In production, this would make HTTP/WebSocket call to external agent
        return A2AResponse(
            agent_id=request.agent_id,
            result=f"[Simulated response from {request.agent_id}] Processed: {request.task}",
            success=True,
            metadata={
                "timestamp": time.time(),
                "framework": self._external_agents[request.agent_id].get("framework", "unknown")
            }
        )

    def register_external_agent(self, agent_id: str, config: Dict[str, Any]) -> None:
        """Register external agent configuration.

        Args:
            agent_id: External agent identifier
            config: Agent configuration (endpoint, auth, etc.)
        """
        self._external_agents[agent_id] = config


@dataclass
class ExternalAgentDefinition:
    """Definition of an external agent.

    Attributes:
        agent_id: Unique identifier for this agent
        framework: Source framework ("langchain", "crewai", "autogen")
        description: Human-readable description of agent's capabilities
        keywords: Keywords for capability-based routing
        tools: List of tool names this agent can use
        config: Framework-specific configuration

    Example:
        >>> definition = ExternalAgentDefinition(
        ...     agent_id="langchain_summarizer",
        ...     framework="langchain",
        ...     description="Document summarization agent",
        ...     keywords=["summarize", "text", "document"],
        ...     tools=["pdf_reader", "text_extractor"],
        ...     config={"model": "gpt-4", "max_tokens": 1000}
        ... )
    """
    agent_id: str
    framework: str  # "langchain", "crewai", "autogen"
    description: str
    keywords: List[str]
    tools: List[str]
    config: Dict[str, Any] = field(default_factory=dict)


class ExternalAgentWrapper:
    """Wraps external agent for ClaudIO compatibility.

    Provides DSPy-compatible forward() interface for external agents.
    Transparently integrates with AgentRegistry - Main Agent doesn't need
    to know whether agent is internal (DSPy) or external (A2A).

    Example:
        >>> wrapper = ExternalAgentWrapper(definition, protocol_handler)
        >>> response = wrapper.forward("Analyze this data")
        >>> print(response.result)
    """

    def __init__(
        self,
        definition: ExternalAgentDefinition,
        protocol_handler: A2AProtocolHandler
    ):
        """Initialize external agent wrapper.

        Args:
            definition: External agent definition
            protocol_handler: A2A protocol handler for communication
        """
        self.definition = definition
        self.protocol_handler = protocol_handler
        self.agent_id = definition.agent_id
        self.framework = definition.framework

        # Register with protocol handler
        self.protocol_handler.register_external_agent(
            self.agent_id,
            {
                "framework": self.framework,
                "config": definition.config
            }
        )

    def forward(
        self,
        question: str,
        context: Optional[Dict[str, Any]] = None,
        session_id: str = "default"
    ) -> A2AResponse:
        """DSPy-compatible forward method.

        Args:
            question: Query/task for the agent
            context: Optional context from previous interactions
            session_id: Session identifier for conversation tracking

        Returns:
            A2AResponse with agent's result

        Example:
            >>> response = wrapper.forward("Summarize this document", session_id="user123")
            >>> if response.success:
            ...     print(response.result)
        """
        # Create A2A request
        request = A2ARequest(
            agent_id=self.agent_id,
            task=question,
            context=context,
            session_id=session_id,
            metadata={
                "framework": self.framework,
                "tools": self.definition.tools
            }
        )

        # Send via protocol handler
        start_time = time.time()
        response = self.protocol_handler.send_request(request)

        # Add timing metadata
        response.metadata["duration_ms"] = (time.time() - start_time) * 1000

        return response

    def __call__(
        self,
        question: str,
        context: Optional[Dict[str, Any]] = None,
        session_id: str = "default"
    ) -> A2AResponse:
        """Callable interface (same as forward).

        Args:
            question: Query/task for the agent
            context: Optional context
            session_id: Session identifier

        Returns:
            A2AResponse with agent's result
        """
        return self.forward(question, context, session_id)


class ExternalAgentCompiler:
    """Compiles external agents into ClaudIO format.

    Converts external agent definitions (LangChain, CrewAI, AutoGen)
    into ClaudIO-compatible wrappers that can be registered in AgentRegistry.

    Example:
        >>> from claudio.registry import AgentRegistry, AgentCapability
        >>>
        >>> compiler = ExternalAgentCompiler()
        >>> registry = AgentRegistry()
        >>>
        >>> # Compile LangChain agent
        >>> lc_config = {
        ...     "agent_id": "langchain_sql",
        ...     "model": "gpt-4",
        ...     "tools": ["sql_analyzer"]
        ... }
        >>> definition = compiler.compile_langchain_agent(lc_config)
        >>> wrapper = compiler.compile_agent(definition)
        >>> capabilities = compiler.extract_capabilities(definition)
        >>>
        >>> # Register in ClaudIO registry
        >>> registry.register_agent("langchain_sql", wrapper, capabilities)
        >>>
        >>> # Use like any ClaudIO agent
        >>> agent = registry.get_agent("langchain_sql")
        >>> response = agent.forward("Optimize SELECT * FROM users")
    """

    def __init__(self):
        """Initialize external agent compiler."""
        self.protocol_handler = A2AProtocolHandler()

    def compile_agent(self, definition: ExternalAgentDefinition) -> ExternalAgentWrapper:
        """Compile external agent definition into ClaudIO-compatible wrapper.

        Args:
            definition: External agent definition

        Returns:
            Wrapped agent with DSPy-like interface

        Raises:
            ValueError: If framework is not supported

        Example:
            >>> definition = ExternalAgentDefinition(
            ...     agent_id="crewai_researcher",
            ...     framework="crewai",
            ...     description="Research agent",
            ...     keywords=["research", "web", "search"],
            ...     tools=["web_search", "scraper"],
            ...     config={"role": "researcher"}
            ... )
            >>> wrapper = compiler.compile_agent(definition)
        """
        supported_frameworks = ["langchain", "crewai", "autogen"]
        if definition.framework not in supported_frameworks:
            raise ValueError(
                f"Unsupported framework: {definition.framework}. "
                f"Supported: {supported_frameworks}"
            )

        return ExternalAgentWrapper(definition, self.protocol_handler)

    def extract_capabilities(
        self,
        definition: ExternalAgentDefinition
    ) -> "AgentCapability":
        """Extract AgentCapability from external agent definition.

        Args:
            definition: External agent definition

        Returns:
            AgentCapability for registry registration

        Example:
            >>> capabilities = compiler.extract_capabilities(definition)
            >>> registry.register_agent("external_agent", wrapper, capabilities)
        """
        # Import here to avoid circular dependency
        from claudio.registry.registry import AgentCapability

        return AgentCapability(
            keywords=definition.keywords,
            description=definition.description,
            tools=definition.tools,
            specialization=definition.framework,  # Use framework as specialization
            priority=7,  # External agents get lower priority than internal (default 5)
            metadata={
                "framework": definition.framework,
                "agent_id": definition.agent_id,
                "config": definition.config,
                "is_external": True
            }
        )

    def compile_langchain_agent(self, config: Dict[str, Any]) -> ExternalAgentDefinition:
        """Create definition from LangChain agent config.

        Args:
            config: LangChain agent configuration
                - agent_id: str (required)
                - description: str (optional)
                - keywords: List[str] (optional)
                - tools: List[str] (optional)
                - model: str (optional)
                - additional LangChain-specific config

        Returns:
            ExternalAgentDefinition for compilation

        Raises:
            ValueError: If required fields are missing

        Example:
            >>> lc_config = {
            ...     "agent_id": "langchain_sql_optimizer",
            ...     "description": "SQL query optimization using LangChain",
            ...     "keywords": ["sql", "database", "query", "optimize"],
            ...     "tools": ["sql_analyzer", "query_planner"],
            ...     "model": "gpt-4",
            ...     "temperature": 0.0,
            ...     "max_tokens": 2000
            ... }
            >>> definition = compiler.compile_langchain_agent(lc_config)
        """
        if "agent_id" not in config:
            raise ValueError("LangChain config must include 'agent_id'")

        return ExternalAgentDefinition(
            agent_id=config["agent_id"],
            framework="langchain",
            description=config.get("description", f"LangChain agent: {config['agent_id']}"),
            keywords=config.get("keywords", ["langchain"]),
            tools=config.get("tools", []),
            config={
                "model": config.get("model", "gpt-3.5-turbo"),
                "temperature": config.get("temperature", 0.7),
                "max_tokens": config.get("max_tokens", 1000),
                **{k: v for k, v in config.items() if k not in [
                    "agent_id", "description", "keywords", "tools", "framework"
                ]}
            }
        )

    def compile_crewai_agent(self, config: Dict[str, Any]) -> ExternalAgentDefinition:
        """Create definition from CrewAI agent config.

        Args:
            config: CrewAI agent configuration
                - agent_id: str (required)
                - role: str (optional)
                - goal: str (optional)
                - backstory: str (optional)
                - description: str (optional)
                - keywords: List[str] (optional)
                - tools: List[str] (optional)
                - additional CrewAI-specific config

        Returns:
            ExternalAgentDefinition for compilation

        Raises:
            ValueError: If required fields are missing

        Example:
            >>> crew_config = {
            ...     "agent_id": "crewai_data_analyst",
            ...     "role": "Data Analyst",
            ...     "goal": "Analyze scientific datasets for insights",
            ...     "backstory": "Expert in HPC data analysis",
            ...     "keywords": ["analysis", "statistics", "data"],
            ...     "tools": ["pandas_analyzer", "numpy_stats"],
            ...     "verbose": True
            ... }
            >>> definition = compiler.compile_crewai_agent(crew_config)
        """
        if "agent_id" not in config:
            raise ValueError("CrewAI config must include 'agent_id'")

        # CrewAI uses role as primary description
        role = config.get("role", "CrewAI Agent")
        goal = config.get("goal", "")
        description = config.get("description", f"{role}: {goal}" if goal else role)

        return ExternalAgentDefinition(
            agent_id=config["agent_id"],
            framework="crewai",
            description=description,
            keywords=config.get("keywords", ["crewai", role.lower()]),
            tools=config.get("tools", []),
            config={
                "role": role,
                "goal": goal,
                "backstory": config.get("backstory", ""),
                "verbose": config.get("verbose", False),
                "allow_delegation": config.get("allow_delegation", True),
                **{k: v for k, v in config.items() if k not in [
                    "agent_id", "description", "keywords", "tools", "framework"
                ]}
            }
        )

    def compile_autogen_agent(self, config: Dict[str, Any]) -> ExternalAgentDefinition:
        """Create definition from AutoGen agent config.

        Args:
            config: AutoGen agent configuration
                - agent_id: str (required)
                - name: str (optional)
                - system_message: str (optional)
                - description: str (optional)
                - keywords: List[str] (optional)
                - tools: List[str] (optional)
                - additional AutoGen-specific config

        Returns:
            ExternalAgentDefinition for compilation

        Raises:
            ValueError: If required fields are missing

        Example:
            >>> autogen_config = {
            ...     "agent_id": "autogen_code_reviewer",
            ...     "name": "CodeReviewer",
            ...     "system_message": "You are a code review expert",
            ...     "keywords": ["code", "review", "quality"],
            ...     "tools": ["linter", "static_analyzer"],
            ...     "llm_config": {"model": "gpt-4", "temperature": 0.0}
            ... }
            >>> definition = compiler.compile_autogen_agent(autogen_config)
        """
        if "agent_id" not in config:
            raise ValueError("AutoGen config must include 'agent_id'")

        name = config.get("name", config["agent_id"])
        system_message = config.get("system_message", "")

        return ExternalAgentDefinition(
            agent_id=config["agent_id"],
            framework="autogen",
            description=config.get("description", f"AutoGen agent: {name}"),
            keywords=config.get("keywords", ["autogen", name.lower()]),
            tools=config.get("tools", []),
            config={
                "name": name,
                "system_message": system_message,
                "llm_config": config.get("llm_config", {}),
                "human_input_mode": config.get("human_input_mode", "NEVER"),
                "max_consecutive_auto_reply": config.get("max_consecutive_auto_reply", 10),
                **{k: v for k, v in config.items() if k not in [
                    "agent_id", "description", "keywords", "tools", "framework"
                ]}
            }
        )
