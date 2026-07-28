# Governance surfaces on one engine: hooks + planning mode

> **STATUS: FINAL** (grounded + adversarially reviewed + owner decisions resolved). Baseline
> `develop @ be24b6ec` (v0.8.1, post-#1031). One combined campaign, four slice-groups
> (P0 engine → P1 planning → P2 hooks), sequenced so P1/P2 ride the P0 uplift. Deletion-first.
> Companion research: `docs/design/hooks-research-2026-07.md`,
> `docs/design/planning-mode-research-2026-07.md`.

## Context

Campaign items #3 (Hooks) and #4 (Planning mode) are **the same feature seen twice.** clio has
**three half-built governance surfaces** — the permission engine, plan mode, and hooks — and each
independently wants *declarative, priority-ordered, tighten-only, annotation-aware,
mode/event-scoped policy on one engine.* Today each expresses that as bespoke hardcoded Python or
dead scaffolding. Both research surveys independently concluded plan-mode ACL and hooks policy
should be **one engine with one priority model**, and that engine is the #1031 `grant_resolver`.

Grounded state (three probes, file:line-verified; confirmed by an adversarial review):
- **Engine** (`grant_resolver`): `resolve()` is **first-match over an unordered append list** — no
  priority bands; `mode` is a literal unused placeholder (`grant_resolver.py:194`); the plan lock
  beats policy only by code-order accident; the `fs_root` projection is **dead**
  (`resolve("fs_root",…)` = zero call sites); no annotation source-of-truth; `GrantRecord.kind` is
  trivially extensible.
- **Plan mode**: MODE is the **same read-only predicate copy-pasted into three modules**
  (`permission_gate.py:499`, `enrichment.py:300`, `proposal_effects.py:187`); no
  repo-tracked-mutation carve-out (can't even run `git status`); `chat` mode is a documented lie
  (no code checks it). PROMPT is **dead-wired** (`session.mode` plumbed to every `forward()` then
  `del`'d at `builders.py:275/1365/1792`; denial message generic). ARTIFACT/EXIT/TODO are zeros.
- **Hooks**: a **dead `/v1/hooks` CRUD stub** (right shape, zero execution) and a **live-but-partial
  in-process registry** (`runtime/hooks.py`: `post_tool`/`on_error` dead, `pre_message` transform
  fictional, **hook-failure reasons dropped** at `permission_gate.py:479-480` — violating "hook
  failure ≠ user rejection" in shipped code, and `pre_tool` fires *before* `is_read_only`, letting a
  hook gate a provably-read-only call). `ToolRuntimeHooks.tool_interceptor` (full synthesize/mutate)
  is fully wired through the dispatch boundary with **no producer**.

So: **build one governance engine and express plan mode and hooks as data on it, deleting the three
bespoke encodings and both old hook systems.** Deletion over forced reuse (owner).

---

## Pillar 0 — Engine uplift (the shared seam)

**Delete inventory.** `resolve()`'s first-match `return`-on-first-hit; the hardcoded 3× plan lock
(re-expressed as data in P1.1); any bash-safety heuristic residue (verify #1032 removed it — do
**not** reintroduce one).

**Design.**
- **P0.1 Priority bands + merge rule.** Add `priority: int` to the policy row and `GrantRecord`.
  `resolve()` evaluates *all* matching rows sorted by priority and applies **most-restrictive-wins**
  (`deny > defer > ask > modify > allow`); `additionalContext` from all matches concatenates; two
  `modify` matches is an **error**, not last-writer-wins. **Migration (the golden-test key):**
  existing rows get **unique descending priorities by insertion index** — a stable sort then
  reproduces today's first-match order exactly, so most-restrictive-merge only ever applies to *new*
  same-band rules and today's policies resolve **identically** (regression-pinned).
- **P0.2 Real `mode` + `event` axes + new kinds.** Thread `mode` through `_scope_matches` as a real
  dimension (rows carry `modes: list[str]`) and add an `event` axis (rows carry `on: list[str]`), so
  plan-mode rules (`modes=[plan]`) and hook rules (`on=[PreToolUse]`) are ordinary grant rows.
  `GrantRecord.kind` gains `plan_acl` and `hook` discriminators (additive; `kind` is unvalidated).
- **P0.3 Annotation source of truth.** Annotate built-in `fs`/`shell` tools with MCP
  `ToolAnnotations` (`readOnlyHint`/`destructiveHint`/`idempotentHint`/`openWorldHint`) at their
  definition sites; `is_read_only` and the ACL matcher consume annotations uniformly for built-ins
  and external MCP, with **fail-safe defaults** (absent → destructive:true, openWorld:true). The
  4-row `read`/`write` catalog becomes a projection of the annotations, not a competing source.
- **P0.4 Merge scope (DECIDED).** Fence to `tool`+`domain`+`plan_acl`+`hook` kinds. The
  `fs_root`/codex write-fence stays its own mechanism (`sandbox_roots.effective_write_roots`); plan
  write carve-outs are `kind="tool"` + `path_pattern` rows at the (live) tool gate. Documented, not
  silently assumed to unify.

**Files:** `grant_resolver.py`, `permission_policies.py`, `permission_gate.py`, `tools/catalog.py`,
`tools/servers/{fs,shell}_server.py`, `types.py`, `routes/permissions.py`.

---

## Pillar 1 — Planning mode (rebuilt on the engine)

### P1.0 Skill privileged-effects substrate (owner-requested foundation)
Skills today return **text** with no side effects (`skill_runtime.py:213-315`). Add a **declared,
runtime-executed privileged-effect** capability to the skill contract — a structured effect the
runtime performs on invocation (never via returned text, so it's injection-safe). Two effects:
- **`enter_mode`** — transition the session mode (plan-entry rides this).
- **`spawn_subagent_with_skill`** — run the skill in a fresh subagent instead of inlining it into
  the current context (parity with Claude Code / Codex skill-as-subagent).

This generalizes cleanly to future variants (`plan_workflow`, `plan_small` — same substrate, more
thinking room). Ties into the skill-semantics and agents-creating-agents campaigns. The `planning`
skill is the first `enter_mode` consumer.

### P1.1 MODE — declarative ACL, not copy-pasted Python
Delete the 3× hardcoded lock; re-express as priority-banded `plan_acl` rows on P0:
```
deny  *                              @40  modes=[plan]     # deny everything
allow *  annotations.readOnly=true   @50  modes=[plan]     # re-allow read-only by capability
allow  ask_user, web_fetch, plan_exit @50 modes=[plan]
deny   <write/edit tools>            @65  modes=[plan]     # classifiable file tools: hard-denied
allow  <write/edit>  path=<plans>/*.md @70 modes=[plan]    # the sole writable path
```
- **Shell in plan mode is governed by the OS write-fence, NOT an args parser** (fixes the
  bash-parser trap; #1032 deleted that heuristic and ⚑#3 forbids it): plan-mode shell runs under
  `PROFILE_SHELL` with `write_roots` = plans-dir + build/cache, repo tree + `.git/` read-only. Then
  `npm test`→`target/` succeeds, `prettier --write` fails `EROFS`, `git commit` fails — the Codex
  "repo-tracked mutation" line enforced by the OS. **Windows:** the fence is a recorded "floor"
  (advisory) not a hard block (`sandbox_roots.py:100-102`); file-edit *tools* are hard-denied on
  **all** platforms (classifiable via annotations), only shell-command writes are floor-on-Windows.
  See §Verification (Linux gate) and §Future (harden Windows shell fence).
- **Plan mode overrides user allow-rules** (priority band, not code-order accident). Subagents
  inherit structurally. `is_read_only` stays the structural first-branch (reads never gated).
- **Delete `chat` mode** (unenforced; duplicates `edit`).

### P1.2 PROMPT — stop `del`-ing the mode
- Stop discarding `session_mode` at `builders.py:275/1365/1792`.
- The plan-mode instructions are the **`planning` skill body** (progressive disclosure); invoking it
  fires the `enter_mode` privileged effect (P1.0). Persistence is the **mode machinery's** job: a
  **periodic attachment** (alternating full/sparse, suppressed if seen within N turns) re-injects the
  contract so it survives compaction without invalidating the KV-cache prefix.
- **Mode-aware denial message**: replace the generic `"denied by permission gate"`
  (`execution.py:1180`) with the matching `plan_acl` row's deny message — states the restriction,
  points at the plan file, says what *is* allowed.

### P1.3 ARTIFACT — the plan file
- `<repo>/.clio/plans/<ts>-<slug>.md` in a VCS repo (committable), `~/.clio/plans/…` otherwise. Sole
  writable path (the @70 carve-out). Created early, edited incrementally; subagents get their own
  file. Human-editable before approval; re-entry evaluates staleness. Adaptive structure
  (simple/standard/complex). **Epistemic ledger section** (`Given / Learned / To look up / To
  derive`). **Recited** into recent context periodically.
- **State, not a new store (RULE 4):** current/previous mode + pending approval live on the
  **session record** (`session.mode` exists) and `session.metadata` (`sessions.json`), following the
  #948 `AgentTask` "no fifth store / #737-forward-compatible" projection pattern
  (`agent_tasks.py:1-8`) — **not** `workflow_state` (which is the pack/blueprint-declared
  structured-merge engine, #646/#648, not a general store). Only the `.md` is a filesystem artifact.

### P1.4 EXIT + approval — durable (turn-ending yield)
- **`plan_exit`**: zero required content params (reads the file); `{summary, recommendedMode,
  riskNotes}`; hard-error if no plan file.
- **N-way approval**: `auto` / `interactive` / **`exit_only`** (leave plan mode, don't execute) /
  **clear-context** modifier; **constraint-lifting message** on approval + plan path + optional
  `write_todos` decomposition.
- **Durable defer**: `plan_exit` is a **turn-ending yield** (structurally like ask-user), so it
  rides the #1031 `deferred_resumes`/loop-inbox fold — suspend with a resumable ticket, approve
  out-of-band, resume as a new turn. (Mid-loop tool-call defer is a *separate* concern — see P2.6.)

### P1.5 TODO tool + entry/exit wiring
- Separate typed **`write_todos`** (`{todos:[{content,status}]}`). **Enforce the separation**
  (Codex): `write_todos` in plan mode errors; `plan_exit` outside plan mode errors. Multiple
  `in_progress` allowed (parallel subagents) but always transition through `in_progress`; whole-list
  replacement; reject parallel writes; reconcile-before-finishing; recited. **State on
  `session.metadata`** (the #948 AgentTask no-fifth-store projection) — not `workflow_state`, not a
  new store.
- **Entry**: `planning` skill (`enter_mode` effect) + user toggle (`PATCH /v1/sessions` / TUI). Save
  previous mode, restore on exit. Transitions **only** via skill-effect/tool/user — never via text.

**Files:** skill contract + `skill_runtime.py` (P1.0), `grant_resolver.py` (rules),
`permission_gate.py`/`enrichment.py`/`artifacts/proposal_effects.py` (delete locks),
`agents/builders.py` (un-`del`), `execution.py` (denial message), `enrichment.py` (attachment), new
`gact/planning.py` (artifact/exit/approval), `routes/sessions.py` (toggle), built-in `planning`
skill, `write_todos` tool.

---

## Pillar 2 — Hooks (delete both, rebuild one on the seam)

**Delete inventory.** `routes/hooks.py` (`/v1/hooks` CRUD) + `app.state.declarative_hooks` (free
delete). `runtime/hooks.py` + config (`hooks.backend/dir/factory`, `CLIO_HOOKS_*`) +
`tests/test_gact/test_hooks.py` — **but its delete is ATOMIC** with porting its four live `fire()`
consumers (`permission_gate.py:478`, `semantic_events.py:774`, `turn.py:356`,
`turn_finalize.py:729`) + the capabilities route fields (`routes/system.py:507-512`,
`types.py:138-139`) to the new dispatcher in ONE slice (RULE 2 baseline). Also delete the dead
references: `agent_blueprints.py:490/494` + `load_hook_descriptors` (727-771),
`SemanticEventSink.add_live_consumer`, the fictional `pre_message` transform comment
(`turn.py:332-334`). **Keep** the substrate: `ToolRuntimeHooks`, `tool_observer` telemetry, the SSE
`EventBus`/`SemanticEventSink` observe surface. **Salvage ideas** (not code): scope-awareness
(workspace/session/blueprint), the timeout guard + structured reason, the exit-code contract.

**Design.**
- **P2.1 Delete the dead + prune references** (the free deletes above; the live-`fire()` port lands
  with P2.2 to keep baseline green).
- **P2.2 Internal middleware interface + subprocess adapter (atomic with the runtime/hooks.py
  port).** One onion interface `wrap(request, next)` over the `ToolRuntimeHooks` seam (call `next`
  0=deny / 1=normal / N=retry). Subprocess adapter = the industry **exit-0/exit-2 wire** (stdin JSON
  envelope with `schema_version`, `hook_id`, session/turn/tool context, `tool_annotations`; stdout
  tagged-union; exit 2 + stderr = deny-with-reason). HTTP + `prompt` (model call) adapters on the
  same interface. Scope-aware discovery (salvaged). Port the four live consumers here.
- **P2.3 Connect `tool_interceptor` + the tool/lifecycle event set.** Wire a hook's tagged-union
  return (`allow`/`deny`/`ask`/`modify`/`synthesize`) into the already-plumbed `tool_interceptor`
  slot → caching / mock / offline-replay / synthesize. Events: `PreToolUse`/`PostToolUse`/
  `PostToolBatch` + `SessionStart`/`SessionEnd` + `SubagentStart`/`SubagentStop` + `Stop` +
  `PreCompact`. Every event ships `schema_version` + `tool_annotations`.
- **P2.4 BeforeModel/AfterModel via a `dspy.LM` wrapper.** *Not* the turn-level
  `streaming.py`/`turn_forward.py` seam (fires once per turn). A custom `dspy.LM` injected via
  `dspy.context(lm=...)` gives **per-request** granularity: `synthesize`→return a canned completion
  (offline replay + caching), model-routing→swap the LM (the model-agnostic-marketplace payoff),
  redact→rewrite the outgoing request.
- **P2.5 Invariants (fix in shipped code).** Stable **`id`** (not positional). **Hook failure ≠
  user rejection** (distinct error type/message/telemetry — stop dropping the reason at the
  `pre_tool` boundary). **Tighten-only** (a hook `allow` never overrides a `deny`; fix the ordering
  so `is_read_only` runs *before* any hook — reads stay ungateable). **Most-restrictive-wins** (rides
  P0.1). **Fail-closed** for deny-capable hooks on infra failure. **Bounded self-loops** (`Stop`-hook
  cap).
- **P2.6 Durable defer — every yield point, nothing punted.** Turn-ending hooks
  (`Stop`/`UserPromptSubmit`) get defer on the #1031 substrate (like plan_exit). **`PreToolUse`
  defer ships too** — it is the most-used hook and its headline use case. It generalizes the
  **existing parked permission gate** (`permission_gate.py` already blocks a tool call mid-step on a
  `threading.Event`): accept the decision from **out-of-band channels** (API / loop-inbox),
  **persist + surface** the pending approval, lift the timeout — the paused call resumes when
  approved from anywhere. **Cross-restart durability** (survive a reboot / release the executor
  thread) is built on the **replay substrate** (P2.3 tool synthesize + P2.4 `dspy.LM` wrapper): a
  deferred loop's checkpoint IS its recorded trajectory, and resume = deterministic replay up to the
  defer point + inject the approved decision — no DSPy-internal surgery; it composes with the
  offline-replay machinery we already build. **Nothing here is postponed to a later spike** — the
  `defer` *capability* is a governance outcome (suspend→approve-out-of-band→resume), distinct from
  "postpone the work"; we build the capability now and postpone none of it.

  **P2.6 implementation status (#1074).** Owner module `gact/hooks/defer.py`; `defer` is a
  first-class merge decision in `wire.py` (rank `deny > defer > ask > synthesize > modify > allow`,
  so *deny beats defer*).
  - **PreToolUse within-session defer — SHIPPED.** `park_pretool_defer` reuses the interactive
    gate's parked-`threading.Event` primitive (`app.state.permissions` + `permission_events`, no new
    store), with the ~600s→deny timeout lifted to a **configurable long bound** (`hooks.defer_timeout`
    / `CLIO_HOOKS_DEFER_TIMEOUT`, default 24h). `permission_gate._make_permission_gate` evaluates a
    policy `deny` **before** parking (deny beats defer). An out-of-band `POST /v1/permissions/{pid}`
    (→ `resolve_permission`) wakes the parked call: `allow` runs the tool (or the `modify`
    (`input`) / `synthesize` (`result`) the approval carries → the `tool_interceptor`); `deny` →
    a typed `DenyDecision`. Fail-safe: no-session and timeout → `deny` with a typed reason
    (`hook_defer_no_session` / `hook_defer_timeout`), never a silent auto-approve. Resume is
    once-only (`resolve_permission` is idempotent + the event fires once). The **thread-occupancy
    tradeoff** is documented on `defer_timeout_s` (a bounded long park, never an infinite pin).
  - **Turn-ending defer (`Stop` / `UserPromptSubmit`) — SHIPPED.** No held thread: `suspend_turn_defer`
    persists a pending approval (kind `turn_defer`) + flips the session to `waiting_user`; the SAME
    `resolve_permission` path calls `resume_turn_defer`, which stages the #1031 deferred-resume
    (loop-inbox fold when busy, else `start_background_user_turn`). UserPromptSubmit: `allow`
    re-drives the original prompt as a new turn (carrying `HOOK_DEFER_RESUME_META` so the hook does
    not re-defer the just-approved prompt — the resume once-gate), `deny` rejects it. Stop: `allow`
    releases (completion accepted), `deny` re-drives one more turn with the feedback. Owner modules
    `gact/hooks/user_prompt.py` (the UserPromptSubmit boundary, extracted from `turn.py`) +
    `gact/hooks/stop_loop.py` (`run_stop_hooks` grows a `deferred` branch).
  - **Cross-restart durability — DURABLE SURFACE shipped; deterministic replay-resume is the flagged
    RESIDUAL (not stubbed-as-working).** Every defer mirrors its pending state onto `session.metadata`
    (`hook_defer_pending`, the #948 no-fifth-store projection) so a restart can *see* what was
    outstanding. What is NOT yet wired: on boot the in-memory `app.state.permissions` ledger starts
    empty, so a persisted pending defer is not yet resolvable/replayable after a restart. The concrete
    remaining work (a follow-up slice, riding the P2.3 tool-synthesize + P2.4 `dspy.LM`-synthesize
    recording): (1) **rehydrate** pending `hook_defer_pending` rows from `session.metadata` into
    `app.state.permissions` at `build_app` startup so `POST /v1/permissions/{pid}` resolves them again;
    (2) for a mid-loop PreToolUse defer whose executor thread is gone, **replay** the recorded
    trajectory deterministically (BeforeModel synthesize + tool synthesize served from the recording)
    up to the defer point, then inject the approved decision and continue live. Turn-ending defers are
    closer to restart-durable already (they resume as a *new* turn, needing only the ledger
    rehydration in (1)), so they are the natural first consumer of the rehydrate step.
- **P2.7 Trust + introspection + audit.** Content-hash fingerprint for repo-shipped hooks (re-prompt
  on change), `allowManagedHooksOnly` admin lockdown, a `/hooks` inspection route (the real
  registry's `metadata()`/`matching_handlers()` data), **audit via the semantic-event highway**
  (RULE 4 / #737 — *not* a new JSONL store), no-TTY.

  **P2.7 implementation status (#1075) — SHIPPED.**
  - **Trust** — `gact/hooks/trust.py`: a sha256 `compute_fingerprint` over each loaded hook's
    declarative config + the resolved command/script bytes, keyed by stable `id`, compared to a
    colocated `hooks.trust.json` (`{id: fingerprint}` — hook config, not a fifth store). `evaluate_trust`
    (called from `build_hook_dispatcher`) tags each entry `trusted`/`untrusted` (TOFU on first sight);
    an `untrusted` (content-changed) hook is dropped from `HookEntry.runs_for`/`matching` — it never runs
    silently — and records the typed `hook_untrusted_content_changed` reason. `allowManagedHooksOnly`
    (`hooks.allow_managed_only` / `CLIO_HOOKS_ALLOW_MANAGED_ONLY`) drops every non-managed source in
    `discover_hook_entries`; scopes are `user < project < managed` with a new `HookEntry.scope` label.
  - **Introspection** — read-only `GET /v1/hooks` in `routes/system.py` off `HookDispatcher.inspect()`
    (id, on-events, match, source scope, trust, enabled/runs) + the bounded recent audit records.
  - **Audit** — `gact/hooks/audit.py`: `HookDispatcher.dispatch` emits exactly one `hook.invoked`
    semantic event per invocation (decision / denial / infra error / pre-exec rejection) on the highway
    (trace-only, added to `SSE_TRACE_ONLY_EVENT_TYPES`), resolving the live app from the keystone-bound
    `context.active_app()` (no new global, no `build_app` wiring line). `SemanticEvent`-event invocations
    are skipped (highway-recursion guard) but still ring-captured. The bounded `_RECENT` ring is the
    authoritative always-on capture; the highway event is its served projection.
  - **Deletions (grep-clean)** — the dead packaged-hook enable subsystem: `run_demo_benchmark.py`'s
    `_enable_blueprint_hook_for_case` / `_probe_packaged_hook_for_case` / `_packaged_hook_invocation_observed`
    + the `marketplace_packaged_hook_blocked_turn` case + `packaged_hook_invocation` proof (it POSTed to a
    never-implemented `/v1/agent-blueprints/{id}/hooks/{hook_id}/enable`); `docs/AGENT_BLUEPRINT_PACKAGED_HOOKS.md`
    + its `docs/README.md` link; stale `hook.pre_message.blocked` doc refs.
  - **Docs** — `docs/HOOKS.md` (wire contract, config schema, trust, events, exit-0/2 adapter, audit).
  - **CONTRACT SWEEP (cross-repo, tracked — do NOT edit the gact-tui submodule here):** the gact-tui
    `contract/SPEC.md` `x_clio_hook_backend` enum needs `declarative` added to match the backend name this
    build reports (see the note on `_BACKEND_NAME` in `dispatcher.py`).

**Files:** delete `routes/hooks.py`, `runtime/hooks.py`, `tests/test_gact/test_hooks.py`; new
`gact/hooks/` (dispatcher, adapters, wire, trust); `execution.py` (`tool_interceptor` producer);
provider/dspy layer (`dspy.LM` wrapper for BeforeModel); `runtime/app_state.py` (wiring);
`routes/` (`/hooks` inspection); `semantic_events.py` (audit events).

---

## Decisions (resolved with owner)

- ONE combined campaign, P0 → P1 → P2, live-gated per slice-group. ✔
- Hooks: **delete BOTH** systems; rebuild one on the `ToolRuntimeHooks` seam + subprocess/HTTP/
  in-process/prompt adapters; salvage ideas, not code. ✔ (Q1a)
- Durable defer: YES for **all** yield points — plan_exit + Stop/UserPromptSubmit on the #1031
  substrate, and **PreToolUse** via the generalized parked-gate; **cross-restart** durability rides
  the replay substrate (P2.3/P2.4). Nothing punted to a spike (owner: we defer nothing). ✔ (Q2)
- Synthesize: EVERYTHING now — tool-layer `tool_interceptor` **and** BeforeModel/AfterModel via a
  `dspy.LM` wrapper (per-request synthesize + model routing). ✔ (Q3)
- Plan entry: **skill privileged-effects substrate** (`enter_mode` + `spawn_subagent_with_skill`);
  the `planning` skill is the first `enter_mode` consumer; user toggle too. ✔ (Q4/Q1-followup)
- Plan-mode shell: **OS write-fence, not a parser**; hard on Linux, recorded-floor on Windows;
  file-edit tools hard-denied on all platforms. ✔ (Q2-followup)
- Engine merge fenced to tool+domain+plan_acl+hook; `fs_root` fence stays separate. Delete `chat`
  mode. Epistemic ledger in the plan artifact. No fifth store (state → session record +
  `session.metadata`, the #948 AgentTask pattern — NOT `workflow_state`; audit → semantic
  highway). ✔
- **P4 goal eval: LLM-judge only; loop bounds are the hard stops** (A4 #1057, review-findings
  fix). The initial two-tier design (LLM first-pass + deterministic `StatePredicate`/file hard
  gate over `workflow_state`) was DELETED. The cross-industry survey settled it: nobody ships a
  predicate over model-authored state as the halt authority, and clio's `workflow_state` is
  written by the acting model — so the predicate let it *mark its own homework* (`goal.py:251`
  self-satisfy). The bounded LLM judge is now the sole completion decision; `goal_status` returns
  armed state only (never runs the judge); a judge-met goal stops any armed loop via the
  `turn_finalize` glue (`loop_goal_met`). Accepted residual: the judge is transcript-persuadable,
  bounded by user-only arming + the caps that hold regardless. ✔ (A4)

## Verification (live gate per slice-group, real box + real CTE + claude/codex)

- **P0:** two overlapping rows resolve by priority (not list order); most-restrictive holds; two
  `modify` = error; `is_read_only` early-returns in every mode; annotation fail-safe defaults;
  **golden test — today's policies resolve identically after insertion-index migration.**
- **P1:** research cases E1–E20 / A1–A11 / X1–X12 (writes denied incl. under `bypass` + a matching
  allow-rule; plan-file `.md` carve-out incl. traversal/wrong-extension; MCP `readOnlyHint` allowed,
  no-annotation denied; `plan_exit` no-file hard-errors; `write_todos` in plan mode errors).
  *Live:* enter plan mode **via the `planning` skill effect**, verify the plan by running the test
  suite *inside plan mode*, `plan_exit → auto` then first action is execution; `plan_exit → defer`
  approved out-of-band resumes. Behavioral evals B1–B12 (LLM-judged).
  **Linux gate** for the shell-fence cases (E6 `npm test` allowed / E7 `prettier --write` denied /
  E9 `git commit` denied) — hard-provable only where the fence is hard.
- **P2:** contract C1–C10, decisions D1–D9 (allow-can't-override-deny; two-modify=error;
  synthesize→PostToolUse `synthetic:true`; defer→resumable), matching M1–M6, coverage L1–L6,
  resilience R1–R10, security S1–S7. *Live fixtures:* format-on-save, secret-scanner (survives
  injection L6), test-gate (`Stop`, bounded), **offline replay** (BeforeModel + tool synthesize
  replay a full recorded session, zero API/network, deterministic), durable approval (turn-ending
  hook `defer` → approved later → resumes), audit-completeness (every call/denial/error once on the
  semantic highway, incl. pre-execution rejections).
- **Composed capstone:** a synthetic multi-job/multi-workspace pipeline exercising all three — a
  `planning`-skill session plans a cross-workspace transform, a `PreToolUse` hook denies-with-
  synthesize a marketplace call (served from cache), a mid-run "allow this host" grant applies live
  (P0 priority row), and a `plan_exit → defer` is approved out-of-band — one pipeline.
- **Smoke (every merge):** `pytest -m "not integration"`, `ruff check src/`, baseline `cli.py`.

## Awaiting an external dependency (the ONLY postponed item)
- **Harden the Windows shell fence from floor → hard.** Today file-writes are hard-denied on Windows;
  shell-command writes are a recorded floor. A better OS sandbox is in the works but still in **alpha**
  — the day we swap to it, plan-mode shell gets the correct **hard** semantics on Windows with **no
  design change** (the ACL/fence contract is already right; only the enforcer improves). This is the
  one thing we wait on, because it is externally blocked — not a choice to postpone.

## In scope this campaign (substrate-enabled — NOT deferred)
Everything the P1.0 skill-effects substrate + the replay substrate unlock is built now, folded into
the pillars above and P1.6:
- **P1.6 Planning extensions:** skill-effect variants `plan_workflow` / `plan_small` (thin `enter_mode`
  consumers giving more/less thinking room); **operator playbooks** (a policy/skill-supplied plan
  skeleton injected into planning, per-step tools_allowed — CUGA); **save-and-reuse** (generalize an
  approved+executed plan into a replayable artifact, composing with provenance #966); **stall-
  triggered replanning** with hysteresis (Magentic-One leaky bucket) monitoring post-approval
  execution and optionally re-entering plan mode.

## Exit bar
Plan-mode enforcement is engine data, not prose · one hook system, industry-wire-compatible,
tighten-only, reasons reach the model · synthesize + model routing live at both layers · durable
defer proven for turn-ending yields · the three bespoke encodings and both old hook systems
**deleted**, not left behind.
