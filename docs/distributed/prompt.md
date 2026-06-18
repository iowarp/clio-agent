# Prompt for tomorrow's Delta session

Paste this to Claude once you're on a Delta login node with an allocation.

---

You are continuing the CLIO **distributable expert runtime** — a parent agent that delegates
expert work to isolated worker processes over clio-core (IOWarp CTE). The single-box work is done
and proven; today's job is to take it **multinode on NCSA Delta** and validate + benchmark it on
real hardware.

**Start here:**
1. Check out the branch `feat/distributable-expert-runtime` (iowarp/clio-agent) on a shared
   filesystem (`/projects` or `/scratch`), and `uv sync --extra dev`.
2. **Read `docs/distributed/HANDOFF.md` in full** before doing anything — it's the knowledge
   transfer: the architecture, exactly what is PROVEN vs UNPROVEN, the install steps, the deploy
   mechanism, the config, the clio-core gotchas, and the known limitations. Then skim
   `docs/distributed/cluster.md` (the runbook) and `external/clio-core/clio-cluster.example.yaml`.

**Your work, in order:**
- **Issue #690 — deploy + validate multinode correctness** (do this first). Bring up the cluster
  with `clio-cluster up`, then prove a parent on one node delegates to a worker on a *different*
  node (real inference, answer folds back), exactly-once holds, and resolve the open
  `pool_query` (broadcast vs dynamic) question. The checklist is in the issue.
- **Issue #691 — benchmark + tune** (after #690 passes). Sweep the configs, measure the latency
  breakdown on real hardware, run real-GPU inference in the workers, and characterize scaling.
  The highest-leverage item is filing a **clio-core notify-primitive feature request** (no
  cross-process push is possible without it — see HANDOFF §6).

**How I expect you to work (these are load-bearing):**
- **Verify before you claim.** Run the thing, read the full output, and state results plainly —
  including failures. Do not call something done that you haven't actually run on the cluster.
- **Suspect your own config/usage before blaming clio-core.** clio-core#561 ("the daemon
  wedges") turned out to be *our* config + *our* transport, not a clio-core bug. When something
  wedges, run clio-core in isolation with a clean client first.
- **One YAML is the source of truth** (config file > env > default). Drive deployment + tests
  from `CLIO_CLUSTER_CONFIG`, not scattered env vars, for reproducibility.
- Update `HANDOFF.md` as you learn (move items from UNPROVEN to PROVEN; record the Delta-specific
  gotchas + the production config you land on). Commit + push as you go; report honestly.

The proven single-box artifacts (run them locally first to confirm your environment, with
`CLIO_RUN_CROSS_PROCESS=1` / `CLIO_RUN_LIVE=1`): `tests/test_distributed/test_isolated_cross_process.py`
and `tests/test_distributed/test_cluster_deploy.py`.

Branch: `feat/distributable-expert-runtime` · Issues: #690, #691 · Handoff:
`docs/distributed/HANDOFF.md`.
