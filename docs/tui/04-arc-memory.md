# 04 — ARC Memory

> **ARC** = Agent Runtime Context. CLIO's native 3-tier memory: hot LRU cache + warm B-tree index / LSM tree + cold IOWarp CTE storage. Docs source: `docs/ARC_MEMORY_LAYER.md`, code in `src/clio_agent/arc/`.

## Purpose

- Persistent **context continuity** across turns + sessions.
- **Performance tracking** — per-agent metrics drive the Optimizer's self-improvement loop.
- **Agent coordination** — multi-expert workflows share dataset profiles + routing history.
- **Fast retrieval** — O(1) for hot data, O(log N) for indexed lookups.

## Three tiers

```
TIER 1 — LRU Cache          (in-memory, O(1))
         · active conversations, recent tool results, routing decisions
         · hit rate target 85–95 %
         · capacity 1000 items (configurable)

TIER 2 — B-Tree Index + LSM Tree  (on-disk, O(log N))
         · B-Tree indexes: session_id, (agent_id, timestamp), (domain, keywords)
         · LSM Tree: write-optimised metrics collection (10K+ writes/sec)

TIER 3 — IOWarp CTE         (persistent, multi-tier storage)
         · Hot    — GPU memory (< 1 h)
         · Warm   — NVMe       (1–24 h)
         · Cold   — Parallel FS (1–30 d)
         · Archive — object store (> 30 d)
```

(`arc/memory.py:69-95`, `docs/ARC_MEMORY_LAYER.md:37-134`)

## Data schema (msgspec.Struct)

```python
# /conversations/<session_id>/
Conversation:
  session_id, user_id, created_at, updated_at, status,
  messages: [Message]              # {role, content, timestamp, message_id}
  routing_decisions: [RoutingDecision]
  metadata, storage_tier

# /invocations/<trace_id>/
Invocation:
  trace_id, session_id,
  agent_id, tier,                  # 1 = Main, 2 = Expert, 3 = Nanoagent
  started_at, completed_at, duration_ms,
  status,                          # "success" | "failure" | "timeout"
  input, output,
  tools_called: [ToolCall],        # [{tool, params, result, duration_ms, cached}]
  performance

# /metrics/<agent_id>/
Metrics:
  agent_id, total_invocations, success_rate,
  avg_latency_ms, p50_latency_ms, p95_latency_ms, p99_latency_ms,
  user_satisfaction, optimization_history

# /context/<domain>/
Context:
  domain, retrieved_docs, cached_tool_results, learned_patterns
```

(`arc/schema.py:28-176`)

## API the TUI cares about

```python
arc = ARCMemory(data_dir=".clio_agent/arc", cache_capacity=1000)

# Cache health
stats = arc.get_cache_stats()              # {"hits", "misses", "hit_rate"}

# Conversation + multi-turn
conv = arc.get_conversation(session_id)    # Conversation | None
arc.store_conversation(conv)               # append messages, update updated_at

# Invocations (per-expert history)
invs = arc.get_invocations_by_agent("data", limit=100)

# Metrics
m = arc.get_metrics("data", period="2025-01")   # or latest if period omitted

# Context retrieval (used by ClioAgent at turn start)
context = arc.search_context(query="compression", domain="hdf5_optimization")

# Tool-result cache
cached = arc.get_cached_tool_result(
    tool="hdf5_analyze",
    params={"filepath": "x.h5"},
)
```

(`arc/memory.py:50-100+`, `retrieval.py:31-62`)

## What to surface in the TUI

| Element | Where | TUI placement |
|---|---|---|
| **Cache hit rate** | `arc.get_cache_stats()["hit_rate"]` | Footer / Settings / `/memory` panel |
| **Tool result cached/fresh glyph** | `Invocation.tools_called[].cached` | Inline under tool call (⚡ fresh, ✓ cached) |
| **Session history** | `arc.get_conversation(sid).messages` | Conversation pane on session resume |
| **Routing explanation** | `Conversation.routing_decisions[-1]` | Hover/modal on expert badge |
| **Expert performance** | `arc.get_metrics(agent_id)` | Metrics overlay (`Ctrl+M` today) |

## What NOT to surface

- Internal storage-tier migration (automatic; just a performance knob).
- LSM-tree / B-tree mechanics (implementation details).
- DSPy `Example` / training data in the Optimizer path (background).

## Context compilation

`retrieval.py:74-162` has `compile_expert_context(session_id, tier_budget=4000)` — it assembles conversation + dataset profiles + procedural memory + routing history into a single context string that gets fed as `file_context` to Experts. The pipeline:

```
filter  → compact → enrich → assemble
```

Budgets: Tier-1 (Main Agent) 2K tokens, Tier-2 (Expert) 4K tokens. Exceeding budget triggers compaction (oldest messages first) — so long conversations silently lose context. The TUI doesn't need to display this, but it should know the **effective** conversation window shrinks as a session grows.

## Storage backends

`arc/storage.py`: `IOWarpCTEBackend(base_dir)` supports tiered writes:

```python
backend.write("test/file.msgpack", b"data", tier="warm")
result = backend.read("test/file.msgpack")   # any tier
```

Tier policy (days): `hot_to_warm=1`, `warm_to_cold=7`, `cold_to_archive=30`. In tests (and dev), `iowarp_available=False` → graceful fallback to local directory tiers.

## Shutdown

`agent.shutdown()` flushes ARC caches to disk and closes the LSM tree. The TUI should call it on process exit — or, when running CLIO as a subprocess, send SIGTERM and let CLIO's own atexit path handle it.
