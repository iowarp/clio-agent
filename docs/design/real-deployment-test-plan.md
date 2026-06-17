# Real-deployment test plan (cross-process / daemon / ALCF — no local GPU)

8-agent design pass (2026-06-16): **52 test designs, 32 suspected production failures.**
The coverage audit was blunt: *everything passes in a single event loop; "concurrent" hid
single-process throughout.* The only real cross-process artifact is `examples/clio_to_clio/`
(one hand-run happy path). This plan makes cross-**process** testing first-class.

**Principle:** a test that shares an embedded runtime or a dir within one interpreter is NOT
a deployment test. Real = separate OS processes + a `clio_run` daemon + real ALCF models.
The lesson: verify cross-PROCESS, not cross-coroutine.

## STATUS (built — `tests/test_distributed/`, gated `CLIO_RUN_CROSS_PROCESS=1`)

A real cross-process harness (session `clio_run` daemon fixture + worker-subprocess spawner +
configurable worker entrypoint + `cross_process` marker) and **11 cross-process tests**:
happy-path; worker `kill -9` → graceful timeout; worker crash → live-worker reclaim;
**lease** (2 workers, 1 request → exactly 1 execution); 12- and 30-delegation throughput across
3/5 worker processes; 200KB large payload; real-ALCF expert on a worker; **heterogeneous**
Sophia+Metis; **NDP-pipeline multi-hop** (geospatial→data, context crossing via a needle).
**5 real bugs found + fixed** that single-process tests could not surface: no-daemon hang,
success-path blob leak, orphan-`.claim` race, slow-worker double-execution (TTL lease +
heartbeat), and the silent-LocalFS-fallback on explicit attach. All committed + pushed.

**Deferred (need their own effort / clio-core side):** daemon-death resilience (→ clio-core
fault-tolerance: replication/erasure-coding, in progress upstream); a *separate-process* gact
worker for the full settle loop cross-process (the cee hinge is wired in-process — see Tier 4);
a live NDP-catalog worker (clio-kit MCP + network — both confirmed reachable); CEE-blackboard +
ARC segments cross-process; true exactly-once (sub-ms simultaneous claim needs a clio-core CAS).

**NIGHT 2026-06-16 follow-up (daemon-free, NOT blocked by the wedge):** found+fixed a **6th
real bug** — `BackgroundTasks` left a task *cancelled before its first event-loop step* stuck
QUEUED forever (the monitor/wait_for engine; a 500-task stress test hung ~31 min). Added the
cee hinge (Tier 4) + two daemon-free LIVE-ALCF suites that exercise the semantics without the
cross-process wedge: **async fan-out** (3 concurrent ALCF children, monitored) and an **NDP 3-hop
pipeline** (geo → 2 concurrent data-discovery → analysis, reference-code needle proves context
crosses every hop). The fixture now pins a deterministic daemon storage config (was ambient
DRAM-only). **The wedge is re-confirmed config-independent:** a file-backed tier + 1GB WAL +
256 concurrent ops (config-load proven in the daemon log) still wedges ~32 — a clio-core code
bug, not storage/WAL. See `docs/design/stress-findings.md`.

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
- [x] Wire the gact delegation hinge to a real cross-process worker — **DONE end-to-end** (overnight
  2026-06-16→17). In-process first (`run_child_via_boundary(mode="cee")`, `CLIO_EXPERT_INVOKER=cee`,
  validated LIVE on ALCF), then the **separate-process worker**:
    1. ✅ Extracted the settle-loop child-runner closure into module-level
       `run_child_expert(app, agent_def, prompt, *, session_id, cancel_requested, await_work)`
       (`app.py`); the settle loop delegates to it, behavior-preserving (full gact suite green bar the
       pre-existing `variant_impact` env fail). Commit `3066596`.
    2. ✅ `runtime/cee_worker.py`: `build_app` + `_resolve_dynamic_agent(expert_id)` →
       `run_child_expert` → `expert_result_from_prediction` → publish; `python -m
       clio_agent.runtime.cee_worker` drains a role queue. Commit `f923185`.
    3. ✅ Proven LIVE on ALCF: a worker SUBPROCESS reconstructs + runs a real registered child over a
       shared LocalFS store while the parent runs NO worker (only the subprocess could answer).
       Commit `884bb49`. No daemon → not wedge-blocked.
  REMAINING (smaller): the same over the real `clio_run` daemon (CTE transport) with the `io_depth:256`
  stopgap (clio-core#561); and carrying routing/`expert_handoffs` back from the worker (the worker maps
  answer+routing via `expert_result_from_prediction`, but the *parent settle loop* consuming a
  worker-produced result through `mode="cee"` end-to-end is the in-process path today).
- [x] **`CLIO_EXPERT_INVOKER=loopback` on real ALCF** through the boundary — survives the lossy
  prediction (answer + routing preserved; trajectory/tools stay in-process until #659 carries
  them). Live-proven (`test_delegation_invoker_live.py`), plus a `cee`-mode live twin.

## Tier 5 — scale + adversarial
- [ ] Throughput: 10–50 concurrent delegations across M worker processes / one daemon; no lost
  messages, no cross-talk, clean drain, realistic latency.
- [ ] Large context/answer payloads through CTE (base64 bloat / shared-memory limits).
- [ ] Poison blob in the shared store doesn't hang/kill a worker; ordering/causality.

## Test infrastructure to build
A `cross_process` pytest marker + a daemon fixture (`clio_run start`/`stop`, LD_LIBRARY_PATH,
free-port pick) + a worker-subprocess helper, so Tier 1–5 are automatable and gated. Live legs
use `CLIO_RUN_LIVE=1` + ALCF; the daemon itself is CPU/shared-memory (no local GPU).
