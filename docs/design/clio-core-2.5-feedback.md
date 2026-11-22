# clio-core 2.5.0: what CLIO found (feedback for the clio-core developer)

**From:** the CLIO agent project. **Release checked:** iowarp-core 2.5.0 (PyPI wheel; tag
`v2.5.0` = `4748ef0d`). **CLIO branch:** `claude/clio-core-2.5-upgrade`. Companion
document with the upgrade details and benchmarks: `docs/design/clio-core-2.5-upgrade.md`.

You asked us to upgrade, test, check 2.5.0 against CLIO's requirements and tell you
what does not meet them. This is that report. CLIO did not change clio-core and did not
build workarounds for the gaps below; each unmet item is a request with the observed
behaviour at v2.5.0 and a minimal reproduction. Source references are `file:line` at
`v2.5.0`. "Live" means run on a 4-vCPU Linux x86_64 container with the 2.5.0 wheel
(and the 2.4.1 wheel for comparisons), through CLIO's own store unless stated.

#1342 (Python binding for `clio_cte_stream`) is closed on purpose and is not counted.

## 1. Verdicts at a glance

| Requirement | Verdict | Section |
|---|---|---|
| #1336 scope BlobQuery / GetContainedBlobs to the tag | **Met** | 3.3 |
| #1337 Python bindings: put-replace, TruncateBlob, multi-get, multi-put, DelTag | **Not met** | 3.2 |
| #1338 deferred-put durability / await | **Partly met** (deferred API removed; no durability barrier in Python) | 3.1 |
| #1339 idle daemon CPU | **Met** (0.52 -> 0.037 of a core, live) | 3.4 |
| #1340 macOS 14 wheels | **Not met** (still `macosx_15_0_arm64` only) | 3.5 |
| #1341 conditional puts in a batch / replacing conditional put | **Not met** | 3.6 |
| #1355 control channel bind | **Met** (verified live) | 3.7 |
| #1359 Windows reserved shared memory | **Partly met** (client segment configurable; no reserve/commit) | 3.8 |
| #1312 WAL unbounded growth | **Met in code**, not reproducible here | 3.9 |
| Metadata log: torn tail followed by appends | **Not met** | 4.1 |
| Metadata log: ENOSPC tears it silently | **Not met** | 4.2 |
| Metadata log: alloc log vs metadata after a torn delete | **Not met** | 4.3 |
| Metadata amplification for small appends | **Not met** (unchanged: 34 bytes of log per data byte) | 4.4 |
| Daemon memory grows ~6 KiB per stored blob (clio-agent#1846) | **Not met** (unchanged since 2.4.1) | 3.10 |
| New in 2.5.0, found during the upgrade | 10 items | 5 |

## 2. What 2.5.0 fixed or improved for CLIO

- The control channel binds through `DefaultServerBindAddr` (#1355). CLIO removed its
  port-hold stopgap (clio-agent#2038). Live, with `CLIO_BIND_ADDR=127.0.0.1`: 2.4.1
  listened on `0.0.0.0:<port+2>`; 2.5.0 listens on `127.0.0.1` for every port.
- The runtime listens on loopback by default (98bb4eb4).
- BlobQuery and GetContainedBlobs walk the per-tag index (#1336).
- Idle wake-up storm fixes (fa77434c, 57ef5025, ee5d7a33, 4964c577).
- FlushMetadata no longer stalls forever on a token and reports a runaway WAL (#1312).
- `runtime.client_data_segment_size` / `CLIO_CLIENT_DATA_SEGMENT_SIZE` (#1359, ask 3).
- rc 28 for a device ENOSPC on the data path (33fedf5d). CLIO now types it
  (`clio_core_device_full`).
- An explicit core `durability:` key (7822175b). CLIO's seed now states
  `durability: persistent`.
- A 2.4.1 store restores under 2.5.0 (verified live, 200 of 200 records).

## 3. The #1335 items

### 3.1 #1338 deferred-put durability / await: partly met

**At v2.5.0.** `AsyncPutBlobDefer`, `AsyncGetBlobDefer`, `AwaitPutsUntilSpace` and
`DeferErrorCount` are removed from the Python binding (058edeac; they were at
`core_bindings.cc:746/772/791/800` in 2.4.1). That settles the "when is a deferred put
durable" question by removing deferred puts from Python. The replacement design
(`core_shm_cache_writethru/DESIGN.md:340-372`) batches writes in the shared-memory
cache and uses `SyncTag` as the barrier, with flush errors reported per tag by
`SyncTag`. **`SyncTag`, `FlushData` and `FlushMetadata` are not bound in Python**, so a
Python client has no way to wait for durability or to learn that a cached write failed
to flush.

`DESIGN.md:370` says "Python AsyncPutBlob gets the cache path"; the code says the
opposite (`core_client.h:490-504`: the private-buffer `AsyncPutBlob` is task path only;
only the synchronous `Client.PutBlob` uses the cache).

**Request.**
1. Bind `SyncTag(tag_id)` (and `AsyncSyncTag`) in Python, returning the per-tag sticky
   rc, so a client can (a) wait until everything it put on a tag is flushed past the
   cache and (b) learn which tag lost a write.
2. Document, for the task path, what `AsyncPutBlob` completion promises: data written to
   the target and the WAL record appended (flushed to the page cache, fsync'd at the next
   FlushMetadata, ~5 s). CLIO relies on exactly this today.
3. Fix `DESIGN.md:370`.

**Reproduction.** `python -c "import iowarp_core, clio_cte_core_ext as c; print([m for m in dir(c.Client) if 'Sync' in m or 'Flush' in m])"` prints `[]`.

### 3.2 #1337 Python bindings: not met

**At v2.5.0.** None of the requested operations is bound
(`context-transfer-engine/wrapper/python/core_bindings.cc`). The C++ client has them:
`kCtePutReplace` (`core_tasks.h:2395`), `AsyncTruncateBlob` (`core_client.h:1369`),
`AsyncDelTag` (`:1387`), `AsyncMultiDelTag` (`:1431`), `AsyncMultiPutVectored` (`:572`),
`AsyncSyncTag` (`:2032`), and the new `kCtePutAppend` (3cbe1fcb). No Python put takes a
flags argument.

**Why CLIO needs them (unchanged from #1337).** A transcript read is up to 3,000
sequential gets (4-5 s for a long session); a shrinking record needs an end marker
instead of a truncate; benchmark and test tags cannot be deleted; one tool call costs
6.93 store round trips.

**Request.** Async Python methods, GIL released while waiting, returning a `Future`:
`AsyncPutBlob(..., flags=0)` accepting `kCtePutReplace` and `kCtePutAppend` (with the
landed offset in the result), `AsyncTruncateBlob(tag_id, name, size)`,
`AsyncMultiGetBlob(tag_id, [(name, size, off)])` and
`AsyncMultiPutBlob(tag_id, [(name, data, off)])` with a per-item rc, and
`AsyncDelTag(tag_id)`.

**Reproduction.** `python -c "import iowarp_core, clio_cte_core_ext as c; print(sorted(m for m in dir(c.Client) if not m.startswith('_')))"`; none of `TruncateBlob`, `MultiGet*`, `MultiPut*`, `DelTag` appears.

### 3.3 #1336 scope BlobQuery / GetContainedBlobs: met

`GetContainedBlobs` (`core_runtime.cc:11533-11536`) and `BlobQuery` (`:11660-11663`) now
iterate `BlobIndexNames(tag_id)` (8599942d, c5335132, merge 6b6307eb). Two small
leftovers: `BlobQuery` still builds an unused `std::regex tag_pattern`, and
`GetContainedBlobs` logs its telemetry as `CteOp::kGetOrCreateTag`.

### 3.4 #1339 idle daemon CPU: met

Live, CLIO's daemon on its own config, measured after 30 s of idle over a 60 s window
(user + system CPU of every thread; `scripts/bench/idle_and_metadata_bench.py`):

| | 2.4.1 | 2.5.0 |
|---|---|---|
| Idle, fresh daemon | 0.519 cores (51 threads, 35 MiB RSS) | **0.037 cores** (54 threads, 32 MiB) |
| Idle after 1,800 writes, 30 s window | 0.505 cores | **0.039 cores** |

Commits fa77434c, 57ef5025, ee5d7a33 and 4964c577 are in the release. Under the
acceptance ("a few percent of one core"). Nothing further requested.

### 3.5 #1340 macOS 14 wheels: not met

PyPI `iowarp-core` 2.5.0 publishes `macosx_15_0_arm64` wheels only (cp310-cp315), the
same as 2.4.1. CLIO's macOS floor therefore stays at 15. There are also no free-threaded
(`cp314t`) wheels.

**Request.** Build the macOS wheels with `MACOSX_DEPLOYMENT_TARGET=14.0`, or name the
macOS 15 API that prevents it.

**Reproduction.** `curl -s https://pypi.org/pypi/iowarp-core/2.5.0/json | python3 -c 'import json,sys; print([u["filename"] for u in json.load(sys.stdin)["urls"] if "macos" in u["filename"]])'`.

### 3.6 #1341 conditional puts in a batch / replacing conditional put: not met

**At v2.5.0.** `AsyncPutBlobIfAbsent`, `AsyncPutBlobIfVersion` and
`AsyncGetBlobVersioned` are bound, unchanged since 2.4.1. There is no batched
conditional put (`wrapper/python/CONDITIONAL_WRITES.md`: a batched put "does not honor
these conditional flags") and no replacing variant (`ConditionalPut` sends `flags=0`;
`AsyncPutBlobIfVersion`'s docstring: "Puts overwrite a range and never truncate").

**Why CLIO cannot adopt the single-blob form.** CLIO's conditional writes are groups: a
memory document write is the document, its search companion and a version record,
issued together through one multi-put; content-addressed records are written in
groups per tag. A per-row conditional put turns one round trip per group into one per
row, and the write is still not atomic across the group.

**Request.** (1) A multi-put whose items each carry a precondition (absent, or an
expected version) and whose result carries a status and the new version per item.
(2) A replacing `AsyncPutBlobIfVersion` (or a version precondition on
`kCtePutReplace`), so a compare-and-swap can shorten a blob.

### 3.7 #1355 control channel bind: met

Verified live (listening sockets of a CLIO daemon with `CLIO_BIND_ADDR=127.0.0.1`):
2.4.1 had `0.0.0.0:<base+2>`, 2.5.0 has `127.0.0.1:<base+2>`.

### 3.8 #1359 Windows reserved shared memory: partly met

`runtime.client_data_segment_size` / `CLIO_CLIENT_DATA_SEGMENT_SIZE` are parsed
(20222438; `config_manager.cc:580-586`, `:253-261`; default 512 MB + 32 MB). Reserve,
don't commit on Windows and grow on demand are not done; the commit message says so.
CLIO could not measure the Defender scan from a Linux container.

Live (`scripts/bench/client_segment_probe.py`, CLIO's store, one client then four
writers of 4,000 x 64 KiB each): the segment bounds the largest single put, and a put
that does not fit fails with the generic **rc 21**. CLIO's framing inflates a record
about 1.65x on the wire (a "48M" record is an 83 MB put).

| `CLIO_CLIENT_DATA_SEGMENT_SIZE` | Largest CLIO record that worked | Daemon `RssShmem`, 4 writers |
|---|---|---|
| 512MB (default) | 200 MB (330 MB put) | 366 MB |
| 128MB | 60 MB; 100 MB fails rc 21 | 113 MB |
| 64MB | 16 MB; 48 MB fails rc 21 | 34 MB |

CLIO therefore keeps the default for now: a smaller segment would bound memory but turn
a large record into a lost write. **Request.** Stream a put larger than the client
segment through it (in segment-sized pieces), or at least fail it with a distinct return
code that names the limit, so a client can split it.

**Request (unchanged).** Reserve, don't commit, on Windows; grow segments on demand.

### 3.9 #1312 WAL unbounded growth: met in code

00d6ab97 (FlushMetadata token wait bounded at 2 s, stuck blobs and a runaway WAL
reported at kError) and 3dcab85a (a holder counts as stuck only after 30 s; the WAL is
not truncated in a cycle that skipped a live holder) are in 2.5.0. The original stall
(io_uring `AIO-STUCK`) was not reproducible here; the soak in 3.10 shows the WAL
staying bounded.

### 3.10 Sustained load (clio-agent#1846)

One daemon, four client processes, 30 minutes of the clio-agent#1846 mix (60% small
appends of 256 B-8 KiB to new blobs, 20% growing transcript blobs rewritten in place up
to 2 MiB, 20% reads), CLIO's own store path (`scripts/bench/soak.py`). 2.4.1 runs
CLIO's develop code (what ships today), 2.5.0 the upgrade branch.

| | 2.4.1 | 2.5.0 |
|---|---|---|
| Operations (small appends) | 554,029 (332,621) | 594,136 (356,653) |
| Latency p50 / p99 / max | 8.7 / 69.7 / 945 ms | 7.8 / 74.5 / 1,308 ms |
| p99, first minute -> last minute | 64 -> 113 ms | 59 -> 117 ms |
| Ops per minute, first -> last | 16,825 -> 17,649 | 19,803 -> 19,663 |
| Ops over 1 s / over 15 s | 0 / 0 | 8 / 0 |
| Errors | 0 | 0 |
| Daemon RSS at 30 min | 5,016 MiB (anon 2,813, shmem 2,186) | 5,264 MiB |

Neither version stalled past CLIO's 15 s bound in 30 minutes on this machine, so the
#1846 latency failure did not reproduce. **The memory growth did, on both versions, and
2.5.0 does not change it.** The breakdown (`/proc/<pid>/status`, sampled every 10 s):

- **Shared memory** rises to 4 x 544 MB and stops: each client process's 512 MB + 32 MB
  data segment is mapped by the daemon, and the pages a client has used stay resident.
  That is bounded per client and is what `client_data_segment_size` now sizes (3.8).
- **Anonymous memory grows without bound, about 6 KiB per stored blob.** A probe
  (`scripts/bench/daemon_growth_probe.py`, one client) separates the causes:

  | Phase (10,000-40,000 ops) | 2.4.1 | 2.5.0 |
  |---|---|---|
  | Overwrites of 100 existing blobs | 0.07 KiB per op | 0.02 KiB per op |
  | New 4 KiB blobs | 6.07 KiB per blob | 6.20 KiB per blob |
  | New 256 B blobs / new 64 KiB blobs | | 6.21 / 6.21 KiB per blob |
  | Gets | 0.01 KiB per op | 0.00 KiB per op |

  The cost does not depend on the payload size, so it is per-blob metadata held in RAM
  for the life of the daemon. CLIO stores about 330,000 small records in 30 minutes of
  this load, about 2 GB of daemon memory; a person's daemon that runs for days reaches
  the 2.7 GiB of #1846.

**Request.** Bound the daemon's per-blob memory: a smaller resident record per blob,
and/or keep cold blob metadata on disk (the WAL and snapshot already hold it) with a
bounded in-memory cache. Report the resident metadata size (blobs, bytes) through a
monitor query, so CLIO can show it.

**Reproduction.** Start a daemon with a file tier, put 40,000 new 4 KiB blobs from one
client and read `RssAnon` in `/proc/<daemon pid>/status` before and after: it grows by
about 240 MB, and the same 40,000 puts rotating over 100 names grow it by under 1 MB.

## 4. Metadata log defects (the 16 Nov comment on #1335)

### 4.1 A torn record at the tail is skipped, then new records are appended after it: not met

**At v2.5.0.** `transaction_log.h` `Open`/`OpenFile` (~285-347) opens in `ios::app` and
leaves an existing file as it is; `Load()` (586-641) stops at a torn record and ignores
it and everything after (623-634). Nothing truncates the file to the last whole record
before appending, so every record written after a tear is invisible to the next start.
The only rewrite is `TruncateThrough` (659-686) at FlushMetadata compaction.

**Request.** At open, after `Load`, truncate the file to the end of the last whole
record (keep the cut bytes aside and log it at kError), before the first append.

**Reproduction** (CLIO's store, any `file` tier with `restart: true`):
1. Start, put 30 blobs, stop cleanly.
2. Truncate the largest `metadata.log.blob.<N>` in the middle of its last record.
3. Start, put 30 more blobs, stop cleanly.
4. Start and read all 60: the second 30 are missing.

CLIO keeps its pre-start repair (`arc/clio_core_wal_repair.py`) because of this.

### 4.2 A full file system tears the metadata log silently: not met

**At v2.5.0.** `WriteRecord` (`transaction_log.h` ~901-905) is `void`: it writes and
flushes and never checks `fail()`/`bad()`. `Sync()` (501-507) ignores `FsyncPath`'s
result. 33fedf5d's rc 28 covers the data path only.

**Request.** Check the stream after every metadata-log write and fsync; on failure fail
the put/delete with rc 28 (and log at kError), and do not acknowledge a mutation whose
WAL record did not land.

**Reproduction, live on 2.5.0** (`scripts/bench/core_repros/run.sh <venv> <dir>
full_wal`, root for the tmpfs, plain clio-core): store on a 6 MB tmpfs filled with a
filler file, 150 puts of 16 KiB: **150 of 150 return 0 and the daemon log names no
error**. Remove the filler and restart: 25 good, 1 unreadable, **124 missing**.

### 4.3 After a torn delete, the allocation log and the metadata disagree: not met

**At v2.5.0.** No change to the allocation log beyond skipping idle fdatasync
(4964c577). `core/METADATA_RECOVERY_DESIGN.md` (073752b2) is a proposal, not
implemented.

**Request.** Make a blob's block frees durable only after its delete record is durable
(or replay the alloc log against the restored metadata at start and refuse to hand out a
block that a restored blob still owns).

**Reproduction.** Put K and M, stop. Start, delete M, stop. Truncate M's delete record
mid-record. Start (M is back). Put N, stop. Start, put M with new bytes, stop. Start and
read N: it holds M's new bytes, with no error.

### 4.4 Metadata amplification for small appends: not met

Live (`scripts/bench/idle_and_metadata_bench.py`): the metadata log's growth for 600
writes of 700 bytes (420,000 bytes of data), each pattern on fresh blob names:

| Pattern | 2.4.1 | 2.5.0 |
|---|---|---|
| One blob grown by 600 appends at increasing offsets | 14,460,661 B (34.4 B per data byte) | 14,460,661 B (34.4) |
| 600 separate blobs | 126,980 B (0.30) | 126,980 B (0.30) |
| 600 CLIO framed records (`store.put`) | 124,580 B (0.30) | 124,580 B (0.30) |

Each put still logs a full-replacement `kExtendBlob` listing every block of the blob
(`core_runtime.cc:2789-2817`), so the log grows with the square of a blob's appends.
(The 16 Nov comment's "each append takes a 4 KB block" does not hold: appends fill the
last block's slack first, `core_runtime.cc:9655-9675`.)

**Request.** A delta record for a put that only adds blocks (log the new blocks, not
all of them), so an append-grown blob costs O(appends) of log, not O(appends^2).

**Reproduction.** Grow one blob with 600 `AsyncPutBlob(tag, "b", 700 bytes, off=i*700)`
calls and compare the size of `metadata.log*` before and after.

## 5. New in 2.5.0, found while upgrading

### 5.1 No release notes

2.5.0 has 411 commits over 2.4.1 and no changelog or release notes. CLIO found the
breaking changes below by reading the code. **Request:** a changelog with a "breaking"
section per release.

### 5.2 Daemon binary renamed without notice

The wheel ships only `bin/clio-run` (139125d5); `clio_run` survives only as a console
script. CLIO located the launcher by path and found nothing, so no daemon started.
**Request:** announce renames; keep a compatibility name in `bin/` for one release.

### 5.3 A long `USER` makes the daemon exit with one line on stdout

The IPC socket is `<$CLIO_MEMFD_DIR or /tmp/clio_$USER>/clio_<port>.ipc`. When that
path does not fit `sun_path` (108 bytes on Linux, 104 on macOS, where
`clio_zmq_<port+3>.ipc` is bound too) 2.5.0 refuses to start "half-bound"
(`ipc_manager.cc:763-787`) and exits 1 after one ERROR line on stdout. With a 5-digit
port a Linux `USER` of 82 characters starts and 83 exits. CLIO now refuses such a path
before spawning, typed (`clio_core_ipc_path_too_long`).

**Request.** Name the path, its length and the limit in the error (stderr), and consider
a socket name that cannot overflow (for example a short hash under the memfd dir).

**Reproduction.** `USER=$(python3 -c 'print("u"*83)') clio-run start` with the default
config: exit 1.

### 5.4 `AsyncRegisterTarget` without `persistence_level` is refused on a persistent core

`AsyncRegisterTarget(..., persistence_level=-1)` (e24a48fd) resolves -1 only against
devices loaded at start, so a target registered later is volatile, and a persistent core
refuses it with rc 16. Live: registering a 64 MiB file target on a running CLIO daemon
returns rc 0 on 2.4.1 and rc 16 on 2.5.0 without the argument, rc 0 with
`persistence_level=1`. CLIO's capacity growth hit this exactly when the store was full.
CLIO now passes the level. **Request:** default -1 to the core's own durability for a
late target (or to the level of the core's existing devices), and say "volatile target
refused by a persistent core" in the error.

### 5.5 A RAM data tier silently turns the whole core volatile

Without a `durability:` key a core is persistent only if every device is durable
(7822175b, `core_config.cc:200-222`); a volatile core keeps no WAL. A config with a file
tier beside a RAM data tier therefore loses every record on restart, with no warning.
Live: one record, clean stop, restart, get: kept on 2.4.1, **lost** on 2.5.0, kept on
2.5.0 with `durability: persistent`. **Request:** log at kWarning at start when the
durability is inferred volatile while a durable device is configured.

### 5.6 A volatile start deletes the existing store

Since 2.5.0 a core that resolves volatile (an unlabelled tier, or one RAM data tier
beside a file tier) also **deletes** the existing snapshot and metadata-log shards at
start (`core_runtime.cc:862`, `:8726-8752`), logged at INFO. Live
(`run.sh ... volatile_wipe`): 50 blobs, stop, remove the tier's `persistence_level`,
start and stop once: 8 metadata-log files before, **0 after**; restore the label and
start: **0 of 50 blobs**. CLIO now refuses a non-durable config before every spawn.
**Request:** refuse to start (or keep the files and log at error) when WAL or snapshot
files exist and the core resolves volatile.

### 5.7 The dashboard destroys pools on a cross-origin POST

Live (`run.sh ... viz`): with the dashboard on (the default for `clio-run start`
unless `CLIO_VIZ_ENABLE=0`), a form-encoded POST with `Origin: http://evil.example` to
`/api/pools/301.0/destroy` answers `{"ok":true,...}` and the pool is gone. CLIO forces
the dashboard off. **Request:** Origin/Host check or a per-start token on mutating
routes; dashboard opt-in.

### 5.8 A different stripe layout reads back wrong bytes

Live (`run.sh ... stripe`): written without striping, reopened with
`CLIO_BDEV_STRIPE_FILES=2`: 100 of 100 blobs read back **wrong**, no error; reopened
without it, 100 of 100 good. CLIO drops the variables from its daemon's environment.
**Request:** record the layout with the backing file and refuse on mismatch.

### 5.9 The synchronous calls cannot be bounded

`Client.PutBlob/GetBlob/GetBlobSize` are the only cache path and take no timeout;
`Tag.PutBlob/GetBlob/GetBlobSize` now call them. A stalled daemon holds the caller.
**Request:** an optional `max_sec`, or an async form of the cache path.

### 5.10 Smaller items

- `libclio_MOD_NAME_*` template libraries ship in the wheel.
- The Windows memfd dir is keyed on `USERNAME` (`%TEMP%\clio_%USERNAME%`), the POSIX one
  on `USER`; a process that sets `USER` to isolate its runtime is isolated on POSIX only.
- Telemetry (`PollTelemetryLog`) is unchanged since 2.4.1 and records only completed core
  ops in a per-container 1024-entry ring; it cannot tell a stalled daemon from an idle
  one. `clio_runtime_ext` `worker_stats` has `num_tasks_processed`, but it counts the
  periodic pollers, so it never stops rising. **Request** (clio-core#1112): export
  `RealTasksProcessed()` (`worker.h:318-335`) and the queued/blocked counts through the
  Python client, so a waiter can tell "busy" from "stuck".

## 6. Ready-to-post comments

Not posted. The text below is ready to post as written on the named issues; each ends
with the project's footer.

### iowarp/clio-core#1335 (comment): 2.5.0 checked against this request

````markdown
We upgraded CLIO to iowarp-core 2.5.0, tested it and benchmarked it against 2.4.1 on the same machine. Per item:

- #1336 met (both handlers walk the per-tag index).
- #1339 met: idle daemon 0.52 -> 0.037 of a core, live.
- #1355 met: the control channel listens on 127.0.0.1, live.
- #1312 met in code (FlushMetadata bounded, runaway WAL reported); not reproducible here.
- #1338 partly met: the deferred API is gone, but Python has no durability barrier (SyncTag/FlushData are not bound).
- #1359 partly met: the client data segment is configurable; reserve-don't-commit is not done.
- #1337, #1340, #1341 not met.
- The 16 Nov metadata-log defects are still present (torn tail followed by appends, a full filesystem under the WAL acknowledging lost puts, alloc log vs metadata after a torn delete), and append amplification is unchanged (34 bytes of log per data byte).

New in 2.5.0, filed separately with reproductions: the volatile-core WAL deletion, the full-filesystem WAL loss, the torn-tail append, the torn-delete block reuse, per-blob daemon memory, append amplification, the dashboard's unauthenticated pool destroy, the stripe-layout mismatch, the late-target `persistence_level`, the long-USER exit and unbounded synchronous calls.

The full report, with file:line evidence and minimal reproductions: `docs/design/clio-core-2.5-feedback.md` on iowarp/clio-agent branch `claude/clio-core-2.5-upgrade`.

---
_Generated by [Claude Code](https://claude.ai/code)_
````

### iowarp/clio-core#1336: met in 2.5.0

````markdown
Checked at v2.5.0: GetContainedBlobs (core_runtime.cc:11533-11536) and BlobQuery (:11660-11663) iterate `BlobIndexNames(tag_id)`. Two leftovers: BlobQuery still builds an unused `std::regex tag_pattern`, and GetContainedBlobs logs its telemetry as `kGetOrCreateTag`. From CLIO's side this can close.

---
_Generated by [Claude Code](https://claude.ai/code)_
````

### iowarp/clio-core#1337: not met in 2.5.0

````markdown
None of the requested operations is bound in `wrapper/python/core_bindings.cc` at v2.5.0 (no put flags, TruncateBlob, multi-get, MultiPutBlob with per-put status, DelTag). The C++ client has all of them plus the new kCtePutAppend and SyncTag (core_tasks.h:2395, core_client.h:572/1369/1387/1431/2032). Still needed, as async methods returning a Future with per-item rc for the multi forms. Check: `python -c "import iowarp_core, clio_cte_core_ext as c; print(sorted(m for m in dir(c.Client) if not m.startswith('_')))"`.

---
_Generated by [Claude Code](https://claude.ai/code)_
````

### iowarp/clio-core#1338: partly met in 2.5.0

````markdown
2.5.0 removes AsyncPutBlobDefer/AwaitPutsUntilSpace/DeferErrorCount from Python (058edeac), which settles the deferred-put question. The underlying need is not met: the replacement design (shm cache + SyncTag) gives Python no durability barrier, because SyncTag, FlushData and FlushMetadata are not bound. Requests: (1) bind SyncTag/AsyncSyncTag returning the per-tag sticky rc; (2) document what AsyncPutBlob completion promises on the task path (data written, WAL record appended, fsynced at the next FlushMetadata); (3) DESIGN.md:370 says Python AsyncPutBlob takes the cache path, but core_client.h:490-504 says task path only.

---
_Generated by [Claude Code](https://claude.ai/code)_
````

### iowarp/clio-core#1339: met in 2.5.0

````markdown
Measured on a 4-vCPU Linux container with CLIO's config, CPU of every daemon thread over 60 s after a 30 s settle: 2.4.1 0.519 cores, 2.5.0 0.037 cores (0.039 after client writes). Under the acceptance. From CLIO's side this can close.

---
_Generated by [Claude Code](https://claude.ai/code)_
````

### iowarp/clio-core#1340: not met in 2.5.0

````markdown
PyPI iowarp-core 2.5.0 and 2.5.1 publish `macosx_15_0_arm64` wheels only (cp310-cp315), as 2.4.1 did, so CLIO's macOS floor stays at 15. Still requested: `MACOSX_DEPLOYMENT_TARGET=14.0`, or the macOS 15 API that prevents it. Free-threaded (cp314t) wheels are also absent.

---
_Generated by [Claude Code](https://claude.ai/code)_
````

### iowarp/clio-core#1341: not met in 2.5.0

````markdown
AsyncPutBlobIfAbsent / AsyncPutBlobIfVersion / AsyncGetBlobVersioned are bound and unchanged since 2.4.1. There is still no batched conditional put (CONDITIONAL_WRITES.md: a batched put "does not honor these conditional flags") and no replacing variant (ConditionalPut sends flags=0; IfVersion "never truncates"). CLIO's conditional writes are groups (a memory document, its search companion and a version record in one multi-put), so the single-blob form cannot be adopted. Both asks stand.

---
_Generated by [Claude Code](https://claude.ai/code)_
````

### iowarp/clio-core#1355: verified fixed in 2.5.0

````markdown
Live, CLIO daemon with CLIO_BIND_ADDR=127.0.0.1: 2.4.1 listened on 0.0.0.0:<base+2>; 2.5.0 listens on 127.0.0.1 for every port. CLIO removed its port-hold stopgap (iowarp/clio-agent#2038). Thank you.

---
_Generated by [Claude Code](https://claude.ai/code)_
````

### iowarp/clio-core#1359: partly met in 2.5.0

````markdown
`runtime.client_data_segment_size` / `CLIO_CLIENT_DATA_SEGMENT_SIZE` are parsed in 2.5.0 (20222438). Measured: with 64MB the daemon's shared memory for four writers falls from 366 MB to 34 MB, but a put larger than the segment fails with the generic rc 21 (64MB: a 48 MB record fails; 128MB: 100 MB fails), so CLIO cannot shrink it until a large put is streamed through the segment or fails with a distinct code. Reserve-don't-commit on Windows and grow-on-demand are not done (as the commit says); still requested.

---
_Generated by [Claude Code](https://claude.ai/code)_
````

### iowarp/clio-core#1312: met in code in 2.5.0

````markdown
00d6ab97 and 3dcab85a are in 2.5.0: the FlushMetadata token wait is bounded (2 s), stuck blobs and a runaway WAL (over 4x transaction_log_capacity) are logged at kError, and the WAL is not truncated in a cycle that skipped a live holder. We could not reproduce the original io_uring stall; in a 30-minute 4-client soak the WAL stayed bounded. Replay memory is not bounded (ask 3).

---
_Generated by [Claude Code](https://claude.ai/code)_
````

### New iowarp/clio-core issue: A core that resolves volatile deletes an existing persistent WAL and snapshot

````markdown
**What CLIO needs:** a config mistake must not destroy data already on disk.

**v2.5.0:** a core whose file tier has no `persistence_level` resolves volatile (an unlabelled device counts as volatile; 7822175b, core_config.cc:200-222). Create then discards the snapshot and every metadata-log shard (core_runtime.cc:862, :8726-8752), logged at INFO. Restoring the label brings nothing back. Related: a file tier beside a RAM data tier also makes the core volatile, silently, so such a store keeps nothing across a restart (on 2.4.1 the same config kept its records).

**Reproduction** (`scripts/bench/core_repros/run.sh <venv> <dir> volatile_wipe` in iowarp/clio-agent, plain clio-core): put 50 blobs, stop, remove `persistence_level` from the tier, start and stop once, restore the label, start: 8 metadata-log files before, 0 after; 0 of 50 blobs readable.

**Ask:** when WAL or snapshot files exist and the core resolves volatile, refuse to start (error naming the files and the reason), or keep the files and log at error. Log at warning whenever durability is *inferred* volatile while a durable device is configured. (CLIO now refuses such a config before every spawn.)

---
_Generated by [Claude Code](https://claude.ai/code)_
````

### New iowarp/clio-core issue: A full filesystem under the metadata log acknowledges puts it then loses

````markdown
**What CLIO needs:** a put that returns 0 is durable, or the put returns an error.

**v2.5.0:** `TransactionLog::WriteRecord` (transaction_log.h ~901-905) is void and never checks the stream; `Sync()` (501-507) ignores `FsyncPath`'s result. rc 28 (33fedf5d) covers only the data device. (Defect 2 of the 16 Nov comment on #1335.)

**Reproduction** (root; `run.sh ... full_wal`): metadata log on a 6 MB tmpfs filled with a filler file, data tier capacity 4 MB on the same fs, 150 puts of 16 KiB: 150 of 150 return 0, the daemon log names no error. Remove the filler, restart: 25 good, 1 unreadable, 124 missing.

**Ask:** fail the put/delete (rc 28 or an I/O rc) when its WAL record or fsync fails, log at error, and never acknowledge a mutation whose record did not land. (CLIO keeps a 256 MB free-space reserve in front of every write until then.)

---
_Generated by [Claude Code](https://claude.ai/code)_
````

### New iowarp/clio-core issue: A torn metadata-log record at the tail is skipped, then new records are appended after it

````markdown
**v2.5.0:** `Open` appends to an existing log as is (transaction_log.h ~285-347); `Load` stops at a torn record and ignores everything after it (586-641, 623-634); nothing truncates the file before the first append. Every record written after a tear is invisible to the next start, at every restart. (Defect 1 of the 16 Nov comment on #1335; unchanged in 2.5.0.)

**Reproduction:** start, put 30 blobs, stop; truncate the largest `metadata.log.blob.<N>` inside its last record; start, put 30 more, stop; start: the second 30 are missing.

**Ask:** after `Load`, truncate each log to the end of its last whole record (keep the cut bytes aside, log at error) before appending.

---
_Generated by [Claude Code](https://claude.ai/code)_
````

### New iowarp/clio-core issue: After a torn delete record, the allocation log hands a live blob's block to a new blob

````markdown
**v2.5.0:** no change to the alloc-log/metadata ordering (only 4964c577, idle fdatasync); `core/METADATA_RECOVERY_DESIGN.md` is a proposal. (Defect 3 of the 16 Nov comment on #1335.)

**Reproduction:** put K and M, stop; start, delete M, stop; truncate M's delete record mid-record; start (M is back); put N, stop; start, put M with new bytes, stop; start and read N: it holds M's new bytes, no error.

**Ask:** make a blob's frees durable only after its delete record is, or reconcile the alloc log against the restored metadata at start so a block owned by a restored blob is never reused.

---
_Generated by [Claude Code](https://claude.ai/code)_
````

### New iowarp/clio-core issue: Daemon memory grows about 6 KiB per stored blob, for the life of the daemon

````markdown
**What CLIO needs:** a daemon that runs for days with bounded memory (iowarp/clio-agent#1846: 2.7 GiB).

**v2.4.1 and v2.5.0, live:** anonymous RSS grows 6.07 (2.4.1) / 6.20 (2.5.0) KiB per new blob whatever the payload (256 B, 4 KiB and 64 KiB blobs all cost 6.21 KiB); overwrites of existing blobs cost 0.02-0.07 KiB per op, gets nothing. In a 30-minute 4-client soak the daemon reached 5.0 GB (2.4.1) / 5.3 GB (2.5.0): ~2.2 GB of client data segments (bounded) and the rest per-blob metadata.

**Reproduction** (`scripts/bench/daemon_growth_probe.py` in iowarp/clio-agent): put 40,000 new 4 KiB blobs and read `RssAnon` in `/proc/<daemon>/status` before and after: +240 MB; 40,000 puts over 100 names: +1 MB.

**Ask:** a smaller resident record per blob and/or cold blob metadata kept on disk behind a bounded cache; report resident metadata (blobs, bytes) through a monitor query.

---
_Generated by [Claude Code](https://claude.ai/code)_
````

### New iowarp/clio-core issue: Growing a blob by appends writes O(appends^2) bytes of metadata log

````markdown
**v2.4.1 and v2.5.0, live:** 600 appends of 700 B to one blob write 14,460,661 B of metadata log (34.4 bytes per data byte); the same data as 600 blobs writes 126,980 B (0.30). Every put logs a full-replacement `kExtendBlob` listing all of the blob's blocks (core_runtime.cc:2789-2817).

**Reproduction:** 600 `AsyncPutBlob(tag, "b", 700 bytes, off=i*700)` and compare the size of `metadata.log*` before and after (`scripts/bench/idle_and_metadata_bench.py`).

**Ask:** a delta record for a put that only adds blocks.

---
_Generated by [Claude Code](https://claude.ai/code)_
````

### New iowarp/clio-core issue: The dashboard destroys pools on an unauthenticated cross-origin POST

````markdown
**v2.5.0, live:** with the dashboard on (`clio-run start` enables it by default unless `CLIO_VIZ_ENABLE=0`), `curl -X POST -H 'Origin: http://evil.example' -H 'Content-Type: application/x-www-form-urlencoded' --data-binary x=1 http://127.0.0.1:8080/api/pools/301.0/destroy` returns `{"ok":true,...}` and the pool is gone. Form-encoded bodies are accepted, so a web page in the user's browser can do this. (`run.sh ... viz`.)

**Ask:** reject mutating routes whose Origin/Host is not the dashboard's own, or require a per-start token; make the dashboard opt-in. (CLIO forces it off.)

---
_Generated by [Claude Code](https://claude.ai/code)_
````

### New iowarp/clio-core issue: Reopening a file bdev with a different CLIO_BDEV_STRIPE_FILES returns wrong data silently

````markdown
**v2.5.0, live:** a store written without striping and reopened with `CLIO_BDEV_STRIPE_FILES=2` reads 100 of 100 blobs back with wrong bytes and no error; reopened without the variable, 100 of 100 are good (`run.sh ... stripe`).

**Ask:** record the stripe count and unit with the backing file and refuse (or adopt the recorded layout) on mismatch. (CLIO drops the variables from its daemon's environment.)

---
_Generated by [Claude Code](https://claude.ai/code)_
````

### New iowarp/clio-core issue: AsyncRegisterTarget without persistence_level is refused by a persistent core

````markdown
**v2.5.0, live:** `AsyncRegisterTarget(..., persistence_level=-1)` (default, e24a48fd) resolves -1 only against devices loaded at start, so a target registered on a running persistent core is volatile and refused with rc 16. Registering a 64 MiB file target on a running daemon: 2.4.1 rc 0; 2.5.0 rc 16 without the argument, rc 0 with `persistence_level=1`. CLIO's capacity growth hit this exactly when its store was full (fixed in CLIO by passing the level).

**Ask:** resolve -1 to the core's own durability (or its devices' level) for a late target, and say "volatile target refused by a persistent core" in the error.

---
_Generated by [Claude Code](https://claude.ai/code)_
````

### New iowarp/clio-core issue: A long USER makes clio-run exit 1 with one line on stdout

````markdown
**v2.5.0, live:** the IPC socket is `<$CLIO_MEMFD_DIR or /tmp/clio_$USER>/clio_<port>.ipc`; when it does not fit `sun_path` (108 bytes Linux, 104 macOS, where `clio_zmq_<port+3>.ipc` is bound too) the daemon exits 1 after one ERROR line on stdout. With a 5-digit port, a Linux USER of 82 characters starts and 83 exits.

**Reproduction:** `USER=$(python3 -c 'print("u"*83)') clio-run start`.

**Ask:** name the path, its length and the limit in the error, on stderr; consider a socket name that cannot overflow (a short hash under the memfd dir). (CLIO now refuses such a path before spawning, typed.)

---
_Generated by [Claude Code](https://claude.ai/code)_
````

### New iowarp/clio-core issue: The synchronous Python calls cannot be bounded

````markdown
**v2.5.0:** `Client.PutBlob/GetBlob/GetBlobSize` (core_bindings.cc:746-779) are the only calls that use the shm cache, and `Tag.PutBlob/GetBlob/GetBlobSize` now call them; none takes a timeout, so a stalled daemon holds the caller indefinitely. CLIO's waits are progress-bounded (it uses the Async forms and `Future.wait(max_sec)`), so it cannot use the cache path.

**Ask:** an optional `max_sec` on the synchronous calls (returning a distinct rc on expiry), or an async form of the cache path.

---
_Generated by [Claude Code](https://claude.ai/code)_
````

### New iowarp/clio-core issue: Release notes, and an announced binary rename

````markdown
2.5.0 has 411 commits over 2.4.1 and no changelog. The wheel's daemon binary was renamed `clio_run` -> `clio-run` (139125d5) with no compatibility name in `bin/`, so a client that locates the launcher by path starts no daemon; the volatile-durability rule (7822175b) and the late-target `persistence_level` (e24a48fd) also break existing deployments. **Ask:** a changelog with a "breaking" section per release, and a compatibility name for one release when a binary is renamed.

---
_Generated by [Claude Code](https://claude.ai/code)_
````


### iowarp/clio-agent#2038: fixed by iowarp-core 2.5.0

````markdown
iowarp-core 2.5.0 binds the control channel through `DefaultServerBindAddr` (iowarp/clio-core#1355). Live, CLIO daemon with `CLIO_BIND_ADDR=127.0.0.1`: 2.4.1 listened on `0.0.0.0:<base+2>`, 2.5.0 on `127.0.0.1:<base+2>`. Branch `claude/clio-core-2.5-upgrade` deletes `arc/core_control_port.py` and its references. Ready to close once that branch merges.

---
_Generated by [Claude Code](https://claude.ai/code)_
````

### iowarp/clio-agent#1846: checked against iowarp-core 2.5.0: unchanged

````markdown
30-minute soak, 4 client processes, this issue's mix, CLIO's own store path, same machine: 2.4.1 (develop code) 554k ops, p99 64 -> 113 ms, 0 errors; 2.5.0 594k ops, p99 59 -> 117 ms, 0 errors; no call past 15 s on either. Daemon RSS reached 5.0 / 5.3 GB on both: client data segments (4 x 544 MB, bounded) plus anonymous memory growing ~6 KiB per stored blob on both versions, independent of payload size (`scripts/bench/daemon_growth_probe.py`). Reported upstream as a new clio-core issue. Not ready to close.

---
_Generated by [Claude Code](https://claude.ai/code)_
````

### iowarp/clio-agent#1851: improved

````markdown
With iowarp-core 2.5.0 an over-long IPC socket path is now refused before the daemon starts, typed `clio_core_ipc_path_too_long`, naming the path, its length and the limit (and an unsafe memfd dir is `clio_core_memfd_dir_unsafe`); the test harness no longer nests USER. The Windows snapshot-path half is untested here (Linux container). Not ready to close.

---
_Generated by [Claude Code](https://claude.ai/code)_
````

### iowarp/clio-agent#1242: improved

````markdown
On iowarp-core 2.5.0 (and 2.4.1), 16 MiB puts and gets succeed through CLIO's store with 0 errors. A full store now grows again (growth passes `persistence_level`, which 2.5.0 requires), and a full data disk is typed `clio_core_device_full` (rc 28). The ask to bound/chunk the events blob is CLIO-side and still open. Not ready to close.

---
_Generated by [Claude Code](https://claude.ai/code)_
````

### iowarp/clio-agent#1577: checked against iowarp-core 2.5.0: unchanged

````markdown
2.5.0's telemetry log is unchanged since 2.4.1 and records only completed core ops, so it cannot tell stuck from idle; the runtime's `worker_stats` counts periodic pollers. The progress signal still needs iowarp/clio-core#1112 (requested again with the 2.5.0 feedback). Not ready to close.

---
_Generated by [Claude Code](https://claude.ai/code)_
````

### iowarp/clio-agent#1891: checked against iowarp-core 2.5.0: unchanged

````markdown
Nothing in 2.5.0 changes this: CLIO keys the store per host (`clio_core_config._default_cte_dir`). Not ready to close.

---
_Generated by [Claude Code](https://claude.ai/code)_
````

### iowarp/clio-agent#658: already done

````markdown
`ClioCoreStore` has been CLIO's default ARC store since before iowarp-core 2.5.0, and 2.5.0 changes nothing here. Ready to close, independent of the upgrade.

---
_Generated by [Claude Code](https://claude.ai/code)_
````


