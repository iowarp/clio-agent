# ARC on clio-core: where the time goes and how to make it faster (2026-11-01)

The tool-calling evaluation showed CLIO's own bookkeeping, not the model or the agent, limiting MCP-heavy work. P7 (2,000 records) spent 187 s of 333 s in tool time against 21 s of model time. A synthetic `get_record` that answers in milliseconds took 0.8-1.8 s per call. This review traces every clio-core write and read on the tool-call and model-step paths and on the storage layer. It measures what it can against the live daemon, with micro-benchmarks in dedicated namespaces whose records were deleted afterwards. It then proposes changes in two groups: (a) faster with the same guarantees, and (b) faster by relaxing durability where the owner accepts it.

Constraints every proposal keeps (owner, 2026-11-01):

- Everything stays in clio-core. No new store, and no local file as a source of truth.
- The agent process stays mostly stateless, so work can later run on a swarm of agents.
- clio-core is the source of truth for the event highway.

The owner accepts dropping durability for read-only and millisecond operations (group b).

## What it costs today

### One MCP tool call (inline reply, such as `get_record`)

Writes, all on the call's critical path (the call waits for each):

| Step | What is written | Where |
| --- | --- | --- |
| start | `tool.call.started` to `_events` | `gact/tool_observer.py:559` |
| admission | submission content `_events/t/content/<sha>` (list + put); `task_lookup r.<ref>`; `task.submission` to `_events/t/log` then to `_events`; `task_lookup h.<handle>`; `task_open t.<handle>`; `task.observed` (accepted) to both | `gact/external_tasks.py:272-309`, `gact/task_history.py:146-278`, `gact/task_event_lane.py:119-126`, `gact/task_index_writes.py:101-139` |
| the MCP call itself | milliseconds | `gact/external_tasks.py:360` |
| result | result content; `task.observed` (completed) to both; `task_open` delete (2 RPCs) | `gact/external_tasks.py:656-671` |
| after | artifact index (list rewrite, confirm re-put, each with a wasted companion delete); `artifact.transform.recorded` and `tool.call.completed` to `_events` | `gact/artifacts/transforms.py:494-524`, `gact/tool_observer.py:723` |

That is about 15 appends plus index writes and reads, 43 ms per call even serially on an idle daemon with empty chunks. Two global choke points sit on top:

- **One writer for the whole server.** `CoreIO` is a thread pool with `max_workers=1` (`gact/core_io.py:48-53`), and `TaskHistory._lock` serializes every admission, including in-memory reads (`external_tasks.py:272,426`) that are queued behind other calls' writes. In a trace, 8 calls started within 23 ms; their admissions finished one by one at 260 ms, 600 ms, ..., 1.26 s.
- **Every task event is written twice.** It goes to the `_events/t/log` lane, which exists so a task tree loads without reading full transcripts, and then to `_events`.

A short call writes two `task.observed` events (accepted and completed), never polling ones. A server that answers with a task adds one per `tasks/get` reply, even when nothing changed, and one per progress notification.

### One model step

The model is 80% of a short turn. On P1, 14.7 s of 18.4 s is model time (TTFT 11.6 s, decode 3.1 s); tools are 2.5 s and CLIO's own work is 1.24 s. The share that is CLIO's own work grows with the width of a step, because every write is synchronous on the loop's path:

- **About 20-25 puts per step:**
  - `lm.call` with the full prompt (25-35 KB, growing each call, `lm/call_trace.py:266-286`);
  - `step_open`;
  - `step_done`: 5 segment writes, each with a search-companion put (`gact/agents/clio_react_record.py:509,555-597`);
  - `record_progress`;
  - `react.step.completed`;
  - `commit_input`: 3 puts (`clio_react_record.py:345-459`).
- **Before the first model call:** 0.62 s median. That is admission writes (0.2 s) and building the agent and toolset on every turn (0.25 s; 5.5 s the first time a plugin's MCP server starts).
- **The claude_code provider:**
  - It closes its pooled CLI connection at the end of every turn, and the idle reaper drops it during any tool step longer than 15 s (`providers.claude_code.stream_idle_ttl_s`).
  - Each re-send costs about 0.45 s of TTFT. The server-side prompt cache still hits.
- Base P6 showed a 2.7 s gap after its 200 direct calls, as `step_done` wrote each call's segments one at a time.

### The storage layer

**Whole-chunk re-put on every append.** The `_events`, `_events/t/log` and `_events/m` (message parts) lanes are chunk families of 512 events (`arc.events_chunk_segments`, `arc/memory.py:135-140`). Each append re-encodes the whole active chunk, base64-encodes it, pads it to a size class and puts it again (`arc/segments.py:260-296`, `arc/segment_cache.py:162-180`, `arc/blob_frame.py:150-153`). A put is 4 KB at the start of a chunk and 0.5-1.5 MB near its end, and 5.7 MB for a chunk holding large events.

| Session | Data stored | Data sent to clio-core | Amplification |
| --- | --- | --- | --- |
| P7 run (`sess_a259752c3c37`): 6,629 puts | about 21 MB | 4.37 GB | about 205× |
| P5 run | about 1 MB | 68 MB | about 70× |

- **Measured:**
  - 512 appends of 1.7 KB cost 6.83 ms each on average.
  - The same appends written at an offset into the chunk cost 0.196 ms (35× less) and send about 350× fewer bytes.
- **The guard on every RPC adds about 1.2 ms per operation** (`arc/rpc_liveness.py:198-262,509-561`, `arc/clio_core_async_ops.py:69-110`). Raw `AsyncPutBlob` takes 1.09 ms; guarded, 2.29 ms. The overhead comes from:
  - a psutil sample of the daemon before each call (0.29 ms);
  - a hand-off to a stall-watch thread;
  - a first sleep of 1 ms before checking completion.
- **One daemon I/O worker.** The generated `cte.yaml` has `runtime: num_threads: 4`, a literal in `arc/clio_core_config.py:145`. That gives one I/O worker, and every put of 4 KB or more (all of CLIO's, because of the size-class floor) queues on it, from every server on the machine (`work_orchestrator.cc:85-90`, `default_sched.cc:63-130` in clio-core).
- **Unused clio-core features** (iowarp-core 2.3.2; the cloned source matches the installed binding):
  - async gets (`AsyncGetBlob`, `AsyncGetBlobSize`, `AsyncGetContainedBlobs`);
  - batched puts (`AsyncPutBlobDefer`: one RPC per 64 puts or 128 KiB);
  - offset writes and reads (`PutBlob` and `GetBlob` take an offset);
  - `AsyncBlobQuery`, which does return rows; the docstring at `clio_core_async_ops.py:17-20` saying it does not is stale;
  - raw bytes, which round-trip without base64;
  - `TemporalSearch`.
- **Smaller wastes:**
  - stale search-companion deletes on `artifact_index` puts, a kind that is never indexed (4.5 ms against 2.3 ms per put);
  - the full segment copied into an `arc.op` file-trace event on every append (21 of 31.6 MB in one session's trace);
  - a stream-audit row per put when `CLIO_STREAM_AUDIT_LOG` is set.

## (a) Faster with the same guarantees

Ranked by measured or estimated impact.

1. **Append only the new bytes.** Write each event at an offset into the padded chunk as a length-prefixed record with a checksum; the reader stops at padding. The chunk family, ordering, names and readers stay the same, and old chunks stay readable. Measured 35× per append and about 350× fewer bytes. This removes most of P6's and P7's tool-time overhead and stops overhead growing over a session. An alternative with the same effect is to roll chunks by bytes (about 64 KB) instead of at 512 events.
   - **Implemented** (`arc/chunk_records.py`): every segment-list record is `MAGIC`, `[u32 length][u32 crc32][msgpack Segment]` records and an 8-byte zero end marker, raw (no base64). An append is one put at the stored end; a growing put is zero-padded to the next size class, because unpadded growth added one clio-core block per append (300 appends of 1 KB: 3.6 MB of `metadata.log` against 0.36 MB). By owner decision old base64 chunks are not read: they fail typed `arc_chunk_format_unsupported` (reset the CLIO state). `arc_bench --only append`: 520 KB and 6.3 ms per append before, about 4.3 KB (record plus amortized class padding) and 2.3 ms after; the remaining time is mostly the per-RPC guard, item 6.
2. **Order per task tree or session, not globally.** Shard `CoreIO` and `TaskHistory._lock` by session or tree root. Take every shard only for a merge or the one-time index build (`gact/task_index_writes.py:212`). The worker count per shard becomes a conf key (`arc.core_io_workers`).
3. **Read without queueing.** Serve the in-memory reads on the call path (latest assignment, task snapshot) from `read_view()` (`gact/task_history.py:650`) instead of queueing them behind writes on `CoreIO`.
4. **Group commit and batched puts.** One writer per lane drains its queue and coalesces queued appends to the same chunk into one put (the `session_records` writer pattern, `gact/session_records.py:20-26`). An admission's content and index rows become one batched put with `put_many`, or `AsyncPutBlobDefer`, then one for the lane and `_events`: about 10 round trips become 2. A step's own writes (`step_done`, `record_progress`, `react.step.completed`) become one put per lane. Callers still wait for confirmation where they do today.
5. **Write each task event once.** Keep the lane and the highway. The readers of `_events` that need task events (export, provenance) merge `_events/t/log` by `logical_time`, as `lane_segments` already does. This halves task-event writes.
6. **A cheaper guard.**
   - Run the GIL-free async RPCs inline: `await_future` already watches progress.
   - Take the daemon sample only once an RPC is slow, after about 100 ms pending.
   - Start polling at about 0.1 ms.
   - Measured on a small operation: about 1.8 ms becomes about 0.2 ms.
7. **Pipelined async gets** for cold loads, chunk walks and session-record reads (`AsyncGetBlob`): 45× on reads.
8. **Fewer records for a short call.** Fold `task.submission` and the accepted `task.observed` into one lane record. Same lock and same durability; the event schema changes. Stop writing an observation for `tasks/get` replies that change nothing.
9. **`lm.call` without the full prompt each time.** Store the prompt as content-addressed sections, or as a delta from the previous call. Every later chunk put is then smaller.
10. **Cache the built agent and toolset** keyed by the resolution digest and tool allowlist, and start a plugin's MCP servers when the session is created. Both can be rebuilt from clio-core and the plugin snapshots, so the process stays stateless.
11. **Provider.** Exempt a session's pooled CLI connection from the idle reaper while its turn is active, and keep it across turns of the same session. This saves about 0.45 s per re-send.
12. **Small fixes:**
    - Add `artifact_index` to the never-indexed kinds (`arc/companion_policy.py:22`).
    - Drop base64 from new blobs (25% of bytes).
    - Stop copying full segments into the `arc.op` trace for the `_events` family.
    - Drop `full_text` from per-chunk stream-audit rows.
13. **Configuration.** The daemon's `num_threads` and CLIO's `CoreIO` workers become conf keys (`arc.core_threads`, `arc.core_io_workers`), and the `cte.yaml` template reads them. Six to eight daemon threads give 2-4 I/O workers. This matters once the client side writes concurrently (items 2 and 4), bounded by the machine's cores.

## (b) Faster by relaxing durability

These change what CLIO can recover after a crash. Today every MCP call is write-ahead: no call is sent without a confirmed submission record. That record is `TaskAwareClient.call_tool_mcp`'s requirement (`gact/task_client.py:251-255`), and recovery uses it to reconnect to a server-created task through `tasks/get` instead of resubmitting it (`gact/external_tasks.py:83-196`). For an inline call, its only use is the crash marker "Acceptance identity is unknown... nothing was resubmitted".

1. **No write-ahead for read-only and idempotent calls** (`readOnlyHint`, no task support). The call goes out at once and one terminal record is written behind it through an ordered writer. If the server answers with a task anyway, CLIO records and adopts it then, as today.
2. **A fast path for short calls.** Queue the write-ahead without awaiting it. A call that finishes within a threshold (a conf key) collapses submission, accepted and completed into one terminal record. A call still running past the threshold gets today's records and recovery.
3. **Write-behind for highway-only events:** `task.observed` progress, `artifact.*`, `tool.call.started`/`completed`, and the records of a `run_code` script's nested calls. They go to a per-turn ordered writer, like the message-part minter (`gact/part_atom_minter.py`), drained before the next model request and at the end of the turn. The highway is still derived from clio-core after each append. The working-set segments the next model call reads stay synchronous.
4. **Record `lm.call` after tool dispatch,** not before it.

What a crash can lose under (b): the in-flight record of a read-only or short call (it is re-run or reported as unknown), and the last moments of highway events that had not yet been written. Long-running and task-capable calls keep full recovery.

## Changes needed in clio-core (upstream)

- **Scope blob queries to the tag.** `BlobQuery` and `GetContainedBlobs` scan the daemon's whole blob map, not the tag (`core_runtime.cc:11250-11290,11402-11425`): 2.8 ms over 38,178 blobs, about 70 ns per blob. A per-tag index already exists (`blob_index_`, `core_runtime.h:765-773`), but only tag deletion uses it. Using it in queries is a small fix, and it is required before one-blob-per-event can scale.
- **Expose to Python** `kCtePutReplace`, `TruncateBlob` and the multi-get (`PodMultiGetBlob`), plus tag deletion. A benchmark tag, `bench-arc-b/segments`, is left behind because Python cannot delete a tag.

## One blob per event (the owner's idea)

Each event becomes its own blob in a per-session bucket, named so that one query selects a subset, for example `<logical time>.<writer>.<kind>.<task id>`.

- **What it would remove:** the `_events/t/log` copy (one regex query finds a session's task events) and all chunk re-encoding (each event is one small put of about 1.7 KB).
- **Why it has to wait:**
  - every query currently scans the whole daemon, so query time grows with all history on the machine (about 70 ms at a million blobs);
  - a full transcript would need about 3,000 pipelined async gets (about 90 ms, fine) instead of today's sequential gets (4-5 s);
  - `logical_time` is per process, so a swarm needs a writer id in the name;
  - query results come back in hash order and must be sorted by the client.
- **Order of work:** item (a)1, appending at an offset, gets most of the write-side benefit now without multiplying blobs. One blob per event follows once the per-tag query fix lands upstream and reads use pipelined async gets.

## Suggested order

1. (a)1 append at an offset; (a)6 cheaper guard; (a)12 small fixes. These are low risk and give the largest measured gain.
2. (a)2 and (a)3: order per session or tree; read without queueing.
3. (a)4 and (a)5: group commit; write each task event once.
4. (a)9 to (a)11: smaller `lm.call` records, cached agent build, provider connection reuse.
5. (b)1 and (b)2 once the owner confirms the recovery trade, then (b)3.
6. Upstream asks; then one blob per event.

Measure each step on P5-P7 and P1 with `scripts/eval/tool_calling` (wall, tool and model time from `behaviour.py`; puts and bytes from the stream audit), and record the results in the tool-calling ledger.

## Measured after (a)1, (a)6 and (a)12 (2026-11-02)

`arc_bench` (shared daemon, namespace `bench-*`):

| Measure | Before | After |
| --- | --- | --- |
| One event append | 5.05 ms, 520 KB | 0.51 ms, 4.3 KB |
| Task-history writes of one MCP call | 41.5 ms, 190 KB, 15 store calls | 13.4 ms, 24 KB |
| Eight such calls at once (`CoreIO`) | 448 ms | 216 ms |
| Small guarded read | 1.21 ms (p90 2.21) | 0.57 ms (p90 0.76) |

End to end, one run each on the current build (config `arc-w3`, namespace `eval-current-w3`), against the three earlier `current` runs:

| Task | Wall s | Tool s | Bytes sent to clio-core | Core puts |
| --- | --- | --- | --- | --- |
| P6 before | 44 | 28 | 1,438 MB | 2,758 |
| P6 after | 29 | 14 | 12.7 MB | 2,755 |
| P7 before | 333 | 303 | 13,840 MB | 25,048 |
| P7 after | 185 | 159 | 107 MB | 26,494 |

Bytes fell by more than 100×, and tool time roughly halved. The number of puts did not change: this work made each write smaller and cheaper, not fewer.

**What limits P7 now: one call at a time at admission.**

- The model's script sends `get_record` in `asyncio.gather` batches of 100, and CLIO keeps up to 9 calls in flight (`tools.per_server_concurrency` 8).
- But the records server sees them strictly one after another: in 2,079 calls, only one gap was under 5 ms, and the median gap was 40 ms (p90 197 ms).
- In the trace, `task.submission` records are a median 46 ms apart. Each call waits for its write-ahead submission to be confirmed through the single history writer before it is sent.
- Each call writes about 13 records (11 `arc.op`, two `task.observed`, the submission, `tool.call.started` and `.completed`, `artifact.transform.recorded`). That is about 46 ms in the live server, against 13.4 ms for the task-history part alone in the benchmark.
- 13 pauses longer than 0.5 s add 45 s; those are model steps.

Ordering per session or tree ((a)2) does not help this case: all 2,021 calls belong to one session and one tree. What helps, in order:

1. (b)1: no write-ahead record for read-only calls. `get_record` is sent at once, and its one terminal record goes through the ordered writer.
2. (a)4: group commit, so nine queued records to one lane become one put.
3. (a)8: fold the submission and accepted observation into one record.

## Measured after the read-only path and the profile fixes (2026-11-02)

One P6 and one P7 run per build, current branch, fresh namespace each. Run-to-run spread on P7 is about ±15 s.

| Build | P6 wall s | P7 wall s | P7 tool s | P7 puts | P7 MB |
| --- | --- | --- | --- | --- | --- |
| After append, guard and reads (`arc-w3`) | 29 | 185 | 159 | 26,494 | 107 |
| Read-only calls skip write-ahead (`arc-w6`) | 34 | 218 | 186 | 26,516 | 105 |
| Store probe uses its TTL, audit handle kept open (`arc-ttl`) | 30 | 210 | 177 | 26,495 | 118 |
| Busy daemon polled before sampling, 8 daemon threads (`arc-w10`) | 30 | 202 | — | — | — |
| Cross-job workspace set built lazily (`arc-tx`) | 31 | 197 | 170 | 26,383 | 103 |
| Read-only inline call settles in one snapshot (`arc-fold`) | 29 | 193 | 160 | 22,470 | 90 |
| A call's tasks found by invocation index, not a session scan (`arc-idx`) | 27 | 166 | 140 | 22,449 | 90 |
| Lockfile hashed once per state, not per call (`arc-lock`) | — | 161 | — | — | — |
| Artifact-index lists appended a page at a time (`arc-page`) | 26 | 155 | 127 | 22,449 | 90 |
| A tool result's call found by index, not a turn scan (`arc-seal`) | 26 | 155 | 126 | 22,457 | 90 |

Three of these were quadratic in the session's length:

- `own_execution_tasks` read every session task at each completion.
- `add_transform` rewrote the session's whole transform list per call.
- `append_part` scanned every part of the turn per result.

With them gone, the median gap between calls is flat across the run: 52, 54, 55 and 53 ms by quarter, against 59 to 66 ms before. From the first evaluation to now, P7 went from 333 s to 155 s and its tool time from 303 s to 126 s.

What the profiles showed:

- **The server is GIL-bound during a call burst.**
  - Server CPU was about 106% and the daemon's about 136% while P6's 202 calls ran.
  - P7 runs at about 13 calls a second, a median 71-83 ms apart. That is Python CPU per call, not waiting.
  - The idle daemon still uses about 60% of a core: it busy-polls.
- **Removed (each confirmed by a GIL-holder profile from `py-spy record --gil`):**
  - **The store's liveness probe.** It opened a TCP connection to the daemon on every store op, under the gate lock: a literal `ttl_s=0.0` overrode `arc.clio_core.liveness_ttl_s`. It was on 17 of 81 active stacks.
  - **The stream audit.** It reopened its file and recreated the directory on every row: about 10% of active time, in every evaluation run.
  - **The daemon progress sample after a 1 ms miss.** It located the pidfile and read `/proc` on most puts while the daemon was busy: 21 of 91.
  - **The cross-job workspace set.** `realpath` was run on every registered workspace root, on every tool call: a third of transform recording.
  - **A running snapshot followed at once by a completed one** for read-only inline calls: about 4,000 puts per P7 run.
- **What is left is spread across layers** (211 samples of the GIL holder over 591 calls):
  - task-history writes for admission and result: 28%;
  - the tool observer's `tool.call.*` and transform events: 21%;
  - the trace writer thread: 9%;
  - the MCP transport: 6%;
  - the rest is the run_code dispatch and the event loop.

Each record still costs a semantic event, a lane append, a highway append and an `arc.op` trace event. P7 tool time is about 160 s against well under 1 s in the server itself. Closing that gap needs the structural items, in this order:

1. **(a)4 group commit and one put per lane per step.** Fewer appends.
2. **(a)9 and W7: a per-turn ordered writer for highway-only events.** `tool.call.*`, `artifact.*` and nested `run_code` records would be written off the call path and flushed before the next model request.
3. **Write each task event once ((a)5).**

The idle daemon's busy-poll is a clio-core matter (upstream).

## Read-only records behind the reply, group-committed (2026-11-03)

A py-spy profile of P7 showed the one history-writer thread (`clio-core-io_0`) spending 98% of its active samples in a read-only call's settled admission (`LazyAdmission.settled` -> `ExternalTasks._call.confirm`): about 13 clio-core round trips per call, one at a time, while the thread was CPU-active only 12% of the time. And `_call` awaited that record before returning, so each call held its slot until it was confirmed.

What changed:

- **The reply no longer waits for the record** (`gact/read_only_outcomes.py`). A read-only call with nothing recorded before its reply queues its one record (admission already settled) on the history writer and returns. The writer is FIFO, so every later operation on it sees the record. Readers that do not go through the writer wait for the queue first: the task tools (`query_tasks`, `observe_tasks`), turn retirement and shutdown. A failed record is logged `read_only_outcome_deferred` and handed to `CoreRecovery`. A retry that fails for any reason but a recording hold is logged `read_only_outcome_unrecorded` and dropped. Write calls keep write-ahead unchanged.
- **Group commit** (`gact/core_io.py` `submit_batch`, `gact/task_history_group.py`, `arc/segment_group.py`). Records queued while the writer is busy run in one writer turn. Their segment appends are staged and put once per scope: task content first, then the task-event lane, then `_events`, each rank one `put_many`. Index rows go in one `put_many` before them. Projections, highway events, UI hints and closing deletes run after the flush. A scope another thread appended to meanwhile (the shared `_events` log) is rebased.
- **No reads for what cannot exist yet.** The call's freshly minted handle and reference are not looked up (`TaskIndex.assume_absent`). Its content records are probed in one pipelined `sizes` call per group, and the submission content, which embeds the minted reference, is not probed at all.

`scripts/bench/mcp_burst_bench.py` (200 read-only calls, 8 in flight, a 2 ms handler, the shared daemon, machine load 10-12 on 4 cores, two interleaved runs each):

| Measure | Before | After |
| --- | --- | --- |
| Calls answered per second | 10.7-11.1 | 249-332 |
| Median call latency | 607-692 ms | 15-19 ms |
| Writer time per call | 89-93 ms | 6.4-7.5 ms |
| Store RPC calls per call | 13 (8 puts, 5 gets) | 0.08-0.10 (a group: 1 probe, 1 index `put_many`, 3 segment `put_many`) |
| Records settled after the last reply | 0 ms (each call waited) | 741 ms |

All 200 calls recorded, end to end in 1.3-1.6 s against 18-19 s before. The remaining writer time is clio-core processing the blobs themselves (about 4 per call: two content records, two index rows) and Python work per record.

Not used: clio-core's deferred puts (`AsyncPutBlobDefer`). They batch small puts into one native task, but small writes may wait in client-side sieve pages, a put has no confirmation of its own (only a sticky process-wide `DeferErrorCount` and the process-wide drain `AwaitPutsUntilSpace(0)`), and a failure cannot be attributed to a blob. Group commit needs both a per-record confirmation before it reports a record durable and per-blob failures to abandon a chunk.

## Measurement notes

- Micro-benchmarks ran against the live daemon in namespaces `bench-arc-a` and `bench-arc-b`, and every record they wrote was deleted. The scripts are in the session scratchpad and are not part of the repository.
- Byte counts come from the stream audit (`CLIO_STREAM_AUDIT_LOG`) of the evaluation sessions.
- Not yet measured: the gain from more daemon threads under concurrent writers.
