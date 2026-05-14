---
title: "ARC Memory Layer: Agent Runtime Context Architecture"
category: architecture
priority: high
version: "1.0"
focus: "Memory Architecture, O(log N) Retrieval, IOWarp CTE Integration"
---

# ARC Memory Layer

**Agent Runtime Context (ARC)** is CLIO Agent's native, high-performance memory system providing persistent context storage, fast retrieval, and agent coordination.

## Overview

### Purpose

ARC solves critical problems in multi-agent systems:
- **Context Continuity**: Resume conversations seamlessly across sessions
- **Performance Tracking**: Store metrics for continuous learning
- **Agent Coordination**: All tiers (T1/T2/T3) share context efficiently
- **Fast Retrieval**: O(log N) search, not linear scan
- **Persistent Storage**: Integrated with IOWarp CTE multi-tier storage

### Key Features

- ⚡ **O(log N) Retrieval**: B-tree indexing for fast search
- 🗄️ **IOWarp CTE Integration**: Persistent storage across tiers (GPU → NVMe → PFS → Archive)
- 🔥 **In-Memory Cache**: LRU cache for hot data (O(1) access)
- 📊 **Metrics Collection**: High-throughput metrics for Optimizer Layer
- 🤝 **Multi-Agent Coordination**: Shared context across all tiers
- 🔍 **Rich Query API**: Search by session, agent, timestamp, domain

---

## Architecture

### 3-Tier Memory Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  TIER 1: In-Memory Layer (Hot Data, Sub-Millisecond Access)│
│                                                              │
│  ┌────────────────────────────────────────────────────────┐ │
│  │  LRU Cache (Least Recently Used)                      │ │
│  │                                                        │ │
│  │  Data Stored:                                         │ │
│  │  • Active conversations (last N accessed)             │ │
│  │  • Recent tool results (1-hour TTL)                   │ │
│  │  • User preferences (session-specific)                │ │
│  │  • Routing decisions (last 100)                       │ │
│  │                                                        │ │
│  │  Performance:                                         │ │
│  │  • Access: O(1)                                       │ │
│  │  • Eviction: LRU policy                               │ │
│  │  • Size: Configurable (default: 1000 items)           │ │
│  │  • Hit Rate: 85-95% for recent data                   │ │
│  └────────────────────────────────────────────────────────┘ │
└──────────────────────────┬───────────────────────────────────┘
                           │ (Cache miss)
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  TIER 2: Index Layer (Fast Search, Millisecond Access)     │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐ │
│  │  B-Tree Index                                         │ │
│  │                                                        │ │
│  │  Indexes:                                             │ │
│  │  • session_id → Conversation                          │ │
│  │  • (agent_id, timestamp) → Invocations                │ │
│  │  • (domain, keywords) → Context                       │ │
│  │  • user_id → Sessions                                 │ │
│  │                                                        │ │
│  │  Performance:                                         │ │
│  │  • Search: O(log N)                                   │ │
│  │  • Range Queries: Supported                           │ │
│  │  • Fan-out: Configurable (default: 128)               │ │
│  │  • Height: log₁₂₈(N) ≈ 2-3 for millions of entries    │ │
│  └────────────────────────────────────────────────────────┘ │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐ │
│  │  LSM Tree (Log-Structured Merge Tree)                │ │
│  │                                                        │ │
│  │  Optimized For:                                       │ │
│  │  • Write-heavy metrics collection                     │ │
│  │  • High throughput (10,000+ writes/sec)               │ │
│  │  • Append-only workloads                              │ │
│  │  • Background compaction                              │ │
│  │                                                        │ │
│  │  Data:                                                │ │
│  │  • /metrics/<agent_id>/ performance data              │ │
│  │  • /invocations/<trace_id>/ execution traces          │ │
│  └────────────────────────────────────────────────────────┘ │
└──────────────────────────┬───────────────────────────────────┘
                           │ (Persistent storage)
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  TIER 3: Persistent Layer (IOWarp CTE Multi-Tier Storage)  │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐ │
│  │  IOWarp Namespace: /clio_agent/arc/*                     │ │
│  │                                                        │ │
│  │  Storage Tier Policy (Automatic Migration):           │ │
│  │                                                        │ │
│  │  🔥 HOT (GPU Memory)                                  │ │
│  │     • Active sessions (< 1 hour old)                  │ │
│  │     • Access pattern: Read/Write heavy                │ │
│  │     • Capacity: Limited (GPU VRAM)                    │ │
│  │     • Latency: < 1ms                                  │ │
│  │                                                        │ │
│  │  🌡️ WARM (NVMe SSD)                                   │ │
│  │     • Recent sessions (1-24 hours old)                │ │
│  │     • Access pattern: Read-mostly                     │ │
│  │     • Capacity: Medium (100GB - 1TB)                  │ │
│  │     • Latency: 1-10ms                                 │ │
│  │                                                        │ │
│  │  ❄️ COLD (Parallel File System)                       │ │
│  │     • Historical data (1-30 days old)                 │ │
│  │     • Access pattern: Infrequent reads                │ │
│  │     • Capacity: Large (10TB+)                         │ │
│  │     • Latency: 10-100ms                               │ │
│  │                                                        │ │
│  │  📦 ARCHIVE (Object Storage)                          │ │
│  │     • Long-term storage (> 30 days old)               │ │
│  │     • Access pattern: Rare                            │ │
│  │     • Capacity: Unlimited                             │ │
│  │     • Latency: 100ms - 1s                             │ │
│  │                                                        │ │
│  │  Migration Policy:                                    │ │
│  │  • Access frequency triggers promotion (cold → warm)  │ │
│  │  • Age triggers demotion (hot → warm → cold → archive)│
│  │  • Configurable thresholds                            │ │
│  └────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

---

## Data Schema

### 1. Conversations (`/conversations/<session_id>/`)

**Structure**:
```python
Conversation = {
    "session_id": "uuid-v4",
    "user_id": "user@example.com",
    "created_at": "2025-01-09T14:30:00Z",
    "updated_at": "2025-01-09T15:45:00Z",
    "last_accessed": "2025-01-09T15:45:00Z",  # For tier migration
    "status": "active" | "completed" | "abandoned",

    "messages": [
        {
            "message_id": "uuid-v4",
            "role": "user" | "assistant",
            "content": "How do I optimize my 100GB HDF5 file?",
            "timestamp": "2025-01-09T14:30:05Z",
            "metadata": {
                "source": "cli",  # or "api", "a2a"
                "model_used": "gpt-oss-20b",
            }
        },
        # ... more messages
    ],

    "routing_decisions": [
        {
            "timestamp": "2025-01-09T14:30:06Z",
            "query": "How do I optimize my 100GB HDF5 file?",
            "capabilities_needed": ["HDF5", "optimization"],
            "selected_agent": "DataExpert",
            "reasoning": "Query mentions HDF5 file and optimization",
            "confidence": 0.95,
            "alternatives": [
                {"agent": "HPCExpert", "score": 0.12},
            ]
        }
    ],

    "metadata": {
        "user_preferences": {
            "verbose": false,
            "preferred_compression": "gzip-6",
        },
        "domain": "scientific_computing",
        "total_tokens": 1234,
        "total_invocations": 3,
    },

    "storage_tier": "warm",  # Current IOWarp CTE tier
}
```

### 2. Invocations (`/invocations/<trace_id>/`)

**Structure**:
```python
Invocation = {
    "trace_id": "uuid-v4",
    "session_id": "uuid-v4",  # Parent conversation
    "parent_trace_id": "uuid-v4" | null,  # For nanoagent spawns

    "agent_id": "DataExpert",
    "tier": 2,  # 1=Main, 2=Expert, 3=Nanoagent
    "source": "native" | "langchain" | "crewai" | "autogen",

    "started_at": "2025-01-09T14:30:07Z",
    "completed_at": "2025-01-09T14:30:08.247Z",
    "duration_ms": 1247,
    "status": "success" | "failure" | "timeout",

    "input": {
        "query": "Optimize 100GB HDF5 file",
        "context": {...},
    },

    "output": {
        "answer": "Apply gzip-6 compression...",
        "reasoning_trace": [
            {"step": 1, "thought": "Need to analyze first", "action": "hdf5_analyze"},
            {"step": 2, "observation": {...}, "thought": "No compression"},
        ],
    },

    "tools_called": [
        {
            "tool": "hdf5_analyze",
            "params": {"filepath": "/data/file.h5"},
            "result": {"compression": "none", "size": "100GB"},
            "duration_ms": 342,
            "cached": false,  # Was result from cache?
        }
    ],

    "nanoagents_spawned": [
        {
            "nanoagent_id": "uuid-v4",
            "trace_id": "uuid-v4",  # Links to separate invocation record
            "task": "analyze_chunk",
            "duration_ms": 123,
            "status": "success",
        }
    ],

    "performance": {
        "latency_ms": 1247,
        "success": true,
        "user_satisfied": true,  # Implicit: task completed without errors
        "prompt_variant": "v2.3.1",  # Which optimized prompt was used
    },

    "storage_tier": "cold",  # Most invocations archived quickly
}
```

### 3. Metrics (`/metrics/<agent_id>/`)

**Structure**:
```python
Metrics = {
    "agent_id": "DataExpert",
    "tier": 2,
    "period": "2025-01-01/2025-01-31",
    "computed_at": "2025-01-31T23:59:59Z",

    "invocations": {
        "total": 1234,
        "success": 1193,
        "failure": 31,
        "timeout": 10,
        "success_rate": 0.967,
    },

    "latency": {
        "avg_ms": 1523,
        "median_ms": 1200,
        "p95_ms": 2500,
        "p99_ms": 4200,
        "min_ms": 234,
        "max_ms": 8900,
    },

    "user_satisfaction": {
        "total_rated": 342,
        "positive": 305,
        "negative": 37,
        "score": 0.89,  # 0-1 scale
    },

    "tools": {
        "hdf5_analyze": {
            "calls": 567,
            "avg_duration_ms": 342,
            "cache_hit_rate": 0.23,
        },
        "hdf5_optimize": {
            "calls": 423,
            "avg_duration_ms": 2134,
            "cache_hit_rate": 0.05,  # Optimization rarely cached
        },
    },

    "optimization_history": [
        {
            "timestamp": "2025-01-15T10:00:00Z",
            "optimizer": "PromptOptimizer",
            "method": "MIPRO",
            "variant_id": "v2.3.1",
            "improvements": {
                "success_rate": {"before": 0.87, "after": 0.94, "delta": "+8%"},
                "avg_latency_ms": {"before": 1732, "after": 1523, "delta": "-12%"},
            },
            "training_examples": 856,
            "optimization_duration": "2h 15m",
        }
    ],

    "storage_tier": "warm",  # Metrics accessed frequently
}
```

### 4. Context (`/context/<domain>/`)

**Structure**:
```python
Context = {
    "domain": "hdf5_optimization",
    "created_at": "2025-01-09T...",
    "updated_at": "2025-01-31T...",

    "retrieved_docs": [
        {
            "doc_id": "uuid-v4",
            "source": "rag_system",
            "title": "HDF5 Compression Best Practices",
            "content": "...",
            "relevance_score": 0.94,
            "accessed_count": 47,
        }
    ],

    "cached_tool_results": {
        "hdf5_analyze": {
            "params_hash": "sha256(...)",
            "result": {...},
            "cached_at": "2025-01-09T14:30:08Z",
            "ttl": 3600,  # 1 hour
            "hit_count": 3,
        }
    },

    "learned_patterns": [
        {
            "pattern_id": "uuid-v4",
            "description": "Large files (>10GB) typically need compression",
            "confidence": 0.92,
            "examples_seen": 47,
            "learned_at": "2025-01-15T...",
            "rule": {
                "condition": "file_size > 10GB and compression == 'none'",
                "recommendation": "Apply gzip-6 or blosc compression",
            }
        }
    ],

    "storage_tier": "cold",  # Context accessed less frequently
}
```

---

## Implementation

### Data Structures

**LRU Cache**:
```python
from lru import LRU

class ARCCache:
    """In-memory LRU cache for hot data"""

    def __init__(self, size: int = 1000):
        self.conversations = LRU(size)
        self.tool_results = LRU(size)
        self.preferences = LRU(size)

    def get(self, key: str) -> Any | None:
        """O(1) access"""
        return self.conversations.get(key) or self.tool_results.get(key)

    def set(self, key: str, value: Any, ttl: int = None):
        """O(1) insertion with optional TTL"""
        # Store with expiration timestamp
        pass
```

**B-Tree Index**:
```python
from sortedcontainers import SortedDict

class BTreeIndex:
    """B-tree index for O(log N) retrieval"""

    def __init__(self):
        self.session_index = SortedDict()  # session_id → offset
        self.agent_index = SortedDict()    # (agent_id, timestamp) → offset
        self.domain_index = SortedDict()   # (domain, keyword) → offsets

    def search(self, key: str) -> List[str]:
        """O(log N) search"""
        return self.session_index.get(key, [])

    def range_query(self, start_key: str, end_key: str) -> List[str]:
        """O(log N + k) range query"""
        return self.session_index.irange(start_key, end_key)
```

**LSM Tree**:
```python
class LSMTree:
    """Log-Structured Merge Tree for write-heavy metrics"""

    def __init__(self):
        self.memtable = {}  # In-memory buffer
        self.sstables = []  # Sorted String Tables (on disk)

    def append(self, key: str, value: Any):
        """O(1) write to memtable"""
        self.memtable[key] = value
        if len(self.memtable) > FLUSH_THRESHOLD:
            self.flush_to_sstable()

    def get(self, key: str) -> Any:
        """O(log N) read (check memtable first, then sstables)"""
        if key in self.memtable:
            return self.memtable[key]
        return self.search_sstables(key)

    def flush_to_sstable(self):
        """Background: flush memtable to sorted SSTable file"""
        pass

    def compact(self):
        """Background: merge sstables, remove duplicates"""
        pass
```

### IOWarp CTE Integration

**Namespace Registration**:
```python
from iowarp import IOWarp

class ARCStorage:
    """ARC persistent storage via IOWarp CTE"""

    def __init__(self):
        # Register CLIO Agent namespace in IOWarp
        self.iowarp = IOWarp.connect()
        self.namespace = self.iowarp.register_namespace(
            path="/clio_agent/arc",
            tier_policy={
                "hot": {
                    "storage": "gpu_memory",
                    "criteria": "age < 1h",
                    "capacity": "8GB",
                },
                "warm": {
                    "storage": "nvme",
                    "criteria": "age < 24h",
                    "capacity": "100GB",
                },
                "cold": {
                    "storage": "parallel_fs",
                    "criteria": "age < 30d",
                    "capacity": "1TB",
                },
                "archive": {
                    "storage": "object_store",
                    "criteria": "age >= 30d",
                    "capacity": "unlimited",
                },
            },
            migration_policy="automatic",  # IOWarp handles tier migration
        )

    def write(self, path: str, data: bytes):
        """Write to IOWarp CTE (initially to 'hot' tier)"""
        self.namespace.write(path, data)
        # IOWarp automatically migrates based on access patterns

    def read(self, path: str) -> bytes:
        """Read from IOWarp CTE (IOWarp fetches from appropriate tier)"""
        return self.namespace.read(path)
        # IOWarp may prefetch or promote to faster tier
```

**Tier Migration Example**:
```
New conversation created at 14:30
  ↓
Stored in HOT tier (GPU memory)
  ↓ (1 hour passes, no access)
IOWarp auto-migrates to WARM tier (NVMe)
  ↓ (24 hours pass)
IOWarp auto-migrates to COLD tier (Parallel FS)
  ↓ (User accesses conversation)
IOWarp promotes back to WARM tier (NVMe)
  ↓ (30 days pass)
IOWarp auto-migrates to ARCHIVE tier (Object Store)
```

---

## API Reference

### Core ARC Class

```python
class ARC:
    """Agent Runtime Context - Memory Layer API"""

    def __init__(
        self,
        cache_size: int = 1000,
        tool_cache_ttl: int = 3600,
        iowarp_namespace: str = "/clio_agent/arc",
        tier_policy: dict = None,
    ):
        """
        Initialize ARC Memory Layer

        Args:
            cache_size: LRU cache size for hot data
            tool_cache_ttl: Tool result cache TTL (seconds)
            iowarp_namespace: IOWarp CTE namespace path
            tier_policy: Storage tier configuration (or use defaults)
        """
        pass
```

### Read Operations

```python
# Get conversation history (O(log N))
conversation = arc.get_conversation(session_id: str) -> Conversation | None

# Get invocations for agent (O(log N))
invocations = arc.get_invocations(
    agent_id: str,
    limit: int = 100,
    start_time: str = None,
    end_time: str = None,
) -> List[Invocation]

# Get aggregated metrics (O(1) - pre-computed)
metrics = arc.get_metrics(
    agent_id: str,
    period: str = "2025-01",  # YYYY-MM
) -> Metrics

# Search context (O(log N))
contexts = arc.search_context(
    query: str,
    domain: str = None,
    limit: int = 10,
) -> List[Context]

# Get cached tool result (O(1))
result = arc.get_cached_tool_result(
    tool: str,
    params: dict,
) -> Any | None

# Get shared context for multi-agent coordination (O(1))
shared = arc.get_shared_context(session_id: str) -> dict
```

### Write Operations

```python
# Store conversation message
arc.store_message(
    session_id: str,
    message: Message,
) -> None

# Store invocation trace
arc.store_invocation(
    invocation: Invocation,
) -> None

# Update metrics (async, batched)
arc.update_metrics(
    agent_id: str,
    metrics: MetricsUpdate,
) -> None

# Cache tool result
arc.cache_tool_result(
    tool: str,
    params: dict,
    result: Any,
    ttl: int = 3600,
) -> None

# Update shared context
arc.update_shared_context(
    session_id: str,
    context: dict,
) -> None
```

---

## Performance Characteristics

### Retrieval Performance

| Operation | Complexity | Typical Latency | Notes |
|-----------|-----------|-----------------|-------|
| Cache hit (hot data) | O(1) | < 1ms | LRU cache |
| B-tree search (indexed) | O(log N) | 1-10ms | N = millions |
| Range query | O(log N + k) | 5-50ms | k = result count |
| LSM tree write | O(1) | < 1ms | Append to memtable |
| Full-text search | O(N) | 100ms - 1s | Avoid if possible |

### Storage Tier Latencies

| Tier | Access Latency | Capacity | Use Case |
|------|----------------|----------|----------|
| **Hot (GPU)** | < 1ms | 8GB | Active conversations |
| **Warm (NVMe)** | 1-10ms | 100GB | Recent sessions (24h) |
| **Cold (PFS)** | 10-100ms | 1TB | Historical data |
| **Archive (Object)** | 100ms - 1s | Unlimited | Long-term storage |

### Scalability

- **Cache capacity**: 1,000 conversations (configurable)
- **Index capacity**: 10M+ conversations (B-tree scales to billions)
- **Write throughput**: 10,000+ invocations/sec (LSM tree)
- **Storage**: Unlimited via IOWarp CTE + Object Storage

---

## Usage Examples

### Example 1: Expert Agent Using ARC

```python
from clio_agent.arc import ARC
from clio_agent.experts.data_expert import DataExpert

class DataExpertWithARC(DataExpert):
    def __init__(self):
        super().__init__()
        self.arc = ARC()

    def forward(self, question: str, context: str, session_id: str):
        # Load conversation history from ARC
        conversation = self.arc.get_conversation(session_id)

        # Load relevant context from previous invocations
        past_invocations = self.arc.get_invocations(
            agent_id="DataExpert",
            limit=5,
        )

        # Check if similar query was answered before
        for inv in past_invocations:
            if similarity(inv.input.query, question) > 0.9:
                # Reuse previous reasoning
                cached_answer = inv.output.answer
                return f"Based on previous analysis: {cached_answer}"

        # Execute with optimized prompts (loaded from ARC)
        result = super().forward(question, context)

        # Store invocation in ARC
        self.arc.store_invocation({
            "trace_id": generate_uuid(),
            "session_id": session_id,
            "agent_id": "DataExpert",
            "tier": 2,
            "input": {"query": question, "context": context},
            "output": {"answer": result},
            "duration_ms": ...,
            "status": "success",
        })

        return result
```

### Example 2: Tool Result Caching

```python
from clio_agent.arc import ARC

arc = ARC()

def call_mcp_tool(tool: str, params: dict) -> Any:
    """Call MCP tool with ARC caching"""

    # Check cache first (O(1))
    cached = arc.get_cached_tool_result(tool, params)
    if cached:
        print(f"Cache hit for {tool}")
        return cached

    # Cache miss - execute tool
    print(f"Cache miss, executing {tool}")
    result = execute_mcp_tool(tool, params)  # Via CAE/PPI

    # Cache result (TTL = 1 hour)
    arc.cache_tool_result(tool, params, result, ttl=3600)

    return result

# Usage in expert
result = call_mcp_tool("hdf5_analyze", {"filepath": "/data/file.h5"})
# First call: Cache miss, executes MCP server
# Second call (within 1 hour): Cache hit, instant return
```

### Example 3: Multi-Agent Coordination via ARC

```python
from clio_agent.arc import ARC

arc = ARC()

# Expert 1: HPCExpert profiles the system
hpc_result = hpc_expert.forward(...)
arc.update_shared_context(
    session_id="current",
    context={"hpc_profile": hpc_result}
)

# Expert 2: DataExpert uses HPC profile from ARC
shared_context = arc.get_shared_context("current")
hpc_profile = shared_context.get("hpc_profile")
data_result = data_expert.forward(..., hpc_context=hpc_profile)
```

---

## Best Practices

### For Agent Developers

1. **Always check ARC cache before expensive operations**:
   ```python
   cached = arc.get_cached_tool_result(tool, params)
   if cached:
       return cached
   result = expensive_mcp_call(tool, params)
   arc.cache_tool_result(tool, params, result)
   ```

2. **Store performance metrics after every invocation**:
   ```python
   start = time.time()
   result = expert.forward(...)
   arc.store_invocation({
       "duration_ms": (time.time() - start) * 1000,
       "success": True,
       ...
   })
   ```

3. **Use shared context for multi-agent coordination**:
   ```python
   # Expert 1 stores results
   arc.update_shared_context(session_id, {"expert1_result": ...})
   # Expert 2 reads results
   expert1_data = arc.get_shared_context(session_id)["expert1_result"]
   ```

4. **Leverage O(log N) for historical queries**:
   ```python
   # Get last 100 invocations for this agent
   history = arc.get_invocations(agent_id="DataExpert", limit=100)
   # Analyze patterns, reuse successful strategies
   ```

### For Performance

1. **Configure appropriate cache size**:
   - Small deployments: 100-500 items
   - Medium: 1,000-5,000 items
   - Large: 10,000+ items

2. **Set reasonable TTLs for tool caching**:
   - Fast-changing data: 300s (5 min)
   - Stable data: 3600s (1 hour)
   - Static data: 86400s (24 hours)

3. **Use IOWarp tier policy to balance cost vs. latency**:
   - Critical data: Keep in GPU/NVMe
   - Historical data: Allow migration to PFS/Object Store

4. **Monitor cache hit rates**:
   ```python
   metrics = arc.get_cache_stats()
   print(f"Cache hit rate: {metrics.hit_rate:.2%}")
   # Adjust cache size if hit rate < 80%
   ```

---

## Troubleshooting

### Issue: Low Cache Hit Rate

**Symptoms**: Cache hit rate < 70%, frequent cache misses

**Solutions**:
1. Increase cache size: `ARC(cache_size=5000)`
2. Increase TTL for stable data: `tool_cache_ttl=7200`
3. Check query patterns - are queries actually similar?

### Issue: Slow Retrieval

**Symptoms**: `get_conversation()` takes > 100ms

**Solutions**:
1. Check if data migrated to slow tier (archive)
2. Increase warm tier size to keep data in NVMe
3. Review IOWarp tier policy
4. Consider pre-warming frequently accessed data

### Issue: IOWarp CTE Connection Failure

**Symptoms**: `ARC.__init__()` fails with IOWarp connection error

**Solutions**:
```bash
# Verify IOWarp is running
curl http://localhost:8080/health

# Check namespace registration
iowarp namespaces list | grep clio-agent

# Re-register namespace
uv run src/clio_agent/arc/storage.py --register-namespace
```

### Issue: High Memory Usage

**Symptoms**: ARC consuming excessive RAM

**Solutions**:
1. Reduce cache size: `ARC(cache_size=500)`
2. Decrease tool cache TTL (more aggressive eviction)
3. Enable aggressive compaction for LSM tree
4. Check for memory leaks in custom code

---

## Configuration Reference

### Environment Variables

```bash
# ARC Configuration
CLIO_AGENT_ARC_CACHE_SIZE=1000           # LRU cache size
CLIO_AGENT_ARC_TOOL_TTL=3600             # Tool cache TTL (seconds)
CLIO_AGENT_ARC_NAMESPACE=/clio_agent/arc    # IOWarp namespace

# IOWarp CTE Connection
IOWARP_ENDPOINT=http://localhost:8080
IOWARP_API_KEY=<your-key>

# Tier Policy Overrides
CLIO_AGENT_ARC_HOT_TIER=gpu_memory
CLIO_AGENT_ARC_WARM_TIER=nvme
CLIO_AGENT_ARC_COLD_TIER=parallel_fs
CLIO_AGENT_ARC_ARCHIVE_TIER=object_store
```

### Config File

```yaml
# .clio_agent/arc_config.yaml

cache:
  size: 1000
  tool_ttl: 3600
  eviction_policy: lru

index:
  type: btree
  fan_out: 128

lsm_tree:
  memtable_size: 1000
  compaction_threshold: 10
  background_compaction: true

iowarp:
  namespace: /clio_agent/arc
  endpoint: http://localhost:8080

  tier_policy:
    hot:
      storage: gpu_memory
      age_threshold: 1h
      capacity: 8GB
    warm:
      storage: nvme
      age_threshold: 24h
      capacity: 100GB
    cold:
      storage: parallel_fs
      age_threshold: 30d
      capacity: 1TB
    archive:
      storage: object_store
      age_threshold: infinity
      capacity: unlimited
```

---

## Future Enhancements

### v0.3.0: ARC-CTE Full Integration
- Complete IOWarp CTE integration
- Automatic tier migration based on access patterns
- Prefetching for predicted queries

### v0.4.0: Advanced Indexing
- Full-text search on conversation content
- Vector embeddings for semantic search
- Graph indexing for conversation flow analysis

### v0.5.0: Distributed ARC
- Multi-node ARC for distributed deployments
- Eventual consistency across replicas
- Sharding for massive scale (billions of conversations)

---

## Related Documentation

- [CLIO Agent Architecture](CLIO_AGENT_ARCHITECTURE.md) - Full system architecture
- [System Identity](SYSTEM_IDENTITY.md) - CLIO Agent capabilities and design
- [Self Improvement](SELF_IMPROVEMENT.md) - How Optimizer Layer uses ARC metrics
- [IOWarp CTE Documentation](https://iowarp.ai/docs/cte) - Context Transfer Engine

---

**Version**: 1.0 (ARC Memory Layer)
**Last Updated**: 2025-01-09
**Focus**: O(log N) Retrieval + IOWarp CTE Integration + Agent Coordination
