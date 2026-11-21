# clio-core (iowarp-core) 2.4.1 → 2.5.0 upgrade

**Status:** adopted on branch `claude/clio-core-2.5-upgrade` (from `develop`) · **Pin:**
`iowarp-core~=2.5.0` · **Upstream:** iowarp/clio-core `v2.4.1..v2.5.0`, 411 commits,
706 files, +78k/−18k lines. `v2.5.1` (tag only, not on PyPI) changes only Linux
packaging (32518175).

Upstream ships **no changelog or release notes** (none in the repo root, `site/pages`
or `docs/`). Everything below comes from the code and commit messages, read at both
tags, and from live runs on this machine. Upstream references are `file:line` at
`v2.5.0` and commit hashes in iowarp/clio-core.

## 1. Summary

| # | Change in 2.5.0 | Effect on CLIO | Done here |
|---|---|---|---|
| 1 | Daemon binary renamed `clio_run` → `clio-run` (139125d5); wheel ships only `bin/clio-run`; `clio_run` survives as a console-script alias | **Break:** CLIO never found the launcher, so no daemon started | Launcher lookup, process census, process tree and disk-GC markers accept both names |
| 2 | Daemon refuses to start "half-bound" when it cannot listen on `<memfd dir>/clio_<port>.ipc` (ipc_manager.cc:763-787) | **Break for long `USER`:** exit 1, one ERROR line. The test harness's nested `USER` (90 chars) hit it; real users can via a long account name, `CLIO_CORE_USER` or `CLIO_MEMFD_DIR` | Typed pre-spawn guard `clio_core_ipc_path_too_long`; harness `USER` no longer nests |
| 3 | `AsyncRegisterTarget(..., persistence_level=-1)` (e24a48fd); −1 resolves only against devices loaded at start | **Break:** live capacity growth refused with rc 16 exactly when the store is full (verified live) | Growth passes the planned tier's level; live test |
| 4 | Core durability: explicit `durability:` key, else persistent only if *every* device is durable (7822175b, core_config.cc:200-222); a volatile core keeps **no WAL** | Latent: CLIO accepted a file tier beside a volatile RAM data tier, which now loses everything on restart | `config_is_durable` follows upstream's rule |
| 5 | Per-user memfd dir must be a real dir owned by this user, not group/other-writable (2a6fc440) | Daemon exit 1 otherwise | Typed pre-spawn check `clio_core_memfd_dir_unsafe` |
| 6 | macOS also binds `<memfd>/clio_zmq_<port+3>.ipc` (ipc_manager.cc:189-206) | 4 bytes less room on a 104-byte `sun_path` | Guard checks every socket |
| 7 | Control channel (port+2) binds through `DefaultServerBindAddr` (60cb3df2, clio-core#1355) | CLIO's port-hold stopgap is obsolete and, with it, the channel failed to bind | Stopgap removed (#2038) |
| 8 | Runtime listens on loopback by default (98bb4eb4) | CLIO already pins `CLIO_BIND_ADDR=127.0.0.1`; now belt and braces | Comment updated |
| 9 | Crash surfaced as "no daemon log output" | CLIO-side race: the crash watcher read the log before the pump drained the daemon's last line | Watcher waits (≤2 s) for the pump; record carries `log_tail_settled` |
| 10 | New rc 28 = device ENOSPC (33fedf5d), was generic rc 21 | Reaches CLIO's lost-write record as `native_code=28`; not yet mapped to a disk-full reason | Recommended (§9) |
| 11 | `runtime.client_data_segment_size` / `CLIO_CLIENT_DATA_SEGMENT_SIZE` now parsed (20222438, #1359) | Each attaching process maps 512 MB + 32 MB | Recommended knob (§9) |
| 12 | Shared-memory caches (`core_shm_cache_writethru`, `core_persist_write_back`, `core_persist_write_thru`) | Only active when composed; CLIO's compose does not list them, so CLIO stays on the task path | Not adopted (§5) |

A store written by 2.4.1 reads back under 2.5.0 (flat-name restore, b030f9cd). The
reverse is unsafe: 2.5.0 writes WAL record types 12/13 that 2.4.1 does not know.

## 2. Python API (`clio_cte_core_ext`, `iowarp_core`)

Binding source: `context-transfer-engine/wrapper/python/core_bindings.cc` (+52/−47).
`iowarp_core/__init__.py` and `_config.py` are unchanged; `_cli.py` gained the
`clio-run` entry.

| Change | Upstream | CLIO use | Breaks? |
|---|---|---|---|
| Removed `Client.AsyncPutBlobDefer`, `AsyncGetBlobDefer`, `AwaitPutsUntilSpace`, `DeferErrorCount` | deleted in 058edeac | none | no |
| Added synchronous `Client.PutBlob/GetBlob/GetBlobSize` (GIL released, shm-cache path when bound) | core_bindings.cc:746-779 | CLIO uses `Tag.PutBlob/GetBlob` and `Client.Async*`, unchanged | no; optional |
| Added `Client.ConnectCache()`, `HasCache()` | core_bindings.cc:796-804 | not used; only meaningful with a composed shm cache | no |
| `AsyncRegisterTarget(..., attach_existing, growth_unit, persistence_level=-1)` | core_bindings.cc:832-866 (e24a48fd) | `arc/clio_core_growth.py::_register` | **yes**, fixed (§1 #3) |
| New return codes: rc 16 (volatile target refused by a persistent core), rc 28 (device ENOSPC) | core_tasks.h:2458, :1060 | rc 28 reaches `LostWrite.native_code` | see §9 |
| Unchanged and used: `initialize_cte`, `RuntimeMode.kClient`, `PoolQuery`, `PoolId/TagId/BlobId`, `PUT_EXISTS_RC`, `BdevType.kFile`, `Tag`, `GetTagId`, `GetContainedBlobs`, `SemanticSearch`, `AsyncGetBlob[Size]`, `AsyncPutBlob[IfAbsent]`, `AsyncDelBlob`, `AsyncTagQuery`, `AsyncStatTargets`, `AsyncGetCapacity`, `AsyncGetTargetInfo` | | `arc/clio_core_attach.py`, `arc/storage.py`, `arc/clio_core_async_ops.py` | no |

Live check of the growth break (`AsyncRegisterTarget` of a 64 MiB file target on a
running CLIO home daemon):

| iowarp-core | no `persistence_level` | `persistence_level=1` |
|---|---|---|
| 2.4.1 | rc 0 | `TypeError` (argument does not exist) |
| 2.5.0 | **rc 16** | rc 0 |

## 3. Bundled binaries and console scripts

- Wheel `iowarp_core/bin/`: `clio-run`, `clio_cte_bench`, `clio_cte_fuse`,
  `clio_run_thrpt_bench`. There is **no `bin/clio_run`** and no `clio-cae` (native
  packages only).
- `[project.scripts]`: `clio-run` (new) and `clio_run` (alias), both exec
  `bin/clio-run`.
- New CLI: `clio-run --version`, `viz <start|stop|status>`, `pool <start|stop|rm> <pool>
  [--force]` (leveled DestroyPool, 301b3465/8360f7e0), `compose stop|rm --force`, global
  `--local`. CLIO needs none of them.
- `stop` now waits on the daemon pid (50506f2c, #1365/#1357).
- CLIO: `arc/runtime_spawn.py::_runtime_launcher_path` tries `clio-run` then `clio_run`
  (`.exe` on Windows); `runtime/process_census.py`, `runtime/process_tree.py` and
  `runtime/disk_gc.py` match both. Messages now name `clio-run`.

## 4. Config keys CLIO generates

CLIO writes its own `cte.yaml` (`arc/clio_core_config.py` template; the test harness
template in `tests/_cte_isolation.py`) and passes it as `CLIO_SERVER_CONF`, so the new
shipped default yaml does not apply.

| Key | 2.5.0 status |
|---|---|
| `networking.port`; `runtime.num_threads`, `queue_depth`, `main_segment_size`, `metadata_segment_size` | honored |
| `runtime.conf_dir` | honored; now env-expanded |
| `clio_bdev` `ram::chi_default_bdev` at `301.0`, `bdev_type ram`, `capacity` | honored |
| `clio_cte_core` `cte_main` at `512.0`, `restart: true`, `storage[path, bdev_type file, capacity_limit, score, persistence_level "temporary"]`, `dpe.dpe_type`, `performance.metadata_log_path`, `transaction_log_capacity` | honored; the single `temporary` device makes the core **inferred persistent**, so the WAL is on and restore runs |
| `clio_cte_indexer` block | honored, byte-identical parser |
| `performance.flush_data_period_ms`, `flush_data_min_persistence` | removed upstream (ignored with a warning); CLIO does not set them |

New core key `durability: persistent|volatile` (7822175b). Unstated, the core is
persistent only when it has devices and every one is `temporary`/`long_term`; a volatile
core keeps no WAL, and a persistent core refuses volatile targets with rc 16.

New env vars (all `CLIO_` prefixed): `CLIENT_DATA_SEGMENT_SIZE`, `PEER_TRANSPORT`,
`NET_TIERS`, `NET_TIER_LANES`, `NET_TIER_ZMQ_FROM`, `NET_DIRECT`, `NET_SPREAD`,
`NET_SEND_THREADS`, `NET_AVOID_SHARDS`, `NUMA_INTERLEAVE`, `BDEV_AIO`,
`BDEV_STRIPE_FILES`, `BDEV_STRIPE_UNIT`, `DAOS_IO_THREADS`, `DAOS_POLL_US`,
`CHIMOD_ABI_CHECK`, `INLINE_ALLOC_DIRECT`, `SHARD_DRAIN_BUDGET`, `SHM_DRAIN_FAST`.
Removed: `CTE_NO_PRIV_PUT`, `CTE_PUT_SIEVE`, `CTE_SHM_BLOB_CAPACITY`,
`CTE_SHM_TAG_CAPACITY`. `CLIO_MEMFD_DIR` is now path-normalized and ownership-checked.

## 5. Shared-memory cache and policy caches

The headline feature: a shared-memory write-through cache (`core_shm_cache_writethru`,
pool 568.0) that batches and coalesces puts and serves reads from client-mapped memory,
plus owner-local policy caches `core_persist_write_back` / `core_persist_write_thru`
(d408362a, 7b41c5f1, 058edeac and the `shm_wtc` series).

- They are **composed only when listed** in `compose`. The client binds to the shm
  cache only when the CTE `Create` returns cache keys or `CLIO_CTE_POOL` names a pool
  in front of one (content_transfer_engine.cc:57-120).
- The shipped default yaml (`iowarp_core/data/clio_default.yaml`) now composes a
  persistent and a volatile core, the write-back cache (flush period 10 s,
  `fallback_to_persistent: true`) and the shm cache (`md_only: true`, 256 MB). The old
  `clio_cte_cache` (563.0) is gone. So the upstream default policy is **write-back**.
- **CLIO's compose (bdev + core + indexer) gets none of it.** Every number in §8 is the
  plain task path, as in 2.4.1.

Not adopted here: a write-back cache moves the durability point (records sit dirty for
up to the flush period), and CLIO's durability gate, WAL repair and crash recovery are
built around the core's own WAL. Composing the shm cache in `md_only` mode is the
lower-risk first step; it needs a design decision and its own soak, and the
synchronous `Client.PutBlob/GetBlob` are the API that takes that path.

## 6. Metadata and storage layout

- **Flat tag names** (fff3d78e): no hierarchy; a name's binding lives on its hash
  owner, the name list on the tag's home. WAL gains record types `kBindName=12` and
  `kUnbindName=13`; magic `2LAW`, header `<u8 type><u64 seq><u32 len>` and types 0-7 are
  unchanged, so `arc/clio_core_wal_repair.py` stays valid.
- **2.4.1 stores restore** (b030f9cd): only legacy hierarchical names (`/` or
  `$tagid{…`) are dropped; CLIO's tags never start with `/`. Verified live: a store
  written by 2.4.1 reads back under 2.5.0.
- **One-way:** a 2.5.0 WAL holds types 12/13 that 2.4.1 cannot read. Do not downgrade
  a store.
- The torn-tail append bug is still upstream (transaction_log.h:335-346, :622-633), so
  CLIO's pre-spawn `repair_metadata_logs` stays.
- Pool log `<conf_dir>/wal/pools.<node>.bin` is cleared on a fresh start (0a82f4b9);
  an explicitly volatile file bdev keeps no allocator log (4e108e2b).
- Upstream added a design for CTE/CLIO-FS metadata recovery after a job (073752b2);
  design only.

## 7. Daemon lifecycle, ports and platforms

- **Ports** (base B): B main ROUTER, B+1 local server, B+2 control channel, B+3 client
  ROUTER (ipc:// on macOS). B, B+1, B+3 and the IPC socket are all-or-nothing. Size-tier
  ports B+16… are off unless `CLIO_NET_TIERS`/`CLIO_NET_TIER_LANES`. Dashboard on
  127.0.0.1:8080 when enabled (CLIO sets `CLIO_VIZ_ENABLE=0`). CLIO's 6-port block
  still covers the default set.
- **Bind address** order: `CLIO_BIND_ADDR`; loopback under `CLIO_TEST_MODE`; 0.0.0.0
  with a hostfile or a non-loopback `CLIO_SERVER_ADDR`; else 127.0.0.1.
- Live listening sockets of a CLIO home daemon with `CLIO_BIND_ADDR=127.0.0.1` and the
  port hold removed:

  | iowarp-core | TCP listeners | Unix socket |
  |---|---|---|
  | 2.4.1 | 127.0.0.1:B, B+1, B+3, **0.0.0.0:B+2** | `/tmp/clio_<user>/clio_B.ipc` |
  | 2.5.0 | 127.0.0.1:B, B+1, B+2, B+3 | `/tmp/clio_<user>/clio_B.ipc` |

- **Memfd dir** `$CLIO_MEMFD_DIR` or `/tmp/clio_$USER` (`%TEMP%\clio_%USERNAME%` on
  Windows) holds the IPC socket, segment links and pid record
  `chi_runtime_pid_${USER}_<B>`. POSIX: must be a real 0700-ish dir owned by the euid.
- **IPC path limit:** `sun_path` is 108 bytes on Linux/Windows, 104 on macOS/BSD. With a
  5-digit port, Linux allows a `USER` of up to 82 characters (verified: 82 starts, 83
  exits 1); macOS, with the extra `clio_zmq_<B+3>.ipc`, allows 74.
- **Windows:** verbatim `\\?\` paths are accepted (c47f1607, #1356); the memfd dir is
  keyed on `USERNAME`, while CLIO's per-home override sets `USER`, so all homes on one
  Windows machine share one memfd dir (no collision: sockets and pid records are keyed
  by port). Stop waits on a process handle and checks the image is `clio-run`.
- **macOS:** local client ROUTER over `clio_zmq_<B+3>.ipc` by default
  (`CLIO_ZMQ_LOCAL_IPC=0` turns it off).

## 8. Benchmarks: 2.4.1 vs 2.5.0

BENCHMARK_RESULTS

## 9. Knobs and follow-ups for CLIO

1. **`CLIO_CLIENT_DATA_SEGMENT_SIZE`** (or `runtime.client_data_segment_size`): every
   CLIO process that attaches maps 512 MB + 32 MB; on Windows that is committed memory.
   Size it from CLIO's largest single put (segments are chunked) after a large-artifact
   put test; not changed here.
2. **Map rc 28** (device ENOSPC) to a typed disk-full reason in the put/lost-write path
   instead of a generic native refusal.
3. **State `durability: persistent`** in CLIO's seeded template so a stray volatile
   device is a loud rc 16 instead of a silent WAL-off core. Needs a
   `_before_clio_upgrades` variant so managed growth keeps recognizing the seed.
4. **Shm cache:** evaluate composing `core_shm_cache_writethru` in `md_only` mode
   behind a flag, with a soak; do not compose the write-back cache without a
   durability design.
5. Optional: record `clio-run --version` in `clio_core_daemon_version.py` rather than
   package metadata alone.

ISSUE_VERDICTS
