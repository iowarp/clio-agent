# Stress findings — the #1 blocker for a distributed run

The soak harness (`tests/test_distributed/test_stress_soak.py`) immediately surfaced the
critical limit for sustained / multi-hour / cluster operation.

## FINDING 1 (CRITICAL): the clio_run daemon wedges after ~700 CTE ops

**Symptom:** under sustained delegation load over a real `clio_run` daemon, the whole
worker pool stops serving — every subsequent delegation 60s-timeouts. Not a clio-agent
hang; the daemon stops responding to all clients.

**Characterization (isolated, repeatable):**
- Pure SEQUENTIAL delegations (no concurrency, no chaos): wedge after **44** with a 10ms
  poll; **71** with a 100ms poll. ~10 CTE ops per delegation → **~700 ops total either
  way.** So it's an op-count limit, only partly poll-rate dependent.
- **Embedded CTE** (`chimaera_init(kClient, True)`, no daemon) does 200 put+delete (600+
  ops) cleanly, scan returns 0 after each delete. So `DelBlob` frees correctly and the
  bug is **daemon-side, not clio-agent and not the delete path.**
- DRAM bdev is "80% of system RAM" — 700 tiny (~200B) blobs is **not** byte capacity.

**Root-cause narrowed (a precise, minimal repro for clio-core):** a battery of isolation
probes over the *same* daemon ruled out everything except cross-process handoff:
- solo raw put/get/delete, **unique** names: **3000 ops clean**
- solo raw, **reused** name: **3000 clean**
- solo raw **+ scan (GetContainedBlobs)** every iter: **3000 clean**
- solo raw **+ 2 worker processes idle-polling** the daemon concurrently: **3000 clean**
- a **tuned daemon config** (queue_depth 1M, 8 threads, max_concurrent_operations 4096,
  WAL 1GB): **no change — still wedges ~50.** So it is NOT a configurable queue/WAL bound.

The ONLY thing that wedges (~44–71) is an **active cross-process delegation**: process A
writes `<rid>.req`; a *different* process B reads it, writes `<rid>.claim` then `<rid>.res`;
A reads `<rid>.res` and deletes all three. So the limit is in the **cross-process blob
handoff** (write-by-one, read/write-by-another, repeated), not op count, op type, scan, or
mere concurrent clients. Minimal repro for Luke: two processes, A puts a unique blob, B
gets it + puts a reply blob, A gets the reply + deletes — loop; expect a wedge ~50.

**Why it matters:** a real workload is millions of ops over hours. At ~700 ops the daemon
must sustain load before *any* multi-hour or cluster run is meaningful. **This is the
single most important thing to fix for distributed deployment**, and it's upstream
(clio-core daemon), not clio-agent.

**Asks for clio-core / things to try:** raise/rotate the WAL/telemetry log; raise/recycle
`queue_depth`; confirm completed-task queue entries are freed; a durable file tier
(`clio.yaml` file bdev) in case DRAM-tier bookkeeping is the limit.

## Mitigations applied (clio-agent side)
- Poll interval 10ms → 100ms in `CEEExpertInvoker` + `run_worker` (≈10× fewer daemon ops
  while idle/waiting; marginal latency cost — negligible for real ALCF experts). Moved the
  wedge 44→71 but did not remove it (it's per-delegation ops, not just polling).
- Further op-reduction per delegation (fewer get/has checks in claim/serve) would push the
  wedge out but cannot substitute for the daemon sustaining load.

## Status of the 6–8h campaign
The soak harness is built and validated (generates mixed load — echo throughput, real
ALCF, bash commands — with chaos kill/respawn and continuous invariant checks). It is
**blocked on FINDING 1**: a sustained run is impossible until the daemon survives more
than ~700 ops. Once the daemon sustains load (config or upstream fix), the harness runs
the full campaign unchanged.
