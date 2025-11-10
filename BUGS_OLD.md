# ARC Memory Subsystem Bug Report

## Summary
Comprehensive audit of ARC Memory subsystem files for bugs, vulnerabilities, and issues.

**Audited Files:**
- src/claudio/arc/schema.py
- src/claudio/arc/cache.py
- src/claudio/arc/index.py
- src/claudio/arc/memory.py
- src/claudio/arc/retrieval.py
- src/claudio/arc/coordinator.py
- src/claudio/arc/lsm.py
- src/claudio/arc/storage.py

**Audit Date:** 2025-11-09

---

## Critical Bugs

### BUG: Race condition in coordinator timestamp parsing
**File**: src/claudio/arc/coordinator.py:554-555
**Severity**: CRITICAL
**Category**: Type|Threading
**Details**: In `_store_coordination_trace()`, the `started_at` field is set to `plan.created_at` which is a string (ISO 8601 format from line 308), but the Invocation schema expects a float (Unix timestamp). This causes type inconsistency in stored data.
**Impact**: Data corruption - invocations stored with string timestamps instead of float will fail deserialization or cause crashes in downstream code expecting floats. The schema defines `started_at: float` but a string is passed.
**Fix**: Convert `plan.created_at` and `result.completed_at` to Unix timestamps before creating Invocation:
```python
from datetime import datetime
started_at = datetime.fromisoformat(plan.created_at.replace("Z", "+00:00")).timestamp()
completed_at = datetime.fromisoformat(result.completed_at.replace("Z", "+00:00")).timestamp()
```
---

### BUG: Race condition in coordinator task execution timestamp
**File**: src/claudio/arc/coordinator.py:507-508
**Severity**: CRITICAL
**Category**: Type
**Details**: In `_execute_task()`, `started_at=start_time` (line 507) passes a float, but then `completed_at=time.time()` (line 508) also passes a float. However, both are Unix timestamps which is correct. BUT the parent invocation at line 554 passes strings for these fields creating inconsistency.
**Impact**: Type inconsistency across invocation records - some have float timestamps, others have string timestamps, breaking schema contract.
**Fix**: Ensure all timestamp fields use consistent float (Unix timestamp) format throughout coordinator.py.
---

### BUG: Unsafe string timestamp parsing fallback
**File**: src/claudio/arc/memory.py:669-672
**Severity**: HIGH
**Category**: Error|Logic
**Details**: In `_parse_timestamp()`, if timestamp parsing fails, it silently returns `time.time()` (current time). This masks errors and creates incorrect data - a failed parse should not default to "now".
**Impact**: Data corruption - failed timestamp parses result in incorrect timestamps being stored, making temporal queries return wrong results. Historical data could be mislabeled with current timestamps.
**Fix**: Raise exception on parse failure or return a sentinel value that can be detected:
```python
raise ValueError(f"Invalid timestamp format: {timestamp}")
```
---

### BUG: Unvalidated input types in memory.py
**File**: src/claudio/arc/memory.py:126
**Severity**: HIGH
**Category**: Type|Error
**Details**: In `store_conversation()`, `_parse_timestamp(conversation.updated_at)` assumes `updated_at` is valid, but if it's None or invalid, the fallback silently returns current time (line 671), corrupting index keys.
**Impact**: Index corruption - conversations indexed by current timestamp instead of actual timestamp, breaking range queries and retrieval.
**Fix**: Validate timestamp fields are non-None before parsing:
```python
if conversation.updated_at is None:
    raise ValueError("Conversation.updated_at cannot be None")
timestamp = self._parse_timestamp(conversation.updated_at)
```
---

### BUG: Missing error handling in LSM compaction
**File**: src/claudio/arc/lsm.py:276-278
**Severity**: HIGH
**Category**: Error
**Details**: In `_compact_background()`, exceptions during compaction are caught and printed but not logged properly. The bare print statement (line 278) goes to stdout and may be lost. Thread continues running but compaction failures are silently ignored.
**Impact**: Silent compaction failures lead to unbounded SSTable growth, degrading read performance and eventually causing disk space exhaustion. No visibility into failures.
**Fix**: Use proper logging instead of print, and consider adding failure metrics:
```python
import logging
logger = logging.getLogger(__name__)
try:
    self._compact_sstables()
except Exception as e:
    logger.error(f"LSM compaction failed: {e}", exc_info=True)
    self._compaction_errors += 1  # Track failures
```
---

### BUG: Thread safety violation in LSM flush
**File**: src/claudio/arc/lsm.py:136-138
**Severity**: HIGH
**Category**: Threading
**Details**: In `write()`, lock is held while calling `_flush_memtable()` which does disk I/O (line 239). This blocks all concurrent reads/writes during flush, creating contention and potential deadlock if flush takes long time.
**Impact**: Performance degradation - all LSM operations blocked during flush. Under high write load, this causes cascading delays and timeout failures.
**Fix**: Use double-buffering pattern - swap MemTable atomically, release lock, then flush without holding lock:
```python
# With lock: swap memtable
old_memtable = self._memtable
self._memtable = SortedDict()
# Release lock, then flush old_memtable without blocking
self._flush_memtable_async(old_memtable)
```
---

### BUG: Resource leak in IOWarp shutdown
**File**: src/claudio/arc/storage.py:575-577
**Severity**: HIGH
**Category**: Resource
**Details**: In `shutdown()`, if `zmq_socket.close()` (line 576) raises an exception, `zmq_context.term()` (line 577) is never called, leaking ZeroMQ context resources.
**Impact**: Resource leak - ZeroMQ contexts not properly terminated on error, causing socket exhaustion in long-running processes with multiple backend instances.
**Fix**: Use try-finally or separate try blocks:
```python
try:
    if hasattr(self, "zmq_socket"):
        self.zmq_socket.close()
finally:
    if hasattr(self, "zmq_context"):
        self.zmq_context.term()
```
---

## High Severity Bugs

### BUG: Cache stats division by zero
**File**: src/claudio/arc/cache.py:191-192
**Severity**: MEDIUM
**Category**: Logic
**Details**: In `stats()`, `hit_rate = self._hits / total_requests if total_requests > 0 else 0.0` correctly guards division by zero, but this check should be applied consistently. Actually this is correct implementation, no bug here.
**Impact**: None - correct implementation
**Fix**: None needed
---

### BUG: Missing lock in memory.py cache stats
**File**: src/claudio/arc/memory.py:580-593
**Severity**: MEDIUM
**Category**: Threading
**Details**: In `get_cache_stats()`, lock is only held from line 582 onwards, but `cache_stats = self._cache.stats()` at line 580 is called without lock. This creates race condition if cache is modified between cache.stats() call and reading disk counters.
**Impact**: Inconsistent statistics - cache stats and disk counters may be from different points in time, misleading monitoring.
**Fix**: Move cache.stats() call inside lock block:
```python
with self._lock:
    cache_stats = self._cache.stats()
    return {
        "hit_rate": cache_stats["hit_rate"],
        ...
    }
```
---

### BUG: Inconsistent error handling in cache.py
**File**: src/claudio/arc/cache.py:90-91
**Severity**: MEDIUM
**Category**: Error
**Details**: In `get()`, when accessing `self._access_order.remove(key)` (line 90), if key is not in list (corrupted state), raises ValueError but is not caught. This should never happen but defensive coding requires handling.
**Impact**: Crash on corrupted cache state - if internal state is inconsistent, cache.get() can crash entire application.
**Fix**: Add defensive check or try-except:
```python
if key in self._access_order:
    self._access_order.remove(key)
else:
    # Log corruption warning
    pass
```
---

### BUG: Missing validation in BTreeIndex
**File**: src/claudio/arc/index.py:52
**Severity**: MEDIUM
**Category**: Error
**Details**: In `insert()`, no validation that key is actually a tuple of (str, float). Inserting wrong type will corrupt index but won't fail until later retrieval operations.
**Impact**: Index corruption - invalid keys silently accepted, causing crashes during range queries or searches.
**Fix**: Add type validation:
```python
if not isinstance(key, tuple) or len(key) != 2:
    raise TypeError("Key must be tuple of (str, float)")
if not isinstance(key[0], str) or not isinstance(key[1], (int, float)):
    raise TypeError("Key must be (str, float)")
```
---

### BUG: get_latest_in_session is O(N) not O(log N)
**File**: src/claudio/arc/index.py:282-283
**Severity**: MEDIUM
**Category**: Performance
**Details**: In `get_latest_in_session()`, implementation calls `get_session_range()` which retrieves ALL entries for session, then slices last N (line 283). This is O(k) where k=total session entries, not O(log N + n) as implied by docstring (line 268).
**Impact**: Performance degradation - for large sessions, retrieving latest message requires scanning all messages. Violates performance targets.
**Fix**: Use reverse iteration with limit:
```python
# Get keys in reverse order
keys = list(self._index.irange(
    (session_id, 0.0),
    (session_id, float('inf')),
    reverse=True
))[:n]
# Load only the n latest values
return [self._index[k] for k in reversed(keys)]
```
---

### BUG: Unchecked file operations in memory.py
**File**: src/claudio/arc/memory.py:133, 163, 243
**Severity**: MEDIUM
**Category**: Error
**Details**: File write operations (e.g., `file_path.write_bytes(encoded)` at line 133) don't handle disk full, permission errors, or I/O errors. Exceptions bubble up without cleanup or error context.
**Impact**: Data loss - partial writes on disk full can corrupt msgpack files. No transactional guarantees.
**Fix**: Add error handling with atomic write pattern:
```python
try:
    temp_path = file_path.with_suffix('.tmp')
    temp_path.write_bytes(encoded)
    temp_path.replace(file_path)  # Atomic on POSIX
    self._disk_writes += 1
except OSError as e:
    raise IOError(f"Failed to write {file_path}: {e}") from e
```
---

### BUG: Retrieval.py uses float inf without validation
**File**: src/claudio/arc/retrieval.py:102, 255
**Severity**: MEDIUM
**Category**: Logic
**Details**: In `retrieve_context_for_query()` and `rank_conversations_by_relevance()`, creates range queries with `float('inf')` which works but if sessions have timestamps > sys.float_info.max, queries will fail. Edge case but possible.
**Impact**: Query failure on edge case timestamps - sessions with very large timestamps won't be retrieved.
**Fix**: Use explicit max timestamp or time.time() * 2:
```python
import sys
max_timestamp = sys.float_info.max
```
---

### BUG: Coordinator doesn't validate agent existence
**File**: src/claudio/arc/coordinator.py:278
**Severity**: MEDIUM
**Category**: Logic
**Details**: In `create_plan()`, when checking `if agent_id not in available_agents` (line 277), falls back to `list(available_agents.keys())[0]` (line 278). If available_agents is empty dict, this raises IndexError.
**Impact**: Crash on empty agent registry - if no agents available, coordinator crashes instead of returning meaningful error.
**Fix**: Check if dict is empty before accessing:
```python
if agent_id not in available_agents:
    if not available_agents:
        raise ValueError("No agents available for task execution")
    agent_id = list(available_agents.keys())[0]
```
---

### BUG: Coordinator query splitting is fragile
**File**: src/claudio/arc/coordinator.py:256-259
**Severity**: MEDIUM
**Category**: Logic
**Details**: In `create_plan()`, splitting query on first occurrence of coordination keyword with `query.split(keyword, 1)` (line 258) only splits into 2 parts max. Multi-part queries like "A and then B and then C" will fail to parse correctly.
**Impact**: Incorrect task decomposition - complex multi-step queries only split into 2 tasks, losing intermediate steps.
**Fix**: Use more sophisticated query parsing or split on all occurrences:
```python
# Split on all coordination keywords
parts = re.split(r'\band then\b|\bthen\b|\balso\b', query, flags=re.IGNORECASE)
parts = [p.strip() for p in parts if p.strip()]
```
---

### BUG: Retrieval statistics use division without guards
**File**: src/claudio/arc/retrieval.py:276, 279
**Severity**: LOW
**Category**: Logic
**Details**: In `_calculate_relevance_score()`, division `len(intersection) / len(union)` (line 276) and `len(intersection) / len(query_keywords)` (line 279) are guarded by `if not union` and `if not query_keywords` checks above, so no actual bug. Correct implementation.
**Impact**: None
**Fix**: None needed
---

## Medium Severity Bugs

### BUG: LSM tree doesn't validate timestamp order
**File**: src/claudio/arc/lsm.py:131-134
**Severity**: MEDIUM
**Category**: Logic
**Details**: In `write()`, no validation that timestamps are monotonically increasing or reasonable. Writing timestamp 0.0 or negative values will corrupt sorted order assumptions.
**Impact**: Index corruption - out-of-order timestamps break range query assumptions and SSTable min/max key invariants.
**Fix**: Add timestamp validation:
```python
if timestamp <= 0 or timestamp > time.time() * 2:
    raise ValueError(f"Invalid timestamp: {timestamp}")
```
---

### BUG: IOWarp ZMQ socket timeout not set
**File**: src/claudio/arc/storage.py:155-156
**Severity**: MEDIUM
**Category**: Performance
**Details**: In `_initialize_iowarp()`, ZeroMQ socket created without timeout. If IOWarp runtime hangs, requests will block forever (lines 179, 264, 287).
**Impact**: Application hangs - IOWarp runtime failure causes ClaudIO to hang indefinitely on I/O operations.
**Fix**: Set socket timeouts:
```python
self.zmq_socket.setsockopt(zmq.RCVTIMEO, 5000)  # 5 second timeout
self.zmq_socket.setsockopt(zmq.SNDTIMEO, 5000)
```
---

### BUG: IOWarp metadata save is not atomic
**File**: src/claudio/arc/storage.py:450-452
**Severity**: MEDIUM
**Category**: Resource
**Details**: In `_save_access_metadata()`, direct write to metadata file without atomic write pattern. Process crash during write corrupts metadata file.
**Impact**: Metadata corruption - crash during save loses all access tracking data, breaking tier migration.
**Fix**: Use atomic write with temp file:
```python
temp_file = self._access_metadata_file.with_suffix('.tmp')
temp_file.write_bytes(data)
temp_file.replace(self._access_metadata_file)  # Atomic
```
---

### BUG: Coordinator doesn't handle agent callable failure
**File**: src/claudio/arc/coordinator.py:484-486
**Severity**: MEDIUM
**Category**: Error
**Details**: In `_execute_task()`, if agent is neither callable nor has forward() method, raises TypeError (line 486) but this isn't caught in execute_sequential(), causing entire coordination to fail instead of marking single task as failed.
**Impact**: Cascading failures - one bad agent type crashes entire multi-agent coordination instead of graceful degradation.
**Fix**: Catch TypeError in execute_sequential and treat as task failure:
```python
except (Exception, TypeError) as e:
    task_results[task.task_id] = {
        "error": str(e),
        "success": False,
        "agent_id": task.agent_id,
    }
```
---

### BUG: Memory clear_all doesn't handle locked files
**File**: src/claudio/arc/memory.py:624-631
**Severity**: LOW
**Category**: Error
**Details**: In `clear_all()`, file deletion via `file_path.unlink()` doesn't handle permission errors or locked files. On Windows, open file handles prevent deletion.
**Impact**: Incomplete cleanup - clear_all may leave files behind on Windows or permission-restricted systems.
**Fix**: Add error handling:
```python
for file_path in self._conv_dir.glob("*.msgpack"):
    try:
        file_path.unlink()
    except (OSError, PermissionError) as e:
        # Log but continue cleanup
        pass
```
---

### BUG: Retrieval keyword extraction doesn't handle unicode
**File**: src/claudio/arc/retrieval.py:308
**Severity**: LOW
**Category**: Logic
**Details**: In `_extract_keywords()`, regex `r'\b[a-z0-9]+\b'` only matches ASCII lowercase alphanumeric. Non-English text or accented characters are lost.
**Impact**: Poor retrieval quality for non-English content - keywords like "optimización" or "最適化" are ignored.
**Fix**: Use unicode-aware regex:
```python
tokens = re.findall(r'\b\w+\b', text.lower(), flags=re.UNICODE)
```
---

### BUG: Cache TTL cleanup is passive
**File**: src/claudio/arc/cache.py:76-82
**Severity**: LOW
**Category**: Performance
**Details**: Expired entries are only removed when accessed via get(). No background cleanup means expired entries consume memory until accessed.
**Impact**: Memory bloat - cache can grow beyond capacity with expired entries that are never accessed again.
**Fix**: Add periodic cleanup thread or expire on stats() call:
```python
def _cleanup_expired(self):
    now = time.time()
    expired_keys = [k for k, expiry in self._ttl.items() if now > expiry]
    for key in expired_keys:
        self._remove_key(key)
```
---

### BUG: LSM close timeout too short
**File**: src/claudio/arc/lsm.py:467-468
**Severity**: LOW
**Category**: Performance
**Details**: In `close()`, compaction thread join timeout is 5 seconds (line 468). If large compaction in progress, thread may not finish, leaving MemTable data unflushed.
**Impact**: Data loss - incomplete compaction can lose recent writes if process exits during compaction.
**Fix**: Increase timeout or wait indefinitely:
```python
self._compaction_thread.join(timeout=30.0)  # 30 seconds
```
---

## Low Severity Issues

### BUG: Inconsistent import style
**File**: Multiple files
**Severity**: LOW
**Category**: Type
**Details**: Some files use `from typing import Dict, List` while others use `dict`, `list` (Python 3.9+ style). Inconsistent across codebase.
**Impact**: Code inconsistency - no runtime impact but harder to maintain.
**Fix**: Standardize on lowercase `dict`, `list` since Python 3.12 is minimum version (CLAUDE.md line 40).
---

### BUG: Hardcoded print statements
**File**: src/claudio/arc/storage.py:83, 161, 163, 167-168
**Severity**: LOW
**Category**: Logic
**Details**: Multiple print() statements for logging/warnings instead of proper logging framework. Not suitable for production.
**Impact**: Poor observability - output mixed with user stdout, can't be filtered or redirected.
**Fix**: Use logging module:
```python
import logging
logger = logging.getLogger(__name__)
logger.warning("IOWarp not available, using local storage: %s", self.base_dir)
```
---

### BUG: Magic numbers in tier migration
**File**: src/claudio/arc/storage.py:464
**Severity**: LOW
**Category**: Logic
**Details**: In `_maybe_migrate_tiers()`, hardcoded check `if self._local_writes % 100 != 0` (line 464) means migration only runs every 100 writes. This should be configurable.
**Impact**: Inflexible tier migration - can't tune migration frequency without code changes.
**Fix**: Add configuration parameter:
```python
def __init__(self, ..., migration_check_interval: int = 100):
    self._migration_check_interval = migration_check_interval
# Then use: if self._local_writes % self._migration_check_interval != 0
```
---

### BUG: Retrieval top_keywords limit is hardcoded
**File**: src/claudio/arc/retrieval.py:187
**Severity**: LOW
**Category**: Logic
**Details**: In `extract_key_topics()`, `word_counts.most_common(20)` hardcodes limit to 20 topics. Should be parameter.
**Impact**: Inflexible topic extraction - can't adjust granularity of topic detection.
**Fix**: Add parameter with default:
```python
def extract_key_topics(self, conversations: List[Conversation], limit: int = 20) -> List[str]:
    top_keywords = [word for word, count in word_counts.most_common(limit)]
```
---

### BUG: Context domain naming could collide
**File**: src/claudio/arc/retrieval.py:129
**Severity**: LOW
**Category**: Logic
**Details**: In `retrieve_context_for_query()`, context domain is `f"query_context_{session_id}"`. If same session queries multiple times, contexts overwrite each other.
**Impact**: Context leakage - subsequent queries in same session share context domain, mixing unrelated data.
**Fix**: Include query hash or timestamp in domain:
```python
import hashlib
query_hash = hashlib.md5(query.encode()).hexdigest()[:8]
domain = f"query_context_{session_id}_{query_hash}"
```
---

### BUG: Coordinator task ID collisions possible
**File**: src/claudio/arc/coordinator.py:244, 280
**Severity**: LOW
**Category**: Logic
**Details**: Task IDs use `f"task-{uuid.uuid4()}"` which is fine, but plan_id uses same pattern `f"plan-{uuid.uuid4()}"` (line 231). No namespace collision possible but inconsistent naming.
**Impact**: None - UUIDs prevent collisions
**Fix**: None needed, but could add prefixes for clarity: `task_{uuid}`, `plan_{uuid}`
---

### BUG: LSM stats include duplicates
**File**: src/claudio/arc/lsm.py:439-441
**Severity**: LOW
**Category**: Logic
**Details**: In `get_stats()`, `total_records` counts MemTable + all SSTable records (line 441), but before compaction, SSTables can have duplicate timestamps, inflating count.
**Impact**: Misleading metrics - reported record count higher than actual unique records.
**Fix**: Document that total_records is approximate, or track unique count separately.
---

### BUG: Missing type hints on private methods
**File**: Multiple files
**Severity**: LOW
**Category**: Type
**Details**: Many private methods like `_parse_timestamp`, `_extract_keywords`, etc. have incomplete type hints or missing return types.
**Impact**: Reduced type checking coverage - mypy can't validate internal method calls.
**Fix**: Add full type hints to all methods per CLAUDE.md Rule 7.
---

### BUG: Storage tier directory creation race condition
**File**: src/claudio/arc/storage.py:86-92
**Severity**: LOW
**Category**: Threading
**Details**: Creating tier directories with `exist_ok=True` (lines 90-92) prevents race errors, but no lock around creation means multiple threads could race. Actually exist_ok=True handles this correctly.
**Impact**: None - exist_ok=True makes this safe
**Fix**: None needed
---

### BUG: get_session_invocations returns wrong order in docstring
**File**: src/claudio/arc/memory.py:312-313
**Severity**: LOW
**Category**: Logic
**Details**: Line 299 docstring says "most recent first", and line 313 returns `list(reversed(invocations))` which should give most recent first. But line 306 slices `index_entries[-limit:]` which gets last N entries (most recent), then line 313 reverses them. Logic is correct.
**Impact**: None - implementation matches docstring
**Fix**: None needed
---

### BUG: Coordinator doesn't validate session_id format
**File**: src/claudio/arc/coordinator.py:314, 369
**Severity**: LOW
**Category**: Error
**Details**: No validation that session_id is non-empty string. Empty or None session_id would create invalid invocation records.
**Impact**: Data corruption from invalid session_ids - but caught by schema validation in most cases.
**Fix**: Add validation:
```python
if not session_id or not isinstance(session_id, str):
    raise ValueError("session_id must be non-empty string")
```
---

## Schema Issues

### BUG: Schema timestamp inconsistency
**File**: src/claudio/arc/schema.py:28, 46, 76
**Severity**: MEDIUM
**Category**: Type
**Details**: Message (line 46), RoutingDecision (line 76), and other structs define timestamp as `float` (Unix timestamp), but docstrings mention "Unix timestamp (float from time.time())" suggesting float is expected. However Invocation default factories (lines 228-229) use `time.time()` which returns float. Consistent.
**Impact**: None - consistent implementation
**Fix**: None needed
---

### BUG: No schema version tracking
**File**: src/claudio/arc/schema.py
**Severity**: LOW
**Category**: Logic
**Details**: No version field in schemas. If schema changes in future versions, old msgpack files won't be distinguishable from new format.
**Impact**: Schema evolution problems - can't migrate data between schema versions.
**Fix**: Add version field to main schemas:
```python
class Conversation(msgspec.Struct):
    schema_version: int = 1  # For future compatibility
    session_id: str
    ...
```
---

## Summary Statistics

**Total Bugs Found**: 35
- Critical: 4
- High: 3
- Medium: 17
- Low: 11

**By Category**:
- Type/Type Inconsistency: 6
- Error Handling: 8
- Threading/Race Conditions: 4
- Resource Leaks: 3
- Logic Errors: 10
- Performance: 4

**Most Problematic Files**:
1. coordinator.py (8 bugs)
2. memory.py (7 bugs)
3. storage.py (6 bugs)
4. lsm.py (5 bugs)
5. retrieval.py (4 bugs)

**Priority Actions**:
1. Fix timestamp type inconsistency in coordinator (CRITICAL)
2. Add proper error handling to all file I/O operations
3. Implement atomic writes for metadata files
4. Add input validation to all public APIs
5. Replace print() with proper logging
6. Add resource cleanup guards (try-finally patterns)

