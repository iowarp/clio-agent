# clio-core (iowarp-core) 2.5.0: feature catalogue for CLIO

**Status:** research, 2026-11-22 · **Upstream range:** iowarp/clio-core `v2.4.1..v2.5.0`
(329 non-merge commits, 82 merges) plus `v2.5.0..v2.5.1` (3 commits) · **Companion:**
[`clio-core-2.5-upgrade.md`](clio-core-2.5-upgrade.md), the compatibility map, which this
document extends and corrects (§6). It does not repeat it.

Upstream ships no changelog. Every commit in the range was read and grouped by subsystem.
Each feature below says what it does, how to turn it on, how mature it is, what upstream
claims for it, what it risks, and whether CLIO should use it. Upstream references are
`path:line` at `v2.5.0` and commit hashes in iowarp/clio-core. CLIO references are
`src/clio_agent/...` on this branch.

Evidence marks:
- **[live]**: checked on this machine with the PyPI wheels (§2 gives the setup).
- **[read]**: read from code or commit messages at the tag, not run.
- **(inferred)**: a conclusion drawn from code, not stated upstream and not run.

The maintainer summed up the release as "shared memory caching for both the CTE and the
file system, which does a much better job of batching and combining requests", plus a change
to how Clio FS stores metadata. In the code:
- **CTE:** the shared-memory cache is the new `core_shm_cache_writethru` module (§3.2).
- **Clio FS:** no clio-fs cache exists at v2.5.0. `filesystem/SHM_CACHE_DESIGN.md` is a
  proposal; what shipped is that the CTE shm cache can absorb clio-fs page writes
  (bc5189d0).
- **Clio FS metadata:** directory blocks are now stored as an append log (§3.9).

---

## 1. Ranked summary for CLIO

Ranked by value against risk for CLIO's deployment: a single node on loopback, one daemon
per user home, a few Python clients over shared memory, one `temporary` file tier, durability
through the core's WAL. The **Branch** column says whether the upgrade branch
(`claude/clio-core-2.5-upgrade` at c222a52) already does it.

### 1a. Do: high value, low risk

| # | Item | Why (evidence) | CLIO module | Branch |
|---|---|---|---|---|
| 1 | Keep the pre-spawn durability gate before **every** spawn. Also require a non-empty `performance.metadata_log_path` | One start with an unlabelled file tier makes the core volatile, and it **deletes** the snapshot and all WAL shards. Restoring the label brings nothing back: 0 of 50 records [live] | `arc/clio_core_durability.py` | fresh seed states `durability: persistent` (5e4a627); `metadata_log_path` check not added |
| 2 | Keep CLIO's free-space reserve (256 MB). rc 28 does not cover a full filesystem under the metadata WAL | All 150 puts on a full tmpfs returned rc 0 with no daemon error, and 122 of them were unreadable after restart [live]. This is #1335 comment defect 2, still present. | `arc/clio_core_disk_guard.py` | unchanged, correct |
| 3 | Map rc 28 (device ENOSPC) to a typed disk-full reason. Never grow or retry on it | `.nospace` marker → new puts, overwrites and `Tag.PutBlob` all give 28; removing the marker recovers them [live] | `gact/core_write_refusal.py`, `arc/clio_core_growth.py` | done (01e717c) |
| 4 | Strip `CLIO_BDEV_STRIPE_FILES` / `CLIO_BDEV_STRIPE_UNIT` from the daemon's environment, with a warning if the parent had them set | A restart with `CLIO_BDEV_STRIPE_FILES=2` opened new empty `.s0/.s1` files. All 100 records then read back with **wrong bytes and no error**; unsetting the variable recovers them [live] | `arc/clio_core_config.py::pin_core_child_env` | not done |
| 5 | Force `CLIO_VIZ_ENABLE=0` even when the parent environment sets it | The dashboard has no auth and no Origin check. A cross-origin form POST destroyed a pool [live]. Any web page could do this while the dashboard is up. | `arc/clio_core_config.py::pin_core_child_env` | sets 0 only when unset |
| 6 | Seed `runtime.client_data_segment_size` | Every attached process maps 577 MB of clio segments; with `64MB` it maps 129 MB [live]. On Windows that is committed memory (#1359). | `arc/clio_core_segments.py` | not done |
| 7 | Lower `runtime.metadata_segment_size` after measuring | The core's shm metadata mirror is gone. 2.4.1 kept 300 MB of the 512 MB metadata segment resident; 2.5.0 keeps < 4 MB. Daemon RSS 949 → 717 MB on the same store [live]. | `arc/clio_core_segments.py` | not done |
| 8 | Record the automatic wins and add an idle-CPU regression check | Idle daemon 0.506 → 0.030 cores, and 0.559 → 0.068 after client activity [live]. Tag listing no longer scans every blob [live]. Other wins read from the code: alloc-log fdatasync no longer runs when idle, free-space coalescing, macOS client recv spin fixed, Windows stop finalizes. | `runtime/doctor`, bench | idle bench added (91be3ae) |

### 1b. Do: cheap hygiene

| # | Item | Why | CLIO module |
|---|---|---|---|
| 9 | Anchor tag regexes (`^…$`) and pass `PoolQuery.Local()` (or `Broadcast()`) instead of `Dynamic()` to `BlobQuery`/`TagQuery`/`GetContainedBlobs` | `Dynamic()` now resolves to Local for these verbs (5fb2940b); the same answer on one container, but a partial one on several. An anchored literal is a hash lookup: 0.21 → 0.105 ms [live] | `arc/storage.py::query_names`, `arc/home_import.py`, liveness probes |
| 10 | Switch the remaining blocking scans and search to the existing async bindings | `AsyncGetContainedBlobs`, `AsyncBlobQuery` and `AsyncSemanticSearch` exist and are bounded with `wait(max_sec)` [read] | `arc/storage.py`, `arc/clio_core_async_ops.py` |
| 11 | Runaway-WAL health row: total WAL size > 4× `transaction_log_capacity`, or the daemon's `FlushMetadata … a snapshot is not finishing` kError line | #1312 is now bounded and logged at kError (00d6ab97, 3dcab85a) [read] | `arc/clio_core_wal_repair.py` health, `arc/clio_core_liveness.py` |
| 12 | Pass `--grace-period` to `clio-run stop`, and do not read exit 1 as "never delivered" | `stop` exits 1 *after* delivering the stop once the daemon outlives grace + 25 s, and SIGKILLs it on POSIX. Upstream's watchdog force-exits after grace + 15 s. This is unchanged from 2.4.1 [read]. | `arc/runtime_stop.py` |
| 13 | Report daemon-log `Skipping ChiMod` as a typed reason | The new ABI check skips a stale module instead of corrupting the heap (af0f9fde). The indexer compose then fails with no obvious cause [read]. | `arc/runtime_log.py` |
| 14 | Use `.nospace` for an end-to-end disk-full test; use `clio-run --version` in the version gate | Both are compiled into the release wheel [live] | tests, `arc/clio_core_daemon_version.py` |

### 1c. Measured and not worth it, or blocked

| # | Item | Verdict | Evidence |
|---|---|---|---|
| 15 | shm cache, `md_only` (opt-in on the branch, c222a52) | Durable, no data path gain. Repeat **sync** `GetBlobSize` drops 12–20 µs → 0.3–0.5 µs. Puts and gets are flat or slower (`GetSync` 34.5k → 28.9k ops/s, async `Get` d16 89.5k → 47.9k), `AsyncGetBlobSize` and `Tag(name)` are unchanged, and the daemon carries +128 MB RSS from start. Worth it only for size-probe-heavy paths on `blob_calls=sync`. | [live] §2.3–2.5 |
| 16 | shm cache, data mode (`md_only: false`) | **Not safe.** Sync puts take 3–6 µs instead of ~100–300 µs and sync gets ~4 µs instead of ~35–190 µs. But a SIGKILL right after 3000 confirmed puts kept only 353–720 readable: the rest were missing, or listed but unreadable. | [live] §2.4 |
| 17 | write-back policy cache (upstream's default) | Not safe: acked in a RAM-only core, durable after a flush every 10 s or 256 MB, and Python cannot fsync | [read] |
| 18 | write-through policy cache | Safe, no gain (upstream: 0.74–0.88× on reads, 0.83× on random writes) | [read] |
| 19 | MultiDelBlob/MultiDelTag, MultiPutBlob, put append/replace, TruncateBlob, multi-get, DelTag, SyncTag/FlushData | Blocked: none are bound in Python. MultiDel also bypasses the BM25 indexer. | [live] binding list |
| 20 | Copy-on-write lazy checkpoints | C++ only, no Python, links probably not restart-durable | [read] |
| 21 | Viz dashboard | Keep off. Use `clio-run monitor --once --json` for worker stats. | [live] |
| 22 | clio-fs/FUSE for workspaces; CAE http(s) ingest | No: slow on small-file workloads, and the CAE download destination is not path-checked | [read] |
| 23 | Networking experiments (tiers, spread, direct, send threads, socket/thallium) | Never set: multi-node only, mostly marked "experiment", and tiers bind ports outside CLIO's port block | [read] |

---

## 2. Live verification

### 2.1 Setup

- **Machine:** Linux 6.18, 4 vCPU, 15 GB RAM VM.
- **Wheels:** PyPI `iowarp-core==2.5.0` and `==2.4.1` (cp312, manylinux_2_28_x86_64), each in
  its own scratch venv.
- **Daemon config:** CLIO-shaped `cte.yaml`:
  - `num_threads: 4`;
  - `main_segment_size: 256MB` and `metadata_segment_size: 512MB`;
  - ram bdev 301.0 at 1 GB;
  - `cte_main` 512.0 with `restart: true` and one `file` tier (2 GB, `temporary`);
  - `metadata_log_path` and a 32 MB `transaction_log_capacity`.

  The indexer at 564.0 was added where stated. With the indexer, the client env carried
  `CLIO_CTE_POOL=564.0`, as CLIO binds.
- **Daemon environment:** `CLIO_BIND_ADDR=127.0.0.1`, `CLIO_VIZ_ENABLE=0` unless stated, one
  port per run.
- **Memfd dir:** `/tmp/clv<port>`. A scratchpad memfd path exceeded `sun_path`, and the
  daemon refused to start ("refusing to start half-bound"), consistent with the upgrade
  doc's guard.
- **Clients:** Python with `clio_init(kClient)` + `initialize_cte`, CLIO's attach sequence.
- **Noise:** single runs on a small shared VM, so treat throughput as ±30–50%. The
  durability and functional results do not depend on noise.

### 2.2 Results index

| Check | Result |
|---|---|
| Idle CPU (#1339), 30 s, `/proc/<pid>/stat` | 2.4.1: 0.506 cores fresh, 0.559 after clients. 2.5.0: **0.030** fresh (43 threads), **0.068–0.075** after clients (60 threads; client threads are not reaped) |
| `GetContainedBlobs` on a 100-blob tag (#1336) | 2.4.1: 1.18 / 2.58 / 5.19 ms with ~12k / 42k / 102k other blobs. 2.5.0: 0.11 / 0.18 / 0.07 ms (flat) |
| `BlobQuery(tag, ".*")` | 2.4.1 4.4 ms; 2.5.0 0.2–1.2 ms unanchored, 0.105 ms anchored `^tag$` (2.4.1 anchored 4.6 ms) |
| Plain task path latency (Python, 4 KiB) | No consistent difference between 2.4.1 and 2.5.0 (`Tag.PutBlob` 216–457 µs vs 291–330 µs; async put issue cost 12–62 µs vs 31–178 µs) |
| Sync `Client.PutBlob/GetBlob`, no cache | The same task path as `Tag.PutBlob/GetBlob` (190–440 µs put, 30–200 µs get) |
| shm cache data mode, Python | §2.3 |
| SIGKILL durability | §2.4 |
| `clio_cte_bench` | §2.5 |
| rc 28 via `<tier>_node0.nospace` | new put 28, overwrite 28, `Tag.PutBlob` raises `rc=28`; 0 again once the marker is removed |
| Full filesystem under the WAL (6 MB tmpfs, 4 MB tier, filler file) | 150/150 puts rc 0, no WAL error logged. After restart 28 good, 122 `GetBlob` failures, and `TransactionLog::Load` logs a bad record (`transaction_log.h:628`) |
| File tier larger than its filesystem | The bdev reserves its capacity at create ("Cannot reserve 67108864 bytes"). The target is not registered, the daemon still comes up, and every put returns rc 11 |
| `CLIO_BDEV_STRIPE_FILES=2` on an existing store | `.s0`/`.s1` created; 100/100 records read back wrong with no exception; intact again once unset |
| Unlabelled tier (inferred volatile) | Log: "volatile core (inferred from its bdevs), WAL off while volatile", "fresh start discarded 10 metadata log file(s)". After relabelling: 0/50 readable |
| `client_data_segment_size` | client mapping 577 MB (default) → 129 MB with `"64MB"` |
| Metadata segment RSS | 2.4.1 `chi_metadata_segment` 300 MB resident; 2.5.0 below 4 MB; daemon RSS 949 → 717 MB |
| Viz (`CLIO_VIZ_ENABLE=1`) | Listens on 127.0.0.1:8080. `/api/pools`, `/api/config`, `/api/nodes/local/workers` (queued, blocked and processed counts per worker) and `/api/pools/512.0/monitor?query=stats` answer JSON. `POST /api/pools/301.0/destroy` with `Origin: http://evil.example`, form-encoded → `{"ok":true}` and the pool is gone |
| Inline block allocation A/B (`CLIO_INLINE_ALLOC_DIRECT=0`) | No measurable difference on one node (async Put 3.6–3.9k vs 3.8–3.9k ops/s) |
| 2.5.1 wheel vs 2.5.0 | Every file identical except `bin/clio-run` (version string) and dist-info |
| PyPI files | 2.5.0 and 2.5.1: cp310–cp315 × {manylinux_2_28 x86_64/aarch64, macosx_15_0_arm64, win_amd64}. No macOS 14, no free-threaded `cp314t`, no sdist |
| Python binding list | `.def` diff 2.4.1→2.5.0: −`AsyncPutBlobDefer`, −`AsyncGetBlobDefer`, −`AwaitPutsUntilSpace`, −`DeferErrorCount`; +`ConnectCache`, +`HasCache`, + `Client.PutBlob/GetBlob/GetBlobSize` |
| `<conf_dir>/models/*.yaml` | Present in both 2.4.1 and 2.5.0 (learned cost models), so not new |

### 2.3 shm cache through Python, µs per call (4 KiB; N = 2000; two runs)

The chain is CLIO's: indexer 564 → [shm 568] → core 512, with `CLIO_CTE_POOL=564.0`. The
cache block has `entry_pool_id: "564.0"`; without it a client bound to 564 does not map the
segment [read]. `HasCache()` was `True` in both cache modes.

| Call | no cache | md_only | data mode |
|---|---|---|---|
| `Tag.PutBlob` | 101–149 | 143–197 | 3.4–13.9 |
| `Client.PutBlob` | 194–208 | 169–171 | 3.5–3.9 |
| `Tag.GetBlob` (always a task; the binding uses `AsyncGetBlob`) | 37–41 | 44–98 | 74–93 |
| `Client.GetBlob` | 32–38 | 61–62 | 4.2–4.6 |
| `AsyncPutBlob`, issue all then wait (total) | 67–89 | 87–92 | 25–69 |
| `AsyncGetBlob` (total) | 50–63 | 52–58 | 32–93 |
| `Client.GetBlobSize`, repeat | 13–21 | **0.5** (13.6 first pass) | – |
| `Tag.GetBlobSize`, repeat | 13–15 | **0.3–0.4** | – |
| `AsyncGetBlobSize` | 7–8 | 7–8 | – |
| `Tag(name)` open of an existing tag | 14–16 | 16–24 | – |
| Daemon RSS at start | 28 MB | 156 MB (capacity 128M, prefaulted) | – |

### 2.4 SIGKILL right after confirmed puts (3000 × 4 KiB, restart, read back)

| Config | API | Readable / listed-unreadable / missing |
|---|---|---|
| plain (no cache) | `Tag.PutBlob` | 3000 / 0 / 0 |
| md_only | `Client.PutBlob` | 3000 / 0 / 0 |
| data mode | `Tag.PutBlob`, three runs | 720 / 272 / 2008 · 353 / 201 / 2446 · 695 / 599 / 1706 |
| data mode | `Client.PutBlob` | 368 / 4 / 2628 |
| data mode, kill 0.5 s / 2 s / 10 s after the last put | `Tag.PutBlob` | 3000 / 0 / 0 each |
| data mode, clean `clio-run stop` | `Tag.PutBlob` | 3000 / 0 / 0 |

"Listed-unreadable" means the name is returned by `GetContainedBlobs` but `GetBlob` raises.
For CLIO that is worse than a clean miss.

The loss window for this burst was under 0.5 s. The clean stop kept everything; upstream's
`Destroy` does not drain the log [read], so that result most likely reflects the stop taking
longer than the flush window, not a guarantee.

### 2.5 `clio_cte_bench`, ops/s (4 KiB unless stated; same machine; one run each)

The cache sits directly over the core, with no indexer.

| Case | no cache | data mode | md_only |
|---|---|---|---|
| `PutSync` t1 d1 | 15,989 | **329,077** | 9,525 |
| `GetSync` t1 d1 | 34,521 | **1,222,200** | 28,895 |
| `Put` t1 d16 (async, score 0.8) | 7,708 | 3,488 | 4,878 |
| `Get` t1 d16 (async) | 89,524 | 26,037 | 47,859 |
| `PutSync` t4 d1 | 11,115 | 1,274,780 | 11,558 |
| `Put` t4 d16 | 9,677 | 7,380 | 6,967 |
| `PutSync` 1 MiB t1 | 533 | 2,076 | 653 |
| `Put` 1 MiB t1 d4 | 1,670 | 1,307 | 2,051 |

Upstream's single-node numbers (Ares, 4 workers, 4 threads × 16 in flight, branch build
a6b70f12c, not the tag) are on the CTE technology page: no-cache put 71k / 62k / 12.5k and get
336k / 132k / 10.8k ops/s at 4 KiB / 64 KiB / 1 MiB. With the 4 GB shm cache they are
941k–1.76M at 4 KiB. These were not reproduced here: this VM is smaller, and the sync path is
what the cache serves.

---

## 3. Catalogue by subsystem

### 3.1 CTE core: durability, metadata, WAL, placement

**Volatile/persistent core split** (7822175b, 34b9ac5a, 60dd9a4d; design docs 13c70007 and
0db78d78, both deleted in 76525ed1)
- What: a core is either volatile (no WAL, starts empty) or persistent (WAL, restore, refuses a
  volatile target with rc 16). The core no longer moves data between classes: periodic
  `FlushData`, the stop-time flush and `MoveBlobToPersistent` are deleted; moving data is now
  the policy caches' job. `FlushData` now means "sync devices, then the WAL"
  (`core_runtime.cc:7911-7951`). The stop hook only sets a stopping flag (`:1150-1159`).
- Turn on: compose key `durability: persistent|volatile` (`core_config.cc:346-355`). If
  unstated, a core is persistent only if it has devices and every one is `temporary` or
  `long_term` (`core_config.cc:200-222`); an **unlabelled device is volatile**. 34b9ac5a's
  "inherit" rule was reversed by 60dd9a4d.
  - New `performance.fsync_mode: durable|deferred`; `deferred` makes `SyncTag` a no-op
    (`core_config.cc:487-495`).
  - `flush_data_period_ms` / `flush_data_min_persistence` are ignored with a warning.
  - Runtime switch: `SetDurability` (method 59), C++ only.
- Maturity: `test_policy_cache.cc` and `test_cte_failure_recovery.cc`. The rule changed twice
  inside the release.
- Risk: a volatile core forces `is_restart_=false` (`core_runtime.cc:1024`), then
  `DiscardPersistentMetadata` deletes the snapshot and every `<log>.blob.*`/`.tag.*` file
  (`:862`, `:8726-8752`). The prefix also matches CLIO's `<log>.blob.N.torn-*` repair
  backups. Only INFO lines report it. [live]
- CLIO: done for the fresh seed (5e4a627).
  - Add a `metadata_log_path` non-empty check: a "persistent" core without one logs "WAL off
    (no metadata_log_path)" and restores nothing (`core_runtime.cc:1025-1031`) [read].
  - Refuse `fsync_mode: deferred` in the gate (inferred).
  - Value H, risk L. Module: `arc/clio_core_durability.py`.

**Runtime-registered targets keep the allocator WAL** (280a2071, 4e108e2b)
- What: a target not in the config (CLIO's grown targets) keeps its bdev `.alloc_log`. Before
  the fix, a restart freed a grown target's live blocks for reuse, which corrupts data.
- CLIO: growth already passes `persistence_level=1`. Add a grow → restart → `remaining_space`
  regression test (value M, risk L). [read]

**Flat tag names** (fff3d78e, b030f9cd, 6163f84e, d1888a6d, 65bcc107)
- What: no tag hierarchy; WAL types 12/13 and snapshot entry 8. A pre-flat store restores
  unless a name is exactly `/` or starts with `$tagid{`.
- CLIO: no action. Do not downgrade a store (2.4.1 replays unknown types with
  `default: break`, silently) (inferred). [read]

**WAL torn-tail append bug**: still present at v2.5.0
- `OpenFile` appends after a torn record (`transaction_log.h:334-346`) and `Load` stops at it
  (`:622-633`). A compaction (WAL > capacity) rewrites the shard without the torn bytes, but
  loses records logged during that snapshot (inferred). No upstream test.
- CLIO: keep `repair_metadata_logs`. `_FREEING_TYPES={1,3}` still covers MultiDel and DelTag,
  which log `kDelBlob` per blob. Add type 2 only if CLIO ever calls `ReorganizeBlob`. [read]

**FlushMetadata bounded; runaway WAL logged** (00d6ab97, 3dcab85a; #1312)
- What: the per-blob token wait is now 2 s of wall time (50 ms once a holder is stuck ≥ 30 s).
  A snapshot that skipped a live writer keeps the WAL for that cycle; 3dcab85a fixed a
  CORRUPT-after-crash regression that 00d6ab97 introduced. A WAL over 4× capacity is logged
  at **kError** once a minute (`core_runtime.cc:7526-7542`).
- Maturity: "50/50 #1312 tests pass".
- CLIO: item 11. Replay memory is still unbounded (#1312 ask 3, no commit). [read]

**Device ENOSPC is rc 28** (33fedf5d)
- What: `kPutDeviceNoSpaceRc` (`core_tasks.h:1060`). `PutRcIsNoSpace` covers 11–13, 28, 41–43
  and 58. A `<backing file>.nospace` marker forces ENOSPC; a `.fail` marker simulates a dead
  device.
- Limit: rc 28 covers only the data device, not the WAL's filesystem (§2.2).
- CLIO: done (01e717c). Never grow on 28: growth adds a file on the same disk. [live]

**"No candidate target" diagnostics** (d0632747)
- What: kWarning only. CLIO runs the daemon at `error`, so it never sees it. Low value. [read]

**Inline block allocation for node-local DirectHash targets** (c0a16ea9)
- What: default on; `CLIO_INLINE_ALLOC_DIRECT=0` turns it off.
- Perf: the claim is multi-node ("0.3-2 ms per 4 KB put under a 4-node 16x16 load"); no
  single-node change was measured. [live]

**`Dynamic()` never broadcasts** (5fb2940b)
- What: `TagQuery`, `BlobQuery` and `GetContainedBlobs` with `Dynamic` run Local. The Python
  defaults for those calls are `Broadcast`.
- CLIO: item 9. [read]

**Copy-on-write lazy checkpoints** (058edeac, `context-transfer-engine/checkpoint`,
`test_checkpoint_cow.cc`)
- What: point-in-time tag copies through the checkpoint pool 566.0 and the `kCowLink` hook.
- Status: C++ only; the Python `AsyncGetOrCreateTag` passes no fault pool. No WAL record for
  links was found, so they are probably not restart-durable (inferred).
- CLIO: future idea for ARC snapshots; L–M value, M risk. [read]

**Metadata recovery design** (073752b2, `core/METADATA_RECOVERY_DESIGN.md`)
- Status: design only.
- Defect 3 applies to CLIO today: restore keeps block references to a target that is no
  longer registered, silently.
- CLIO-side fix: at attach, check that every configured target's backing file exists.
- Watch for the planned `.c<id>` log names and the `sealed` manifest flag; they would change
  what `clio_core_wal_repair` scans. [read]

**Compressed put generation** (f59860f1): compressor only; CLIO composes none.

### 3.2 Shared-memory write-through cache (`clio_cte_core_shm_cache_writethru`, 568.0)

The headline feature. The code is in `context-transfer-engine/core_shm_cache_writethru/` and
`core/include/clio_cte/core/shm_wtc*.h`. Commits: the 1fff755a…dec991f7 series, 058edeac and
7b41c5f1.

**What it is**
- One POSIX shm segment per node, `clio_wtc_<runtime_pid>_<major>_<minor>`, mapped by
  clients. It holds a 64-stripe blob table, tag table, name index, an MPSC dirty-range log and
  per-blob buffers.
- **Data mode:** a sync put is a memcpy plus a log push, with no task. A runtime `FlushLog`
  task drains the log every `flush_period_us` (50) with `flush_depth` (4) batches in flight, as
  `MultiPutBlob` to `next_pool_id`. No WAL, no tiering. Reads hit when the entry is complete,
  or when the range was written through this cache. Eviction is by frecency over clean
  buffers.
- **md_only:** caches blob sizes, tag name → id bindings and tag attributes; every put and get
  is forwarded.

**How a client binds** (`content_transfer_engine.cc:57-145`, `core_client.h:624-693`)
- The core's Create returns cache keys found by `FindFrontCache`, and the client connects.
- With `CLIO_CTE_POOL` (CLIO's route), the client connects only if that pool is the cache or
  its `entry_pool_id`. CLIO's block therefore needs `entry_pool_id: "564.0"` [live].
- Python: `Client.ConnectCache()`, `HasCache()`.
- Only the synchronous `PutBlob`, `GetBlob`, `GetBlobSize` and `GetOrCreateTag` use the
  segment. All `Async*` calls are tasks. `Tag.GetBlob` is always a task: the binding uses
  `AsyncGetBlob`, and its docstring is stale [live].
- With the cache, an explicit put score is silently dropped (`core_client.h:736-738`).

**Compose keys and defaults** (`core_shm_cache_writethru_tasks.h:58-190`; parse errors are
swallowed)
```yaml
next_pool_id: required
entry_pool_id: null (= 568)
md_only: false
invalidate_on_flush: false
capacity: "256M"            # prefaulted at create: resident from start [live]
log_depth: 0                # 1 entry per KiB of capacity
flush_batch_bytes: "16M"
flush_period_us: 50
flush_retries: 3
flush_shards: 8
flush_depth: 4
max_tags: 4096
max_blobs: 65536
max_fill: "16M"
evict_sample: 16
half_life_ms: 10000
max_sharer_entries: 65536
nospace_wait_ms: 250
# extent mode: data_pool / data_capacity
```
- Blob names over 200 bytes take the task path.
- The `DESIGN.md` example is stale: `next_pool_id 563.0`, `max_name`, `inval_timeout_ms`.

**Maturity**
- `DESIGN.md` says "Status: proposal". `HANDOFF.md` ("delete before the PR leaves draft",
  draft PR #1371) ships in the tag.
- Tests: `test_shm_wtc.cc`, about 20 cases. The 4-node `shm_wtc_coherence/` suite (coherence,
  stress, restart) runs only with Docker CI. The restart checker's phase driver is not in the
  tree.
- Thirteen correctness fixes landed in the week before the tag, among them:
  - torn reads (7c0bab0d);
  - a flush that dropped acked writes while `SyncTag` reported success (ce216441);
  - eviction hangs (93c3e899, 486b0f1d);
  - fragmentation down to 0.4 ops/s (c764c2e1, d56cd7a5);
  - deletes resurrecting (1be7e942);
  - a distributed deadlock (0e032870);
  - an intermittent write-around (dec991f7).
- Open: #1372 (assumes hash placement, multi-node only).

**Perf claims** (commit messages)
- 4 KiB puts 152k → 440k ops/s at 1 thread (5c6238e2); gets ~505k ops/s (25987146).
- The site's Redis comparison is quoted in §2.5.
- Nothing is published for md_only, the async path or Python.
- [live]: §2.3–2.5.

**Risks**

Data mode:
- **Acked writes are lost on daemon crash** [live]: the ack comes before the WAL, and a
  restarted daemon formats a fresh segment under its new pid.
- **Graceful stop:** `Destroy` does not drain the log [read].
- **Failed flushes are invisible to Python:** after 3 retries they become a sticky tag error
  that only `SyncTag` returns, and Python has no binding. The data is then marked clean and
  evictable.
- **Sync puts never pass the indexer:** "index on flush" (`DESIGN.md:275-277`) is not
  implemented [read].

Both modes:
- **Client crash** (inferred, untested upstream): stripe locks are ticket locks with no owner
  recovery, so a client SIGKILLed mid-put can wedge 1/64 of the keys or leave a blob pending
  forever.
- **Daemon restart under a live client** (inferred): a client that outlives a daemon restart
  keeps writing to the orphaned old segment.

**CLIO**
- md_only is safe and on the branch as opt-in (c222a52, capacity 128M), but it gains only on
  repeat sync size probes (item 15). Before enabling it by default, check the two inferred
  risks above against CLIO's process model: does any process survive a daemon respawn, and
  is a SIGKILL mid-put reachable?
- Data mode: do not adopt until upstream ships:
  - a drain on stop;
  - an ack-after-flush option, or a Python `SyncTag`;
  - index on flush;
  - lock and pin reaping;
  - reconnect detection.

### 3.3 Policy caches (`core_persist_write_back` 567.0, `core_persist_write_thru`, `cache_base`)

**What**
- Owner-local caches over a volatile core (540.0, RAM) and a persistent chain. d408362a
  rewrote them from ~3,300 to ~1,080 lines.
- **Write-through:** put to persistent, then to the volatile copy.
- **Write-back:** put to volatile and log the dirty range. Since 058edeac it buffers on the
  writing node, and other nodes see the data only after a flush.

**Turn on:** compose `next_volatile`, `next_persistent`, `fill` (`on_hit_count:2`), `evict`
(`lru`), `flush_period_ms` (10000), `flush_fill_pct` (80), `flush_dirty_mb` (256),
`flush_pool` and `fallback_to_persistent` (true). Unplug through `clio-run pool stop <cache>`
flushes up to 8 rounds and aborts the stop if dirty data remains; `--force` discards.

**Maturity**
- The default flipped twice in two days (d5c51840 → 0812d234). Fetch-on-write was reverted
  (2f4dddac) and re-applied (482d9265).
- Tests: `test_policy_cache.cc` (18 cases), `test_cache_unplug.cc` (7), `test_cli_cache_unplug.cc`.
  No test kills the daemon mid-write-back.

**Perf** (0812d234, 6 Ares nodes, ratio vs no cache)

| | write-back | write-through |
|---|---|---|
| random 4 KiB write | 2.02× | 0.83× |
| random 4 KiB read | 0.77× | 0.74× |

62419d69 and 3114646f report IO500 gains at 32 Aurora nodes.

**Risks**
- Write-back is durable only after a flush (10 s or 256 MB by default); kill -9 loses up to
  that window, and Python has no fsync.
- Upstream's shipped default yaml composes indexer 564 → shm 568 (md_only) → write-back
  567 → {volatile 540 | replication 561 → persistent 512}. So **upstream's default stack
  acks before durability**.

**CLIO:** do not compose either. CLIO writes its own `cte.yaml`, so the default does not
apply. [read]

### 3.4 CTE client API and Python bindings

**Two-path client** (058edeac)
- Removed: the deferred-put pipeline (`AsyncPutBlobDefer`, `AsyncGetBlobDefer`,
  `AwaitPutsUntilSpace`, `DeferErrorCount`), the put sieve, private-put staging, and the core's
  shm metadata mirror (`TryShmGet` and related).
- Removed env vars: `CLIO_CTE_PUT_SIEVE`, `CLIO_CTE_NO_PRIV_PUT`, `CTE_SHM_*_CAPACITY`,
  `CLIO_FORCE_NET`.
- `Tag.PutBlob/GetBlob/GetBlobSize` now route through the new synchronous client methods.
- The synchronous `Client.PutBlob/GetBlob/GetBlobSize` (Python `core_bindings.cc:746-795`)
  release the GIL but have **no timeout**: against a stopped daemon the thread never returns.
  The branch's `blob_calls=sync` (110a189) is opt-in for that reason.
- With `CLIO_CTE_POOL` set, `ClientInit` makes one extra unbounded get-or-create of 512.0
  (`core_client.h:657-685`). CLIO already runs init under a stall watch. [read]

**Python binding changes**
- The list in §2.2 is complete [live].
- Present but unused by CLIO: `AsyncGetContainedBlobs`, `AsyncBlobQuery`,
  `AsyncSemanticSearch` (item 10), `AsyncPutBlobIfVersion`, `AsyncGetBlobVersioned`,
  `AsyncTemporalSearch`, `AsyncReorganizeBlob`, `AsyncPollTelemetryLog`.
- Upstream relaxed its own GIL-test wait bounds (85fdd2ce: `wait(0.5)` on a suspended daemon
  may take up to 10 s). Keep CLIO's sliced wait loop.

**BlobQuery / GetContainedBlobs use the per-tag index** (8599942d, c5335132; #1336)
- What: cost is now O(blobs in the tag). BlobQuery's tag regex is an O(1) hash lookup when it
  is an anchored literal, otherwise a trigram index.
- Maturity: "51/51 query/tag/core tests". [live]

**MultiDelBlob / MultiDelTag** (04b1b339, f536f987; methods 60/61)
- What: one task per set, one `FreeBlocks` per pool. Returns only a count and the first error.
- Status: C++ only. The BM25 indexer does not interpose methods 60/61, so deleting `.text`
  companions this way would leave their search docs. Blocks are freed before any `TxnDelBlob`
  is logged, over the whole set (inferred). Test: `test_multi_del.cc`, with no restart test.
- CLIO: blocked. File upstream: bind it in Python and interpose it in the indexer.
  `arc/clio_core_async_ops.py::store_delete_many` would use it. [read]

**MultiPutBlob per-record flags and score** (3dbb9758, ccfe8d29)
- Wire change only; no Python binding. The indexer handles MultiPutBlob, so a binding would be
  indexer-safe. [read]

**PutBlob append** (`kCtePutAppend`=0x4, 3cbe1fcb)
- What: the server resolves the offset.
- Status: not bound. The commit itself says there is no unit test and no restart test.
- rc 15 means `kCtePutAppendInvalidRc` and also `kCteInvalidDurabilityRc`; map both if CLIO
  ever types rc 15. [read]

**Per-task cost models** (058edeac, `task_cost.h`, `core_task_stats.h`)
- What: scheduler seeds, internal to the daemon. Learned models persist under
  `<conf_dir>/models/` (already the case in 2.4.1 [live]). No action.

**Indexer, semantic and temporal search, BM25, telemetry structs, ReorganizeBlob API**
- No change in range (the indexer directory is byte-identical).
- ReorganizeBlob's #863 data-loss corner is still open per `test_reorganize_blob.cc:702-712`.
  Avoid it. [read]

**Not used by CLIO**
- **Stream module:** changed (null default pool id, cost stats). CLIO does not use streams
  (#1342 closed on purpose).
- **Other modules:** replication, compressor, lmcache, cpu/gpu vector (058edeac, ef18d59a,
  f59860f1, 36f60342). No uvm commits.

### 3.5 Block devices (bdev, safe-bdev, ctp io)

| Feature (commits) | What / turn on | Maturity / perf | CLIO |
|---|---|---|---|
| Free-space coalescing (bfed0390, 5187cdc5; #1333) | One shared extent index: merges neighbours, best-fit allocation, heap-tail retraction. Automatic | 5 Catch2 cases. "108 vs 403 MB/s" for 1 MiB I/O after small-I/O churn | Automatic M win for CLIO's small puts, deletes and large artifacts. Also de-risks growth on rc 12 when space is fragmented [read] |
| Alloc log not fdatasynced while idle (4964c577) | Sync skipped when no new record. Automatic | "1 MiB write 353 → 984 MB/s" | Automatic. Ends ~20 fdatasync/s on an idle desktop daemon (inferred; not counted live, strace unavailable) |
| Explicitly volatile file bdev has no alloc log (4e108e2b, afa6dc1a) | `persistence_level: volatile` | `bdev_volatile_skips_alloc_log` | No change: CLIO's tier is `temporary` |
| Aligned reads ≥ 64 KiB use O_DIRECT (ee52477b) | Automatic, Linux | No numbers | Large-blob re-reads no longer come from the page cache; possible read-latency change, L [read] |
| POSIX AIO on FUSE/Lustre/NFS/SMB/GPFS; fd pool (20392afb, 4568573e) | Automatic by fs type; `CLIO_BDEV_AIO=posix\|default`, `CTP_POSIX_AIO_FDS` (1–64), `CTP_POSIX_AIO_DIRECT=0` | Pool default reverted to 1 after EMFILE on macOS | Safer on network home dirs. A user's `CTP_POSIX_AIO_FDS` leaks into CLIO's daemon (inferred) |
| Striping across backing files (dfbc2e20) | `CLIO_BDEV_STRIPE_FILES`, `CLIO_BDEV_STRIPE_UNIT`; env only | "(experiment)". No test. Reopen with a different N is unhandled. Throughput collapses with the alloc WAL. "1 node 899 → 5,619 MiB/s" on DAOS dfuse | **Hazard, verified** (item 4) |
| Native DAOS (f6070192, 14694beb, 2181a898) | `bdev_type: daos` | Not in the wheel | No |
| S3/GCS tiers in every wheel; compose keys (5c1bb312, bca42965, ac1c7808) | `bdev_type: s3\|gcs` | CI against emulators | Not for desktops |
| RAM bdev NUMA interleave; `CLIO_PREFAULT` honoured by clients (a6b70f12) | `CLIO_NUMA_INTERLEAVE` (on) | | A no-op on one NUMA node. Do not set `CLIO_PREFAULT`: it commits the 1 GB arena |
| safe-bdev #1343 (b5a796bd, 930d8696, ee52477b, e9a90626) | `clio_safe_bdev` | `safe_bdev_small_alloc_packs` | Not in the wheel |
| Raw bdev bench modes (4ad5ea9f) | `clio_run_thrpt_bench --test-case bdev_write\|bdev_read --working-set --pool-id` | | Bench ceiling for the file tier |

### 3.6 Pool management and CLI

**Leveled DestroyPool** (301b3465, edada356)
- What: flags `KeepRestartable`, `NoFlush`, `DeleteData`, `QuiesceOnly`; unplug redirects to
  a successor pool. edada356 fixed leaked pool containers.
- With `DeleteData`, the CTE core deletes its WAL and snapshot, but the bdev Destroy deletes
  **nothing**, despite the flag's comment.

**`clio-run pool start|stop|rm <pool> [--force]`; `compose stop|rm --force`** (8360f7e0,
ffb27c11)
- Landed one day before the version bump.
- CLIO: never `pool rm cte_main` as cleanup. It removes the index but leaves the tier file and
  `.alloc_log`, a partial destruction. CLIO's stop-then-remove-state-dir path stays. [read]

**Pool log cleared before compose on `--fresh`** (0a82f4b9): CLIO never uses `--fresh`.

**Rename to `clio-run`/`clio-cae`, `--version`, `viz`** (139125d5, b5ff2392, f5a69744,
86cbd9a0)
- `clio-run --version` prints `clio-run 2.5.0` [live]. Item 14.

**`--local`** (14582133)
- Sets `CLIO_BIND_ADDR` and `CLIO_SERVER_ADDR` to 127.0.0.1 and ignores a hostfile.
- CLIO already pins the bind address. Optionally also set `CLIO_SERVER_ADDR=127.0.0.1` in
  `pin_loopback_bind`. A non-loopback `CLIO_SERVER_ADDR` in the user's env flips the default
  bind to 0.0.0.0 when `CLIO_BIND_ADDR` is unset.

**Stop waits on the pid; finalize on the main thread** (50506f2c; #1365, #1357)
- Windows teardown now runs with workers alive, and stop takes ~1 s instead of ~5 s.
- Inferred: 2.4.1's graceful stop on Windows did not run the core's teardown. CLIO's WAL
  replay covered that.
- Item 12 covers the exit-code and grace-period semantics.

### 3.7 Scheduler, runtime and networking

**Automatic wins**

| Feature (commits) | Detail | CLIO |
|---|---|---|
| Idle wakeup storm (fa77434c, 57ef5025; #1339) | Park floor 1 ms, backing off to 50 ms; chi-client-recv 100 µs → 10 ms when idle | [live] item 8 |
| Unix-socket client recv spin (ee5d7a33) | Every macOS client, and IPC-mode clients elsewhere, pegged a core; now `poll()` with 10 ms | H on macOS [read] |
| Deserializer overrun fails the message (4cab0ec1; #1353) | The flood that `arc/runtime_log_flood.py` guards against. Expect any remaining symptom as an RPC stall. Keep the guard | [read] |
| Bulk copies bounded by bytes received (af531c16) | | [read] |
| ChiMod ABI check (af0f9fde, 8448878d, 72fb78d7) | `CLIO_CHIMOD_ABI_CHECK=0` disables it; never set that | Item 13 |
| Control channel starts after modules load (dcfcc0b7) | Fixes a startup race | [read] |
| Shutdown fix (50506f2c) | §3.6 | [read] |
| macOS fd limit | Clamped to `kern.maxfilesperproc`; the daemon was stuck at 256 fds | Removes an EMFILE ceiling under growth [read] |
| `TouchPages` lost one byte per page on macOS RAM bdevs (74a52c06) | | Do not downgrade macOS stores with a RAM data tier [read] |
| `Mutex::TryLock` gate (bdfa4c56) | | [read] |
| Windows `\\?\` paths (c47f1607; #1356) | | [read] |
| Memfd dir ownership check (2a6fc440) | | Already in the upgrade doc |
| Destroyed pools freed (edada356) | | [read] |

**Knobs CLIO should know**
- `runtime.client_data_segment_size` / `CLIO_CLIENT_DATA_SEGMENT_SIZE` (20222438; #1359):
  - the env var wins over yaml, and 0 means 512 MB;
  - the client reads it from its own config, so it must be in the shared `cte.yaml` or the
    client env [live].
- `CLIO_EVLAT=1`: per-stage latency histograms, including the client-side `c_e2e`. Use it for
  soaks and benchmarks only (acc389e4, c1a745c1, 846f6347) [read].

**Network bind**
- 0586c90b (#1355): TCP `Listen` binds the address it is given, and a too-long AF_UNIX path
  now fails with `ENAMETOOLONG` instead of being truncated silently.
- 98bb4eb4 and 60cb3df2: loopback by default, and the control channel binds the same way.
  CLIO already pins loopback. [read]

**Multi-node experiments**: CLIO must never set these
- Variables: `CLIO_NET_TIERS`, `_TIER_LANES`, `_TIER_ZMQ_FROM`, `CLIO_NET_SPREAD`,
  `CLIO_NET_DIRECT`, `CLIO_NET_SEND_THREADS`, `CLIO_NET_AVOID_SHARDS`,
  `CLIO_SHARD_DRAIN_BUDGET`, `CLIO_PEER_TRANSPORT=socket|thallium`, `CLIO_LBM_THALLIUM_*`.
- Related changes: the peer dial table, SWIM and dial-death fixes, reply aggregation.
- Tiers bind ports from base+16 upward. [read]

**Upstream module-dev skill** (058edeac, `.claude/skills/clio-module-dev/`)
- Useful facts for CLIO maintainers:
  - with N ≥ 3 workers the last two do network send and receive;
  - elastic workers are never retired;
  - a task that never yields is rescued only after more than 1 s, which matters for RPC
    liveness thresholds;
  - `Future::Wait(-1/0/>0)` means forever / poll once / timeout.
- Some of its pages document knobs that 058edeac removed. [read]

### 3.8 Viz dashboard

**What** (139125d5, 86cbd9a0, 98bb4eb4; `src/viz/viz_server.cc`, `modules/admin/src/admin_viz.cc`)
- New `clio-run viz start|stop|status`; on `start`, `--port 0` picks an ephemeral port.
- The dashboard itself already existed in 2.4.1 and is on by default for `clio-run start`.
  Config: env `CLIO_VIZ_ENABLE`, `CLIO_VIZ_PORT` (8080), `CLIO_VIZ_BIND` (127.0.0.1); yaml
  `viz:`.
- Endpoints [live]:
  - `/api/pools`, `/api/config`, `/api/topology`;
  - `/api/nodes/local/workers`: per-worker queued, blocked and processed counts, which is
    what #1111/#1112 asked for;
  - `/api/pools/{pool}/monitor?query=stats`;
  - mutating POSTs: compose, destroy, `register_target`/`unregister_target`, bdev create,
    safe-bdev members.

**Security** [live]
- No authentication.
- No CORS header, so other sites cannot *read* responses. But form-encoded POSTs are parsed,
  and there is no Origin, Referer or Host check, so any page the user visits can fire
  `POST /api/pools/512.0/destroy` blind.
- DNS rebinding could also read the GETs (inferred).

**CLIO**
- Keep it off, and force it (item 5).
- For diagnostics use `clio-run monitor --once --json` (same worker stats, no listener), or a
  short-lived `clio-run viz start --port 0` during a support session.
- `/api/health` is answered without a task, so it does not prove the workers are alive.

### 3.9 clio-fs / FUSE and its metadata

**Directory blocks as an append log** (3cbe1fcb, `fs_block_log.h/.cc`): the metadata change
the maintainer meant.

| | Old (2.4.1) | New (2.5.0) |
|---|---|---|
| Directory block blob | One bare image, magic `CIT1`, padded and rewritten whole on every commit | A log of `CLG1` frames (kind, length, FNV-1a checksum) |
| A commit writes | The whole image | A change frame with `kCtePutAppend` |
| Full image written | Always | Past `dir_log_compact_bytes` (256 KiB), on split/seal/new, after a failed write, and on the first commit after a reload |
| Load | The image | The last valid image plus later frames, stopping at a torn frame |

- With `metadata_log_path` set (the default), the node-local journal `.dirs.<container>`
  (fsynced on fsync and every 1 s) is authoritative, and the blob is a replicated mirror. On
  load, the home takes the highest version among journal, CTE copy and stand-in (#1322
  fixes: 923df681, 9faa12f9, 202caacf).
- Compatibility: `CIT1` images still load, `CDB1` is refused, and the format is one-way
  (inferred).
- Gaps: no codec unit test and no restart test of log replay (the commit says so).
- Perf: mdtest per-rank-dir create 5,361 → 8,635 files/s (2 Aurora nodes × 16 ranks).

**Other clio-fs and FUSE changes**
- Combined create+open at the inode home (71445470, 03e34f2a).
- Seekable ranged readdir; entry types and 8192-entry subtasks (37cb9f19, 5fb2940b): "find at
  64 nodes 2.4k → 64k IOPS".
- `data_score` (ccfe8d29); volatile-fallback race (abc1280e); size overlay order (6632e8e3).
- The filesystem pool is now required wherever `clio_cte_fuse` mounts; a failed init exits
  non-zero (c8bf5337, 04522f4c).
- FUSE knobs: `CLIO_FUSE_DIR_ATTR_CACHE_S`, `_MAX_BACKGROUND`, `_READAHEAD_PAGES`, `_ODIRECT`,
  `_MAX_THREADS`, `_READDIR_PREFETCH*`.
- #1323 ESTALE retries.

**Site claims**
- IO500 vs OrangeFS on Ares: 1.415 vs 0.431 at 4 nodes, 2.465 vs 0.776 at 8 nodes. One run
  each, a branch build, "not valid list submissions".
- `git clone` of the Linux kernel: 171 s vs 39 s on ext4.
- The "durable on fsync" claim conflicts with the page's own NTFS run.

**CLIO:** no, for workspaces and for ARC storage. It is slow on small files, needs a FUSE
daemon plus libfuse3/WinFsp, and has an unclear durability point. [read]

### 3.10 CAE (context-assimilation-engine)

**http(s) sources with per-site credentials** (1b76b23f, 8f5a91a3, d2f4edf4, b40b1ddb)
- Variables: `CAE_HTTP_*`. OMNI `run:` scripts before ingest (929444e6).
- Local path policy for `file::`/`hdf5::` sources and exports (7ade0339, 39978b5e).
- HDF5 assimilator builds on Windows (100c062b).

**In the wheel**
- The CAE chimod libraries ship, and so does `clio_cee`, which drives OMNI parsing through
  `ContextInterface.context_bundle`. Only the `clio-cae` CLI is missing (it is native-packages
  only). [live: `iowarp_core/lib/libclio_cae_core_*.so` and
  `iowarp_core/ext/clio_cee.cpython-312-*.so` are in the wheel]
- http(s) is compiled in on Linux and macOS; HDF5 is off in the wheel.

**CLIO:** maybe later for dataset ingest, but **not without a CLIO-side guard**. The download
runs inside the daemon, outside CLIO's write fence, and the path policy does not check where
an http download is written (`assimilator_factory.cc:90-99`). Value M, risk H. [read]

### 3.11 Packaging and releases

**Wheels** [live]
- 24 per release, listed in §2.2.
- `bin/`: `clio-run`, `clio_cte_bench`, `clio_cte_fuse`, `clio_run_thrpt_bench`.
- Libraries: all cache chimods, the stream, filesystem, indexer, replication and CAE chimods,
  and S3/GCS support.
- Not included: safe_bdev, compressor, summarizer, DAOS.

**v2.5.1** (32518175, merged by 1ba684db and b3721664): Linux DEB/AppImage built on ubuntu-24.04. 2.4.1 and 2.5.0 published
**no** GitHub-release packages. On PyPI, 2.5.1 is identical to 2.5.0 except the version string.
CLIO's `~=2.5.0` resolves to 2.5.1.

**Other channels:** Spack, vcpkg and CMake fixes. The jarvis package always passes
`CLIO_SERVER_CONF` and a hostfile (2fe53076, 3338ffa2). Not used by CLIO.

**CI maturity**
- macOS and Windows are build-only on PRs; sanitizers run only on dev/main sweeps.
- The 4-node docker suites are manual-only.
- An intermittent leak in `cte_cli_cache_unplug` was fixed on the day of the version bump
  (edada356, 73c3f521).

---

## 4. clio-core#1335 and sub-issues at v2.5.0

As requested in iowarp/clio-core#1335 (open, "2 / 9 completed"):

| Issue | Ask | State at v2.5.0 | Evidence |
|---|---|---|---|
| #1336 | Scope BlobQuery/GetContainedBlobs to the tag | **Done** (8599942d, c5335132); the issue is still open on GitHub | [live] flat listing time |
| #1337 | Python bindings for put-replace (`kCtePutReplace`), `TruncateBlob`, multi-get, multi-put with per-put status, `DelTag` | **Not done.** None is bound. New natives in range (MultiDelBlob/MultiDelTag, append, `SyncTag`, `SetDurability`) are not bound either | [live] `.def` list |
| #1338 | Durability contract for deferred puts, or an await | **Moot as asked**: the deferred pipeline was removed (058edeac). The need behind it (N records in one round trip with per-record status, confirmed durable) is **still unmet** from Python. Upstream's replacement for batching, the shm cache data mode, acks before durability | [live] §2.4 |
| #1339 | Idle daemon under a few percent of a core | **Fixed** (fa77434c, 57ef5025): 0.030 cores fresh, 0.068–0.075 after clients. The issue is still open | [live] |
| #1340 | macOS 14 wheels | **Not done**: 2.5.0 and 2.5.1 publish `macosx_15_0_arm64` only | [live] PyPI |
| #1341 | Conditional puts in a batch; a conditional replace | **Not done**: `MultiPutDesc` gained `flags_`/`score_` but no per-record precondition, version or status; nothing bound | [read] |
| #1342 | Python binding for `clio_cte_stream` | Closed on purpose; CLIO does not use streams. Not a gap | – |
| #1355 | Control channel ignores `CLIO_BIND_ADDR` | **Fixed and closed** (60cb3df2, 0586c90b) | upgrade doc §7 [live] |
| #1359 | Windows: reserve don't commit; grow on demand; client segment key | **Partly done**: the client segment key is parsed (20222438) [live], and the 300 MB metadata mirror is gone [live]. Reserve-don't-commit and grow-on-demand are not done; the issue is open and needs its own design (maintainer, Nov 19) | |
| #1312 | Unbounded WAL growth | **Asks 1 and 2 addressed** (00d6ab97, 3dcab85a: bounded waits, kError report). **Ask 3** (bound replay memory, or compact on restart): no commit. The issue is open | [read] |
| #1335 comment (Nov 16), defect 1 | WAL torn-tail append | **Not fixed** (`transaction_log.h:334-346`); CLIO's repair stays | [read] |
| defect 2 | Full filesystem: metadata silently lost with put rc 0 | **Not fixed**: reproduced on 2.5.0, 150/150 rc 0, then 122 unreadable | [live] |
| defect 3 | Alloc log reuses a restored blob's block after a torn delete | **No commit in range** touches it; CLIO's workaround stays | [read] |
| #1335 body | Free-threaded `cp314t` wheels (optional) | Not published | [live] |

Asks worth adding upstream from this research:
1. Python bindings for `MultiDelBlob`/`MultiDelTag`, plus indexer interposition of methods
   60/61.
2. A Python `SyncTag`/`FlushData`.
3. An Origin check (or token) on the viz dashboard's mutating routes.
4. Report a WAL write that fails for lack of space, and fail the put.
5. Drain the shm cache log in `Destroy` and the stop hook.
6. Bounded variants of the synchronous `Client.PutBlob/GetBlob/GetBlobSize`.

---

## 5. Environment variables new in range

What each one means is in the sections above.

| Group | Variables |
|---|---|
| **CLIO should set** | `CLIO_CLIENT_DATA_SEGMENT_SIZE` (or the yaml key) |
| **CLIO should strip or force** | `CLIO_BDEV_STRIPE_FILES`, `CLIO_BDEV_STRIPE_UNIT` (strip); `CLIO_VIZ_ENABLE=0` (force). Consider stripping `CTP_POSIX_AIO_FDS` and `CLIO_CHIMOD_ABI_CHECK` |
| **Leave unset** | `CLIO_PEER_TRANSPORT`, `CLIO_NET_*`, `CLIO_SHARD_DRAIN_BUDGET`, `CLIO_SHM_DRAIN_FAST`, `CLIO_INLINE_ALLOC_DIRECT`, `CLIO_LBM_THALLIUM_*`, `CTP_POSIX_AIO_DIRECT`, `CTP_SYCL_DEVICE_TYPE`, `CLIO_NUMA_INTERLEAVE`, `CLIO_BDEV_AIO`, `CLIO_DAOS_*`, `CLIO_FUSE_*`, `CLIO_CFS_POOL`, `CLIO_WTC_TRACE`, `CLIO_CAE_TELEMETRY`, `CAE_HTTP_*` |
| **Bench only** | `CLIO_BENCH_PUT_SCORE` (default 0.8; -1 = auto score), `CLIO_EVLAT` |
| **Removed** | `CLIO_CTE_NO_PRIV_PUT`, `CLIO_CTE_PUT_SIEVE`, `CTE_SHM_BLOB_CAPACITY`, `CTE_SHM_TAG_CAPACITY`, `CLIO_FORCE_NET` (client), `CLIO_BENCH_STREAM_REGION` |

---

## 6. Corrections to `clio-core-2.5-upgrade.md`

1. **Header:** "`v2.5.1` (tag only, not on PyPI)" is wrong. 2.5.1 is on PyPI (24 wheels,
   uploaded 2026-11-21 23:47 UTC), byte-identical to 2.5.0 apart from the `clio-run`
   version string, and `~=2.5.0` installs it [live].
2. **§1 #4 and §4:** a volatile core does not just skip restore; it **deletes** the snapshot
   and WAL shards on start (`core_runtime.cc:862`, `:8726-8752`) [live]. An unlabelled device
   counts as volatile; 34b9ac5a's "inherit" rule was reversed by 60dd9a4d.
3. **§1 #2:** the "refusing to start half-bound" refusal predates v2.4.1. What is new is
   0586c90b: a too-long AF_UNIX path now fails with `ENAMETOOLONG` instead of binding a
   silently truncated name.
4. **§1 #6:** the macOS `clio_zmq_<B+3>.ipc` socket and `CLIO_ZMQ_LOCAL_IPC` already existed in
   v2.4.1. Only the length check is new.
5. **§3:** CAE is in the wheel: its chimod libraries and `clio_cee` ship. Only the `clio-cae`
   CLI is native-packages only.
6. **§4 env list:** add `CTP_POSIX_AIO_FDS`, `CTP_POSIX_AIO_DIRECT`, `CTP_SYCL_DEVICE_TYPE`,
   the nine `CLIO_LBM_THALLIUM_*`, and the FUSE/CAE variables (§5 above).
7. **§5:** for CLIO's client binding (`CLIO_CTE_POOL=564.0`) the cache block needs
   `entry_pool_id: "564.0"`. `Tag.GetBlob` never uses the cache. Measured: md_only gains
   only on sync size probes, and data mode loses acked writes on SIGKILL (§2.3–2.4).
8. **§6:** 2.4.1 already had WAL record types 0–11 (not 0–7); 2.5.0 adds 12/13. The
   conclusion stands.
9. **§2:** `Tag.PutBlob/GetBlob/GetBlobSize` now call the new synchronous client methods.
   CLIO's behaviour is unchanged (task path), but those waits are unbounded.
10. **New:** `PoolQuery.Dynamic()` resolves to Local for TagQuery, BlobQuery and
    GetContainedBlobs (5fb2940b); and the core's resident shm metadata mirror is gone
    (−300 MB RSS) [live].
11. **Stale CLIO comments:**
    - `arc/clio_core_config.py:117-122`: data moving from memory to the file tier no
      longer happens.
    - `arc/clio_core_durability.py:7-8`: there is no "periodic flush". WAL records reach the
      kernel with `write(2)` as they are logged, and are fsynced every
      `flush_metadata_period_ms`, 5000 ms by default.

---

## 7. Commits read with no CLIO-relevant feature

Every other commit in range is cited in §3.

- **Site and docs:** b57c70eb, c7d6b465, 18758dff, b357e059, d9818615, c5dba42b, 2047d90c,
  cb22fe10, 360bbcf1, 29f8ae81, 64b01f79, c89f7f10, c0a1fc2e, f312c5f1, f5a74c08, 5fa209d9,
  3695a1ec, 1eafb3b8, d67ad9cf, 185e1cde, 33a82ec2, 6ca3a63c, 768f19ac, a3a681ce, 4beb1eb0,
  ae9d8510, b9e2f416, c520c241, f0fe069b, 1c861001, 898cea90, 8f7d9a9a, a7cb6ed5, 80b622e5,
  e7cfe8ec, 7b6a15bf, df43577a, 54582431, 0fa0bc98, 0cc66a70, 79c3dd00, c29e0393, 30f49c85,
  3102f123, 76525ed1, a369dace.
- **IO500, WfCommons and the clio_fs_suite harness:** 20392afb (scripts), b4696c34, 551e846f,
  c339a7b7, 2c426cff, 22917a4b, 1b1df5b6, eb2d9611, dd9e59f9, 090ce608, 765f960f, 56ec6216,
  ec2ef1fe, eae4349e, 2999d5b0, 6036a62f, cc361293, d798bb97, 2b6cd4ee, 976afcde.
- **CI and build:** 6d656265 (cp315), f425fe9c, 5c1bb312, 8810dea2, 946a11ac, 1e5c083c,
  6b0827b8, ef61265d, 86ef2afe, 10cdd685, 8be29960, d31b176c, 16a60feb, 94bb38e9, 02a11ffd,
  b2505a56, f1926d0f, 878cfb92, 8971a774, fc2eb190, f944ee9c, 73c3f521, 50415502, 39e935f9,
  9a402e3f, 7618ea75, 63afabd2, 743daa94, 87791dd2, 10046597, 4d6db6ce, 3a77366c, 74f7d514,
  0b66c510, 14b27b26, 1d2e2b00, 8b60e3a3, aa315678, ecc7ca27, 42715b80, ade5d626, 983eb0a9,
  bdbe3f21, 0563b530, 7b393662, 36f60342.
- **Tests only:** 5187cdc5, b05f2e0f, 98a781d9, 47ecf634, 1ce3e13b, cea2871f, af1d3f9b,
  b7671c12, 4d608642, a6359ec7, 9336ee89, 06b7417c, d9069478, bc78d258, b99fcf0e, deef4942,
  54d67a0d, 3c9a4d47, 85fdd2ce, 7d924fe9, c570d59a, 1e8d0237, a10168e0, 9653314b, 264b4c4a,
  72fb78d7, ffb27c11, 86cbd9a0, f5a69744.
- **CAE tests and fixes:** 5ecdef40, 6a49f13d, b40b1ddb, f9a2254f.
- **GPU and vector:** 2efd314f, 1cd66dd1 (GCC ICE), gpu_vector benchmark scripts.
- **Version bump:** f43acc73.

## 8. Commit index for commits not named above

These commits belong to a feature in §3 that is described without naming them. With §3 and
§7, this accounts for all 329 non-merge commits in `v2.4.1..v2.5.0`.

| Section | Commits |
|---|---|
| §3.1 core | 7ac8012c (WAL stats no longer throw when a file vanishes), e24a48fd (`AsyncRegisterTarget(persistence_level)`, upgrade doc §1 #3) |
| §3.2 shm cache | 0ae61ce3 (clio-fs cache design), cdb6fc6f (handoff notes), 3d9b5af2, d2397acd (pool-id moves), 545a62d8, 1d420d82 (#1372 note), 46c671c4, 9cc7fe38, 5d75da61, 3f8bb08e, 77c2c84a, 4b177acf (`SyncTag` on remote homes), 2147531f, 62eeeb56, f20ac360, d3141b0e, bf3f74b9 (md_only series), 4672c649, 742c3c8e, 93c2ee66, 28bb06ed, 998707af (extent mode), b8a6d829 (test config), 94cc6a60 (wheel), fc17481a, 64d51c53 (UBSan fix in create) |
| §3.3 policy caches | 9388b529 (later reverted), d0ed79c3, 22abe561, e7b04659, 991f7e89, 189d8247, e409aab6, 57f94e9f (unplug flush, Monitor counters), a348f579 (wheel) |
| §3.4 client API | d24944a0 (`CLIO_BENCH_PUT_SCORE`) |
| §3.7 runtime and networking | eb4a10ef (#1345 TLS), 92459426 (fast-periodic waiters), 3d7e4d81, 3b81a156 (`CLIO_NET_SPREAD`, lock-free empty drain), 3a401ff0, 8bd53699 (size tiers), 2b81188e, eece6f4b, d5a2dc54 (socket and Thallium transports), a3dea9e9 (send threads), b196ef91 (`CLIO_NET_DIRECT`), 5843f6f2 (drain budget, avoid shards), aa5285b4 (reply aggregation), 5e6f9deb, bdb8dc36, 0139780a, 16135918 (peer death and dial) |
| §3.9 clio-fs / FUSE | 8dce3f7c, fff12374, 313a6ec4 (#1323), e131e9e3, 71bfc793 (holder-push polling, duplicate commits), a3e72ac5 (EVLAT timers), 41f4489d (fsync through the chain), e90cd9b1 (StatPath init), 0421f884 (read-ahead through the bound client), 48a265c3, 29c17794, 1a3869d4, 2f53edab, cac33360 (FUSE knobs) |


## 9. Requests to the clio-core developer (ready to post)

CLIO uses clio-core; it does not own it. Each request below gives what CLIO needs, what
v2.5.0 does, and a minimal reproduction against the PyPI wheel. All the reproductions use
the setup in §9.0. Requests R1–R5 were reproduced on 2026-11-22. R6–R10 come from the
code, the binding list or the PyPI file list.

### 9.0 Common setup (Linux, `pip install iowarp-core==2.5.0`)

```bash
export H=$PWD/h P=19500 USER=lv19500
export CLIO_BIND_ADDR=127.0.0.1 CLIO_VIZ_ENABLE=0
export CLIO_MEMFD_DIR=/tmp/clv$P CLIO_SERVER_CONF=$H/cte.yaml
mkdir -p $H/conf $CLIO_MEMFD_DIR && chmod 700 $CLIO_MEMFD_DIR
cat > $CLIO_SERVER_CONF <<Y
networking: {port: $P}
runtime: {num_threads: 4, conf_dir: "$H/conf"}
compose:
  - {mod_name: clio_bdev, pool_name: "ram::chi_default_bdev", pool_query: local, pool_id: "301.0", bdev_type: ram, capacity: "1GB"}
  - mod_name: clio_cte_core
    pool_name: cte_main
    pool_query: local
    pool_id: "512.0"
    restart: true
    storage:
      - {path: "$H/storage.bin", bdev_type: file, capacity_limit: "${CAP:-2GB}", score: 1.0, persistence_level: temporary}
    dpe: {dpe_type: max_bw}
    performance: {metadata_log_path: "$H/metadata.log", transaction_log_capacity: "32MB"}
Y
start() { setsid clio-run start > $H/daemon.log 2>&1 < /dev/null & sleep 4; }
stop()  { clio-run stop; }
```

`put.py` writes N blobs, each acknowledged, and records the names. `check.py` reads them
back and counts the outcomes.

```python
# put.py N [size]
import json, os, sys, clio_cte_core_ext as c
c.clio_init(c.RuntimeMode.kClient, False)
c.initialize_cte(os.environ["CLIO_SERVER_CONF"], c.PoolQuery.Dynamic())
cl = c.get_cte_client()
t = c.Tag("t")
tid = t.GetTagId()
n, sz = int(sys.argv[1]), int(sys.argv[2]) if len(sys.argv) > 2 else 4096
ok = [i for i in range(n)
      if cl.AsyncPutBlob(tid, f"k{i}", (f"{i:08d}" * sz)[:sz].encode()).wait(30) == 0]
json.dump([ok, sz], open("ok.json", "w"))
print("acked", len(ok), "of", n)
```

```python
# check.py
import json, os, clio_cte_core_ext as c
c.clio_init(c.RuntimeMode.kClient, False)
c.initialize_cte(os.environ["CLIO_SERVER_CONF"], c.PoolQuery.Dynamic())
t = c.Tag("t")
ok, sz = json.load(open("ok.json"))
names = set(t.GetContainedBlobs())
good = wrong = unreadable = missing = 0
for i in ok:
    if f"k{i}" not in names:
        missing += 1
        continue
    try:
        b = bytes(t.GetBlob(f"k{i}", sz))
    except RuntimeError:
        unreadable += 1
        continue
    if b == (f"{i:08d}" * sz)[:sz].encode():
        good += 1
    else:
        wrong += 1
print(dict(good=good, wrong=wrong, unreadable=unreadable, missing=missing))
```

### R1. A full filesystem under the metadata WAL loses acknowledged puts with no error

- **CLIO needs:** a put that returns 0 to be durable, or a non-zero rc when its WAL record
  cannot be written. This is defect 2 of the #1335 comment of Nov 16, re-checked on 2.5.0.
- **v2.5.0:** every put returns 0 and the daemon logs nothing about the failed WAL write.
  After restart, most blobs are listed but `GetBlob` fails, and `TransactionLog::Load`
  reports a bad record (`transaction_log.h:628`). The rc 28 from 33fedf5d covers only the
  data device.
- **Repro** (root, for the tmpfs):

  ```bash
  mkdir h && mount -t tmpfs -o size=6m tmpfs h
  CAP=4MB    # then run the §9.0 setup
  start
  dd if=/dev/zero of=h/filler bs=4k   # fill the rest of the fs
  python put.py 150 16384             # prints: acked 150 of 150
  stop; rm h/filler; start
  python check.py                     # observed: good=28, unreadable=122
  ```

- **Ask:** fail the put (rc 28 or an I/O rc) when its WAL append fails, and log it at error.

### R2. shm cache data mode acknowledges puts that a daemon crash then loses

- **CLIO needs:** an acknowledgement that means durable, or a way to wait for durability from
  Python.
- **v2.5.0:** with `clio_cte_core_shm_cache_writethru` (`md_only: false`) in front of the
  core, a sync put returns once the bytes are in the segment. A SIGKILL right after the last
  put loses most of them.
  - Observed over 3000 × 4 KiB puts: 353–720 good, the rest missing or listed-but-unreadable.
    A clean stop, or a kill 0.5 s later, keeps all 3000.
  - `Runtime::Destroy` does not drain the log.
  - A failed flush becomes a sticky error that only `SyncTag` reports, and `SyncTag` is not
    bound in Python.
- **Repro:**
  1. Run the §9.0 setup, appending to the compose:

     ```yaml
     - {mod_name: clio_cte_core_shm_cache_writethru, pool_name: clio_cte_core_shm_cache_writethru,
        pool_query: local, pool_id: "568.0", next_pool_id: "512.0", md_only: false, capacity: "256M"}
     ```

  2. In `put.py`, replace the `AsyncPutBlob(...)` loop with `t.PutBlob(f"k{i}", data)` (the
     sync cache path); `cl.HasCache()` returns True.
  3. `start; python put.py 3000; kill -9 <clio-run pid>; start; python check.py`.
- **Asks:**
  - a Python `SyncTag(tag_id)` (or `FlushData`) that returns once earlier puts are in the
    core's WAL;
  - drain the dirty log in `Destroy` and the stop hook;
  - optionally, a compose key that makes the client ack only after the range has landed.

### R3. The viz dashboard accepts cross-origin, unauthenticated pool destroys

- **CLIO needs:** a loopback daemon that a web page in the user's browser cannot reconfigure.
- **v2.5.0:** `clio-run start` enables the dashboard by default (127.0.0.1:8080). Form-encoded
  POSTs are parsed (`viz_server.cc:775-800`), and there is no Origin, Referer or Host check
  and no token.
- **Repro:**

  ```bash
  CLIO_VIZ_ENABLE=1   # then run the §9.0 setup
  start
  curl -s -X POST -H 'Origin: http://evil.example' \
    -H 'Content-Type: application/x-www-form-urlencoded' \
    --data-binary 'x=1' http://127.0.0.1:8080/api/pools/301.0/destroy
  # observed: {"ok":true,...}; /api/pools no longer lists 301.0
  ```

- **Ask:**
  - Reject mutating routes whose `Origin`/`Host` is not the dashboard's own, or require a
    per-start token.
  - Consider making the dashboard opt-in for `clio-run start`.

### R4. A core that infers volatile deletes an existing persistent WAL

- **CLIO needs:** a config mistake not to destroy data that is already on disk.
- **v2.5.0:** a core whose file tier has no `persistence_level` infers volatile. Create then
  runs `DiscardPersistentMetadata` (`core_runtime.cc:862`, `:8726-8752`), logged only at INFO:
  "fresh start discarded 10 metadata log file(s)". Restoring the label recovers nothing.
- **Repro:**

  ```bash
  # §9.0 setup
  start; python put.py 50; stop
  sed -i 's/, persistence_level: temporary//' h/cte.yaml
  start; ls h/metadata.log*          # observed: gone
  stop                               # re-run the §9.0 heredoc to restore the label
  start; python check.py             # observed: unreadable=50
  ```

- **Ask:** when existing WAL or snapshot files are found and the core is (inferred) volatile,
  refuse to start with an error, or at least keep the files and log at error, instead of
  deleting them.

### R5. Reopening a file bdev with a different `CLIO_BDEV_STRIPE_FILES` silently returns wrong data

- **CLIO needs:** a store that refuses to open under a layout it was not written with.
- **v2.5.0:** the commit (dfbc2e20, marked experiment) notes that reopening with a different
  N is not handled. A store written with N=1 and reopened with N=2 creates empty
  `<file>.s0`/`.s1`. Every block then reads back wrong, with no error. Unsetting the variable
  recovers the data.
- **Repro:**

  ```bash
  # §9.0 setup
  start; python put.py 100 65536; stop
  CLIO_BDEV_STRIPE_FILES=2 start
  python check.py                    # observed: wrong=100
  stop; start; python check.py       # observed: good=100
  ```

- **Ask:** record the stripe count and unit in the backing file or the alloc log, and refuse
  (or adopt the recorded layout) on mismatch.

### R6. Python bindings still missing (#1337, #1341; still open at v2.5.0)

- **CLIO needs:** one round trip for N records with a status per record, plus replace,
  truncate and tag delete.
- **v2.5.0 binding diff:** only the four Defer methods were removed, and `ConnectCache`,
  `HasCache` and sync `PutBlob`/`GetBlob`/`GetBlobSize` were added. None of these is bound:
  - MultiPutBlob (now with per-record `flags_`/`score_`) and multi-get;
  - put-replace (`kCtePutReplace`), append (`kCtePutAppend`) and `TruncateBlob`;
  - `DelTag`, and the new MultiDelBlob/MultiDelTag;
  - `SyncTag`, `FlushData` and `SetDurability`.
- **Indexer:** MultiDelBlob/MultiDelTag (methods 60/61) are not interposed by the BM25 indexer
  (`indexer/clio_mod.yaml:14-26`), so a MultiDel through it would leave search docs for
  deleted blobs.
- **Repro:**

  ```bash
  python -c "import clio_cte_core_ext as c; print([n for n in dir(c.Client) if 'Multi' in n or 'Trunc' in n or 'Sync' in n or 'DelTag' in n])"
  # prints []
  ```

- **Asks:**
  - async bindings with a per-item rc;
  - indexer interposition of methods 60/61;
  - per-record preconditions (absent / expected version) on MultiPut (#1341).

### R7. The new synchronous Python calls cannot be bounded

- **CLIO needs:** every call to the daemon bounded, so a stalled daemon ends in a typed
  timeout instead of a hung thread.
- **v2.5.0:** `Client.PutBlob`/`GetBlob`/`GetBlobSize` wait with an unbounded `fut.Wait()`
  (`core_client.h:745`, `:834`, `:879`). They are the only calls that use the shm cache, so
  CLIO has to choose between the cache and bounded waits.
- **Repro:**

  ```bash
  # §9.0 setup, no cache
  start
  python -c "...; import os,signal; os.kill(<clio-run pid>, signal.SIGSTOP); cl.GetBlobSize(tid, 'k0')"
  # never returns
  ```

- **Ask:** an optional `max_sec` that returns None on timeout, like `Future.wait`.

### R8. A file tier that cannot be reserved leaves a daemon with no targets running

- **CLIO needs:** a start that fails when its configured storage cannot be created.
- **v2.5.0:** a `capacity_limit` larger than the free space logs `Cannot reserve … bytes` and
  `Failed to register target`, but the daemon reports started. Every put then returns rc 11.
- **Repro:**

  ```bash
  mkdir h && mount -t tmpfs -o size=6m tmpfs h
  CAP=64MB    # then run the §9.0 setup
  start                            # starts
  python put.py 10 65536           # observed: acked 0 of 10, every rc 11
  ```

- **Ask:** fail `clio-run start` (or expose a "no storage target" state the client can read)
  when a compose storage target fails to register.

### R9. macOS 14 wheels (#1340): still not published

- **Observed:** PyPI 2.5.0 and 2.5.1 publish `macosx_15_0_arm64` only, for cp310–cp315.
- **Ask:** unchanged from #1340.

### R10. WAL torn-tail append (#1335 comment, defect 1): still present

- **Observed:** `OpenFile` still opens with `std::ios::app` after a torn record
  (`transaction_log.h:334-346`), and `Load` stops at the torn bytes (`:622-633`). Every
  record appended after a tear is lost on the next restart, until a compaction rewrites the
  shard.
- **Repro:** the steps in the #1335 comment of Nov 16 still apply. To make a tear, append a
  header claiming 4096 bytes followed by 10 bytes:
  `struct.pack('<BQI', 1, 999999, 4096) + b'x' * 10`.
- **Ask:** truncate to the last good record on open.

### Smaller notes

- **`Tag.GetBlob` docstring:** it promises "zero-IPC on a shared-cache hit", but the binding
  calls `AsyncGetBlob`, which never consults the cache.
- **Explicit put score:** silently dropped on the cache path (`core_client.h:736-738`).
- **Shm cache docs:** `core_shm_cache_writethru/DESIGN.md`'s compose example uses
  `next_pool_id 563.0` and the unparsed `max_name`/`inval_timeout_ms` keys, and `HANDOFF.md`
  still ships.
- **rc 15:** means both `kCtePutAppendInvalidRc` and `kCteInvalidDurabilityRc`.
