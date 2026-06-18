# Distributable Expert Runtime — Handoff

You're picking up the CLIO **distributable expert runtime**: a parent agent that delegates
expert work to **isolated worker processes** over **clio-core** (IOWarp CTE), so experts can
run in separate processes and, on a cluster, separate nodes. This doc passes you everything I
know so you can deploy + validate + benchmark it on **NCSA Delta**.

Branch: `feat/distributable-expert-runtime` (iowarp/clio-agent). Epic: #667.

---

## 1. What this is (architecture in one screen)

```
            parent agent (clio app)                         one NODE
   CLIO_EXPERT_INVOKER=clio_core_isolated                 ┌─────────────┐
                    │                                     │  clio_run   │  ← daemon (CTE),
   _invoke_child_expert → run_child_via_boundary          │  daemon     │    one per node,
        (mode="clio_core_isolated")                       │  :9413      │    networked into a
                    │                                     └─────┬───────┘    cluster by clio-core
        IsolatedExpertInvoker  ── routes to a live ──────►  CTE blobs  ◄──── isolated workers
        (presence-discovered, lease-free)                   (mailbox)        (separate processes,
                    │                                                         attached to the
   delegation result folds back into the turn                                LOCAL daemon)
```

- **Transport = a mailbox over CTE blobs.** The parent writes a `.req` blob to a worker's
  private queue + rings a doorbell; the worker (sole reader of its queue) serves it and writes
  a `.res`; the parent reads it and discards. **No claim/lease — exactly-once by construction.**
  Workers announce **presence** (a heartbeat blob, on its own task) so the parent routes to live
  ones and reassigns on timeout.
- **Three layers:** the *transport* (`runtime/clio_core_transport.py`), the single-box
  *orchestration* (`runtime/worker_fleet.py` — `WorkerFleet` spawns/supervises workers), and the
  multinode *deployment* (`runtime/cluster_deploy.py` + `cluster_cli.py` — daemons + workers
  across nodes over SSH).
- **clio-core does the cross-node part.** A worker on node B reading a blob the parent wrote via
  node A is clio-core's (production-validated) daemon-mesh networking — not our code.

---

## 2. What is PROVEN vs UNPROVEN (read this before trusting anything)

**Proven (all on ONE box, over the REAL clio_run daemon, this branch):**
- Isolated delegation cross-process, exactly-once, zero claim blobs — `test_isolated_cross_process.py`.
- **Sustained: 1000 delegations / ~10k CTE ops, 0 failures, throughput rising** (not collapsing).
- Full stack: `WorkerFleet` → CTE → real gact worker → **real ALCF child** →
  `test_fleet_orchestrated_isolated_delegation_over_cte_with_real_child`.
- Deploy orchestration: `clio-cluster up/status/down` brings up a real daemon + isolated worker
  processes, in **local** mode AND over **real ssh** (localhost) — `test_cluster_deploy.py`.
- clio-core itself: 128 concurrent client processes, 0 failures, bounded RSS.

**UNPROVEN — this is your job on Delta:**
- **True multinode**: ≥2 physical nodes, daemons forming a cluster via the hostfile, `--induct`
  joins, and a node-B worker actually reading a node-A blob. Single box can't test this.
- **`pool_query: broadcast`** for cross-node blob visibility actually working (we made it
  config-driven but couldn't verify cross-node on one box — see #7 below).
- Throughput at cluster scale / with GPUs doing real inference.

**Hard-won lesson (don't repeat it):** clio-core#561 ("daemon wedges after ~700 ops") was
**our** misconfiguration, not a clio-core bug. Two causes: (1) `capacity: "0g"` = 80% of system
RAM (a memory bomb); (2) our transport hammered the daemon with `GetContainedBlobs` tag-scans
(~28ms each) + a `GetBlob` that raced `discard`. Fixed our side → it sustains. **If something
"wedges," suspect our config/usage first.** Always run clio-core in isolation with a clean
client before blaming it. #561 is closed.

---

## 3. Install on Delta

Delta = SLURM, A100/A40 nodes, shared `/scratch` + `/projects`, per-node local `/tmp`, ssh
between allocated nodes.

1. **Get the code + deps** (on a shared FS so every node sees it, e.g. `/projects/<alloc>/clio`):
   ```bash
   git clone -b feat/distributable-expert-runtime git@github.com:iowarp/clio-agent.git
   cd clio-agent
   module load python            # or the Delta python module; need >=3.12
   pip install uv                # or `module load uv` if available
   uv sync --extra dev           # installs clio_agent + iowarp-core (the clio-core wheel)
   ```
   `iowarp-core>=2.1.0` is a pip dep — it bundles `clio_run` + the CTE `.so`s. Find them:
   ```bash
   uv run python -c "import iowarp_core,os;p=os.path.dirname(iowarp_core.__file__);print(p+'/bin', p+'/lib')"
   ```
   `LD_LIBRARY_PATH` MUST include both `bin` and `lib` for clio_run + any CTE client (ssh does
   not load a login profile — the deployer injects it per command from `clio_core.ld_library_path`).

2. **Allocate nodes** (SLURM): `salloc -N 3 --gpus-per-node=1 -t 02:00:00 ...` then
   `scontrol show hostnames $SLURM_JOB_NODELIST` for the node list. ssh between compute nodes
   works inside an allocation (verify: `ssh <other-node> hostname`).

3. **LM provider for the workers.** Each worker runs a real expert child. Either point them at
   ALCF (as the tests do: `CLIO_LM_PROVIDER=argonne` + the Sophia endpoint + a Globus token under
   `~/.globus` — NOTE: do NOT override `$HOME`, the token lives there) or stand up local vLLM on
   the Delta GPUs and point `CLIO_LM_API_BASE` at it. Set these in `cluster.worker_env`.

---

## 4. Deploy + configure

The whole deployment is ONE yaml (its `cluster:` + `transport:` sections). Copy
`external/clio-core/clio-cluster.example.yaml`, edit it, put it on the shared FS.

```bash
export CLIO_CLUSTER_CONFIG=/scratch/<you>/clio-cluster.yaml
clio-cluster validate     # check the spec
clio-cluster up           # render config+hostfile to shared_dir; start daemons; start workers
clio-cluster status       # per-node daemon + worker liveness
clio-cluster down         # teardown
# then start the PARENT agent with CLIO_EXPERT_INVOKER=clio_core_isolated + the same config
```

Key config (full reference: `docs/distributed/cluster.md`, `cluster_config.py`, the example yaml):
- `cluster.shared_dir` — a shared (NFS-like) path on Delta, e.g. `/scratch/<you>/clio-shared`.
  The daemon config + hostfile render here ONCE; every node reads the identical file.
- `cluster.nodes` — `host` (must match each node's `gethostname()`) + `addr` (dialable).
  **Line order == clio-core node_id.** On Delta, `scontrol show hostnames` gives the hosts.
- The CTE **data tier path** in `external/clio-core/clio.yaml` is node-local (`/tmp/clio_cte_tier`)
  — that's correct (each node's own disk), NOT the shared FS. Make sure `/tmp` is writable on
  compute nodes (it is on Delta).
- `transport.pool_query: broadcast` — **REQUIRED for multinode** so a worker sees a blob another
  node wrote. (`dynamic` is fine single-node.)
- Precedence everywhere: **config file > env var > default**.

Bring-up order (what `up` does): render → start seed daemon (`clio_run start`) → start the rest
(`clio_run start --induct`) → **barrier on every daemon's port** → launch workers attached to
each node's LOCAL daemon. The parent then routes delegations to the pool.

---

## 5. Code map (where to look)

| Concern | File |
|---|---|
| Mailbox transport, isolated invoker, presence, doorbell | `src/clio_agent/runtime/clio_core_transport.py` |
| The parent hinge (`_invoke_child_expert`, `isolated_delegation_store`) | `src/clio_agent/gact/app.py` |
| `run_child_via_boundary` (modes incl. `clio_core_isolated`) | `src/clio_agent/gact/delegation_invoker.py` |
| Single-box orchestration (`WorkerFleet`, spawners) | `src/clio_agent/runtime/worker_fleet.py` |
| The worker entrypoint (`python -m clio_agent.runtime.clio_core_worker`) | `src/clio_agent/runtime/clio_core_worker.py` |
| CTE store, `_resolve_pool_query`, the `iter_names`/`get` race fix | `src/clio_agent/arc/storage.py` |
| Config system (config>env>default), `apply_to_env` | `src/clio_agent/runtime/cluster_config.py` |
| SSH exec (`SshRunner`/`SshSpawner`) | `src/clio_agent/runtime/ssh.py` |
| Multinode deployer + `clio-cluster` CLI | `src/clio_agent/runtime/{cluster_deploy,cluster_cli}.py` |
| Bundled daemon config + example deployment | `external/clio-core/clio.yaml`, `clio-cluster.example.yaml` |
| Tests (gated `CLIO_RUN_CROSS_PROCESS=1`, some `CLIO_RUN_LIVE=1`) | `tests/test_distributed/`, `tests/test_runtime/test_cluster_config.py` |

Run the gated proofs locally first: `CLIO_RUN_CROSS_PROCESS=1 uv run pytest tests/test_distributed -m cross_process`.

---

## 6. Known limitations (don't be surprised)

- **Throughput ~9/s (poll-dominated).** Each delegation crosses two ~50ms poll quanta (worker
  pickup + parent result-wait) + a ~28ms `GetContainedBlobs`. The daemon is NOT the bottleneck
  (it sat at 66MB). The real fix is a **push/blocking transport**, which needs a **clio-core
  blob-notify primitive that DOES NOT EXIST** (the CTE API is `PutBlob/GetBlob/GetBlobSize/
  GetContainedBlobs/DelBlob/BlobQuery/Semantic/TemporalSearch` — no notify/subscribe/watch). So
  cross-process "push" is currently impossible over pure CTE; pull-with-configurable-rate is the
  lever. **This is the single biggest thing to raise with the clio-core team.**
- **`pool_query: broadcast` is config-wired but unverified cross-node** (couldn't test on one box).
- The deployer's `Spawner` seam is built for SSH; a `SrunSpawner` (SLURM-native placement) would
  be a clean addition if ssh-between-nodes is awkward on Delta.

---

## 7. The clio-core gotchas (baked into the deployer, but know them)

1. Bounded config only — never `capacity: "0g"` (= 80% of RAM).
2. **Identical YAML on every node** — clio-core self-identifies by `gethostname()`; per-node
   variation is unsupported. Only env vars vary per node.
3. **Hostfile line order == node_id** — reordering renumbers the cluster.
4. `LD_LIBRARY_PATH` mandatory on every remote command.
5. `--induct` only for nodes joining a live cluster, never the seed.
6. Workers attach **LOCAL-only** — co-located with a running daemon; that's why `up` barriers on
   daemons before launching workers. Cross-node visibility needs `broadcast`.
7. Ports are a triplet: `port` (RPC), `port+3` (client ROUTER). Keep daemons ≥4 apart if you ever
   run two per host.
