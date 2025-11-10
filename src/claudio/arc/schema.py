"""Data schemas for ARC (Conversation, Invocation, Metrics, Context)

This module defines msgspec.Struct-based schemas for efficient serialization
in the ARC Memory Layer. All schemas support msgpack encoding/decoding.

Architecture:
    - Conversation: Session history and routing decisions
    - Invocation: Individual agent execution traces
    - Metrics: Aggregated performance metrics per agent
    - Context: Cached tools, learned patterns, domain knowledge

See docs/ARC_MEMORY_LAYER.md for detailed schema specifications.
"""

import time
import uuid
from typing import Any, Dict, List, Optional

import msgspec


class Message(msgspec.Struct):
    """Individual message within a conversation.

    Attributes:
        role: Message role (user or assistant)
        content: Message text content
        timestamp: Unix timestamp (float from time.time())
        message_id: Unique identifier for the message (auto-generated if not provided)
        metadata: Optional metadata (source, model_used, etc.)

    Example:
        >>> import time
        >>> msg = Message(
        ...     role="user",
        ...     content="How do I optimize HDF5?",
        ...     timestamp=time.time(),
        ...     metadata={"source": "cli"}
        ... )
        >>> encoded = msgspec.msgpack.encode(msg)
        >>> decoded = msgspec.msgpack.decode(encoded, type=Message)
    """

    role: str  # "user" or "assistant"
    content: str
    timestamp: float  # Unix timestamp from time.time()
    message_id: str = msgspec.field(default_factory=lambda: str(uuid.uuid4()))
    metadata: Dict[str, Any] = msgspec.field(default_factory=dict)


class RoutingDecision(msgspec.Struct):
    """Agent routing decision metadata.

    Attributes:
        timestamp: When routing decision was made (Unix timestamp)
        query: User query that triggered routing
        capabilities_needed: List of required capabilities
        selected_agent: Agent ID that was selected
        reasoning: Explanation for selection
        confidence: Confidence score (0.0-1.0)
        alternatives: Alternative agents considered

    Example:
        >>> import time
        >>> decision = RoutingDecision(
        ...     timestamp=time.time(),
        ...     query="Optimize 100GB HDF5 file",
        ...     capabilities_needed=["HDF5", "optimization"],
        ...     selected_agent="DataExpert",
        ...     reasoning="Query mentions HDF5",
        ...     confidence=0.95,
        ...     alternatives=[{"agent": "HPCExpert", "score": 0.12}]
        ... )
    """

    timestamp: float
    query: str
    capabilities_needed: List[str]
    selected_agent: str
    reasoning: str
    confidence: float
    alternatives: List[Dict[str, Any]] = msgspec.field(default_factory=list)


class Conversation(msgspec.Struct):
    """Complete conversation session with all messages and routing decisions.

    Attributes:
        session_id: Unique session identifier (UUID v4)
        user_id: User identifier
        created_at: Session creation timestamp (Unix timestamp)
        updated_at: Last update timestamp (Unix timestamp, defaults to current time)
        last_accessed: Last access timestamp for tier migration (Unix timestamp, defaults to current time)
        status: Session status (active, completed, abandoned, defaults to "active")
        messages: List of messages in conversation
        routing_decisions: List of routing decisions made
        metadata: Session metadata (preferences, domain, tokens, etc.)
        storage_tier: Current IOWarp CTE storage tier (defaults to "warm")

    Example:
        >>> import time
        >>> conv = Conversation(
        ...     session_id="550e8400-e29b-41d4-a716-446655440000",
        ...     user_id="user@example.com",
        ...     created_at=time.time()
        ... )
        >>> encoded = msgspec.msgpack.encode(conv)
        >>> decoded = msgspec.msgpack.decode(encoded, type=Conversation)
    """

    session_id: str
    user_id: str
    created_at: float
    updated_at: float = msgspec.field(default_factory=lambda: time.time())
    last_accessed: float = msgspec.field(default_factory=lambda: time.time())
    status: str = "active"  # "active", "completed", "abandoned"
    messages: List[Message] = msgspec.field(default_factory=list)
    routing_decisions: List[RoutingDecision] = msgspec.field(default_factory=list)
    metadata: Dict[str, Any] = msgspec.field(default_factory=dict)
    storage_tier: str = "warm"


class ToolCall(msgspec.Struct):
    """Record of a single tool invocation.

    Attributes:
        tool: Tool name
        params: Tool parameters
        result: Tool execution result
        duration_ms: Execution duration in milliseconds
        cached: Whether result was from cache

    Example:
        >>> tool_call = ToolCall(
        ...     tool="hdf5_analyze",
        ...     params={"filepath": "/data/file.h5"},
        ...     result={"compression": "none", "size": "100GB"},
        ...     duration_ms=342,
        ...     cached=False
        ... )
    """

    tool: str
    params: Dict[str, Any]
    result: Any
    duration_ms: float
    cached: bool = False


class NanoagentSpawn(msgspec.Struct):
    """Record of a nanoagent spawned during execution.

    Attributes:
        nanoagent_id: Nanoagent identifier
        trace_id: Linked invocation trace ID
        task: Task description
        duration_ms: Execution duration in milliseconds
        status: Execution status

    Example:
        >>> spawn = NanoagentSpawn(
        ...     nanoagent_id="nano-123",
        ...     trace_id="trace-456",
        ...     task="analyze_chunk",
        ...     duration_ms=123,
        ...     status="success"
        ... )
    """

    nanoagent_id: str
    trace_id: str
    task: str
    duration_ms: float
    status: str


class Invocation(msgspec.Struct):
    """Individual agent invocation trace with full execution details.

    Attributes:
        trace_id: Unique trace identifier (UUID v4)
        session_id: Parent conversation session ID
        parent_trace_id: Parent trace ID for nanoagent spawns
        agent_id: Agent identifier
        tier: Agent tier (1=Main, 2=Expert, 3=Nanoagent)
        source: Integration source (native, langchain, crewai, autogen)
        duration_ms: Total execution duration in milliseconds
        status: Execution status (success, failure, timeout)
        input: Input data (query, context, etc.)
        output: Output data (answer, reasoning_trace, etc.)
        started_at: Execution start timestamp (Unix timestamp, defaults to current time)
        completed_at: Execution completion timestamp (Unix timestamp, defaults to current time)
        tools_called: List of tool calls made
        nanoagents_spawned: List of nanoagents spawned
        performance: Performance metrics
        storage_tier: Current IOWarp CTE storage tier (defaults to "cold")

    Example:
        >>> import time
        >>> inv = Invocation(
        ...     trace_id="trace-789",
        ...     session_id="session-123",
        ...     parent_trace_id=None,
        ...     agent_id="DataExpert",
        ...     tier=2,
        ...     source="native",
        ...     duration_ms=1247,
        ...     status="success",
        ...     input={"query": "Optimize HDF5"},
        ...     output={"answer": "Apply gzip-6"}
        ... )
        >>> encoded = msgspec.msgpack.encode(inv)
        >>> decoded = msgspec.msgpack.decode(encoded, type=Invocation)
    """

    # Required fields first
    trace_id: str
    session_id: str
    parent_trace_id: Optional[str]
    agent_id: str
    tier: int  # 1=Main, 2=Expert, 3=Nanoagent
    source: str  # "native", "langchain", "crewai", "autogen"
    duration_ms: float
    status: str  # "success", "failure", "timeout"
    input: Dict[str, Any]
    output: Dict[str, Any]
    # Optional fields with defaults
    started_at: float = msgspec.field(default_factory=lambda: time.time())
    completed_at: float = msgspec.field(default_factory=lambda: time.time())
    tools_called: List[ToolCall] = msgspec.field(default_factory=list)
    nanoagents_spawned: List[NanoagentSpawn] = msgspec.field(default_factory=list)
    performance: Dict[str, Any] = msgspec.field(default_factory=dict)
    storage_tier: str = "cold"


class InvocationStats(msgspec.Struct):
    """Invocation statistics for metrics aggregation.

    Attributes:
        total: Total invocation count
        success: Successful invocation count
        failure: Failed invocation count
        timeout: Timed-out invocation count
        success_rate: Success rate (0.0-1.0)
    """

    total: int
    success: int
    failure: int
    timeout: int
    success_rate: float


class LatencyStats(msgspec.Struct):
    """Latency statistics for metrics aggregation.

    Attributes:
        avg_ms: Average latency in milliseconds
        median_ms: Median latency in milliseconds
        p95_ms: 95th percentile latency
        p99_ms: 99th percentile latency
        min_ms: Minimum latency
        max_ms: Maximum latency
    """

    avg_ms: float
    median_ms: float
    p95_ms: float
    p99_ms: float
    min_ms: float
    max_ms: float


class UserSatisfactionStats(msgspec.Struct):
    """User satisfaction statistics.

    Attributes:
        total_rated: Total number of rated interactions
        positive: Positive rating count
        negative: Negative rating count
        score: Overall satisfaction score (0.0-1.0)
    """

    total_rated: int
    positive: int
    negative: int
    score: float


class ToolMetrics(msgspec.Struct):
    """Metrics for a specific tool.

    Attributes:
        calls: Total call count
        avg_duration_ms: Average duration in milliseconds
        cache_hit_rate: Cache hit rate (0.0-1.0)
    """

    calls: int
    avg_duration_ms: float
    cache_hit_rate: float


class OptimizationRecord(msgspec.Struct):
    """Record of an optimization event.

    Attributes:
        timestamp: Optimization timestamp (Unix timestamp)
        optimizer: Optimizer name
        method: Optimization method (e.g., "MIPRO")
        variant_id: New variant identifier
        improvements: Improvement metrics
        training_examples: Number of training examples used
        optimization_duration: Duration of optimization process in seconds
    """

    timestamp: float
    optimizer: str
    method: str
    variant_id: str
    improvements: Dict[str, Dict[str, Any]]
    training_examples: int
    optimization_duration: float


class Metrics(msgspec.Struct):
    """Aggregated performance metrics for an agent over a time period.

    Attributes:
        agent_id: Agent identifier
        tier: Agent tier (1=Main, 2=Expert, 3=Nanoagent)
        period: Time period (e.g., "2025-01-01/2025-01-31")
        computed_at: Metrics computation timestamp (Unix timestamp)
        invocations: Invocation statistics
        latency: Latency statistics
        user_satisfaction: User satisfaction statistics
        tools: Tool-specific metrics
        optimization_history: Optimization event history
        storage_tier: Current IOWarp CTE storage tier

    Example:
        >>> import time
        >>> metrics = Metrics(
        ...     agent_id="DataExpert",
        ...     tier=2,
        ...     period="2025-01/2025-01",
        ...     computed_at=time.time(),
        ...     invocations=InvocationStats(1234, 1193, 31, 10, 0.967),
        ...     latency=LatencyStats(1523, 1200, 2500, 4200, 234, 8900),
        ...     user_satisfaction=UserSatisfactionStats(342, 305, 37, 0.89),
        ...     tools={},
        ...     optimization_history=[],
        ...     storage_tier="warm"
        ... )
        >>> encoded = msgspec.msgpack.encode(metrics)
        >>> decoded = msgspec.msgpack.decode(encoded, type=Metrics)
    """

    agent_id: str
    tier: int
    period: str
    computed_at: float
    invocations: InvocationStats
    latency: LatencyStats
    user_satisfaction: UserSatisfactionStats
    tools: Dict[str, ToolMetrics] = msgspec.field(default_factory=dict)
    optimization_history: List[OptimizationRecord] = msgspec.field(default_factory=list)
    storage_tier: str = "warm"


class RetrievedDoc(msgspec.Struct):
    """Retrieved document from RAG system.

    Attributes:
        doc_id: Document identifier
        source: Document source (e.g., "rag_system")
        title: Document title
        content: Document content
        relevance_score: Relevance score (0.0-1.0)
        accessed_count: Number of times accessed
    """

    doc_id: str
    source: str
    title: str
    content: str
    relevance_score: float
    accessed_count: int = 0


class CachedToolResult(msgspec.Struct):
    """Cached tool result entry.

    Attributes:
        params_hash: Hash of tool parameters
        result: Tool execution result
        cached_at: Cache timestamp (Unix timestamp)
        ttl: Time-to-live in seconds
        hit_count: Number of cache hits
    """

    params_hash: str
    result: Any
    cached_at: float
    ttl: int
    hit_count: int = 0


class LearnedPattern(msgspec.Struct):
    """Learned pattern from historical data.

    Attributes:
        pattern_type: Type of pattern (e.g., "frequent_topic", "tool_usage", "error_pattern")
        pattern_data: Pattern-specific data dictionary
        confidence: Confidence score (0.0-1.0)
        learned_at: Pattern learning timestamp (Unix timestamp, defaults to current time)
    """

    pattern_type: str
    pattern_data: Dict[str, Any]
    confidence: float
    learned_at: float = msgspec.field(default_factory=lambda: time.time())


class Context(msgspec.Struct):
    """Domain-specific context with cached tools and learned patterns.

    Attributes:
        domain: Domain identifier (e.g., "hdf5_optimization")
        created_at: Context creation timestamp (Unix timestamp, defaults to current time)
        updated_at: Last update timestamp (Unix timestamp, defaults to current time)
        retrieved_docs: Retrieved RAG documents
        cached_tool_results: Cached tool execution results
        learned_patterns: Learned patterns from data
        storage_tier: Current IOWarp CTE storage tier (defaults to "cold")

    Example:
        >>> import time
        >>> ctx = Context(
        ...     domain="hdf5_optimization"
        ... )
        >>> encoded = msgspec.msgpack.encode(ctx)
        >>> decoded = msgspec.msgpack.decode(encoded, type=Context)
    """

    domain: str
    created_at: float = msgspec.field(default_factory=lambda: time.time())
    updated_at: float = msgspec.field(default_factory=lambda: time.time())
    retrieved_docs: List[RetrievedDoc] = msgspec.field(default_factory=list)
    cached_tool_results: Dict[str, CachedToolResult] = msgspec.field(default_factory=dict)
    learned_patterns: List[LearnedPattern] = msgspec.field(default_factory=list)
    storage_tier: str = "cold"


# Type aliases for convenience
ConversationDict = Dict[str, Any]
InvocationDict = Dict[str, Any]
MetricsDict = Dict[str, Any]
ContextDict = Dict[str, Any]


def encode_conversation(conv: Conversation) -> bytes:
    """Encode Conversation to msgpack bytes.

    Args:
        conv: Conversation object

    Returns:
        Msgpack-encoded bytes

    Example:
        >>> conv = Conversation(session_id="123", ...)
        >>> encoded = encode_conversation(conv)
        >>> decoded = decode_conversation(encoded)
    """
    return msgspec.msgpack.encode(conv)


def decode_conversation(data: bytes) -> Conversation:
    """Decode msgpack bytes to Conversation.

    Args:
        data: Msgpack-encoded bytes

    Returns:
        Conversation object
    """
    return msgspec.msgpack.decode(data, type=Conversation)


def encode_invocation(inv: Invocation) -> bytes:
    """Encode Invocation to msgpack bytes.

    Args:
        inv: Invocation object

    Returns:
        Msgpack-encoded bytes
    """
    return msgspec.msgpack.encode(inv)


def decode_invocation(data: bytes) -> Invocation:
    """Decode msgpack bytes to Invocation.

    Args:
        data: Msgpack-encoded bytes

    Returns:
        Invocation object
    """
    return msgspec.msgpack.decode(data, type=Invocation)


def encode_metrics(metrics: Metrics) -> bytes:
    """Encode Metrics to msgpack bytes.

    Args:
        metrics: Metrics object

    Returns:
        Msgpack-encoded bytes
    """
    return msgspec.msgpack.encode(metrics)


def decode_metrics(data: bytes) -> Metrics:
    """Decode msgpack bytes to Metrics.

    Args:
        data: Msgpack-encoded bytes

    Returns:
        Metrics object
    """
    return msgspec.msgpack.decode(data, type=Metrics)


def encode_context(ctx: Context) -> bytes:
    """Encode Context to msgpack bytes.

    Args:
        ctx: Context object

    Returns:
        Msgpack-encoded bytes
    """
    return msgspec.msgpack.encode(ctx)


def decode_context(data: bytes) -> Context:
    """Decode msgpack bytes to Context.

    Args:
        data: Msgpack-encoded bytes

    Returns:
        Context object
    """
    return msgspec.msgpack.decode(data, type=Context)
