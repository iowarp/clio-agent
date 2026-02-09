# Codebase Concerns

**Analysis Date:** 2026-02-09

## Tech Debt

### Module-Level Global State Antipattern

**Area:** Main Agent Tool Wrapping
- **Issue:** `_data_expert_instance` global variable in `src/clio_agent/agent.py` (line 122-239) used as bridge between ReAct pattern and module-level tool functions
- **Files:** `src/clio_agent/agent.py`
- **Impact:** Makes testing difficult, causes tight coupling between ClioAgent and ask_data_expert function, creates state that persists across calls
- **Fix approach:** Replace module-level global with either (1) factory function returning tool registry, (2) partial application of expert instance, or (3) closure-based tool binding

### Duplicate Field Definitions in Signature

**Area:** MainAgentSignature Schema
- **Issue:** Fields `available_experts` and `history` defined twice (lines 51-53 and 58-66), fields `reasoning` and `selected_expert` defined twice (lines 56-57 and 69-79)
- **Files:** `src/clio_agent/signatures/main_agent_sig.py`
- **Impact:** Second definition silently overrides first; DSPy may ignore first version causing unexpected behavior. Can cause confusion during signature updates.
- **Fix approach:** Consolidate to single definition per field with complete metadata in one place. Clean up `src/clio_agent/agent.py` line 92-115 which has similar duplication.

### Hardcoded LM Configuration

**Area:** Model and Endpoint Hardcoding
- **Issue:** LM Studio URL hardcoded as `http://127.0.0.1:1234` throughout codebase; default model hardcoded as `ibm/granite-4-h-tiny`
- **Files:** `src/clio_agent/config.py` (lines 33, 141-142), used in `src/clio_agent/tools/mcp_connector.py`
- **Impact:** Cannot connect to remote LM services; cannot use different models without code changes; prevents deployment in different environments
- **Fix approach:** Use environment variables (LM_STUDIO_URL, LM_STUDIO_MODEL) with validation, load from config files before defaulting to hardcoded values

### Version Mismatch: FastMCP Specification Drift

**Area:** Dependency Specification
- **Issue:** `pyproject.toml` line 39 specifies `fastmcp>=2.13.0` but documentation and code target FastMCP 3.x patterns (mount(), from_mcp_tool(), transforms)
- **Files:** `pyproject.toml`, code patterns in `src/clio_agent/tools/`, docs/MCP_TOOL_INTEGRATION.md
- **Impact:** Type hints and actual FastMCP API may not match; installation could pull 2.x versions incompatible with 3.x code patterns; CRITICAL blocker for MCP tool integration
- **Fix approach:** Update `pyproject.toml` to `fastmcp>=3.0.0`, verify all tool servers use 3.x patterns, add integration tests

## Known Bugs

### ReAct Pattern Incompatibility with LM Studio

**Area:** Main Agent Execution
- **Issue:** Code comment on line 253-255 of `src/clio_agent/agent.py` indicates ReAct requires JSONAdapter which LM Studio rejects; workaround is ChainOfThought which loses tool-calling capability
- **Files:** `src/clio_agent/agent.py`, `src/clio_agent/config.py`
- **Impact:** Main agent cannot call tools or use true reasoning-acting loops. Locked into CoT without actions, reducing problem-solving capability. DataExpert can call tools, but Main Agent cannot.
- **Trigger:** Any question requiring complex multi-step tool use will fail to invoke tools
- **Workaround:** Currently none; ReAct tools disabled, Main Agent delegates all work to DataExpert
- **Expected fix:** (1) Test ChatAdapter with LM Studio, (2) Switch to ChatAdapter per config.py pattern, or (3) Use external LLM (Claude API) for Main Agent if local models insufficient

### Incomplete Error Context in Tool Calls

**Area:** Tool Result Extraction
- **Issue:** `src/clio_agent/agent.py` lines 388-398 assumes trajectory format but trajectory structure from ChainOfThought is not verified or documented
- **Files:** `src/clio_agent/agent.py` (forward method)
- **Impact:** Silent failures when trajectory format unexpected; tool results lost or malformed; metrics storage incomplete
- **Trigger:** Using Main Agent with any tool that returns unexpected format
- **Workaround:** Check verbose mode logs for trajectory content

### ARC Memory Context Retrieval Silent Fallback

**Area:** Context Compilation
- **Issue:** Lines 313-339 of `src/clio_agent/agent.py` catch all exceptions during ARC context retrieval and fall back to "No prior context" without detailed logging
- **Files:** `src/clio_agent/agent.py` (forward method)
- **Impact:** Context retrieval errors hidden from user; diagnostics impossible without verbose mode; could mask issues with index corruption or storage backend failure
- **Trigger:** Any ARC storage error, index corruption, or storage backend issue
- **Workaround:** Enable verbose mode to see error messages

## Security Considerations

### Unsafe Global State in MCP Connector

**Area:** Thread Safety in Event Loop Management
- **Issue:** `src/clio_agent/tools/mcp_connector.py` spawns dedicated event loop thread on import (lines 84-190+); global state initialized at module import time not protected against concurrent imports
- **Files:** `src/clio_agent/tools/mcp_connector.py`
- **Mitigation Current:** Documented in module docstring; initialization happens once per process
- **Recommendation:** (1) Add initialization guard (check if loop already running), (2) Provide explicit init() call instead of import-time initialization, (3) Use asyncio.get_running_loop() patterns instead of dedicated thread

### Unvalidated Tool Parameters

**Area:** MCP Tool Invocation
- **Issue:** Tool parameters passed directly to MCP servers with minimal validation; no schema enforcement visible in `src/clio_agent/tools/`
- **Files:** `src/clio_agent/tools/mcp_connector.py`, tool server implementations
- **Risk:** Injection attacks if tool parameters derived from user input without sanitization
- **Mitigation Needed:** (1) Validate all user-derived parameters against tool schema before passing to MCP, (2) Add parameter type coercion and bounds checking, (3) Document which parameters are user-controllable

### Conversation Storage Without Encryption

**Area:** Persistent Memory
- **Issue:** ARC Memory stores conversations and invocations as plaintext msgspec files in `.clio_agent/arc/` directory
- **Files:** `src/clio_agent/arc/storage.py`, `src/clio_agent/arc/memory.py`
- **Risk:** User queries and agent responses stored in plaintext on disk; no encryption at rest
- **Mitigation Needed:** (1) Encrypt sensitive fields (queries, responses) using Fernet/nacl, (2) Document that ARC directory should be in protected location, (3) Add --no-persist flag to skip storage if needed

## Performance Bottlenecks

### Blocking Async/Sync Bridge in Main Thread

**Area:** IOWarp MCP Tool Calls
- **Issue:** `src/clio_agent/tools/mcp_connector.py` uses `asyncio.run_coroutine_threadsafe()` (line 84+) to bridge sync DSPy code with async MCP clients; blocks caller on Future.result()
- **Files:** `src/clio_agent/tools/mcp_connector.py`
- **Impact:** Each tool call blocks; no concurrent tool execution; latency multiplies with tool count. Degrades with high-latency MCP servers.
- **Current Limit:** Sequential tool calls only; no parallelism
- **Improvement Path:** (1) Use native async in DataExpert using dspy.Parallel pattern, (2) Implement tool result caching to avoid repeated calls, (3) Pre-warm common tool results at session start

### LSM Tree Compaction Pauses

**Area:** Metrics Storage Performance
- **Issue:** `src/clio_agent/arc/lsm.py` compaction thread runs synchronously (lines 400+), can block writes during high-concurrency periods
- **Files:** `src/clio_agent/arc/lsm.py`
- **Impact:** Metrics writes may stall during compaction; unpredictable latency spikes
- **Improvement Path:** (1) Implement non-blocking compaction queue, (2) Tune compaction threshold (currently 5) based on expected write volume, (3) Profile with concurrent write load (>100 writes/sec)

### BTree Index O(log N) but High Constant Factor

**Area:** Context Retrieval Latency
- **Issue:** `src/clio_agent/arc/index.py` uses sortedcontainers.SortedDict (line 366+); each retrieval requires tree traversal; no prefetching or bloom filters
- **Files:** `src/clio_agent/arc/index.py`
- **Impact:** Cache miss -> disk read -> O(log N) index lookup adds 5-10ms per retrieval; target <10ms (line 12 of memory.py spec) may not be met under load
- **Improvement Path:** (1) Profile on real workload, (2) Add bloom filter for negative lookups, (3) Pre-fetch related entries on cache miss, (4) Consider read-ahead caching layer

## Fragile Areas

### Entirely Stub Implementation: Optimizers

**Area:** Optimizer Layer
- **Files:** `src/clio_agent/optimizers/base.py`, `src/clio_agent/optimizers/routing_opt.py`, `src/clio_agent/optimizers/prompt_opt.py`, `src/clio_agent/optimizers/tool_opt.py`, `src/clio_agent/optimizers/metrics.py`, `src/clio_agent/optimizers/evaluator.py`, `src/clio_agent/optimizers/deployer.py`
- **Why Fragile:** Entire Phase 4 optimizer infrastructure is stubs; any code importing these will crash at runtime
- **Safe Modification:** (1) Keep stubs but raise NotImplementedError immediately, (2) Add type hints so IDE catches issues early, (3) Don't import in non-optional code paths
- **Test Coverage:** 0% (no tests); Phase 4 implementation required before production

### MCP Connector Subprocess Management

**Area:** Tool Server Lifecycle
- **Files:** `src/clio_agent/tools/mcp_connector.py` (lines 84-200+)
- **Why Fragile:** Spawns external processes (uvx iowarp-mcps); no timeout on subprocess startup; no health checking; subprocess crashes silently if not logged
- **Safe Modification:** (1) Add explicit timeout on process launch, (2) Implement heartbeat/health check, (3) Add automatic restart logic with exponential backoff, (4) Test with unavailable servers
- **Test Coverage:** MCP servers are all external (not testable in-process without fastmcp.Client)

### DataExpert Tool Results Processing

**Area:** Tool Result Parsing
- **Files:** `src/clio_agent/experts/data_expert.py` (lines 76-118+ mock implementations)
- **Why Fragile:** Mock tools return hardcoded dicts with known keys; actual MCP tools may return different schemas; no validation or parsing layer
- **Safe Modification:** (1) Add jsonschema validation for tool results, (2) Define canonical schema for each tool output, (3) Add defensive parsing with try/except per field, (4) Test with malformed tool results
- **Test Coverage:** Mock implementations only; no tests against real tool servers

## Scaling Limits

### ARC Memory Cache Size Fixed at Initialization

**Area:** Hot Tier Capacity
- **Issue:** `src/clio_agent/arc/memory.py` line 60 sets cache_capacity at __init__ time; cannot resize without restart
- **Current Capacity:** Default 1000 entries (likely 50-200MB depending on entry size)
- **Limit:** Long-running sessions accumulate conversations; cache fills, eviction starts, hit rate drops
- **Scaling Path:** (1) Make capacity dynamic (adjust based on memory pressure), (2) Implement tiered cache with auto-leveling, (3) Add cache_capacity config option and CLI flag

### LSM Tree Unbounded Growth

**Area:** Metrics Storage
- **Issue:** `src/clio_agent/arc/lsm.py` writes all metrics indefinitely; no retention policy or auto-cleanup
- **Files:** `src/clio_agent/arc/lsm.py`
- **Current Limit:** Disk space; SSTables accumulate indefinitely
- **Scaling Path:** (1) Add retention policy (keep last N days of metrics), (2) Implement automatic cleanup of old SSTables, (3) Add archive tier (e.g., to IOWarp cold tier), (4) Provide admin tools for metrics export/cleanup

### BTree Index Memory Unbounded

**Area:** Conversation and Invocation Indexing
- **Issue:** `src/clio_agent/arc/index.py` BTreeIndex loads all keys into memory; no pruning or TTL
- **Files:** `src/clio_agent/arc/index.py`
- **Current Limit:** RAM; index grows linearly with total invocations
- **Scaling Path:** (1) Implement index TTL (discard old entries), (2) Add garbage collection for abandoned sessions, (3) Use external index (SQLite) if memory needed for other purposes

## Dependencies at Risk

### FastMCP Specification Mismatch (CRITICAL)

**Risk:** `pyproject.toml` specifies 2.13.0+ but code assumes 3.x API
- **Impact:** Installation may pull incompatible version; code breaks at runtime
- **Migration Plan:** (1) Update pyproject.toml to 3.0.0+, (2) Verify all Tool imports and patterns work with target version, (3) Add integration tests with pinned fastmcp version

### IOWarp CTE Backend Not Implemented

**Risk:** `src/clio_agent/arc/storage.py` has graceful fallback to local FS, but fallback path is untested
- **Impact:** If IOWarp unavailable, storage works but has no tier migration (runs in warm tier only)
- **Migration Plan:** (1) Test storage.py against file system in CI, (2) Verify graceful degradation path works end-to-end, (3) Add metrics to show when fallback is active

## Missing Critical Features

### Tool Schema Validation Missing

**Feature Gap:** No validation layer for MCP tool parameters
- **What's Missing:** Tool parameter schema enforcement, type coercion, bounds checking
- **Blocks:** Safe delegation of tool calls to untrusted/complex tools
- **Workaround:** Manual validation in each tool wrapper function (manual and repetitive)

### A2A Protocol Endpoint Incomplete

**Feature Gap:** Agent-to-Agent communication is stubbed
- **Files:** `src/clio_agent/registry/a2a_adapter.py`, `src/clio_agent/ui/a2a_endpoint.py`
- **What's Missing:** Real network protocol implementation, authentication, session routing across agents
- **Blocks:** Multi-agent coordination; calling remote CLIO instances from other services

### REST API Stub

**Feature Gap:** FastAPI server not implemented
- **Files:** `src/clio_agent/ui/api.py`
- **What's Missing:** HTTP endpoints for agent queries, conversation history, stats
- **Blocks:** Integration with web frontends, curl clients, third-party integrations

## Test Coverage Gaps

### MCP Connector Integration Tests Missing

**Untested Area:** IOWarp MCP tool invocation
- **What's Not Tested:** (1) Tool call execution end-to-end, (2) Error handling when servers unavailable, (3) Tool result parsing with various schemas, (4) Caching layer behavior
- **Files:** `src/clio_agent/tools/mcp_connector.py`
- **Risk:** Tool failures undetected until production; silent failures in result parsing
- **Priority:** HIGH (Phase 1 baseline critical path)

### ARC Memory Concurrent Access

**Untested Area:** Thread-safe access patterns
- **What's Not Tested:** (1) Simultaneous reads/writes from multiple threads, (2) Lock contention under load, (3) Cache eviction with concurrent operations
- **Files:** `src/clio_agent/arc/memory.py`, `src/clio_agent/arc/cache.py`
- **Risk:** Race conditions, data corruption in multi-threaded deployment
- **Priority:** HIGH (required for production reliability)

### Optimizer Evaluation Framework

**Untested Area:** Optimizer training and evaluation
- **What's Not Tested:** (1) SIMBA training convergence, (2) Metric statistical significance, (3) Variant deployment safety checks
- **Files:** `src/clio_agent/optimizers/*`
- **Risk:** Bad optimizations deployed; performance regression unnoticed
- **Priority:** MEDIUM (Phase 4, required before optimization enabled in production)

### LSM Tree Compaction Stress

**Untested Area:** High-write-rate metrics ingestion
- **What's Not Tested:** (1) Compaction behavior under 1000+ writes/sec, (2) Blocking/pausing on compaction, (3) Data loss on crash during compaction
- **Files:** `src/clio_agent/arc/lsm.py`
- **Risk:** Metrics loss under high load; unexpected latency spikes
- **Priority:** MEDIUM (required for scale testing)

---

*Concerns audit: 2026-02-09*
