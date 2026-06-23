"""Data schemas for ARC (Conversation, Invocation, Metrics, Context)

This module defines msgspec.Struct-based schemas for efficient serialization
in the ARC Memory Layer. All schemas support msgpack encoding/decoding.

Architecture:
    - Conversation: Session history and routing decisions
    - Invocation: Individual agent execution traces
    - Metrics: Aggregated performance metrics per agent
    - Context: Cached tools, learned patterns, domain knowledge

Timestamp Consistency:
    - All timestamp fields use float type (Unix time from time.time())
    - All timestamp defaults use msgspec.field(default_factory=lambda: time.time())
    - Examples: updated_at, started_at, completed_at, learned_at, created_at, cached_at
    - This ensures consistent serialization, temporal ordering, and comparison

See docs/ARC_MEMORY_LAYER.md for detailed schema specifications.
"""

import time
import uuid
from typing import Any, Dict, List, Literal, Optional

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


# One ordered, scoped piece of the ARC live context plane. The four context
# operations (append/insert/delete/summarize) act on these; the gact ReAct loop
# writes one per produced piece (thought/tool_call/observation) and rebuilds its
# prompt by rendering the live ordered set. See docs/design/arc-live-context-plane.md.
SegmentKind = Literal[
    "system",
    "user",
    "tool_def",
    "thought",
    "tool_call",
    "observation",
    "summary",
    # Richer ARC-as-source kinds (substrate-ready; no writer emits these yet — they
    # are the targets of the live-atom phase). They are NOT part of the dspy
    # trajectory projection (segments_to_keys ignores any kind it doesn't model).
    "lm_io",  # one raw LM call's input + output (the I/O of a single ReAct step)
    "extract_io",  # a dspy.Extract call's input + output
    "answer",  # an expert/turn final message
    "turn_event",  # one folded semantic event (the live-observer's per-turn fold atom)
]
SegmentStatus = Literal["live", "tombstoned"]

# The kinds the model's PROMPT is rendered from (the dspy trajectory projection's
# domain) + the static framing kinds. These are the ONLY kinds the live-plane
# consumers that MUTATE the working set operate over: the per-turn working-set reset
# and the auto-compaction target. The richer ARC-as-source kinds (lm_io/extract_io/
# answer) are part of ARC's COMPLETE freeze-anytime state but are NOT working-set
# context, so they must never be reset-tombstoned at a new turn nor folded into a
# compaction summary. Equivalently: WORKING_SET_KINDS == every kind EXCEPT
# lm_io/extract_io/answer. (render/render_keys are UNCHANGED — segments_to_keys is a
# kind-allowlist that already ignores the new kinds, so the prompt is immune.)
WORKING_SET_KINDS: frozenset[str] = frozenset(
    {
        "system",
        "user",
        "tool_def",
        "thought",
        "tool_call",
        "observation",
        "summary",
    }
)


class Segment(msgspec.Struct):
    """One ordered, scoped piece of live context — the unit of the ARC live plane.

    Locked schema (GOAL.md). ``content`` shape depends on ``kind``:
        thought / observation / summary / system / user -> {"text": str}
        tool_call -> {"name": str, "args": dict[str, Any]}
        tool_def  -> {"name": str, "schema": Any} or {"text": str}

    Attributes:
        scope: Tag address ("agentX/expertY") — expert/agent addressing.
        kind: Render + token-attribution category.
        content: Payload, shape per kind (see above).
        session_id: Owning session (infra; never emitted into the rendered prompt).
        step: ReAct iteration index; -1 for static system/user/tool_def segments.
        order: Render order within scope; gap-allocated float so mid-inserts never
            renumber later segments.
        logical_time: Store-assigned monotonic clock (as-of-T reads, write ordering).
        id: Stable unique id — the operation target; survives reorder/edit.
        token_count: Cached per-segment token estimate (attribution + compaction).
        derived_from: Provenance; for ``summary`` segments, the ids it replaced.
        status: ``"tombstoned"`` deletions are skipped by render but kept for replay.
        trace_ref: Link to the durable Trace event that logged this segment's write.
        created_at: Wall-clock creation time (diagnostics only).
        turn_id: Owning expert lifetime (one expert turn, start..extract). ``""`` when
            the writer doesn't span-stamp (back-compat default).
        expert_span_id: Owning expert-turn span; distinct from ``turn_id`` because
            expert turns can OVERLAP (concurrent experts), so a span id disambiguates
            which concurrent turn a segment belongs to. ``""`` when unstamped.
        run_span_id: Owning agent-run span (the whole agent run). ``""`` when unstamped.
    """

    # Required (the locked render fields the writer always supplies).
    scope: str
    kind: SegmentKind
    content: Dict[str, Any]
    session_id: str
    step: int
    order: float
    logical_time: int
    # Optional with defaults.
    id: str = msgspec.field(default_factory=lambda: str(uuid.uuid4()))
    token_count: int = 0
    derived_from: List[str] = msgspec.field(default_factory=list)
    status: SegmentStatus = "live"
    tombstoned_at: int = 0  # logical_time of tombstoning; 0 = live (for as-of-T reads)
    trace_ref: str = ""
    created_at: float = msgspec.field(default_factory=lambda: time.time())
    # Trajectory-correlation span ids (additive, optional, msgspec back-compatible:
    # records written before these fields existed decode with the "" defaults). The
    # live-atom phase will populate them; this phase only makes the schema carry them.
    turn_id: str = ""  # expert lifetime (one expert turn)
    expert_span_id: str = ""  # expert turn; CAN overlap with another expert's turn
    run_span_id: str = ""  # the whole agent run


def segment_text(seg: "Segment") -> str:
    """Best-effort flat text of a segment's content (render + token counting).

    Pure, no I/O. ``tool_call`` content renders as ``name(json-args)``; everything
    else uses the ``"text"`` field, falling back to a JSON dump of the content.
    """
    if seg.kind == "tool_call":
        name = str(seg.content.get("name") or "")
        args = seg.content.get("args") or {}
        return f"{name}({msgspec.json.encode(args).decode()})"
    text = seg.content.get("text")
    return text if isinstance(text, str) else msgspec.json.encode(seg.content).decode()


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

    This schema uses generic pattern_type + pattern_data design rather than
    fixed fields (description, examples_seen, rule) to support extensible
    pattern types without schema migration. ARC can store any pattern_type
    with arbitrary data structure, enabling future pattern categories.

    Attributes:
        pattern_type: Type of pattern (e.g., "frequent_topic", "tool_usage",
            "error_pattern", "latency_anomaly"). Used as key for pattern-specific
            handlers and analyzers.
        pattern_data: Pattern-specific data dictionary. Content depends on
            pattern_type. Common keys: "frequency", "examples", "rule",
            "threshold", "conditions", etc. No fixed schema per type.
        confidence: Confidence score (0.0-1.0). Indicates statistical
            significance or certainty of pattern detection.
        learned_at: Pattern learning timestamp (Unix timestamp from time.time(),
            defaults to current time). Used for temporal ordering and TTL.

    Design Rationale:
        - Flexible: New pattern types added without schema migration
        - Efficient: Single msgpack-serializable structure for all patterns
        - Queryable: pattern_type enables filtering, indexing, retrieval
        - Extensible: pattern_data grows with discovery needs

    Example - Frequent Topic Pattern:
        >>> pattern = LearnedPattern(
        ...     pattern_type="frequent_topic",
        ...     pattern_data={
        ...         "topic": "HDF5 optimization",
        ...         "frequency": 42,
        ...         "last_occurrence": 1704067200.0,
        ...         "confidence": 0.94
        ...     },
        ...     confidence=0.94
        ... )

    Example - Tool Usage Pattern:
        >>> pattern = LearnedPattern(
        ...     pattern_type="tool_usage",
        ...     pattern_data={
        ...         "tool": "hdf5_analyze",
        ...         "following_tool": "compression_suggest",
        ...         "co_occurrence_count": 18,
        ...         "success_rate": 0.89
        ...     },
        ...     confidence=0.89
        ... )

    Example - Error Pattern:
        >>> pattern = LearnedPattern(
        ...     pattern_type="error_pattern",
        ...     pattern_data={
        ...         "error_type": "timeout",
        ...         "trigger": "large_file_analysis",
        ...         "frequency": 7,
        ...         "suggested_mitigation": "chunk_processing"
        ...     },
        ...     confidence=0.76
        ... )

    Example - Latency Anomaly:
        >>> pattern = LearnedPattern(
        ...     pattern_type="latency_anomaly",
        ...     pattern_data={
        ...         "agent": "DataExpert",
        ...         "baseline_ms": 1200.0,
        ...         "spike_ms": 3500.0,
        ...         "occurrence_hour": 14,
        ...         "correlation": "high_concurrency"
        ...     },
        ...     confidence=0.82
        ... )
    """

    # Core fields: pattern_type enables flexible categorization
    pattern_type: str
    # Flexible pattern_data: structure varies by pattern_type, no fixed schema
    pattern_data: Dict[str, Any]
    # Statistical confidence in pattern validity
    confidence: float
    # Timestamp from time.time() for temporal ordering and retrieval
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


class DatasetProfile(msgspec.Struct):
    """Profile of an analyzed dataset for cross-expert collaboration.

    Stores schema, statistics, and quality information about a dataset
    so that multiple experts can share analysis results within a session.
    For example, DataExpert stores a profile after initial analysis, and
    AnalysisExpert or VisualizationExpert can read it to avoid re-analyzing.

    Attributes:
        session_id: Session that created this profile
        filepath: Path to the analyzed file
        file_format: File format identifier ("hdf5", "parquet", "csv")
        created_by: Expert that created it ("data", "analysis")
        created_at: Creation timestamp (Unix timestamp)
        schema_info: Column names, types, row count
        statistics: Per-column stats (min, max, mean, etc.)
        quality_notes: Human-readable observations ("15% nulls in column X")
        metadata: Additional information

    Example:
        >>> import time
        >>> profile = DatasetProfile(
        ...     session_id="session-1",
        ...     filepath="/data/experiment.parquet",
        ...     file_format="parquet",
        ...     created_by="data",
        ...     created_at=time.time(),
        ...     schema_info={"columns": ["temp", "pressure"], "rows": 1000},
        ...     statistics={"temp": {"mean": 24.5, "std": 3.2}},
        ...     quality_notes=["No nulls detected"],
        ...     metadata={}
        ... )
    """

    session_id: str
    filepath: str
    file_format: str
    created_by: str
    created_at: float
    schema_info: Dict[str, Any] = msgspec.field(default_factory=dict)
    statistics: Dict[str, Any] = msgspec.field(default_factory=dict)
    quality_notes: List[str] = msgspec.field(default_factory=list)
    metadata: Dict[str, Any] = msgspec.field(default_factory=dict)


class VariantRecord(msgspec.Struct):
    """Record of an optimized expert variant stored in ARC.

    Tracks variant metadata including training data size, before/after
    scores, statistical significance, and deployment state. Used by the
    optimizer to manage variant lifecycle.

    Attributes:
        variant_id: Unique variant identifier (e.g., "data_expert_v2")
        agent_id: Which expert this variant belongs to ("data", "analysis", "visualization")
        created_at: Creation timestamp (Unix timestamp)
        training_examples: Number of training examples used
        before_score: Score before optimization
        after_score: Score after optimization
        improvement_delta: after_score - before_score
        p_value: Statistical significance p-value
        is_significant: Whether improvement passed p<0.05 test
        is_active: Whether this is the currently deployed variant
        file_path: Path to saved variant JSON
        dspy_version: DSPy version used for optimization
        metadata: Additional metadata

    Example:
        >>> import time
        >>> record = VariantRecord(
        ...     variant_id="data_expert_v2",
        ...     agent_id="data",
        ...     training_examples=50,
        ...     before_score=0.65,
        ...     after_score=0.82,
        ...     improvement_delta=0.17,
        ...     p_value=0.003,
        ...     is_significant=True,
        ...     is_active=True,
        ...     file_path="variants/data_expert_v2.json",
        ...     dspy_version="3.1.3",
        ... )
    """

    variant_id: str
    agent_id: str
    created_at: float = msgspec.field(default_factory=lambda: time.time())
    training_examples: int = 0
    before_score: float = 0.0
    after_score: float = 0.0
    improvement_delta: float = 0.0
    p_value: float = 1.0
    is_significant: bool = False
    is_active: bool = False
    file_path: str = ""
    dspy_version: str = ""
    metadata: Dict[str, Any] = msgspec.field(default_factory=dict)


class ProceduralMemory(msgspec.Struct):
    """Record of what worked or failed for an expert in a session.

    Stores success/failure patterns so experts can learn from past attempts
    within and across sessions. Used by the context compilation pipeline to
    inject relevant procedural knowledge into expert prompts.

    Attributes:
        session_id: Session this memory belongs to
        expert_id: Which expert this applies to
        pattern_type: Type of pattern ("success", "failure", "optimization")
        description: Human-readable description of the pattern
        context: What was happening when the pattern was observed
        outcome: What happened as a result
        learned_at: Timestamp when the pattern was recorded (Unix timestamp)
        confidence: How reliable this pattern is (0.0-1.0)

    Example:
        >>> import time
        >>> mem = ProceduralMemory(
        ...     session_id="session-1",
        ...     expert_id="data",
        ...     pattern_type="success",
        ...     description="gzip-6 achieved 3x compression on float64 data",
        ...     context={"file_type": "hdf5", "dtype": "float64"},
        ...     outcome="compression_ratio=3.1",
        ...     learned_at=time.time(),
        ...     confidence=0.9
        ... )
    """

    session_id: str
    expert_id: str
    pattern_type: str  # "success", "failure", "optimization"
    description: str
    context: Dict[str, Any]
    outcome: str
    learned_at: float = msgspec.field(default_factory=lambda: time.time())
    confidence: float = 0.5


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


def encode_dataset_profile(profile: DatasetProfile) -> bytes:
    """Encode DatasetProfile to msgpack bytes.

    Args:
        profile: DatasetProfile object

    Returns:
        Msgpack-encoded bytes
    """
    return msgspec.msgpack.encode(profile)


def decode_dataset_profile(data: bytes) -> DatasetProfile:
    """Decode msgpack bytes to DatasetProfile.

    Args:
        data: Msgpack-encoded bytes

    Returns:
        DatasetProfile object
    """
    return msgspec.msgpack.decode(data, type=DatasetProfile)


def encode_procedural_memory(memory: ProceduralMemory) -> bytes:
    """Encode ProceduralMemory to msgpack bytes.

    Args:
        memory: ProceduralMemory object

    Returns:
        Msgpack-encoded bytes
    """
    return msgspec.msgpack.encode(memory)


def decode_procedural_memory(data: bytes) -> ProceduralMemory:
    """Decode msgpack bytes to ProceduralMemory.

    Args:
        data: Msgpack-encoded bytes

    Returns:
        ProceduralMemory object
    """
    return msgspec.msgpack.decode(data, type=ProceduralMemory)


def encode_variant_record(record: VariantRecord) -> bytes:
    """Encode VariantRecord to msgpack bytes.

    Args:
        record: VariantRecord object

    Returns:
        Msgpack-encoded bytes
    """
    return msgspec.msgpack.encode(record)


def decode_variant_record(data: bytes) -> VariantRecord:
    """Decode msgpack bytes to VariantRecord.

    Args:
        data: Msgpack-encoded bytes

    Returns:
        VariantRecord object
    """
    return msgspec.msgpack.decode(data, type=VariantRecord)


def encode_segment(seg: Segment) -> bytes:
    """Encode a Segment to msgpack bytes."""
    return msgspec.msgpack.encode(seg)


def decode_segment(data: bytes) -> Segment:
    """Decode msgpack bytes to a Segment."""
    return msgspec.msgpack.decode(data, type=Segment)


def encode_segments(segments: List[Segment]) -> bytes:
    """Encode a list of Segments to msgpack bytes (one record per scope)."""
    return msgspec.msgpack.encode(segments)


def decode_segments(data: bytes) -> List[Segment]:
    """Decode msgpack bytes to a list of Segments."""
    return msgspec.msgpack.decode(data, type=List[Segment])
