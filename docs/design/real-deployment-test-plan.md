# Real-deployment test plan (cross-process / daemon / ALCF — no local GPU)

8-agent design pass (2026-06-16): **52 test designs, 32 suspected production failures.**
The coverage audit was blunt: *everything passes in a single event loop; "concurrent" hid
single-process throughout.* The only real cross-process artifact is `examples/clio_to_clio/`
(one hand-run happy path). This plan makes cross-**process** testing first-class.

**Principle:** a test that shares an embedded runtime or a dir within one interpreter is NOT
a deployment test. Real = separate OS processes + a `clio_run` daemon + real ALCF models.
The lesson: verify cross-PROCESS, not cross-coroutine.

## Tier 0 — confirmed bugs (fix as found)
- [x] **`CLIO_CTE_WITH_RUNTIME=0` with no daemon HANGS** ~30s in `chimaera_init` then proceeds
  broken; `make_arc_store`'s fallback can't catch a hang. FIXED: `CTEStore._require_daemon_
  reachable()` fail-fast (TCP probe) + `make_arc_store` no longer silently LocalFS-falls-back
  when attach was explicitly requested. (commit pending)

## Tier 1 — make cross-process REAL + the crash failures (highest bug-likelihood)
- [ ] **Automated cross-process pytest** (gated `cross_process`): a fixture starts the
  `clio_run` daemon (subprocess, LD_LIBRARY_PATH) + a worker subprocess; the test (parent)
  delegates via `CEEExpertInvoker` and asserts the result came from the worker PID. Promote
  `examples/clio_to_clio/` from hand-run to CI-gated. **The headline gap.**
- [ ] **Worker `kill -9` mid-delegation** → parent times out gracefully (✓ invoker), AND the
  orphaned in-flight request is reclaimed by another worker OR surfaced as failed — today it
  is neither (no lease). **Expected real bug.**
- [ ] **Daemon death while clients attached** → clients fail cleanly, not hang/corrupt. Untested.
- [ ] **Provider-routing contract** (design decision): does the parent dictate the child's
  model, or does the worker host the expert + its own declared model? `ExpertRequest` carries
  no provider/model today. Decide + test the worker honors the EXPERT's model (not ambient env).

## Tier 2 — multi-worker coordination + resilience
- [ ] **Claim race at PROCESS level**: two real worker processes double-execute one request
  (at-least-once). Characterize; add a **lease** (TTL/heartbeat) — exactly-once likely needs a
  clio-core CAS/lease primitive (#659). Side-effecting handlers duplicate without it.
- [ ] Worker restart + request re-discovery from the shared store.
- [ ] Slow/stuck worker: timeout visibility to the parent; doesn't starve other pending requests.
- [ ] Orphan `.req`/`.claim` blob cleanup when a parent crashes/times out (mailbox leak).

## Tier 3 — real cross-process scenarios (exploit two ALCF providers)
- [ ] **Heterogeneous workers**: worker1=`argonne_sophia`, worker2=`argonne_metis`, parent routes
  to each — per-expert model across PROCESSES (not two sequential calls in one process).
- [ ] **Multi-hop A→B→C** delegation, all separate processes over the daemon; no deadlock when C
  reads A-scoped context via CEE.
- [ ] **CEE blackboard cross-process**: clio A `context_publish`, clio B discovers via BM25 +
  `context_get` — "one produces, another finds it" across real processes.
- [ ] **ARC segments cross-process**: a worker expert's ReAct segments visible to the parent
  after delegation through the shared clio-core.

## Tier 4 — real gact integration
- [ ] Wire `CEEExpertInvoker` into `_execute_delegated_experts` so a REAL gact session delegates
  to a child in a separate worker process (full settle loop, real ALCF). Today gact only uses
  in-process/loopback invokers.
- [ ] **Full-app `CLIO_EXPERT_INVOKER=loopback` on real ALCF** through the real settle loop —
  does it survive the lossy prediction (no trajectory/tools)? Still undone.

## Tier 5 — scale + adversarial
- [ ] Throughput: 10–50 concurrent delegations across M worker processes / one daemon; no lost
  messages, no cross-talk, clean drain, realistic latency.
- [ ] Large context/answer payloads through CTE (base64 bloat / shared-memory limits).
- [ ] Poison blob in the shared store doesn't hang/kill a worker; ordering/causality.

## Test infrastructure to build
A `cross_process` pytest marker + a daemon fixture (`clio_run start`/`stop`, LD_LIBRARY_PATH,
free-port pick) + a worker-subprocess helper, so Tier 1–5 are automatable and gated. Live legs
use `CLIO_RUN_LIVE=1` + ALCF; the daemon itself is CPU/shared-memory (no local GPU).
