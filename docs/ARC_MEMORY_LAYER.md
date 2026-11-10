---
title: "ARC Memory Layer: Agent Runtime Context Architecture"
category: architecture
priority: high
version: "2.1"
focus: "Live context plane, the semantic-event log and its lanes, the clio-core record backend, honest storage topology"
---

# ARC Memory Layer

**Agent Runtime Context (ARC)** is CLIO Agent's in-process memory layer. It is
the live, mutable context plane the DSPy ReAct loop reads on every iteration, the
semantic-event log every turn appends to (with its task-event lane), and a thin
durable tier beneath a hot in-memory layer. It is **not** a multi-tier storage engine and does **not**
migrate data across GPU/NVMe/PFS tiers — the durable backend is either the
clio-core daemon or plain files on disk.

The class that ties it together is `ARCMemory` (`src/clio_agent/arc/memory.py`).
It composes four pieces:

- `LRUCache` (`arc/cache.py`) — the hot in-memory layer for recently touched
  records (bounded, LRU eviction; its own lock).
- `BTreeIndex` (`arc/index.py`) — a `sortedcontainers`-backed index over
  invocation keys `(session_id, timestamp, trace_id)` for ordered range reads.
- `SegmentStore` (`arc/segments.py`) — the **live context plane** and the
  semantic-event log (see below).
- `LSMTree` (`arc/lsm.py`) — an append-optimized log-structured merge tree for
  the write-heavy invocation-metrics path.

Durable records for all of the above flow through one pluggable seam,
`ARCStore` (`arc/storage.py`), whose two concrete backends are described under
[Backends](#backends).

`ARCMemory._lock` guards **only** the in-process hot structures (cache,
invocation index, counters). It is never held across store or LSM I/O; the
`SegmentStore`, `LSMTree`, and `LRUCache` each self-lock. The invariant is
documented at the lock's declaration in `arc/memory.py`.

---

## The live context plane

`SegmentStore` is an ordered, scoped, mutable sequence of `Segment`s
(`arc/schema.py`). The ReAct loop *writes* one segment per produced piece
(thought / tool_call / observation) and *reads* the prompt back by rendering the
live ordered set for a scope. The context operations — `append` / `insert` /
`delete` / `summarize` / `replace` — mutate segments between renders, so an
out-of-band edit changes the *next* prompt. That is the whole point of the live
plane: compressing ARC actually changes what the model attends over, instead of
being a post-hoc record with zero effect on the turn.

Design detail lives in `docs/archive/arc-live-context-plane.md`. Two properties
matter for storage:

- Each `(session_id, scope)` persists as **one** record through the injected
  `ARCStore`, in the append-only chunk format (see
  [The chunk record format](#the-chunk-record-format)); `render` batches the whole
  scope into a single get/decode because it is the every-iteration hot path.
- `order` is a gap-allocated float, so a mid-sequence `insert` picks a midpoint
  and never renumbers later segments. `delete` **tombstones** rather than erases,
  so segments survive for Trace reconstruction and as-of-`T` reads.

Every applied op on a **context** scope is forwarded to the durable Trace through
an injected `op_logger` (wired by `gact` via `set_segment_op_logger`, so `arc/`
never imports `gact/`), and the live context is replayable from those `arc.op`
events — see `arc/replay.py`. The highway lanes are **not** logged
(`logs_context_op`): the `_events` chunks, the task lanes and content under
`_events/t`, message parts and state ops are held by clio-core as the source of
truth, and the trace already holds each semantic event, so an `arc.op` for them
was a fourth copy. They are therefore not rebuilt by `arc.op` replay. The
working-set partitions (`_events/w`) are context and stay logged, and a
`semantic_event` segment's `arc.op` carries a reference to its event, not a copy.

### The semantic-event log (`_events`, chunked) and the task-event lane

A session's semantic log is held in **two** chunked families. Every recorded
semantic event (`ARCMemory.record_semantic_event`) except a task event is appended
as one lean `semantic_event` segment to the `_events` family via
`ARCMemory._append_event_segment`. A task event's one record is the session's
task-event lane, `_events/t/log` (`gact/task_event_lane.py`; chunk `N >= 2` is
`_events/t/log/N`). A reader of the whole log (the trace route, the session
export's ARC fallback) takes both through `session_log_segments`
(`gact/task_event_merge.py`), which merges them by each event's `occurred_at`;
a reader that needs only task events reads the lane alone. `build_event_content`
(`arc/live.py`) stores the event **verbatim — no truncation, no caps**: ARC is
the source and holds everything (freeze-anytime); any downstream bound is that
consumer's own deliberate choice.

`LiveRuntimeContext` (`arc/live.py`) is a pure **reader** over the `_events` log.
`view` / `project_conversation` / `project_invocations` are queries that render
the log, group by `turn_id`, and replay each turn through one reducer to rebuild
`Conversation` / `Invocation` projections. ARC's conversation and invocation
records are therefore **projections of the log**, not independently built copies.

The log is a **chunk family**, not one ever-growing scope. The reserved scope
`EVENTS_SCOPE = "_events"` is chunk 1; chunk `N >= 2` is `_events/N`
(`events_chunk_scope` / `events_chunk_index` / `is_events_scope` in `arc/live.py`).
The writer rolls to the next chunk once the active one reaches
`arc.events_chunk_segments` segments (env `CLIO_ARC_EVENTS_CHUNK_SEGMENTS`,
default 512). An append is **one raw put of the new record at the chunk's stored
end** (see [The chunk record format](#the-chunk-record-format)); the previous
designs re-encoded the whole chunk (O(chunk)) and, before chunking, the whole log
(O(N²)) per event. Reads concatenate `render` across the family in chunk
order; a store-wide monotonic `logical_time` guarantees the concatenation equals
the old single-scope render (each chunk fills to capacity before the next opens).

The `_events` family is its own scope and `semantic_event` is not a working-set
kind, so the log is invisible to any expert-prompt render and is deliberately
kept out of every backend's keyword-search companion — it can never leak into a
model prompt.

Lifecycle: the log's chunks are erased on `release`/`clear` **only** when a
durable Trace backend is enabled (so the full history is preserved elsewhere);
under the default `none` trace backend the log is the only copy and is retained
instead (#762).

### The chunk record format

Every segment-list record of the `segments` kind (a plain scope and every chunk of
the `_events` lane families) is stored in one append-only binary format
(`arc/chunk_records.py`): a 2-byte magic, then records of
`[u32 length][u32 crc32][msgpack Segment]`, then an 8-byte zero end marker.

- **Append** is one raw put (`ARCStore.put_at`) at the stored end offset: the new
  record(s) plus a fresh end marker, overwriting the old marker.
- **Full rewrite** (a first write, an op that changes earlier segments, a
  shrinking rewrite) is one put at offset 0; the end marker stops a reader before
  any stale tail, since `PutBlob` never shrinks a blob.
- A put that would grow the blob is zero-padded to the next size class, so the blob
  gains a clio-core storage block (and a longer `metadata.log` entry) only when it
  crosses a class.
- **Read** (`get_raw`, `parse_chunk`) walks records to the end marker. A torn
  trailing record is the end of the chunk (trace event `arc_chunk_torn_tail`).
- Records are raw msgpack, never base64. **The previous base64 frame is not read:**
  a blob that does not start with the magic raises `ChunkFormatUnsupportedError`
  (`arc_chunk_format_unsupported`). There is no compatibility reader; ARC state
  written by an older build is reset instead.

Whole records of the other kinds (`put` / `get`) keep the length-framed,
size-class-padded body of `arc/blob_frame.py`.

### Group commit

Several appends can be confirmed in one round trip per record:

- `segment_group` (`arc/segment_group.py`) stages appends made on one thread. That
  thread reads its own staged writes, while other threads see the last committed
  record. `SegmentGroup.flush` puts each staged scope once, every scope of a rank
  in one `put_many`, ranks in ascending order (content before the events that cite
  it). A scope another thread appended to meanwhile is rebased behind those
  appends. A lost append abandons (seals) its chunk, drops the other uninstalled
  scopes from the cache, and raises `ArcPersistError`.
- `run_grouped` (`gact/task_history_group.py`) runs several task-history
  operations in one writer turn on `segment_group`. Task index rows go first, then
  content, the task-event lane and `_events`. What follows a confirmed write
  (projections, highway emission, UI hints) runs only after the flush.

### Highway records written behind the call

Some semantic events are read only from the record afterwards, never by the step
that produced them: `tool.call.started` / `tool.call.completed`,
`routing.decision`, `artifact.transform.recorded` and `lm.call`. With
`arc.highway_deferred` on (the default), `gact/highway_writer.py` records them off
the caller's thread:

- One writer thread per app records them in queue order, in groups of up to 512
  (one `segment_group` per group, so one put per `_events` chunk).
- An event reaches the highway (trace, SSE, hooks), and an `artifact.*` event
  confirms its index record, only after clio-core holds it.
- The ReAct loop's barrier before each model request (`flush_session`) and the
  turn's end wait for a session's queued records.
- A group clio-core did not confirm is logged `highway_record_unconfirmed` and
  raised once at the session's next reporting barrier. The turn's publication
  logs it and goes on (`flush_session_reported`). The wait that keeps a
  synchronous event behind the queued ones only waits.
- The lifespan closes the writer, waiting at most 10 s, before clio-core is
  released. Anything it could not record is logged `highway_close_unrecorded`.

With `arc.highway_deferred` off, every event is recorded on the caller's thread.

### Read-only call records written behind the reply

A read-only MCP call's record (admission and settled outcome) is queued on the
task-history writer and the reply goes back at once (`gact/read_only_outcomes.py`).
Queued records are group-committed through `CoreIO.submit_batch` and `run_grouped`.
Readers that do not go through the writer (task tools, turn retirement,
shutdown) wait for them first (`outcomes_recorded`). A failed record is logged
`read_only_outcome_deferred` and retried alone by the recovery path. A retry that
fails for any other reason than a recording hold is logged
`read_only_outcome_unrecorded`: the call already answered, and its outcome is
reported as unknown.

### Record kinds

The logical record families ARC persists are the single source of truth in
`ARC_KINDS` (`arc/storage.py`):

| Kind | What it holds |
| --- | --- |
| `conversations` | One `Conversation` per session (projected from the log). |
| `invocations` | Per-turn `Invocation` records (projected from the log). |
| `variants` | Optimizer prompt/program variants. |
| `response_feedback` | Append-only ratings and comments on answers (`arc/response_feedback.py`). |
| `task_index` | The task layer's per-owner index of task handles. |
| `segments` | The live context plane: one record per `(session_id, scope)`, including the `_events` log chunks. |

> Earlier revisions of this doc described `profiles` (dataset profiles) and
> `procedural` (procedural memory) kinds with `[Available Data]` / `[Prior
> Analysis]` context sections. Those schemas had **zero writers** and were
> deleted in #771 (Slice D); they no longer exist in `arc/schema.py`,
> `ARC_KINDS`, or `arc/context_compiler.py`.

### Invocation metrics (LSM)

`ARCMemory.store_invocation` is the single caller of `LSMTree` (`arc/lsm.py`).
The LSM tree is append-optimized: a `SortedDict` MemTable takes recent writes,
immutable on-disk SSTables hold the rest, and a background thread compacts
SSTables to bound read amplification (the MemTable is double-buffered so a flush
does not block writers). Metrics live under `.clio/agent/arc/lsm/sst_*.msgpack`.

---

## Backends

Durable records go through the `ARCStore` protocol (`arc/storage.py`):
`put(kind, name, data, search_text=...)` / `get(kind, name)` /
`scan(kind, prefix)` over opaque `bytes` keyed by `(kind, name)`, plus the raw
`put_at` / `get_raw` / `put_if_absent` of the segment records. One backend ships:
**`ClioCoreStore` (`CLIO_ARC_STORE=cte`, the only value)**, the clio-core CTE binding.
It connects to a shared per-host daemon (connect-or-spawn) and releases it when the
server shuts down (see the shutdown note below). Data lives on one file tier at
`<user_data_dir>/cte/hosts/<host>/storage.bin` with `persistence_level: "temporary"`,
behind a DRAM working-memory budget, and the metadata write-ahead log
(`metadata.log`) is replayed on restart. `arc/clio_core_durability.py` refuses a
config without a durable tier. Measured on iowarp-core 2.3.2 (memory ledger P0-dur):
a record written through the default topology reads back after a clean restart and
after `kill -9` of the daemon. CLIO now pins `iowarp-core~=2.4.0` (`pyproject.toml`);
its atomic `AsyncPutBlobIfAbsent` creates a content-addressed record in one round trip
(`arc/segment_create.py`). The durability measurement has not been repeated on
2.4.0.

`LocalFSStore` and `CLIO_ARC_STORE=local` were deleted in the agent-loop rebuild (2026-09-28);
`tests/test_arc/test_store_fail_stop.py` guards that. The in-process `_MemoryStore`
in `arc/live.py` exists only for unit tests and has no search.

### Search: the BM25 indexer

`ARCStore.search` is BM25 over a record's plain-text `.text` companion, served by
clio-core's optional `clio_cte_indexer` chimod (clio-core#905), an interposer in front
of the CTE core. The seeded config composes it after the core
(`arc/clio_core_indexer.py`), and the client binds to it with `CLIO_CTE_POOL`:

```yaml
  - mod_name: clio_cte_indexer
    pool_name: clio_cte_indexer
    pool_query: local
    pool_id: "564.0"
    restart: true
    next_pool_id: "512.0"
    tag_re: "(.+/)?(memories|segments)"
    blob_re: ".*\\.text"
    index_log_path: "<cte dir>/indexer_index.log"
```

Only the search companions of long-term memory documents and of the context
plane's segment search records are tokenized; segment bodies and `_events` chunks
are out of scope and cost one regex check per put. The index persists in its own
snapshot and write-ahead log, so a restart or a killed daemon restores it without a
rescan. A seeded config from before this was upgraded in place once. A config
without the indexer cannot search: callers get the typed reason
`clio_core_search_unavailable`, and `clio doctor` shows a required, red
`clio_core_search` row with the remedy. Companions are padded with spaces to their
size class, and a companion that shrinks across a class is deleted before it is
rewritten, so a shorter document is never found by words it lost
(`arc/companion_write.py`). Qualification: memory ledger P0, MD1 and MD9.

**Selection is fail-loud, never a silent fallback** (`make_arc_store`,
`arc/storage.py`). If the clio-core binding is absent or fails to initialize,
the store **raises** a typed `ArcStoreUnavailableError`. This is the
`[[deliberate-config-fail-loud]]` policy.

**Doctor surfaces the backend's real state.** `clio doctor` (`runtime/status.py`,
`_probe_arc_clio_core`) reports `arc` as a required integration:
when the clio-core backend is selected but `iowarp_core` is not installed, or its
shared daemon is not listening, the probe returns `UNAVAILABLE` (red) with a
concrete next action (install the package or start the daemon) —
never a green "everything's fine" over a missing store.

### Shutdown story

The gact lifespan releases the shared clio-core runtime in one place,
`release_runtime_after_drain` (`gact/desktop_lifecycle.py`). This happens after
the turn drain and the task runtime have settled, and after the highway writer
has been closed (bounded, `close_app_writer`). At that point no application work
can reacquire the runtime. The release is app-specific: an app with no CTE-backed
ARC on `app.state.arc` does not deregister another owner's client or stop its
daemon. The `atexit`-registered `release_runtime_client` (`arc/storage.py`) remains
a crash and legacy backstop, not the primary cleanup path.

### One daemon per user home

The daemon's port is machine-wide, but its spawn lock, pidfile, client registry and
version record live in the user's runtime state directory
(`<host state dir>/core-hosts/<host>`). Two users, or one user under two `HOME`
values, never share a daemon (`arc/runtime_ownership.py`):

- A listener counts as this home's daemon only when this home's pidfile names a
  live process (PID-reuse checked) that, or a child of which, holds the listening
  socket.
- Connect-or-spawn never attaches to a listener it does not own. It moves this
  home's generated `cte.yaml` to a free port block (from 9421, in steps of 8, from
  a start derived from the state directory) and spawns its own daemon there. The
  move is logged at WARNING (`clio_core_port_moved_off_foreign_daemon`) and holds
  across restarts. A hand-written config (`arc.store_config`, a workspace
  `.clio/core/cte.yaml`) is never rewritten: the attach fails with
  `clio_core_foreign_daemon_on_port` and names the setting to change.
- A stop (last client out, crashed-client replacement, log-flood replacement,
  the opt-in recycle) runs `clio_run stop` only against this home's own daemon.
  Without one it stops nothing (`StopOutcome.path == "not_owned"`); when another
  daemon holds the port it kills only this home's pidfile process
  (`foreign_listener_kill`).

The rules are the same on Linux, macOS and Windows.

---

## Storage topology

ARC is one materialization among several the running server keeps. Per RULE 4
(honest topology), here is every durable location, its owner, and who reads it.
The default paths below assume the gact session store's default root
`<cwd>/.clio/agent/`.

| # | Location | Owner | Path (default) | Reader |
| --- | --- | --- | --- | --- |
| 1 | ARC records (`ARC_KINDS`) | ARC (`arc/storage.py`) | CTE tags `<kind>` in the default namespace; data on `<user_data_dir>/cte/hosts/<host>/storage.bin` | `ARCMemory` reads/projections; `arc/context_compiler.py` |
| 1a | Long-term memory (`memories`, `memory_versions`, `memory_proposals`, `memory_usage`) | `gact/memory/service.py`, the one writer | CTE tags `memory/<kind>` (namespace `memory`, its own kinds; not `ARC_KINDS`) on the same daemon and file tier | memory tools, the session snapshot, the memory page |
| 1b | BM25 index of `.text` companions | clio-core `clio_cte_indexer` | `<user_data_dir>/cte/hosts/<host>/indexer_index.log` (+ `.wal`) | `ARCStore.search` |
| 2 | LSM invocation metrics | ARC (`arc/lsm.py`) | `.clio/agent/arc/lsm/sst_*.msgpack` | `ARCMemory.store_invocation` range reads |
| 3 | Session registry | gact (`gact/sessions.py`) | `.clio/agent/sessions.json` | gact session routes; TUI |
| 4 | Message ledgers | gact (`gact/messages.py`, `MessageStore`) | `.clio/agent/messages/<session_id>.json` | gact message routes; the turn transcript prepend |
| 5 | Agentic provenance providers | gact (`gact/provenance/`) | `.clio/agent/semantic_traces/` — **JSONL enabled by default**; optional Flowcept | audit/replay and provider-neutral execution queries |
| 6 | Context-files ledger | gact (`gact/app.py`) | `.clio/agent/context_files.json` | context-file routes |

Plus `~/.clio/` daemon coordination artifacts (lock/port files) owned by the
clio-core runtime — infrastructure, not an ARC record store.

> The former **workspace session mirror** (a second copy of sessions + messages
> under a workspace storage root) was **deleted** in #771 (Slice E): it had zero
> readers in `src/` or the TUI. Only the `storage_root` wire field
> (`resolve_workspace_storage_root`) remains. Any pre-existing mirror files are
> orphaned artifacts and safe to hand-delete.

Note that today conversations, invocations, the message ledger, and the
semantic-event log are still **parallel materializations of the same history**.
The agreed direction for collapsing them is below.

---

## FUTURE: one normalized log (event-sourcing, #737)

The end-state is tracked in
[#737](https://github.com/iowarp/clio-agent/issues/737) and is **not yet
built** — this section is a pointer, not a description of current behavior.

The plan is to collapse the parallel materializations (ARC conversations/
invocations, the gact message ledger, the `_events` log, and workflow state)
into **one normalized append-only log with thin projections** — event sourcing.
ARC's chunked `_events` log and its projection readers (`LiveRuntimeContext`)
are the first step in that direction: conversation and invocation records are
already *derived* from the log rather than built independently.

The separable physical follow-on is the clio-core KV plane
(`context-transfer-engine` / `llm-hooks` / `kvcache`) described in
`docs/archive/arc-live-context-plane.md`: a store-level append/KV-surgery backend
that turns the live-plane ops into true O(1) appends behind the same `apply`
interface. Until #737 lands, treat the topology table above as the real,
present-day layout.

---

## Configuration reference

The canonical, source-derived list of every knob lives in
[ENVIRONMENT.md](ENVIRONMENT.md) (generated by `scripts/gen_env_reference.py`;
`tests/test_docs/test_env_reference.py` fails on drift). The ARC-specific
variables are:

```bash
# Record backend selection (fail-loud; no silent fallback)
CLIO_ARC_STORE=cte                    # "cte" (clio-core CTE), the only backend
CLIO_ARC_STORE_CONFIG=                # path to a clio-core CTE config; blank = auto-discover

# Live semantic-event log
CLIO_ARC_EVENTS_CHUNK_SEGMENTS=512    # segments per _events chunk before rolling to the next
CLIO_ARC_HIGHWAY_DEFERRED=true        # record highway-only events behind the call (gact/highway_writer.py)

# clio-core daemon
CLIO_ARC_CORE_THREADS=8               # runtime threads of the seeded clio-core daemon (read at its start)

# In-memory hot layer + LSM metrics index
CLIO_ARC_CACHE_CAPACITY=1000          # LRUCache capacity (entries)
CLIO_ARC_LSM_MEMTABLE_SIZE=1000       # LSM memtable size before flush
CLIO_ARC_LSM_COMPACTION_THRESHOLD=5   # SSTables before a compaction

# CTE spillover (clio-core backend only)
CLIO_ARC_CTE_DIR=                     # CTE working dir; blank = OS data dir
CLIO_ARC_CTE_FILE_CAPACITY=50GB       # on-disk capacity for the CTE file tier
```

The durable JSONL agentic-provenance provider is enabled by default. Configure
the provider set with `CLIO_PROVENANCE_PROVIDERS`; the old
`CLIO_SEMANTIC_TRACE_BACKEND` remains a compatibility input (see
ENVIRONMENT.md).

---

## Related Documentation

- [ARC as the Live Context Plane](archive/arc-live-context-plane.md) — the design
  rationale for the mutable plane and the #737 north-star.
- [CLIO Agent Architecture](CLIO_AGENT_ARCHITECTURE.md) — full system architecture.
- [Environment variable reference](ENVIRONMENT.md) — every `CLIO_*` knob.

---

**Version**: 2.1 (truthful rewrite, #771; chunk format, group commit and
behind-the-call records, 2026-11)
**Focus**: Live context plane + the chunked semantic-event log and task-event lane +
the fail-loud clio-core record backend + honest storage topology
