# clio-core requests from CLIO (to file upstream)

Status (2026-11-06): **filed** in `iowarp/clio-core` once the owner granted access: the parent is iowarp/clio-core#1335 and each request is its sub-issue (numbers below).

Nothing goes in `iowarp/clio-agent`. The clio-core team decides how to solve each issue.

| # | Issue (in iowarp/clio-core) | Filed as | State |
| --- | --- | --- | --- |
| parent | CLIO agent parent request: clio-core changes for the next release | iowarp/clio-core#1335 | open |
| 1 | Scope BlobQuery / GetContainedBlobs to the tag | iowarp/clio-core#1336 | open |
| 2 | Expose the existing native write and read primitives to Python | iowarp/clio-core#1337 | open |
| 3 | Document, or make awaitable, when a deferred put is durable | iowarp/clio-core#1338 | open |
| 4 | The idle daemon uses most of a core | iowarp/clio-core#1339 | open |
| 5 | Build the macOS wheels for macOS 14 again | iowarp/clio-core#1340 | open |
| 6 | Conditional puts in a batch, and a conditional put that replaces | iowarp/clio-core#1341 | open |
| 7 | A Python binding for `clio_cte_stream` | iowarp/clio-core#1342 | open |

The numbers match the sections below. The parent issue lists them by priority: 3, 2, 1, 4, 6, 5, 7.

Checked against clio-core `v2.4.0` (`3ae9fd9a`, released 2026-11-02). Issues 1 to 5 and 7 are not in that release; issue 6 asks to extend what it added. CLIO moved to 2.4.0 (`ae67aba4`) after a neutral benchmark.

---

## Parent issue — title: "CLIO agent parent request: clio-core changes for the next release"

CLIO's evaluation shows that clio-core round trips, not the model or the MCP servers, limit tool-heavy work. In P7, 2,001 read-only MCP calls against a server that answers in milliseconds made 21,665 puts and 156 s of tool time before CLIO's own work; after it (2026-11-03) they make 7,214 puts and 71 s of tool time, and a script's tool call still costs 6.93 store round trips (2026-11-05). CLIO has already done what it can on its side: read-only calls no longer wait for their records, and queued records are group-committed (on a P7-shaped burst, about 11 calls/s became 250–330 calls/s; CLIO's serialized time per call fell from about 90 ms to 6–7 ms). These upstream items remove the rest and unblock the one-blob-per-event store design:

Sub-issues, in priority order:
1. Deferred puts: durability contract and a per-put or per-batch await (or multi-put with per-put status)
2. Python bindings for the existing native put-replace, `TruncateBlob`, multi-get, multi-put and `DelTag`
3. Scope `BlobQuery`/`GetContainedBlobs` to the tag (blocks one blob per event)
4. Idle daemon CPU (busy-poll)
5. macOS 14 wheels (2.4.0 publishes macOS 15 wheels only, so CLIO Desktop's floor moved to macOS 15 with it)
6. Conditional puts in a batch, and a conditional put that replaces (so the 2.4.0 conditional writes reach CLIO's grouped writes and shared index rows)
7. A Python binding for `clio_cte_stream` (CLIO stores streams as plain blobs on a tag until then)

Already in 2.4.0 and used by CLIO: `AsyncPutBlobIfAbsent` creates each content-addressed task record in one round trip instead of a read and a put, and decides which of two writers creates it. `AsyncPutBlobIfVersion` and `AsyncGetBlobVersioned` are not used yet; issue 6 says what CLIO needs to use them.

Not in 2.4.0: free-threaded (`cp314t`) wheels. The bindings release the GIL during blocking calls, as 2.3.2 already mostly did, but the wheels are GIL builds. Free-threaded wheels are optional for CLIO today.

CLIO's references:
- `docs/design/arc-performance-review-2026-11.md` (sections "Changes needed in clio-core", "Measured after").
- `docs/design/sdk/tool-calling-ledger.md` ("ARC performance").
- Pin: `pyproject.toml` `iowarp-core~=2.4.0` (benchmarked neutral against 2.3.2). CLIO's macOS floor moved to macOS 15 with it, since 2.4.0's macOS wheels target macOS 15.

---

## clio-core issue 1: Scope `BlobQuery` and `GetContainedBlobs` to the tag

**Problem.** Both handlers walk the daemon's whole blob map and filter by key prefix, so their cost grows with every blob on the node, not with the tag queried:

- `Runtime::GetContainedBlobs`: `context-transfer-engine/core/src/core_runtime.cc:11536`, `tag_blob_name_to_info_.for_each(...)` with a `rfind(prefix, 0)` filter.
- `Runtime::BlobQuery`: `core_runtime.cc:11661`, the same full walk once per matching tag.

Measured from CLIO on 2.3.2: 2.8 ms for one tag's listing over 38,178 blobs, about 70 ns per blob in the daemon. That extrapolates to about 70 ms per query at a million blobs.

**Fix.** The per-tag index already exists: `blob_index_`, `BlobIndexNames(tag)` at `core_runtime.cc:7015`. Today only tag deletion reads it. Using it in both handlers (names from the index, then `ServesBlob`) makes a query proportional to the tag's own blobs.

**Why CLIO needs it.** Storing each event as its own blob in a per-session tag (instead of segment chunks) is blocked on this. Without the fix, every session read scans the whole machine's history.

**Acceptance.** `GetContainedBlobs`/`BlobQuery` on a tag of 100 blobs takes the same time whether the daemon holds 1k or 1M other blobs. The results are unchanged, including the `ServesBlob` filtering.

---

## clio-core issue 2: Expose the existing native write and read primitives to Python

All of these exist natively in `context-transfer-engine/core/include/clio_cte/core/core_tasks.h`. None is bound in `wrapper/python/core_bindings.cc`, which in 2.4.0 differs from 2.3.2 only by the three conditional-write methods.

| Native | Where | What CLIO needs it for |
| --- | --- | --- |
| `kCtePutReplace` flag on put | `core_tasks.h:2247` | Rewrite a chunk whole when it shrinks. Today CLIO writes the body plus an end marker because a put cannot truncate. |
| `TruncateBlobTask` | `core_tasks.h:4007` | Same, as an explicit operation. |
| `PodMultiGetBlobTask` | `core_tasks.h:3869` | Read a transcript's chunks in one round trip. Today it is up to 3,000 sequential gets, about 4–5 s for a long session. |
| `MultiPutBlobTask` (issue #862), with a per-put status | `core_tasks.h:3226` | One round trip for the small records CLIO writes per tool call (6.93 store round trips per call from a script, see issue 3). |
| `DelTagTask` | `core_tasks.h:4069` | Python cannot delete a tag, so benchmark and test tags are left behind in the daemon. |

**Measured cost of the current path (2026-11-03, iowarp-core 2.4.0, local daemon, `arc.core_threads` 8).** CLIO writes many small blobs, one `AsyncPutBlob` each. All are issued before any is awaited:

| Blobs | Issuing them | Waiting | Rate |
| --- | --- | --- | --- |
| 3,000 × 4 KB | 3,495 ms (1.2 ms per call) | 1,757 ms | 571 blobs/s, 2.3 MB/s |
| 3,000 × 256 B | 2,470 ms | 583 ms | 983 blobs/s |
| 300 × 40 KB | 98 ms | 1,961 ms | 146 blobs/s, 6.0 MB/s |

Issuing an `AsyncPutBlob` costs about 1 ms of synchronous binding work per blob. In CLIO's burst bench (2,000 read-only tool calls), one grouped write of 3,220 blobs (13 MB) took 8.4 s. The history writer spent 28 of its 45 s inside clio-core puts, and per-call latency rose from 26 ms (p90, one session) to 750 ms (eight sessions) while the process used under half a core. Later runs on CLIO's branch head (2026-11-04 and 2026-11-05) found the same burst bound by CLIO's own interpreter instead (CPU over wall about 1.0, sessions served evenly), so the per-put issue cost is one part of the time, not all of it. A multi-put with one submission for N blobs (`MultiPutBlobTask`, below), and a cheaper single issue, would remove most of that.


**Acceptance.** Async Python methods for each, matching the existing `AsyncPutBlob`/`AsyncGetBlob` style: release the GIL while waiting, return a `Future`, and give a per-item return code for the multi operations.

---

## clio-core issue 3: Document, or make awaitable, when a deferred put is durable

**Context.** The Python client already exposes `AsyncPutBlobDefer`, `AwaitPutsUntilSpace` and `DeferErrorCount`. The client batches deferred puts into `MultiPutBlobTask` (`core_client.h:1047`, `:1649`).

**Why it matters.** CLIO's records for a tool call are now group-committed (records queued while the writer is busy go in one writer turn), but each record is still a separate `AsyncPutBlob`: a script's tool call costs 6.93 store round trips, median 4.55 ms each (2026-11-05, 1,000 sequential calls). Measured: P7 in CLIO's evaluation makes 2,001 calls; it sent about 22,000 puts and took about 160 s of tool time before group commit, and 7,214 puts and 71 s after it (2026-11-03), for a server that answers in milliseconds.

CLIO's rule is that a record is only claimed durable after clio-core confirms it. The deferred pipeline could remove most of those round trips, but its completion contract is not documented.

**Ask.**
- **Document** when a deferred put is visible to `GetBlob`, and when it is durable on the target tier.
- **Provide a way to await one specific deferred put, or a flush point.** For example, `AsyncPutBlobDefer` could return a handle, or there could be a `FlushDeferred(tag)` that returns once everything submitted before it is confirmed. `DeferErrorCount` should be able to say which put failed.
- **Alternatively,** expose `MultiPutBlob` directly with per-put status (issue 2), and CLIO will batch itself.

---

## clio-core issue 4: The idle daemon uses most of a core

**Measured (2.3.2, Linux, 4 cores):** `clio_run start` with no client activity, after about 1h40 of uptime, used 4.13 CPU-seconds in 5 s (about 83% of one core), read from `/proc/<pid>/stat`.

This matters for CLIO because it runs a local daemon per user on desktops and laptops. An idle agent costing most of a core drains batteries and slows everything else.

**Re-measured on 2.4.0 (2026-11-03):** about 0.92 to 0.94 of one core idle (9.2 and 9.45 CPU-seconds in 10 s, measured 35 s and 90 s after the last client finished), so the scheduler change did not remove it.

**Ask.** Please look at this on 2.4.0. The new scheduler ("schedule by predicted worker hold time; one elastic pool", `efd9349b`) may already change it. When there's no work, workers should back off to a blocking wait (futex/epoll) rather than spin.

**Acceptance.** An idle daemon stays under a few percent of one core.

---

## clio-core issue 5: Build the macOS wheels for macOS 14 again

**Problem.** iowarp-core 2.4.0 publishes its macOS wheels as `macosx_15_0_arm64` (cp312, cp313, cp314); 2.3.2 published `macosx_14_0_arm64`. CLIO requires 2.4.0, so CLIO Desktop's floor moved from macOS 14 to macOS 15 with it (installer, bundle build and release checks), and macOS 14 users can no longer install it.

**Ask.** Build the macOS wheels with `MACOSX_DEPLOYMENT_TARGET=14.0` (as 2.3.2 did), unless 2.4 needs a macOS 15 API; if it does, please say which, so CLIO can state why its floor is macOS 15.

**Acceptance.** A 2.4.x release has `macosx_14_0_arm64` wheels for cp312, cp313 and cp314; CLIO then moves its floor back to macOS 14 (`install/install.sh`, `scripts/check_bundle_matches_lock.py`, the bundle workflows).

---

## clio-core issue 6: Conditional puts in a batch, and a conditional put that replaces

**What CLIO uses from 2.4.0.** `AsyncPutBlobIfAbsent` for content-addressed records (a task's content is stored once under its SHA-256). A new record costs one round trip instead of a read and a put, and when two writers create the same record, clio-core decides which one does (`clio_agent/arc/segment_create.py`).

**Where it cannot be used.** CLIO writes most records in groups: the records of several tool calls are staged and then written with one multi-put per tag. A conditional put exists only for one blob at a time, so using it there would turn one round trip per group into one per record. CLIO keeps the group path, which reads the sizes of the new records first (one `sizes` probe per group). Two processes can then both find a record absent and both write it. For content-addressed records that is harmless (same content), but it is not exact.

**Where `AsyncPutBlobIfVersion` does not fit yet.** The task index has rows that several writers update by reading, changing and rewriting them, for example a task tree's member list (`clio_agent/gact/task_index_writes.py`, `_merge`). What stops a compare-and-swap there is that the conditional put is single-blob: these rows are staged and written with the rest of a group (the same multi-put as above), and a conditional put per row would turn one round trip per group into one per row. Truncation is not a blocker: CLIO's segment chunks are length- and CRC-framed records with an end marker (`clio_agent/arc/chunk_records.py`), so a reader stops before a stale tail, and index rows carry their own length frame.

**Ask.**
1. A multi-put where each record carries a precondition (absent, or an expected version) and the result carries a status and new version per record, like `MultiPutBlobTask` with per-put status (issue 2).
2. Optional: a replace variant of `AsyncPutBlobIfVersion` that sets the blob to exactly the given bytes, or a version precondition on put-replace (`kCtePutReplace`, issue 2). CLIO's framing reads a shorter rewrite correctly without it; it would drop the stale tail.

**Acceptance.** Python can create or compare-and-swap N records in one round trip with a status per record (and, for the optional ask, a compare-and-swap can leave a blob shorter than before).

---

## clio-core issue 7: A Python binding for `clio_cte_stream`

**What CLIO needs.** A stream is an artifact whose content is rewritten often (a device's status, a feed of recent readings) and read mostly for its latest value, kept as its latest content or a bounded window and flushed by a policy. clio-core already keeps blobs in memory by default (a blob reaches disk under memory pressure or when it goes unused), so placement is not the gap. The gap is the stream module: iowarp-core ships `clio_cte_stream` (page blobs, deferred appends) with no Python binding, so CLIO cannot use it.

**Until then.** CLIO stores a stream as plain blobs on a tag: one meta blob (state, entry type, window, last sequence number, times) and a ring of entry blobs named by sequence number modulo the window. A write is two puts and a window read is one get per entry, behind one module that `clio_cte_stream` would replace.

**Ask.** A Python binding for `clio_cte_stream`: create a stream on a tag with a retention (latest only, or a window of N pages), append, read the latest and the window, and flush.

**Acceptance.** From Python, CLIO creates a stream on a tag with either retention, appends many times a second, reads the latest entry and the window back, and flushes on demand with a per-flush confirmation.
