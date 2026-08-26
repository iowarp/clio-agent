---
title: "ARC Memory Layer: Agent Runtime Context Architecture"
category: architecture
priority: high
version: "2.0"
focus: "Live context plane, one semantic-event log, pluggable record backends, honest storage topology"
---

# ARC Memory Layer

**Agent Runtime Context (ARC)** is CLIO Agent's in-process memory layer. It is
the live, mutable context plane the DSPy ReAct loop reads on every iteration, the
one semantic-event log every turn appends to, and a thin durable tier beneath a
hot in-memory layer. It is **not** a multi-tier storage engine and does **not**
migrate data across GPU/NVMe/PFS tiers — the durable backend is either the
clio-core daemon or plain files on disk.

The class that ties it together is `ARCMemory` (`src/clio_agent/arc/memory.py`).
It composes four pieces:

- `LRUCache` (`arc/cache.py`) — the hot in-memory layer for recently touched
  records (bounded, LRU eviction; its own lock).
- `BTreeIndex` (`arc/index.py`) — a `sortedcontainers`-backed index over
  invocation keys `(session_id, timestamp, trace_id)` for ordered range reads.
- `SegmentStore` (`arc/segments.py`) — the **live context plane** and the one
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
  `ARCStore`; `render` batches the whole scope into a single get/decode because
  it is the every-iteration hot path.
- `order` is a gap-allocated float, so a mid-sequence `insert` picks a midpoint
  and never renumbers later segments. `delete` **tombstones** rather than erases,
  so segments survive for Trace reconstruction and as-of-`T` reads.

Every applied op is forwarded to the durable Trace through an injected
`op_logger` (wired by `gact` via `set_segment_op_logger`, so `arc/` never imports
`gact/`). ARC is replayable from those `arc.op` events — see `arc/replay.py`.

### The one semantic-event log (`_events`, chunked)

There is exactly **one** persisted semantic-event log. Every recorded semantic
event (`ARCMemory.record_semantic_event`) is appended as one lean
`semantic_event` segment via `ARCMemory._append_event_segment`. `build_event_content`
(`arc/live.py`) stores the event **verbatim — no truncation, no caps**: ARC is
the source and holds everything (freeze-anytime); any downstream bound is that
consumer's own deliberate choice.

`LiveRuntimeContext` (`arc/live.py`) is a pure **reader** over this log.
`view` / `project_conversation` / `project_invocations` are queries that render
the log, group by `turn_id`, and replay each turn through one reducer to rebuild
`Conversation` / `Invocation` projections. ARC's conversation and invocation
records are therefore **projections of the log**, not independently built copies.

The log is a **chunk family**, not one ever-growing scope. The reserved scope
`EVENTS_SCOPE = "_events"` is chunk 1; chunk `N >= 2` is `_events/N`
(`events_chunk_scope` / `events_chunk_index` / `is_events_scope` in `arc/live.py`).
The writer rolls to the next chunk once the active one reaches
`arc.events_chunk_segments` segments (env `CLIO_ARC_EVENTS_CHUNK_SEGMENTS`,
default 512). A single append re-encodes only the active chunk — **O(chunk)** —
instead of re-encoding the whole log per event, which was the previous
**O(N²)** hot-path cost. Reads concatenate `render` across the family in chunk
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

### Record kinds

The logical record families ARC persists are the single source of truth in
`ARC_KINDS` (`arc/storage.py`):

| Kind | What it holds |
| --- | --- |
| `conversations` | One `Conversation` per session (projected from the log). |
| `invocations` | Per-turn `Invocation` records (projected from the log). |
| `variants` | Optimizer prompt/program variants. |
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
`scan(kind, prefix)` over opaque `bytes` keyed by `(kind, name)`. Any backend
satisfying it plugs in. Two ship:

- **`ClioCoreStore` (default, `CLIO_ARC_STORE=cte`)** — the clio-core CTE (Convergent
  Tiered Environment) binding. It connects to a shared per-user daemon
  (connect-or-spawn) and stops it at interpreter exit via `atexit` (see the
  shutdown note below). The daemon's DRAM tier is the live working set; a file
  tier at `<user_data_dir>/cte/storage.bin` backs it. On-disk recovery of the
  file tier is still WIP, so for guaranteed disk durability today prefer the
  local backend.
- **`LocalFSStore` (`CLIO_ARC_STORE=local`)** — plain files under the ARC data
  dir: one `<kind>/<name>.msgpack` record per key, plus a `<kind>/<name>.search`
  plain-text companion for the degraded keyword-overlap search. Durable on disk,
  no external process.

**Selection is fail-loud, never a silent fallback** (`make_arc_store`,
`arc/storage.py`). `cte` is the default; if its binding is absent or fails to
initialize it **raises** — it does not quietly degrade to `LocalFSStore`, which
would mask a misconfigured deploy and hide that ARC is no longer on clio-core.
`LocalFSStore` is used only when `local` is selected explicitly. This is the
`[[deliberate-config-fail-loud]]` policy.

**Doctor surfaces the backend's real state.** `clio doctor` (`runtime/status.py`,
`_probe_arc_clio_core` / `_probe_arc_local`) reports `arc` as a required integration:
when the clio-core backend is selected but `iowarp_core` is not installed, or its
shared daemon is not listening, the probe returns `UNAVAILABLE` (red) with a
concrete next action (install the package or set `CLIO_ARC_STORE=local`) —
never a green "everything's fine" over a missing store.

### Shutdown story (atexit, deliberate)

The shared clio-core daemon is released via an `atexit`-registered
`release_runtime_client`, **not** from the gact server's lifespan shutdown. This
is deliberate: uvicorn returns from `serve` on `SIGTERM`, the interpreter exits,
and `atexit` fires. Stopping the shared daemon inside the FastAPI lifespan would
wrongly kill it on any non-exit app teardown (tests, reloads, embedded use)
while another process may still be attached. See the lifespan note in
`gact/app.py`.

---

## Storage topology

ARC is one materialization among several the running server keeps. Per RULE 4
(honest topology), here is every durable location, its owner, and who reads it.
The default paths below assume the LocalFS layout and the gact session store's
default root `<cwd>/.clio/agent/`.

| # | Location | Owner | Path (default) | Reader |
| --- | --- | --- | --- | --- |
| 1 | ARC records (`ARC_KINDS`) | ARC (`arc/storage.py`) | CTE: daemon DRAM + `<user_data_dir>/cte/storage.bin`; LocalFS: `.clio/agent/arc/<kind>/*.msgpack` (+ `.search`) | `ARCMemory` reads/projections; `arc/context_compiler.py` |
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
CLIO_ARC_STORE=cte                    # "cte" (clio-core CTE, default) or "local" (file-based)
CLIO_ARC_STORE_CONFIG=                # path to a clio-core CTE config; blank = auto-discover

# Live semantic-event log
CLIO_ARC_EVENTS_CHUNK_SEGMENTS=512    # segments per _events chunk before rolling to the next

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

**Version**: 2.0 (truthful rewrite, #771)
**Focus**: Live context plane + one chunked semantic-event log + pluggable
fail-loud record backends + honest storage topology
