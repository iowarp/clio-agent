"""A2A Protocol adapters for external agent communication.

A2A Protocol: https://a2a-protocol.org/latest/specification/

This module provides adapters for integrating external agent frameworks
(LangChain, CrewAI, AutoGen) into ClaudIO's agent ecosystem via the
Agent-to-Agent (A2A) protocol.

Example:
    >>> from claudio.registry.a2a_adapter import A2AProtocolHandler, A2ARequest
    >>>
    >>> handler = A2AProtocolHandler()
    >>> request = A2ARequest(
    ...     agent_id="external_langchain_agent",
    ...     query="Analyze this dataset",
    ...     context={"dataset_path": "/data/file.csv"},
    ...     session_id="session_123"
    ... )
    >>>
    >>> # Convert to LangChain format
    >>> response = handler.send_request("langchain", request)
    >>> print(response.answer)
"""

from typing import Dict, Any, Optional, Protocol
from dataclasses import dataclass, asdict
import json


@dataclass
class A2ARequest:
    """Standard A2A request format.

    Attributes:
        agent_id: Unique identifier for target external agent
        query: User query text to send to agent
        context: Additional context data (history, metadata, etc.)
        session_id: Session identifier for conversation tracking
    """
    agent_id: str
    query: str
    context: Dict[str, Any]
    session_id: str

    def to_dict(self) -> Dict[str, Any]:
        """Convert request to dictionary.

        Returns:
            Dictionary representation of request
        """
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "A2ARequest":
        """Create request from dictionary.

        Args:
            data: Dictionary with request data

        Returns:
            A2ARequest instance
        """
        return cls(**data)


@dataclass
class A2AResponse:
    """Standard A2A response format.

    Attributes:
        agent_id: ID of agent that generated response
        answer: Agent's response text
        confidence: Confidence score (0.0 to 1.0)
        metadata: Additional response metadata (tokens, latency, etc.)
    """
    agent_id: str
    answer: str
    confidence: float
    metadata: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        """Convert response to dictionary.

        Returns:
            Dictionary representation of response
        """
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "A2AResponse":
        """Create response from dictionary.

        Args:
            data: Dictionary with response data

        Returns:
            A2AResponse instance
        """
        return cls(**data)


class A2AAdapter(Protocol):
    """Protocol for A2A adapters.

    All external framework adapters must implement this protocol
    for message format conversion.
    """

    def convert_request(self, request: A2ARequest) -> Any:
        """Convert A2A request to external framework format.

        Args:
            request: Standard A2A request

        Returns:
            Framework-specific request format
        """
        ...

    def convert_response(self, response: Any) -> A2AResponse:
        """Convert external framework response to A2A format.

        Args:
            response: Framework-specific response

        Returns:
            Standard A2A response
        """
        ...


class LangChainAdapter:
    """Adapter for LangChain agents.

    LangChain uses the format:
    - Request: {"input": str, "chat_history": List[Tuple[str, str]]}
    - Response: {"output": str, "intermediate_steps": List}

    Example:
        >>> adapter = LangChainAdapter()
        >>> request = A2ARequest(
        ...     agent_id="lc_agent",
        ...     query="What is HDF5?",
        ...     context={"history": []},
        ...     session_id="s1"
        ... )
        >>> lc_format = adapter.convert_request(request)
        >>> lc_format["input"]
        'What is HDF5?'
    """

    def convert_request(self, request: A2ARequest) -> Dict[str, Any]:
        """Convert A2A request to LangChain format.

        Args:
            request: A2A request to convert

        Returns:
            LangChain-compatible request dictionary
        """
        # Extract chat history from context if available
        chat_history = request.context.get("chat_history", [])

        # LangChain format: {"input": query, "chat_history": [(user, ai), ...]}
        lc_request = {
            "input": request.query,
            "chat_history": chat_history,
        }

        # Add any additional context fields
        for key, value in request.context.items():
            if key not in ["chat_history"]:
                lc_request[key] = value

        return lc_request

    def convert_response(self, response: Dict[str, Any]) -> A2AResponse:
        """Convert LangChain response to A2A format.

        Args:
            response: LangChain response dictionary

        Returns:
            A2A response
        """
        # Extract output (LangChain's answer field)
        answer = response.get("output", response.get("result", ""))

        # Extract intermediate steps for metadata
        metadata = {
            "intermediate_steps": response.get("intermediate_steps", []),
            "source_framework": "langchain"
        }

        # Add any additional response fields to metadata
        for key, value in response.items():
            if key not in ["output", "result", "intermediate_steps"]:
                metadata[key] = value

        # Confidence based on whether we got a valid answer
        confidence = 0.9 if answer else 0.1

        return A2AResponse(
            agent_id=response.get("agent_id", "langchain_agent"),
            answer=answer,
            confidence=confidence,
            metadata=metadata
        )


class CrewAIAdapter:
    """Adapter for CrewAI agents.

    CrewAI uses the format:
    - Request: {"task": str, "context": str, "agent_role": str}
    - Response: {"result": str, "task_output": str}

    Example:
        >>> adapter = CrewAIAdapter()
        >>> request = A2ARequest(
        ...     agent_id="crew_agent",
        ...     query="Optimize this file",
        ...     context={"file_path": "/data/file.h5"},
        ...     session_id="s1"
        ... )
        >>> crew_format = adapter.convert_request(request)
        >>> crew_format["task"]
        'Optimize this file'
    """

    def convert_request(self, request: A2ARequest) -> Dict[str, Any]:
        """Convert A2A request to CrewAI format.

        Args:
            request: A2A request to convert

        Returns:
            CrewAI-compatible request dictionary
        """
        # Build context string from context dict
        context_str = json.dumps(request.context) if request.context else ""

        # CrewAI format: {"task": query, "context": context_str, ...}
        crew_request = {
            "task": request.query,
            "context": context_str,
            "agent_role": request.context.get("agent_role", "assistant"),
        }

        # Add session tracking
        crew_request["session_id"] = request.session_id

        return crew_request

    def convert_response(self, response: Dict[str, Any]) -> A2AResponse:
        """Convert CrewAI response to A2A format.

        Args:
            response: CrewAI response dictionary

        Returns:
            A2A response
        """
        # Extract result (CrewAI's output field)
        answer = response.get("result", response.get("task_output", ""))

        # Build metadata
        metadata = {
            "source_framework": "crewai",
            "task_output": response.get("task_output", "")
        }

        # Add any additional response fields to metadata
        for key, value in response.items():
            if key not in ["result", "task_output"]:
                metadata[key] = value

        # Confidence based on whether we got a valid answer
        confidence = 0.9 if answer else 0.1

        return A2AResponse(
            agent_id=response.get("agent_id", "crewai_agent"),
            answer=answer,
            confidence=confidence,
            metadata=metadata
        )


class AutoGenAdapter:
    """Adapter for AutoGen agents.

    AutoGen uses the format:
    - Request: {"message": str, "sender": str, "recipient": str}
    - Response: {"message": str, "metadata": Dict}

    Example:
        >>> adapter = AutoGenAdapter()
        >>> request = A2ARequest(
        ...     agent_id="autogen_agent",
        ...     query="Run this analysis",
        ...     context={"sender": "user"},
        ...     session_id="s1"
        ... )
        >>> autogen_format = adapter.convert_request(request)
        >>> autogen_format["message"]
        'Run this analysis'
    """

    def convert_request(self, request: A2ARequest) -> Dict[str, Any]:
        """Convert A2A request to AutoGen format.

        Args:
            request: A2A request to convert

        Returns:
            AutoGen-compatible request dictionary
        """
        # AutoGen format: {"message": query, "sender": ..., "recipient": ...}
        autogen_request = {
            "message": request.query,
            "sender": request.context.get("sender", "user"),
            "recipient": request.agent_id,
        }

        # Add session context
        autogen_request["session_id"] = request.session_id

        # Add any additional context
        if request.context:
            autogen_request["context"] = request.context

        return autogen_request

    def convert_response(self, response: Dict[str, Any]) -> A2AResponse:
        """Convert AutoGen response to A2A format.

        Args:
            response: AutoGen response dictionary

        Returns:
            A2A response
        """
        # Extract message (AutoGen's response field)
        answer = response.get("message", "")

        # Build metadata
        metadata = response.get("metadata", {})
        metadata["source_framework"] = "autogen"

        # Add sender info if available
        if "sender" in response:
            metadata["sender"] = response["sender"]

        # Confidence based on whether we got a valid message
        confidence = 0.9 if answer else 0.1

        return A2AResponse(
            agent_id=response.get("sender", "autogen_agent"),
            answer=answer,
            confidence=confidence,
            metadata=metadata
        )


class A2AProtocolHandler:
    """Handles A2A protocol communication with external agents.

    The handler manages adapters for different external frameworks
    and provides a unified interface for sending requests and
    receiving responses.

    Note:
        This implementation focuses on message format conversion.
        Actual HTTP/network communication will be added in v0.5.0
        when the REST API is implemented.

    Example:
        >>> handler = A2AProtocolHandler()
        >>> request = A2ARequest(
        ...     agent_id="ext_agent",
        ...     query="Analyze data",
        ...     context={},
        ...     session_id="s1"
        ... )
        >>>
        >>> # Convert to LangChain format
        >>> lc_request = handler._adapters["langchain"].convert_request(request)
        >>>
        >>> # Register custom adapter
        >>> from claudio.registry.a2a_adapter import A2AAdapter
        >>> handler.register_adapter("custom", CustomAdapter())
    """

    def __init__(self):
        """Initialize protocol handler with default adapters."""
        self._adapters: Dict[str, A2AAdapter] = {
            "langchain": LangChainAdapter(),
            "crewai": CrewAIAdapter(),
            "autogen": AutoGenAdapter()
        }

    def send_request(
        self,
        framework: str,
        request: A2ARequest,
        external_response: Optional[Dict[str, Any]] = None
    ) -> A2AResponse:
        """Send request to external agent and convert response.

        Note:
            In this version, you must provide the external_response parameter
            since actual network communication is not yet implemented.
            This will be added in v0.5.0 with the REST API.

        Args:
            framework: External framework name ("langchain", "crewai", "autogen")
            request: A2A request to send
            external_response: Mock external response (required for now)

        Returns:
            A2A response from external agent

        Raises:
            ValueError: If framework not supported or external_response not provided

        Example:
            >>> handler = A2AProtocolHandler()
            >>> request = A2ARequest(
            ...     agent_id="ext_agent",
            ...     query="Test query",
            ...     context={},
            ...     session_id="s1"
            ... )
            >>> # Mock LangChain response
            >>> lc_response = {"output": "Test answer"}
            >>> response = handler.send_request("langchain", request, lc_response)
            >>> print(response.answer)
            'Test answer'
        """
        if framework not in self._adapters:
            raise ValueError(
                f"Unsupported framework: {framework}. "
                f"Supported: {list(self._adapters.keys())}"
            )

        if external_response is None:
            raise ValueError(
                "external_response parameter required (network communication "
                "not yet implemented - coming in v0.5.0)"
            )

        adapter = self._adapters[framework]

        # Convert A2A request to external format
        # (Not used in this version since we're mocking, but validates conversion)
        _ = adapter.convert_request(request)

        # Convert external response to A2A format
        a2a_response = adapter.convert_response(external_response)

        return a2a_response

    def register_adapter(self, framework: str, adapter: A2AAdapter) -> None:
        """Register custom adapter for external framework.

        Args:
            framework: Framework name identifier
            adapter: Adapter instance implementing A2AAdapter protocol

        Example:
            >>> class CustomAdapter:
            ...     def convert_request(self, request):
            ...         return {"custom": request.query}
            ...     def convert_response(self, response):
            ...         return A2AResponse(
            ...             agent_id="custom",
            ...             answer=response["result"],
            ...             confidence=0.9,
            ...             metadata={}
            ...         )
            >>>
            >>> handler = A2AProtocolHandler()
            >>> handler.register_adapter("custom", CustomAdapter())
        """
        self._adapters[framework] = adapter

    def get_supported_frameworks(self) -> list[str]:
        """Get list of supported external frameworks.

        Returns:
            List of framework names

        Example:
            >>> handler = A2AProtocolHandler()
            >>> handler.get_supported_frameworks()
            ['langchain', 'crewai', 'autogen']
        """
        return list(self._adapters.keys())

    def get_adapter(self, framework: str) -> Optional[A2AAdapter]:
        """Get adapter for specific framework.

        Args:
            framework: Framework name

        Returns:
            Adapter instance or None if not found

        Example:
            >>> handler = A2AProtocolHandler()
            >>> adapter = handler.get_adapter("langchain")
            >>> isinstance(adapter, LangChainAdapter)
            True
        """
        return self._adapters.get(framework)
