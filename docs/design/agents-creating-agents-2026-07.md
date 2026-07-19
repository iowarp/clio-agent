# Agents Creating Agents (2026-07)

Campaign close-out record. The parent issue is
[#948](https://github.com/iowarp/clio-agent/issues/948) (full design + owner-locked
decisions + the campaign-done gate list); part of epic
[#667](https://github.com/iowarp/clio-agent/issues/667); prepares the #671
federation seam. This document is the **landed** ledger — what each slice actually
shipped, what it deleted, its review depth, and where the live evidence sits — plus
the honest deviations from #948's original text and the typed open follow-ups.

For the design rationale, do not re-read it here — read #948. For how the shipped
runtime is USED, read `docs/DSPY_BLUEPRINT_EXPERT_RUNTIME.md`. The GACT-surface
contract changes are in `CHANGELOG.md`.

## 0. What shipped, in one paragraph

Tier-1 mains are now **react agents whose `answer` IS the user deliverable**; they
route by CALLING spawn-runtime tools over **real child turns in real child
sessions** (durable `AgentTask` records projected on the child session's metadata —
no fifth store). Children spawn **synchronously and asynchronously**, mid-run,
returning to a **live parent** (no re-invoke, no synthesized resume prompt); the
model decides spawn-vs-wait-vs-observe and collects results three ways (wait / check
/ next-turn notification injection). Deterministic `a→b→c` pathways are an explicitly
**declared** `workflow:` block run by a typed-predicate runner. `dspy.BestOfN` /
`dspy.Refine` are **declarable module variants**. The entire settle/synthesis
orchestration layer this replaces — the settle loop, parent re-invoke,
`final_responder` synthesis children, nanoagent post-hoc materialization, the
`next_expert` routing vocabulary, and the legacy `ClioAgent` planner — is **deleted,
not preserved**, guarded baseline-0 in CI. The #671 executor seam
(`ExpertInvoker`) is in place, serializable in/out, parity-tested against the direct
substrate.

## 1. The landed table

Execution discipline per slice (the #930 memory-campaign cadence): tests →
adversarial subagent review → PR → live canary gate. "Review" below is the confirmed
finding count from the slice's adversarial workflow. Live gates ran on the accepted
substrate (real clio-core CTE daemon + claude_code/haiku|sonnet; canary-blueprint
technique — invented per-child facts prove which children actually contributed).
Gate artifacts are worktree-local (`out/…`, deliberately untracked — see the
`chore: untrack gate artifacts` commits); the durable evidence is each PR's posted
gate report and the #948 status comments.

| Slice | PR(s) | What landed | What was deleted | Review | Live-gate evidence |
|---|---|---|---|---|---|
| **S1** turn-runner ownership + within-session busy gate (#949, closes #662) | #981 | `gact/turn_runner.py`: `TurnRunner` owns turn-task lifetime (master strong-ref set → no GC-cancel; app-loop anchoring; `busy(sid)`; typed shutdown drain). Within-session busy gate shared across **all 5** spawn-engine producers (POST/retry/scheduler/ask-user-resume/MCP-app). `/v1/health` orphan-scan deferred (cold ~10s→~0.6s). | — (foundation) | 4 adversarial rounds; busy-gate coverage, drain/scheduler race, coarse-cron drop, silent resume-drop, shutdown idle-hook side-effect all found+fixed | Foundation slice — no model-facing surface yet; `test_turn_runner_s1.py` (14) + `test_health_orphan_scan_deferred.py` (3). Live gate deferred to S3+ (first spawnable surface) |
| **S2** AgentTask registry + `agent.task.*` events + task API (#950) | #982 | `gact/agent_tasks.py`: frozen `AgentTask` record + validated status lifecycle + typed reason catalogs + `AgentTaskRegistry` (one `threading.Event`/task = the S6 wait primitive) **rebuilt at boot** by folding `session_type=="agent_task"` sessions. `routes/agent_tasks.py`. `agent.task.*` on both parent+child channels. Store = child-session metadata — **no fifth store**. | — | Terminal-record immutability, malformed-row-tolerant boot fold, persist-refuses-divergence, operational-bus-events (ARC-absent safe) — found+fixed | Real-boot task projection rebuilt from `sessions.json`; `agent.task.cancelled` observed on both SSE channels over HTTP. 14 tests |
| **S3** child-turn substrate: `spawn_child_turn` (#951) | #983 | `gact/turn_spawn.py`: `spawn_child_turn(app, TaskSpec) → AgentTask` mints a child session + stages a **real turn** via the S1 runner; `spawn_child_turn_threadsafe` for the parent's forward thread. Dedicated child-turn `ThreadPoolExecutor` (a waiting parent can't starve children). Guards: declared-only, computed-depth runaway backstop (`depth > MAX_SPAWN_DEPTH` refused — MAX shipped at 3 here, **raised to 8 in S4** as a runaway backstop, NOT a 3-tier rule), FIFO queue admission at the cap. `TaskSpec` serializable — the **#671 seam** minted. | — | 2 majors: cancel-cascade freed slots but never admitted queued tasks; executor never shut down — both fixed | Real claude_code child turn completed with result `message_ref` + real answer + full child transcript + parent-visible `queued→started→completed`. 7 tests |
| **S4** THE PIVOT: main-as-react + the deletion inventory + pack migrations (#952) | #984 (+ marketplace repin) | `gact/agents/spawn_runtime.py`: react mains route by spawning real children; re-emits `blueprint.delegation.{started,completed,failed,parent_resumed}` + `blueprint.fanout.started` + the `expert_handoff` Parts (verbatim child output, #880). Orchestrator react iteration budget scales with declared children; child sessions inherit the parent's session-scoped blueprint activation; disabled blueprint root fails typed. `answer` REQUIRED again. | **−7,100 lines net**: `turn_delegation.py`, `turn_delegation_arc.py` (settle loop, parent re-invoke resume prompts, walk helpers), `turn_terminal.py` (`final_responder` adoption) + `agents/migration_signals.py` + `final_responder`/`answer_stream_visible`, `turn_nanoagents.py` **and** the orphaned `runtime/nanoagent.py`, `next_expert`/`next_task` fields + composition vocabulary, inline `delegate_to_<child>`/fan-out tools, the optional-answer default | Spawn-substrate review + render-parity + deletion-closure passes (verbatim output, per-depth pools, computed depth, empty-answer typed failure, orphan purge) | End-to-end react mains on **all three packs** (earthscope-gnss-region, wildfire-smoke-impact-review, data-semantics — #948 comment); wire-fixture parity for delegation events; `check_no_settle_vocabulary.py` grep-zero green in CI |
| **S4b** legacy `ClioAgent` planner deletion (#948, in-campaign) | #987, #988 | `turn_forward` else-branch → typed `no_resolvable_agent` (+ `recovery_actions`). `agent.py` **2798→723** (planner half deleted; `ClioAgent` is the runtime HOST — providers, tool gateway, ARC, fleet). `CLIO_AGENT_ENABLE_LEGACY_NATIVE_EXPERTS` + `CLIO_AGENT_MAX_STEPS` retired. Test harness (~34 driver files) migrated onto the ONE blueprint branch via `host_agent_executor`. `gact/agent_blueprint_refresh.py`: stale default-registry installs **self-heal** (typed, swap-safe). | legacy planner loop + JSON/prompt/capability/chat-tool-loop/routing/persistence stack; the two env flags | 13-agent workflow: **8 confirmed** + 2 refuted — all fixed. Notable major: default-blueprint suppression fired on mere pack *discoverability*; narrowed to explicit-activation-only, sabotage-proven both directions | Self-heal proven live on this box (`old_commit=6b90f036 → new_commit=5ec9bf55`), then a default session answered by the react main, `end_turn`. #988: CI settle-wait 10s→30s + loud on timeout |
| **S5** fan-out + BestOfN/Refine variants + declared workflows (#953) | #989 (+ marketplace #41/#42) | Same-child **ensembles**: durable `run_index` (per parent turn+child, additive across events/Parts/tool returns) + request-order `workflow_state` merge with typed `workflow_state_merge_conflict` rows (+ `agent_id`). `gact/agents/module_variants.py`: real `dspy.BestOfN`/`dspy.Refine` at the module dispatch (`{kind,variant,n,threshold,reward}`; source-backed generated reward def; `variant_selection` → assistant metadata). `gact/workflows.py`: declared `workflow:` runner over typed `when_state`/`when_child_completed` predicates with per-step `AgentTask` records + typed stalls + `run_workflow` tool. `fanout.max_workers` enforced. Per-try ARC isolation via the `react_run` keying discriminator. | — (S4 handoff `fanout.max_workers` closed; wildfire's inert `continuation_contracts` re-declared as `workflow:`) | 18-agent workflow: **12 confirmed** (3 majors) + 2 refuted — all fixed, sabotage-verified | G1 concurrent fan-out (both canaries in main's answer, overlapping windows); G2 same-child 3× (run_index 0/1/2) + real BestOfN (`variant_selection` stamped); G3 declared a→b→c in order + typed-stall probe (honest model report). Real CTE + claude_code/sonnet. Live-gate fix: leaf children's typed `workflow_state` reaches the task record (every kind, `ebd454ac`) |
| **S6** async spawn/wait/observe + observe-later injection (#954) | #990 | Every spawn is **fire-and-forget** (handle returns immediately; parent-turn end never cancels children; session cancel cascades). `wait_agent_tasks` **requires `timeout_s`** (breaking tool-arg change). `check_agent_tasks` returns finished results. Completed-but-unconsumed tasks inject a bounded, sanitized, server-composed **notification into the parent's NEXT turn** with exactly-once consumption (atomic under-lock claim, durable `consumed_at`, `agent.task.consumed`, commit-seam timing so a vetoed turn re-injects). Boot-folded non-terminal tasks settle typed (`server_restart_interrupted`). Thread-topology stress test. Failed children inject identically — clio never branches on content. | — | 15-agent workflow: **11 confirmed** (4 majors) + 1 refuted — all fixed, sabotage-proven | G1 spawn→unrelated-work→wait interleave proven in transcript ordering; G2 (flagship) finish-then-observe — next turn's answer sourced from the injected notification, **zero tool calls**, consumed exactly once, wire balanced; G3 typed timeout→honest continuation→late collection. Real CTE + claude_code/sonnet. Evidence `out/s6-gate` |
| **S7** budget gate w/ children + #671 invoker seam + close-out (#955) | this branch (`spawn/s7-budget-invoker-closeout`) | `gact/agents/invoker.py`: `ExpertInvoker` Protocol + `InProcessExpertInvoker` (delegates to the exact substrate primitives — the seam IS the substrate, no second pathway); serializable `TaskHandle`/`TaskResult`/`TaskEvent`; `RELAY_STATE_MAP` (1:1 to clio-relay's job-record vocabulary). `arc/rpc_liveness.py`: per-RPC progress-based stall ladder + reconnecting backoff + typed `clio_core_rpc_stalled` degrade + gate quarantine (wraps every `ClioCoreStore` RPC). Budget acceptance load extended with a **background-children** scenario. | — | Invoker **15-test parity suite** (records/events/typed-errors/queue-cancel/run_index-notify identical to direct substrate). Liveness: **35 tests, sabotage-proven** (14 per-RPC stall-guard in `test_rpc_liveness.py` + 21 daemon-gate/quarantine in `test_clio_core_liveness.py`; reconnect/raise/quarantine/rpc-probe-recovery deletions all bite) | Budget gate **PASSED** live (real CTE on 9413, claude_code/haiku): cold **max** peak **1.01 GB** / settled final **0.73 GB** (peak = cold max, final = median; 3 recorded runs + 1 gate-assert), recorded in `scripts/mcp_mem_budget.json` `children` block; `workspace_fleet_reaped reason=idle_ttl` every run; **zero untyped degrades**; `--assert-budget` exits 0 |

## 2. Deviations from #948's original text

Honesty is the point of this section — where the campaign diverged from the issue's
plan, and why.

1. **S4b was not in the original 7-slice plan.** #948 listed S1–S7 with S4 as the
   pivot. Deleting the settle layer surfaced a second legacy Tier-1 pathway — the
   `ClioAgent.forward` planner reached only through `turn_forward`'s else branch
   (blueprint sessions never touched it; the default-blueprint fallback covered
   pack-less sessions). It was carved out as **S4b** rather than bolted onto S4, so
   the pivot slice stayed reviewable. `agent.py` went 2798→723; `ClioAgent` is now
   the runtime HOST only.

2. **The `settle_` grep-zero criterion is scoped to `settle_dynamic`.** #948 asked
   for baseline-0 on `settle_`. A bare `settle_` token collides with the unrelated
   **#756 finalize error envelope** (`settle_failed_finalize` / `settle_turn_transcript`),
   which is a different, KEPT subsystem. `scripts/check_no_settle_vocabulary.py`
   therefore bans `settle_dynamic` (plus `next_expert`/`next_task`/`final_responder`/
   `_dynamic_parent_resume_prompt`/`delegate_to_`/`fanout_to_children`/
   `max_sync_delegation_rounds`/`answer_stream_visible`/`migration_signals`), not
   bare `settle_`. Documented in the guard's own docstring.

3. **The agent-task API path moved.** S2 originally registered
   `/v1/sessions/{sid}/tasks` + `/v1/tasks/{tid}`, which **shadowed the #18
   per-session manual task CRUD** at those same paths by registration order. Fixed
   in S4: the agent-task routes are `/v1/sessions/{sid}/agent-tasks` +
   `/v1/agent-tasks/{tid}`; the `/tasks` paths stay the #18 manual CRUD.

4. **The ARC/CTE liveness ladder is an in-campaign addition.** #948 did not scope
   ARC-runtime hardening. The S4 live gate hit a **zombie CTE daemon** that froze the
   event loop; the interim fix was a typed `arc_unavailable` client timeout, and S7
   formalized it into the owner-locked **progress-based** design in
   `arc/rpc_liveness.py` (never absolute wall-clock bounds — a call that answers on a
   later attempt succeeds; only whole-RPC no-response counts as a stall; N
   reconnecting retries with growing backoff; then the typed degrade + quarantine).

5. **The loop-offload was attempted and reverted.** The liveness design's point 4
   (offloading the on-loop semantic-event persist / authoring `aforward`) was
   attempted and **reverted** after it empirically hung the turn suite under the
   `TestClient` portal (`run_in_executor` awaits never resume). `turn.py` /
   `turn_forward.py` are untouched; the on-loop persist is now **bounded by the
   ladder** (a freeze-forever cannot recur). The clean fix — a dedicated ARC writer
   thread + `aforward` on the ReActV2 subclass — is a typed open follow-up (§3).
   Note this is consistent with #948 design decision 8, which already put `aforward`
   and ARC-fold injection out of v1 scope.

6. **S4 recorded builders.py file-size ratchet increases.** The CI ratchet was
   already **red on the `develop` base** from #947 (MCP Apps host) exceeding
   baselines. S4's child-scaled iteration budget grew `builders.py`, recorded as
   `+29` then `+1` (mypy optional-narrow). These are ratchet **debt** carried, not
   ratcheted down — the #947 baselines are expected to be deleted/redrawn by a
   separate stream, not this campaign. Flagged so it is not mistaken for a silent
   regrow.

7. **S7 folded in an unrelated wart fix.** The every-turn `PROMPT-CTX provider
   summary serialize failed` log line was root-caused during the invoker slice:
   `app.state.lm_config` is a plain dict fed to `dataclasses.asdict`. Now serialized
   by shape (Mapping direct / dataclass via `asdict` / typed-repr fallback). Not part
   of #948, fixed opportunistically.

## 3. Open follow-ups (typed, honest)

These are **deferred by design**, not forgotten. None is a silent gap; each is
either explicitly out of #948's v1 scope or surfaced as a deviation above.

- **#671 seam caller migration.** `InProcessExpertInvoker` exists, is
  parity-proven, and has **no callers yet** — the model-facing spawn tools still call
  the substrate primitives directly (they layer parent-side wire choreography that
  stays local under federation). Callers migrate to route the substrate calls through
  an `ExpertInvoker` when the federation campaign swaps `InProcess` for a detached
  executor. Seam-first by intent (#948 decision 7).
- **Dedicated ARC-writer thread + `aforward`.** The clean resolution of the
  reverted loop-offload (deviation §2.5). The on-loop persist is bounded today; a
  dedicated writer thread would remove it from the loop entirely without the portal
  hang. Blocked on authoring `aforward` on the ReActV2 subclass (#948 decision 8
  scoped it out of v1).
- **`PENDING_TASK_NOTIFICATION_MARKER` split-registration.** The observe-later
  injection carries the clio-owned marker (`gact/enrichment.py`, the #881 marker
  discipline). When the presentation-model split machinery lands on **this** lineage,
  register the marker in its `_SERVER_APPENDED_CONTEXT_MARKERS` set so the split keeps
  it out of the user-text lane. That set does not exist on this branch yet.
- **`artifact_ref` spill.** `AgentTask` / `TaskResult` carry a **RESERVED**
  `artifact_ref` (empty today). The #670 artifacts campaign fills it with a child
  large-output spill ref; carried from day one so a federation record matches
  clio-relay's durable `ArtifactRef` vocabulary.
- **ARC-fold observation injection.** Explicitly **NOT v1** (#948 decision 8):
  folding a completed child's result directly into a live parent's ARC context needs
  loop-pause machinery and would violate the sole-author append-only invariant. The
  next-turn notification injection is the shipped observe-later channel.

Verified **DONE**, not open (claims checked against the code):

- Leaf / `chain_of_thought` structured `workflow_state` reaches the task record —
  fixed in S5 (`ebd454ac`, every kind through one finalize seam).
- `fanout.max_workers` enforcement — landed in S5.
- The `PROMPT-CTX provider summary serialize failed` wart — fixed in S7 (§2.7).

## 4. Where the runtime lives now (owner modules)

New (no accretion):
`gact/turn_runner.py` · `gact/agent_tasks.py` · `gact/turn_spawn.py` ·
`gact/workflows.py` · `gact/routes/agent_tasks.py` · `gact/agents/spawn_runtime.py`
· `gact/agents/module_variants.py` · `gact/agents/invoker.py` ·
`gact/agent_blueprint_refresh.py` · `arc/rpc_liveness.py`.

Deleted: `gact/turn_delegation.py` · `gact/turn_delegation_arc.py` ·
`gact/turn_terminal.py` · `gact/turn_nanoagents.py` · `gact/agents/migration_signals.py`
· `runtime/nanoagent.py`, and the `ClioAgent` planner half of `agent.py`.

Guard: `scripts/check_no_settle_vocabulary.py` (baseline-0, CI-blocking).
