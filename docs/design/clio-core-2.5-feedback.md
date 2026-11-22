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
| #1339 idle daemon CPU | IDLE_VERDICT | 3.4 |
| #1340 macOS 14 wheels | **Not met** (still `macosx_15_0_arm64` only) | 3.5 |
| #1341 conditional puts in a batch / replacing conditional put | **Not met** | 3.6 |
| #1355 control channel bind | **Met** (verified live) | 3.7 |
| #1359 Windows reserved shared memory | **Partly met** (client segment configurable; no reserve/commit) | 3.8 |
| #1312 WAL unbounded growth | **Met in code**, not reproducible here | 3.9 |
| Metadata log: torn tail followed by appends | **Not met** | 4.1 |
| Metadata log: ENOSPC tears it silently | **Not met** | 4.2 |
| Metadata log: alloc log vs metadata after a torn delete | **Not met** | 4.3 |
| Metadata amplification for small appends | AMPLIFICATION_VERDICT | 4.4 |
| New in 2.5.0, found during the upgrade | 6 items | 5 |

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

### 3.4 #1339 idle daemon CPU: IDLE_VERDICT

IDLE_SECTION

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

SEGMENT_SECTION

**Request (unchanged).** Reserve, don't commit, on Windows; grow segments on demand.

### 3.9 #1312 WAL unbounded growth: met in code

00d6ab97 (FlushMetadata token wait bounded at 2 s, stuck blobs and a runaway WAL
reported at kError) and 3dcab85a (a holder counts as stuck only after 30 s; the WAL is
not truncated in a cycle that skipped a live holder) are in 2.5.0. The original stall
(io_uring `AIO-STUCK`) was not reproducible here; the soak in 3.10 shows the WAL
staying bounded.

### 3.10 Sustained load (clio-agent#1846)

SOAK_SECTION

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

**Reproduction.** Put `metadata_log_path` on a 3 MB tmpfs, keep the data tier on a disk
with space, and put from four threads until the tmpfs is full. Every `AsyncPutBlob`
returns 0, the daemon logs nothing, and `metadata.log.blob.0` ends mid-record.

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

### 4.4 Metadata amplification for small appends: AMPLIFICATION_VERDICT

AMPLIFICATION_SECTION

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

### 5.6 Smaller items

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

COMMENTS
