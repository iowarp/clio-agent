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

## Measurement notes

- Micro-benchmarks ran against the live daemon in namespaces `bench-arc-a` and `bench-arc-b`, and every record they wrote was deleted. The scripts are in the session scratchpad and are not part of the repository.
- Byte counts come from the stream audit (`CLIO_STREAM_AUDIT_LOG`) of the evaluation sessions.
- Not yet measured: the gain from more daemon threads under concurrent writers, and the end-to-end effect of each change on P6/P7. Those come with the implementation.
