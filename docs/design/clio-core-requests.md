# clio-core requests from CLIO (to file upstream)

Status (2026-11-03): **not filed yet.** The texts are ready, but this session's GitHub integration cannot write to `iowarp/clio-core` (`403 Resource not accessible by integration`). The owner will grant access later. When it is granted:
- file the parent issue first, in `iowarp/clio-core`;
- file each of the others as its sub-issue;
- record the issue numbers in the table below.

Nothing goes in `iowarp/clio-agent`. The clio-core team decides how to solve each issue.

| # | Issue (in iowarp/clio-core) | Filed as | State |
| --- | --- | --- | --- |
| parent | CLIO agent parent request: clio-core changes for the next release | — | not filed |
| 1 | Scope BlobQuery / GetContainedBlobs to the tag | — | not filed |
| 2 | Expose the existing native write and read primitives to Python | — | not filed |
| 3 | Document, or make awaitable, when a deferred put is durable | — | not filed |
| 4 | The idle daemon uses most of a core | — | not filed |
| 5 | Build the macOS wheels for macOS 14 again | — | not filed |
| 6 | Conditional puts in a batch, and a conditional put that replaces | — | not filed |

The numbers match the sections below. The parent issue lists them by priority: 3, 2, 1, 4, 6, 5.

Checked against clio-core `v2.4.0` (`3ae9fd9a`, released 2026-11-02). Issues 1 to 5 are not in that release; issue 6 asks to extend what it added. CLIO moved to 2.4.0 (`ae67aba4`) after a neutral benchmark.

---

## Parent issue — title: "CLIO agent parent request: clio-core changes for the next release"

CLIO's evaluation shows that clio-core round trips, not the model or the MCP servers, limit tool-heavy work. In P7, 2,001 read-only MCP calls make about 22,000 puts, which is about 160 s of tool time, against a server that answers in milliseconds. CLIO has already done what it can on its side: read-only calls no longer wait for their records, and queued records are group-committed (on a P7-shaped burst, about 11 calls/s became 250–330 calls/s; CLIO's serialized time per call fell from about 90 ms to 6–7 ms). These upstream items remove the rest and unblock the one-blob-per-event store design:

Sub-issues, in priority order:
1. Deferred puts: durability contract and a per-put or per-batch await (or multi-put with per-put status)
2. Python bindings for the existing native put-replace, `TruncateBlob`, multi-get, multi-put and `DelTag`
3. Scope `BlobQuery`/`GetContainedBlobs` to the tag (blocks one blob per event)
4. Idle daemon CPU (busy-poll)
5. macOS 14 wheels (2.4.0 publishes macOS 15 wheels only, so CLIO Desktop's floor moved to macOS 15 with it)
6. Conditional puts in a batch, and a conditional put that replaces (so the 2.4.0 conditional writes reach CLIO's grouped writes and shared index rows)

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
| `MultiPutBlobTask` (issue #862), with a per-put status | `core_tasks.h:3226` | One round trip for the ~11 small records CLIO writes per tool call (see issue 3). |
| `DelTagTask` | `core_tasks.h:4069` | Python cannot delete a tag, so benchmark and test tags are left behind in the daemon. |

**Acceptance.** Async Python methods for each, matching the existing `AsyncPutBlob`/`AsyncGetBlob` style: release the GIL while waiting, return a `Future`, and give a per-item return code for the multi operations.

---

## clio-core issue 3: Document, or make awaitable, when a deferred put is durable

**Context.** The Python client already exposes `AsyncPutBlobDefer`, `AwaitPutsUntilSpace` and `DeferErrorCount`. The client batches deferred puts into `MultiPutBlobTask` (`core_client.h:1047`, `:1649`).

**Why it matters.** CLIO writes about 11 small records per MCP tool call, each a separate `AsyncPutBlob`. On one session's ordered writer this costs about 60 ms per call. Measured: P7 in CLIO's evaluation makes 2,001 calls, sends 22,113 puts, and takes about 160 s of tool time for a server that answers in milliseconds.

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

**Where `AsyncPutBlobIfVersion` does not fit yet.** The task index has rows that several writers update by reading, changing and rewriting them, for example a task tree's member list (`clio_agent/gact/task_index_writes.py`, `_merge`). Two things stop a compare-and-swap there:
- these rows are staged and written with the rest of a group (the same multi-put as above);
- CLIO's segment records are raw, append-only chunks with no length frame, so a shorter rewrite needs truncation. `AsyncPutBlobIfVersion` writes at an offset and never truncates. (CLIO's index rows carry their own length frame, so for them truncation is not the blocker.)

**Ask.**
1. A multi-put where each record carries a precondition (absent, or an expected version) and the result carries a status and new version per record, like `MultiPutBlobTask` with per-put status (issue 2).
2. A replace variant of `AsyncPutBlobIfVersion` that sets the blob to exactly the given bytes, or a version precondition on put-replace (`kCtePutReplace`, issue 2).

**Acceptance.** Python can create or compare-and-swap N records in one round trip with a status per record, and a compare-and-swap can leave a blob shorter than before.
