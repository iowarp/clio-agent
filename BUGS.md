# ClaudIO v0.3.0 Bug Report (Post-Redesign)

**Date**: 2025-11-09
**Phase**: Post-redesign fresh bug hunt
**Status**: Research prototype (Alpha)
**Version**: 0.2.0 → 0.3.0

---

## Redesign Changes

### What Was Redesigned:
1. Main Agent: Now uses DSPy ReAct with subagents as tools (proper pattern)
2. FastMCP Bridge: Optimal async/sync with long-lived event loop + persistent clients
3. LSM Integration: Connected to ARCMemory.store_invocation()
4. Dependencies: All script headers updated to v0.3.0 specs

### Validation:
✓ Real LM Studio inference: 3711 char responses in 1-2s
✓ ReAct pattern working: 8-step trajectories
✓ ARC Memory: Conversations persisted
✓ LSM Tree: Metrics logged

---

## FRESH BUG HUNT (Parallel Agents)


## ARC SUBSYSTEM BUGS

### BUG 1: LearnedPattern Schema Mismatch in retrieval.py
File: src/claudio/arc/retrieval.py:141-148
Severity: CRITICAL
Details: retrieval.py creates LearnedPattern with fields (pattern_id, description, examples_seen, rule) but schema.py LearnedPattern only defines (pattern_type, pattern_data, confidence, learned_at). Four field names are completely wrong. This will fail msgspec serialization.
Impact: Context retrieval fails with AttributeError on LearnedPattern instantiation. retrieve_context_for_query() crashes when building Context objects.
---

### BUG 2: Type Annotation Bug in lsm.py Line 372
File: src/claudio/arc/lsm.py:372
Severity: CRITICAL
Details: Return type annotation uses `tuple[float, Dict[str, Any]]` (lowercase tuple) instead of `Tuple[float, Dict[str, Any]]` from typing module. Python 3.8/3.9 incompatible. Also inconsistent with rest of codebase which uses typing.Tuple.
Impact: Type checking fails in pre-3.10 Python environments. Runtime NameError on type hint evaluation.
---

### BUG 3: Race Condition in ARCMemory.get_conversation_history()
File: src/claudio/arc/memory.py:184-207
Severity: MEDIUM
Details: get_conversation_history() does not use self._lock when querying. get_conversation() is called without lock, but store_conversation() acquires lock. Conversation object can be modified by another thread between index update and retrieval.
Impact: Returns stale or corrupted conversation data under concurrent access. Index entry points to changed object.
---

### BUG 4: Missing Type Annotation on CoordinationPlan.created_at
File: src/claudio/arc/coordinator.py:98
Severity: MEDIUM
Details: created_at parameter is str type but no validation. Later uses datetime.now().isoformat() format. coordinator.py Line 554 stores as string but Invocation.started_at expects float Unix timestamp. Type mismatch on storage.
Impact: Invocation records created from coordination have wrong timestamp type. Breaks time-range queries in LSM tree.
---

### BUG 5: Timestamp Format Inconsistency in coordinator.py
File: src/claudio/arc/coordinator.py:507-508, 554
Severity: MEDIUM
Details: MultiAgentCoordinator._execute_task() stores started_at/completed_at as float (Unix timestamp) correctly, but _store_coordination_trace() stores plan.created_at as ISO string to Invocation.started_at field expecting float. Type mismatch will cause decode failures.
Impact: Coordination traces cannot be deserialized from disk. ARCMemory.get_invocation() fails with TypeError on timestamp field.
---

### BUG 6: Unsafe Dictionary Access in Coordinator
File: src/claudio/arc/coordinator.py:328, 278
Severity: MEDIUM
Details: Line 278 uses list(available_agents.keys())[0] with no bounds check. If available_agents is empty dict, IndexError. Line 292 has same issue.
Impact: Crashes when no agents registered or all agents filtered out. Potential denial of service.
---

### BUG 7: LSM Tree _load_sstables() Index Order Bug
File: src/claudio/arc/lsm.py:391
Severity: MEDIUM
Details: _load_sstables() loads files in reverse chronological order but _flush_memtable() adds to index with insert(0, ...). Ordering inconsistency: newest-first intention vs oldest-first load. Read logic assumes newest-first.
Impact: SSTable scan order wrong - reads old data before new. Stale data returned from LSM queries.
---

### BUG 8: Cache Lock Release Bug in ARCMemory.get_metrics()
File: src/claudio/arc/memory.py:389-405
Severity: MEDIUM
Details: When period=None, code acquires self._lock in lines 390-405 but doesn't consistently hold lock for all operations. Between glob() and read_bytes() in locked section, file could be deleted by another thread. But most critically, early return at line 405 doesn't properly exit lock context.
Impact: Deadlock risk. File not found errors race condition.
---

### BUG 9: Incorrect LSM Sorted Entry Extraction
File: src/claudio/arc/lsm.py:242-243
Severity: CRITICAL
Details: Line 242-243 tries to access self._memtable.keys()[0] and keys()[-1]. SortedDict.keys() returns a keysview, not indexable list. Will fail with TypeError: 'keysview' object is not subscriptable.
Impact: _flush_memtable() crashes immediately when MemTable is full. Metrics never flushed to disk.
---

### BUG 10: Timestamp Type Inconsistency in ARCMemory._parse_timestamp()
File: src/claudio/arc/memory.py:703-737
Severity: MEDIUM
Details: Method accepts float|str but Invocation schema defines started_at/completed_at as float. Coordinator passes ISO strings to _store_coordination_trace() at lines 554-555. _parse_timestamp not called on these values. Values stored directly as strings in Invocation.started_at field.
Impact: Invocation decode fails - schema expects float, gets str. Breaking change in serialization.
---

### BUG 11: Context.retrieved_docs Type Violation in ContextRetriever
File: src/claudio/arc/retrieval.py:132
Severity: MEDIUM
Details: Context.retrieved_docs is List[RetrievedDoc] (schema requires RetrievedDoc objects). Line 132 sets retrieved_docs=[] which is fine, but docstring says "Will be populated in future with RAG docs" - code never populates it correctly. retrieve_context_for_query() doesn't return RetrievedDoc objects per schema.
Impact: Retrieved docs always empty. Context.retrieved_docs cannot be used downstream.
---

### BUG 12: Missing Lock on LSM Tree Statistics in get_stats()
File: src/claudio/arc/lsm.py:438-450
Severity: MEDIUM
Details: get_stats() acquires self._lock but stats only for MemTable. SSTables list self._sstables read without lock. _compact_background() modifies self._sstables while get_stats() iterates over it (line 440). Race condition.
Impact: Stats calculation crashes with "list changed size during iteration". Concurrent access not thread-safe.
---

### BUG 13: IOWarp Storage Backend Timestamp Format Bug
File: src/claudio/arc/storage.py:422, 428, 479
Severity: MEDIUM
Details: Stores ISO 8601 timestamps with "Z" suffix (.isoformat().replace("+00:00", "Z")). Later _maybe_migrate_tiers() tries to parse with datetime.fromisoformat() which fails on custom "Z" format before Python 3.11. Line 479 replaces "Z" with "+00:00" but inconsistent with stored format.
Impact: Tier migration fails with ValueError on timestamp parsing. ISO 8601 format incompatibility.
---

### BUG 14: Index Memory Leak in ARCMemory
File: src/claudio/arc/memory.py:133-136, 244-246
Severity: MEDIUM
Details: store_conversation() and store_invocation() add to index but never purge old entries. B-tree index grows unbounded. No eviction policy. With long-running sessions, memory grows without limit.
Impact: Memory leak. Long-running ClaudIO instances consume unbounded RAM from index entries.
---

### BUG 15: Uncaught Exception in LSM Background Thread
File: src/claudio/arc/lsm.py:273-278
Severity: MEDIUM
Details: _compact_background() catches exceptions and prints to stdout but doesn't re-raise or log properly. Silent failures in background thread. Compaction silently fails while thread continues running.
Impact: Compaction stops working without visible error. LSM performance degrades silently.
---

### BUG 16: Context Domain Naming Bug in ContextRetriever
File: src/claudio/arc/retrieval.py:129
Severity: LOW
Details: retrieve_context_for_query() creates domain=f"query_context_{session_id}" but get_context() expects static domain like "hdf5_optimization". Each query gets unique domain. Context not reusable across queries.
Impact: Context caching ineffective. Every query creates new context domain, defeating cache-first design.
---

### BUG 17: Missing Thread Safety in Cache Clear Methods
File: src/claudio/arc/cache.py:147-165
Severity: MEDIUM
Details: clear() method has critical section (line 156-165) that doesn't hold lock for the entire operation. After releasing lock at line 159-160, _ttl is cleared but race condition exists where get() could still check expired entries.
Impact: Stale TTL entries accessed after clear(). Cache state inconsistent.
---

### BUG 18: LRUCache Access Order Bug Without lru-dict
File: src/claudio/arc/cache.py:89-91
Severity: MEDIUM
Details: In get() for manual LRU, code removes and re-appends key to _access_order. But if key not in _access_order (shouldn't happen but defensive), remove() throws ValueError. No bounds checking.
Impact: Cache crashes on edge case of corrupted _access_order state.
---

### BUG 19: Coordinator Invocation Timestamp Type Mismatch
File: src/claudio/arc/coordinator.py:500-517
Severity: CRITICAL
Details: Line 507-508 correctly uses time.time() (float), but line 554 stores plan.created_at (ISO string) to Invocation.started_at. Schema Invocation.started_at expects float but receives str. Deserialization fails.
Impact: All coordination invocations fail to deserialize from ARC storage.
---

### BUG 20: Missing Validation in LearnedPattern Usage
File: src/claudio/arc/retrieval.py:141-148
Severity: CRITICAL
Details: retrieval.py creates LearnedPattern(pattern_id=...) but schema.py LearnedPattern has NO pattern_id field. Schema fields are: pattern_type, pattern_data, confidence, learned_at. This is instantiation with invalid arguments.
Impact: msgspec.Struct validation fails. LearnedPattern constructor rejects all parameters. Runtime TypeError.
---


## TOOLS/MCP SUBSYSTEM BUGS

### BUG: Race condition in event loop initialization (IOWarpMCPConnector)
**File**: src/claudio/tools/mcp_connector.py:155-157
**Severity**: HIGH
**Details**: 
The `_start_event_loop()` method busy-waits with `time.sleep(0.001)` for loop initialization. This is inefficient but more critically, there's a race condition where `_loop` could be `None` if `call_tool()` is invoked before the loop thread sets it, even after the wait completes. No synchronization primitive ensures the loop is fully initialized and running.

```python
# UNSAFE: loop could still be None or not running
while self._loop is None:
    time.sleep(0.001)
```

**Impact**: Intermittent `RuntimeError: Event loop not initialized` on first tool calls, especially under load when multiple threads call `call_tool()` concurrently.

**Root Cause**: No `threading.Event` or `threading.Condition` to signal loop readiness. Busy-wait is unreliable.

---

### BUG: Event loop thread never sets exception handler
**File**: src/claudio/tools/mcp_connector.py:142-146
**Severity**: MEDIUM
**Details**: 
The event loop in `run_loop()` never sets an exception handler. If an exception occurs in a coroutine or callback, it logs a warning but doesn't propagate cleanly. This can silently mask failures in async operations.

```python
def run_loop():
    self._loop = asyncio.new_event_loop()
    asyncio.set_event_loop(self._loop)
    self._loop.run_forever()  # No exception handler set
```

**Impact**: Silent failures in tool calls due to unhandled exceptions in async context. Debugging becomes extremely difficult because errors don't propagate to sync `call_tool()`.

---

### BUG: Shutdown race condition (client context exit ordering)
**File**: src/claudio/tools/mcp_connector.py:543-550
**Severity**: HIGH
**Details**: 
In `shutdown()`, clients are exited via `__aexit__()` while the loop is still running. If the loop processes another task (or callback) during shutdown, a new client could be created via `_connect_server_async()` after `self._clients.clear()`, causing the client thread reference to leak. The clear operation doesn't happen atomically with context exit.

```python
for client in list(self._clients.values()):
    try:
        future = asyncio.run_coroutine_threadsafe(
            client.__aexit__(None, None, None),
            self._loop
        )
        future.result(timeout=5.0)
    except Exception:
        pass

# BUG: Loop still running, new client could be created here
self._clients.clear()
```

**Impact**: Resource leaks (unclosed FastMCP client connections, zombie subprocesses from `uvx iowarp-mcps` commands), especially when `shutdown()` is called with pending async tasks.

---

### BUG: `create_iowarp_tool_function()` creates new IOWarpMCPTools per function
**File**: src/claudio/tools/mcp_connector.py:731-740
**Severity**: CRITICAL
**Details**: 
Each tool function created by `create_iowarp_tool_function()` creates its own `IOWarpMCPTools` instance, which spawns its own event loop thread and clients. When `DataExpert` creates 10+ tool functions (line 170-189 in data_expert.py), this creates 10+ event loop threads and 10+ separate client connections to the same servers.

```python
def create_iowarp_tool_function(server_name: str, tool_name: str, arc_memory: Optional[Any] = None) -> Callable:
    tools = IOWarpMCPTools(arc_memory)  # NEW CONNECTOR EACH TIME!
    
    def tool_function(**kwargs):
        return tools.call_tool(server_name, tool_name, kwargs)
    
    return tool_function
```

Then in `DataExpert.__init__()` (lines 170-194):
```python
self.tools = [
    create_iowarp_tool_function("hdf5", "analyze_hdf5", arc_memory),      # Loop #1
    create_iowarp_tool_function("hdf5", "optimize_chunks", arc_memory),   # Loop #2
    create_iowarp_tool_function("hdf5", "check_compression", arc_memory), # Loop #3
    # ... 7 more loops ...
]
```

**Impact**: 
- Memory explosion: 10 event loop threads per agent instance
- File descriptor exhaustion: 10 subprocess connections per agent
- Performance degradation: Thread context switching overhead
- Resource leak: No cleanup mechanism if agent is deleted
- Violates RULE 5 (Performance Targets): Multiple loops = cache thrashing, >10ms latency per tool

---

### BUG: `mcp_wrapper.py` creates new event loop per call in non-daemon thread
**File**: src/claudio/tools/mcp_wrapper.py:216-222
**Severity**: HIGH
**Details**: 
The `call_tool()` function uses `asyncio.get_event_loop()` and `loop.run_until_complete()`. In Python 3.10+, `asyncio.get_event_loop()` is deprecated and will error in non-main threads. More critically, `asyncio.run()` in `is_server_available()` (line 248) creates a NEW event loop each time, then closes it—this doesn't integrate with the persistent event loop in `IOWarpMCPConnector`.

```python
# mcp_wrapper.py line 216-219 - UNSAFE
try:
    loop = asyncio.get_event_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
```

**AND**

```python
# mcp_wrapper.py line 248 - Creates/destroys loop each call
return asyncio.run(check())  # asyncio.run() creates NEW loop, closes after
```

**Impact**: 
- Deprecation warnings in Python 3.10+
- If called from thread without event loop, creates NEW loop (non-persistent)
- `is_server_available()` burns CPU creating/destroying event loops repeatedly
- Incompatible with `IOWarpMCPConnector`'s persistent loop design

---

### BUG: No timeout on `_connect_server_async()` or FastMCP context entry
**File**: src/claudio/tools/mcp_connector.py:283-338
**Severity**: MEDIUM
**Details**: 
The `await client.__aenter__()` call at line 336 has no timeout. If the MCP server is hung or slow to connect, the async operation blocks forever (up to the 60s timeout in `run_coroutine_threadsafe()` at line 430). But the client is added to `self._clients` before confirming it's actually ready.

```python
try:
    if config.url:
        client = Client(config.url)
    elif config.command:
        # ... build config ...
        client = Client(mcp_config)
    
    # NO TIMEOUT HERE - could hang for 60s
    await client.__aenter__()
    self._clients[server_name] = client  # Added even if __aenter__() fails
    return client
```

**Impact**: Tool calls hang silently if MCP server is unreachable, and failed clients pollute `self._clients` dict.

---

### BUG: Client lock never protects `_clients` dict modifications
**File**: src/claudio/tools/mcp_connector.py:123-124, 300-301, 337
**Severity**: CRITICAL
**Details**: 
A `_client_lock` is created but NEVER USED. The `_clients` dict is accessed without synchronization:
- Line 300: `if server_name in self._clients:` (read)
- Line 337: `self._clients[server_name] = client` (write)  
- Line 556: `self._clients.clear()` (write)

All via `run_coroutine_threadsafe()` into the event loop thread (async safe), but concurrent calls to `call_tool()` from multiple threads can race on checking/writing the dict.

```python
def __init__(self, ...):
    self._client_lock = threading.Lock()  # CREATED BUT NEVER USED
    self._clients: Dict[str, Client] = {}

async def _connect_server_async(self, server_name: str) -> Client:
    if server_name in self._clients:  # NO LOCK - race condition
        return self._clients[server_name]
    # ...
    self._clients[server_name] = client  # NO LOCK - race condition
```

**Impact**: 
- Concurrent requests could both try to connect same server simultaneously
- Dict mutation during iteration (if `shutdown()` runs while `call_tool()` enumerates)
- Potential KeyError or duplicate client instances

---

### BUG: Tool function closure captures arc_memory but IOWarpMCPTools creates own
**File**: src/claudio/tools/mcp_connector.py:731-735 + data_expert.py:172-173
**Severity**: MEDIUM
**Details**: 
In `create_iowarp_tool_function()`, the closure captures `arc_memory`, but each function creates a new `IOWarpMCPTools(arc_memory)` instance. The arc_memory is passed but caching is per-tool-instance, not shared across all tools of an agent.

```python
def create_iowarp_tool_function(server_name: str, tool_name: str, arc_memory: Optional[Any] = None):
    tools = IOWarpMCPTools(arc_memory)  # Separate ARC instance per function
```

Then in data_expert.py, 10 tool functions each have their own ARC connector, so cache hits don't propagate.

**Impact**: Cache misses increase, violating RULE 5 (> 85% hit rate target). Same tool called twice on same data hits disk both times.

---

### BUG: No cleanup of event loop thread if `__init__` fails
**File**: src/claudio/tools/mcp_connector.py:109-134
**Severity**: MEDIUM
**Details**: 
If an exception occurs in `_initialize_agent_toolkit_servers()` or `_load_config_file()` after `_start_event_loop()` is called, the thread is never stopped. The daemon thread will linger until process exit.

```python
def __init__(self, arc_memory: Optional[Any] = None, config_file: Optional[str] = None):
    self.arc = arc_memory
    self._loop: Optional[asyncio.AbstractEventLoop] = None
    self._loop_thread: Optional[threading.Thread] = None
    # ... other init ...
    
    if config_file:
        self._load_config_file(config_file)  # Could raise
    else:
        self._initialize_agent_toolkit_servers()  # Safe
    
    self._start_event_loop()  # Thread started, but if above fails, thread orphaned
```

**Impact**: Resource leaks in tests and error scenarios.

---

### BUG: `mcp_wrapper.MCPConfig.__post_init__` has mutable default argument
**File**: src/claudio/tools/mcp_wrapper.py:56-76
**Severity**: LOW
**Details**: 
Using `Dict[str, Dict[str, Any]] = None` with mutable default in `__post_init__` is a code smell. The `__post_init__` recreates the dict, so it's not a direct bug, but it's not Pythonic.

```python
@dataclass
class MCPConfig:
    servers: Dict[str, Dict[str, Any]] = None  # Should use field(default_factory=dict)
    
    def __post_init__(self):
        if self.servers is None:
            self.servers = {...}
```

**Impact**: Minor code smell, but not a functional bug since `__post_init__` handles it.

---

### BUG: No error propagation from event loop thread exceptions
**File**: src/claudio/tools/mcp_connector.py:142-146
**Severity**: MEDIUM
**Details**: 
If an exception occurs in the event loop thread (e.g., in `run_loop()`), it's silently ignored. The event loop just exits, and all subsequent `call_tool()` calls fail with a confusing "Event loop not initialized" error instead of the real root cause.

```python
def run_loop():
    self._loop = asyncio.new_event_loop()
    asyncio.set_event_loop(self._loop)
    self._loop.run_forever()  # If exception here, thread dies silently
```

**Impact**: Difficult debugging; root cause of loop failure is hidden.

---

### BUG: Timeout in `call_tool()` doesn't clean up the async task
**File**: src/claudio/tools/mcp_connector.py:424-430
**Severity**: MEDIUM
**Details**: 
If a tool call times out (60s), the `future.result(timeout=60.0)` raises `TimeoutError`, but the coroutine is still running in the event loop. It will continue executing and consuming resources.

```python
future = asyncio.run_coroutine_threadsafe(
    self._call_tool_async(server_name, tool_name, arguments),
    self._loop
)

return future.result(timeout=60.0)  # Timeout fires, but coroutine still running
```

**Impact**: Hanging MCP operations accumulate, eventually exhausting resources or blocking shutdown.

---

### BUG: ARC caching uses raw arguments dict as cache key
**File**: src/claudio/tools/mcp_connector.py:363-365, 378-384
**Severity**: MEDIUM
**Details**: 
The `arguments` dict is passed directly to `arc.get_cached_tool_result()` and `arc.cache_tool_result()`. If arguments contain unhashable types (dicts, lists) or are in different order, cache keys won't match even for identical calls.

```python
# Line 363
cached = self.arc.get_cached_tool_result(server_name, tool_name, arguments)

# Line 378-384
self.arc.cache_tool_result(
    server_name, tool_name, arguments,  # arguments as-is
    result, ttl_seconds=3600
)
```

**Impact**: Cache misses for identical tool calls with arguments in different order. Need deterministic key (e.g., sorted JSON string).

---

### BUG: DataExpert creates 10+ tool functions but doesn't clean up IOWarpMCPTools
**File**: src/claudio/experts/data_expert.py:165-200
**Severity**: CRITICAL
**Details**: 
`DataExpert.__init__()` creates 10+ `IOWarpMCPTools` instances (via `create_iowarp_tool_function()`), each with its own event loop. But `DataExpert` doesn't call `shutdown()` on any of them. The closures in the tool functions hold references, so `__del__` might fire, but there's no explicit cleanup.

```python
self.tools = [
    create_iowarp_tool_function("hdf5", "analyze_hdf5", arc_memory),
    # ... 9 more ...
]

# No cleanup on delete:
def __del__(self):
    # MISSING: for tool in self.tools: tool.connector.shutdown()
    pass
```

**Impact**: Resource leak; 10 event loop threads per `DataExpert` instance. In multi-agent scenarios (main orchestrator + 3-5 subagents), this is 30-50 threads for a single query.

---

### BUG: mcp_wrapper and mcp_connector are both loaded but conflict on event loops
**File**: src/claudio/tools/mcp_wrapper.py + src/claudio/tools/mcp_connector.py
**Severity**: HIGH
**Details**: 
The codebase has TWO independent MCP bridges:
1. `mcp_connector.py` - Persistent event loop, `IOWarpMCPTools`
2. `mcp_wrapper.py` - Transient loops per call, `call_tool()` function

Both are imported in different places. `DataExpert` uses `mcp_connector`, but if other code uses `mcp_wrapper`, there's no synchronization between the two event loops. They're entirely separate systems.

**Impact**: Confusion about which API to use. Tests may use one, production code another. Inconsistent caching, error handling, and resource management.

---


## MAIN AGENT BUGS

### BUG: LearnedPattern Constructor Arguments Mismatch in ContextRetriever
File: src/claudio/arc/retrieval.py:141-148
Severity: CRITICAL
Details: retrieval.py creates LearnedPattern with arguments (pattern_id, description, examples_seen, rule) but the schema in arc/schema.py defines LearnedPattern with fields (pattern_type, pattern_data, confidence, learned_at). All four arguments are wrong. msgspec.Struct will reject these invalid field names.
Impact: retrieve_context_for_query() crashes with TypeError: LearnedPattern() got unexpected keyword argument 'pattern_id' when building Context. ARC context retrieval completely broken.
---

### BUG: Context Extraction Accessing Non-Existent Fields
File: src/claudio/claudio.py:250-252
Severity: CRITICAL
Details: forward() tries to extract context with `p.pattern_data.get("topic", "")` and `if p.pattern_type == "frequent_topic"` but retrieval.py never sets pattern_type field, and pattern_data is created as dict with "rule" key, not "topic". The filter always returns empty list.
Impact: session_context always becomes "No prior context" on every query. Historical context never used to enrich main agent reasoning. Context-aware features broken.
---

### BUG: CLI Expects Non-Existent Fields from Main Agent
File: src/claudio/ui/cli.py:337-341
Severity: CRITICAL
Details: ask_question() tries to access result.selected_expert, result.routing_reasoning, result.expert_reasoning, result.tool_calls but ClaudIO.forward() returns dspy.Prediction with fields: answer, trajectory, session_id, duration_ms, arc_stats, lsm_stats. The expected fields don't exist.
Impact: Interactive CLI crashes immediately with AttributeError: 'dspy.Prediction' object has no attribute 'selected_expert' on first question.
---

### BUG: Non-Interactive CLI Mode Assumes Wrong Response Fields
File: src/claudio/ui/cli.py:495-500
Severity: CRITICAL
Details: Non-interactive mode (--query flag) tries to access result.selected_expert, result.confidence which don't exist in ClaudIO.forward() response. Line 495: "selected_expert": result.selected_expert fails.
Impact: CLI --query mode crashes before outputting JSON. Users can't use non-interactive mode: uv run src/claudio/ui/cli.py --query "How to optimize HDF5?"
---

### BUG: ReAct Signature Created as String Instead of Class
File: src/claudio/claudio.py:187-191
Severity: HIGH
Details: dspy.ReAct(signature="question, session_context -> answer", ...) uses string signature notation, not a proper dspy.Signature class. DSPy ReAct expects class-based signatures for proper field validation and type hints.
Impact: DSPy may not properly parse inputs/outputs, reducing effectiveness of ReAct reasoning. Tool calling mechanism may not work correctly.
---

### BUG: Tool Function Defined Inside __init__ Creates Closure Issues
File: src/claudio/claudio.py:166-179
Severity: MEDIUM
Details: ask_data_expert() is defined as a nested function inside __init__ and captures self. DSPy tool functions should be module-level or have proper introspection support. The closure also makes it difficult for DSPy to inspect the function signature.
Impact: Tool calling in ReAct may fail or not recognize the tool properly. Trajectory generation incomplete.
---

### BUG: ask_data_expert Tool Not Passing Required History Parameter
File: src/claudio/claudio.py:176
Severity: MEDIUM
Details: ask_data_expert() calls self.data_expert(question=question, file_context=file_context) but DataExpert.forward() signature requires history parameter. DataExpertSignature defines history as required InputField without default.
Impact: DataExpert.forward() fails with TypeError: missing required positional argument 'history'. Tool invocation crashes.
---

### BUG: ask_data_expert Doesn't Return Structured Data for Trajectory
File: src/claudio/claudio.py:176-179
Severity: MEDIUM
Details: ask_data_expert() returns simple string but ReAct expects tool functions to return data that can be parsed into trajectory objects. The tool call is invisible to trajectory tracking.
Impact: result.trajectory never contains DataExpert tool calls. ReAct reasoning trace incomplete.
---

### BUG: Session Context Building on First Query
File: src/claudio/claudio.py:240-252
Severity: MEDIUM
Details: retrieve_context_for_query() is called with current query before first response is stored. arc.get_conversation(session_id) returns None on first invocation (no conversation yet), so retrieved_docs stays empty and learned_patterns stays empty.
Impact: First query has no context (always "No prior context"). Context only available from second query onward.
---

### BUG: Missing Main Agent Signature Class Definition
File: src/claudio/claudio.py:187
Severity: MEDIUM
Details: ReAct is given a string signature, not a dspy.Signature class like DataExpertSignature in signatures/expert_sig.py. No main agent signature defined. Should create MainAgentSignature with proper input/output fields.
Impact: ReAct agent may not understand required output fields or validate inputs correctly.
---

### BUG: DataExpert Forward Signature Mismatch
File: src/claudio/experts/data_expert.py:206-228
Severity: MEDIUM
Details: DataExpert.forward() has parameters (question, file_context, history=None) but DataExpertSignature defines history without default. When called from ask_data_expert() without history, dspy.History(messages=[]) is created but may not match schema expectations.
Impact: Potential type mismatch between provided History object and expected format in signature.
---

### BUG: No Validation of ARC Memory Retrieval Results
File: src/claudio/claudio.py:240-244
Severity: MEDIUM
Details: retrieve_context_for_query() can fail (due to LearnedPattern bug) but code has no try-catch. If arc_context is malformed, key_topics extraction fails silently or crashes.
Impact: ReAct agent gets None or malformed session_context. Reasoning may fail.
---

### BUG: Tool Trajectory Extraction Doesn't Handle All ReAct Formats
File: src/claudio/claudio.py:297-308
Severity: MEDIUM
Details: Code extracts tool_calls from trajectory assuming dict format, but ReAct may return different formats. If trajectory steps aren't dicts, code fails or skips them.
Impact: ToolCall objects not correctly populated. Metrics missing from stored invocations.
---

### BUG: Main Agent Tier Misconfiguration
File: src/claudio/claudio.py:315
Severity: LOW
Details: Invocation tier is hardcoded to 1 (Main), but registry.register_agent() at line 154-163 doesn't record this. Main agent not tracked in registry for discovery/stats.
Impact: Registry queries for "main" agent fail. Main agent metrics not aggregated.
---


## REGISTRY SUBSYSTEM BUGS

### BUG: ZeroDivisionError in route_query() when priority=0
File: src/claudio/registry/registry.py:292
Severity: CRITICAL
Details: When calculating routing scores, code divides by caps.priority without validating priority > 0. If any registered agent has priority=0, this crashes with ZeroDivisionError. Priority field has no minimum value constraint in AgentCapability dataclass.
Impact: registry.route_query() crashes on first call if any agent has priority=0. Routing system becomes entirely non-functional.
---

### BUG: Thread Safety Race Condition in route_query() fallback logic
File: src/claudio/registry/registry.py:295-303
Severity: MEDIUM
Details: After checking "if not agent_scores", the code assumes self._agents is still non-empty on line 297 ("first_agent = list(self._agents.keys())[0]"). However, another thread could unregister all agents between the check at line 271 and line 297. Both are inside the lock, but the lock is released after line 303. Race condition window exists if agent_scores check passes but agents dict emptied before line 297 tries to access it.
Impact: IndexError when accessing empty dict keys. Though technically protected by outer lock, the logic is fragile and depends on unstated assumptions.
---

### BUG: Shallow Copy Vulnerability in get_all_capabilities()
File: src/claudio/registry/registry.py:339
Severity: HIGH
Details: get_all_capabilities() returns self._capabilities.copy() - a shallow copy. Since AgentCapability is a mutable dataclass with mutable fields (keywords: List[str], metadata: Dict), callers can modify the returned capabilities dict and corrupt the original metadata dicts. The List and Dict objects are shared references.
Impact: External code can corrupt capability metadata by modifying returned dict values. Agent priority, keywords, and metadata can be altered without using register_agent() or unregister_agent(). Cache invalidation is bypassed.
---

### BUG: LangChain A2A Adapter loses request.agent_id in conversion
File: src/claudio/registry/a2a_adapter.py:152-175
Severity: HIGH
Details: LangChainAdapter.convert_request() creates lc_request dict with "input" and "chat_history" but never includes "agent_id" from A2ARequest. The agent_id is part of A2ARequest but gets dropped during conversion. LangChain agent can't identify which ClaudIO agent sent the request.
Impact: External LangChain agents lose context about which ClaudIO agent initiated the call. Breaks session tracking and agent attribution in bidirectional communication.
---

### BUG: A2A Response Converters don't preserve session_id in metadata
File: src/claudio/registry/a2a_adapter.py:177-208, 255-286, 334-362
Severity: MEDIUM
Details: All three adapters (LangChain, CrewAI, AutoGen) discard session_id from the A2AResponse. The original A2ARequest contains session_id for conversation tracking, but convert_response() methods don't include it in response.metadata. It's lost in the round-trip.
Impact: Cannot link responses back to original conversation sessions. ARC Memory can't track external agent responses to specific sessions because metadata is incomplete.
---

### BUG: ExternalAgentWrapper.forward() sends mock response instead of real call
File: src/claudio/registry/external_compiler.py:122-129
Severity: CRITICAL
Details: ExternalAgentWrapper.forward() calls protocol_handler.send_request() with hardcoded mock external_response: {"output": f"[Simulated {self.framework} response] Processed: {question}", "agent_id": self.agent_id}. The wrapper never actually invokes the external framework. It just echoes input back with a "[Simulated]" prefix.
Impact: External agents (LangChain, CrewAI, AutoGen) are completely non-functional. All A2A protocol integration is fake. System appears to work but produces synthesized responses instead of actual framework output.
---

### BUG: extract_capabilities() hardcodes priority=7 ignoring definition metadata
File: src/claudio/registry/external_compiler.py:248
Severity: MEDIUM
Details: extract_capabilities() always creates AgentCapability with priority=7 (line 248), regardless of any priority specified in ExternalAgentDefinition or its config dict. External agent priorities are hardcoded and unmutable per-definition.
Impact: Cannot set custom routing priority for specific external agents. All external agents are treated as lower priority (7) than internal agents (default 5) regardless of capability.
---

### BUG: A2AProtocolHandler.register_adapter() has no thread safety
File: src/claudio/registry/a2a_adapter.py:463-485
Severity: MEDIUM
Details: A2AProtocolHandler.__init__() initializes _adapters dict, but register_adapter() directly assigns to self._adapters[framework] without locks. If multiple threads call register_adapter() concurrently, or if send_request() is called while register_adapter() is executing, there's a TOCTOU (Time-of-Check-Time-of-Use) race condition on _adapters dict access.
Impact: Concurrent adapter registration can corrupt _adapters dict or cause KeyError in send_request() if adapter is unregistered during iteration.
---

### BUG: CapabilityMatcher.match_query() fails silently on empty query
File: src/claudio/registry/capability_matcher.py:119-121
Severity: LOW
Details: If query contains only stopwords (e.g., "the is a"), extract_keywords() returns empty list, and match_query() returns [] (empty list). No error or warning is raised. Caller gets silent failure - empty match list looks the same as "no agents match".
Impact: Router can't distinguish between "query had no content" and "no agents matched query". Makes debugging routing failures harder.
---

### BUG: find_agents_by_keyword() doesn't check if keyword is empty string
File: src/claudio/registry/registry.py:214-245
Severity: LOW
Details: find_agents_by_keyword(keyword: str) doesn't validate that keyword is non-empty before searching. If keyword="", the expression "keyword_lower in cap_keyword.lower()" will always be True (empty string is substring of all strings), matching all agents.
Impact: Calling registry.find_agents_by_keyword("") returns all registered agents. Can cause unexpected routing if empty strings are passed programmatically.
---

### BUG: A2ARequest.from_dict() doesn't validate required fields
File: src/claudio/registry/a2a_adapter.py:53-63
Severity: MEDIUM
Details: A2ARequest.from_dict(data) uses **data to initialize dataclass without checking that required fields (agent_id, query, context, session_id) exist in data. If data is missing any required field, TypeError is raised instead of ValueError with helpful message.
Impact: Poor error messages when constructing requests from untrusted dictionaries (API payloads, deserialized messages).
---

### BUG: ExternalAgentDefinition.config dict is mutable and shared
File: src/claudio/registry/external_compiler.py:61
Severity: MEDIUM
Details: ExternalAgentDefinition has config: Dict[str, Any] = field(default_factory=dict), but when multiple definitions share the same config reference through module-level constants or copy operations, mutations in one definition affect others. The default_factory=dict prevents default sharing but doesn't protect against explicit sharing.
Impact: If config dict is shared between definitions (intentionally or accidentally), modifying one agent's config affects all references.
---

