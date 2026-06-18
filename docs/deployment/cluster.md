# Deploying the CLIO distributable runtime on a cluster

The distributable runtime is a parent agent that delegates expert work to **isolated worker
processes** over **clio-core** (IOWarp CTE). On a cluster that means: a `clio_run` daemon per
node (networked by clio-core), worker processes attached to each node's local daemon, and the
parent agent routing delegations to them. clio-core's cross-node networking is the
(production-validated) transport; CLIO supplies the orchestration on top.

## The mechanism to DEFINE a deployment

One YAML file — its `cluster:` section. See `external/clio-core/clio-cluster.example.yaml`.
It declares: the nodes (`host` = each node's `gethostname()`, `addr` = the dialable address;
**line order is the clio-core node_id**), worker placement (`role` + `replicas`, optional
`nodes:`), the shared config path, the clio-core binary + `ld_library_path`, ssh settings, and
the env every worker needs. The `transport:` section sets the runtime semantics (poll rate,
`pool_query`, timeouts). Everything resolves **config file > environment variable > default**.

Assumptions (per the target environment): SSH from the master/login node to every node
(SLURM acquisition or cloud allocation), and a **shared mount** (NFS or a common path) where
the rendered daemon config + hostfile are written **once** and read by every node. The CTE
**data** tier is node-local disk (`/tmp/...`, same path string on every node), not the share.

## The command to DEPLOY the infrastructure

```bash
export CLIO_CLUSTER_CONFIG=/shared/clio-agent/clio-cluster.yaml

clio-cluster validate     # check the spec (node/role/port/placement)
clio-cluster up           # render config+hostfile -> start daemons -> barrier -> start workers
clio-cluster status       # per-node daemon reachability + per-worker liveness
clio-cluster down         # stop workers, then `clio_run stop` each daemon
```

`up` does, in order:
1. **Render** the byte-identical daemon config (the bundled bounded+disk config, plus
   `networking.hostfile` pointing at the rendered hostfile) and the hostfile to `shared_dir`.
   Every node reads the *same* file — clio-core picks its own node by `gethostname()`.
2. **Start daemons** — the seed node runs `clio_run start`; the rest `clio_run start --induct`
   to join the live cluster. Each command carries `LD_LIBRARY_PATH` + `CLIO_SERVER_CONF`
   (ssh does not load a login profile).
3. **Barrier** — wait until every node's daemon port answers (the real readiness gate;
   clio-core's `wait_for_restart` probes lazily, so the deployer is authoritative).
4. **Start workers** — for each role × placed node × replica, launch
   `python -m clio_agent.runtime.clio_core_worker` (`CLIO_CORE_ISOLATED=1`, the role, attached
   to that node's **local** daemon via `CLIO_ARC_STORE=cte` + `CLIO_CTE_WITH_RUNTIME=0`).

Then start the **parent agent** normally with `CLIO_EXPERT_INVOKER=clio_core_isolated` and the
same `CLIO_CLUSTER_CONFIG`; `_invoke_child_expert` routes each delegation to a live worker in
the pool. Cross-node blob visibility uses `transport.pool_query: broadcast` (a worker on node
B reading a request the parent wrote via node A).

## Validate on one box before spending allocation

`clio-cluster` runs the local host **without ssh** (no keys needed), so a single-node "cluster"
exercises the whole orchestration — render, daemon start, worker launch, presence, status,
teardown — on the dev box. `tests/test_distributed/test_cluster_deploy.py` does exactly this
(`CLIO_RUN_CROSS_PROCESS=1`). Set `cluster.ssh.force_ssh: true` to also exercise the real ssh
path against `localhost` when keys are configured.

## clio-core gotchas baked into the deployer

- **Bounded config.** The bundled daemon config never uses `capacity: "0g"` (= 80% of RAM).
- **Identical config everywhere.** Per-node variation is *not* supported by clio-core (it
  self-identifies by hostname); only env vars vary per node, never the YAML.
- **Hostfile order == node_id.** Reordering `nodes:` renumbers the cluster.
- **`LD_LIBRARY_PATH` is mandatory** on every remote command (no login profile over ssh).
- **`--induct` only for joins**, never the seed node.
- **Workers attach LOCAL-only** — each worker must be co-located with a running daemon; `up`
  starts daemons (with a barrier) before workers for this reason.
