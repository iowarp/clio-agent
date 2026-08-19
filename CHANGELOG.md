# Changelog

All notable changes to clio-agent's GACT-contract surface are
documented in this file. Internal changes that don't affect the
TUI/HTTP surface aren't tracked here.

## Unreleased

## [0.10.0a1] — 2026-08-19

The first pre-release of the v0.10 program (MCP v2, federation, and the
Session v3 UI — `docs/design/v010-program-2026-08.md`, program umbrella
#1096). Per that program's own versioning rule, phases P0-P2 (protocol
upgrade, MCP v2 compliance, and relay federation) tag as `0.10.0-alphaN`
off `develop` as they gate; the full `v0.10.0` cuts only after P5, once
the paired gact-tui Session v3 UI lands and the program's composed gate
is green. This build does **not** merge to `main`. Alongside the program
work, this build also carries document artifacts + human review, the
spotter-ai approval mode, live model-catalog discovery, and a session-scoped
async-processes surface.

### Added — MCP protocol v2 (P0-P1, #1096)

- **Protocol upgrade to the 2026-07-28 line** (#1107). fastmcp 4.0.0b1 /
  mcp 2.0.0 replace the prior 2025-11-25 pin; a published compatibility
  matrix confirms both the new line and legacy (pre-2026-07-28) MCP
  servers round-trip. A `server/discover` handshake floor stamps
  per-request `_meta` capability flags and `clientInfo` on every call
  through one client factory (#1111).
- **Elicitation bridged into the HITL question pipeline** (#1113). A
  server-initiated elicitation (form or URL mode) now mints the same
  `UserQuestion` a native ask does — one answer surface, no parallel
  store — and parks the in-flight tool call on an async-safe future
  until the user answers, declines, or cancels.
- **Bounded multi-round tool-input retry (MRTR)** (#1114). A tool call
  that comes back `InputRequiredResult` is retried with the user's
  supplied inputs for a bounded number of rounds instead of failing
  outright, with a typed exhaustion reason if the rounds run out.
- **Durable, reconnectable tasks extension client** (SEP-2663, #1115).
  Long-running MCP tool calls get a durable, deduplicated task record
  that survives a reconnect (poll/resume by task id) instead of being
  tied to one connection's lifetime.
- **Foreground call cancellation** (#1116) and **`prompts/get` support**
  (#1117) round out the client surface.
- **MCP client auth** (#1118): custom headers on runtime-configured
  server specs, plus OAuth, for non-loopback and third-party MCP servers.
- **Typed protocol refusals + an extended degrade catalog** (#1112) so a
  version mismatch or unsupported result shape surfaces as a specific
  reason instead of a generic tool failure.
- Our own fs/shell/gateway MCP servers speak the new line, verified by a
  standing conformance suite pinned to the 2026-07-28 spec.

### Added — relay federation (P2, #1096)

- **One spawn surface, local or remote** (#1127). `spawn_agent_task` /
  `spawn_agents_parallel` take a `placement` parameter (`local` or
  `relay:<cluster>`, with a session-policy fallback) instead of separate
  local/remote tools; a `RelayExpertInvoker` (#1126) runs a spawned
  child on a remote cluster through the identical grammar (same
  `expert_handoff`/run-handle part shapes, same typed errors) a local
  child uses, verified by a parity suite parametrized over both
  implementations. A runs registry/API projects both uniformly.
- **`message-an-agent`, unified across the boundary** (#1128): steer,
  queue, and wake a spawned agent — local or remote — through one tool.
- **Remote MCP federation tools** (#1129): a remote application's tools
  are called as durable `mcp_call` jobs behind a handle-first surface;
  an oversized result fails with a typed reason instead of being
  silently truncated.
- **Curated JARVIS/Spack durable-job surface** (#1130): submit and query
  HPC application runs (e.g. ParaView, LAMMPS) over the relay transport;
  `jarvis_run` is handle-first (submit only), and `jarvis_get_execution`
  gives one unified lifecycle/progress/artifacts/services view.
- **Background-exit injection + live execution views** (#1131): a
  detached remote job's completion is injected back into the session
  exactly once, with a live progress view streamed over SSE.
- **`relay_fetch_artifact`**: transfers one bounded remote execution
  output into the local workspace as a first-class artifact (the
  execution-output artifact path on the clio-agent side).
- Relay's own state machine (`RELAY_STATE_MAP`) and schema shapes are
  shared via the new `clio-schemas` package (#1120, #1121), with
  cross-repo schema-equality CI guarding drift.

### Added — model catalog & provider governance

- **Live model-catalog discovery** (#1211): `codex` and `claude_code`
  provider catalogs are now discovered from the live CLI/account instead
  of hardcoded, with a `POST /v1/providers/models/refresh` overlay and
  the built-in `update-models` skill as the model-facing interface. A
  failed probe never clears a provider's prior discovered list.
- **Typed model-rejection classification** (#1184): a model the account
  no longer serves now raises a typed rejection on both the `codex` and
  `claude_code` bridges instead of being misclassified as a transient
  error and retried 2-3 times for nothing.
- `claude_code` defaults to Sonnet under the standing cost policy.

### Added — session surfaces

- **Session-scoped async-processes surface** (#1205): `GET
  /v1/sessions/{sid}/async-processes` unions spawned agent tasks with
  durable MCP task records (relay/JARVIS jobs included) into one tray,
  with live SSE updates and explicit retention/dismiss semantics.
- **Document artifacts + human review**: document production and
  rendition, plus routes for a human to review a generated document
  before it's finalized.
- **Spotter-ai approval mode**: a fifth `session.approval_mode` (grants
  no auto-approval of its own — behaves like `ask`) that arms a standing
  push-wake watcher child bound to its own Agent Blueprint; a generic
  `action_card` part and a `raise_alert_card` tool let an agent surface
  something that needs attention without a bespoke UI part per use case.
- Blueprint marketplace packs install by default (all packs, deduped
  discovery, durable uninstalls) instead of requiring manual selection.

### Changed

- **`wait_for_terminal` is honored as a real commitment** (#1224,
  #1225). The relay federation wrapper previously forwarded
  `wait_for_terminal` but always returned the same queued handle,
  forcing a second `relay_wait` call and roughly doubling agent tool
  calls; it now resolves through the durable task record, and the MCP
  executor no longer silently truncates an undeclared-budget wait at a
  30s per-tool default.
- **Unlimited turn budget is the default** (#1224, #1226). The ReAct
  loop's per-turn iteration cap no longer defaults to a deterministic
  formula that could starve a long-running orchestrator mid-task; a cap
  now survives only as an explicit, blueprint-declared opt-in. The
  provider-level session turn budget (`max_turns`) is likewise unlimited
  by default, bounding the SDK session rather than one query.
- **Relay's tool-surface catalog refreshes on a TTL** (#1224, #1227,
  default 300s) instead of being discovered once at boot and cached
  forever, so a tool that appears on the door later becomes usable
  without a restart.
- **A named-but-unprojected relay tool degrades per-tool, not per-agent**
  (#1224, #1228): if an agent's tool ACL names a relay tool the door
  advertises but clio hadn't projected (e.g. `relay_artifact_lineage`,
  `relay_status`), that one tool now degrades with a typed reason
  instead of bricking the whole agent build.
- **Provider chain-of-thought reaches the wire again**: a CLI default
  change had silently dropped every `claude_code` thinking delta: the
  SDK now requests summarized display explicitly, Sonnet gets the same
  conservative thinking floor Haiku already had, and a redacted delta is
  now a typed `provider_thinking_redacted` reason instead of a silent drop.

### Fixed

- **Ambient-relay turn poison** (#1229): a stale repo-root `.env` and a
  library's import-time dotenv load could leak relay endpoint
  environment variables into an unrelated turn, dialing a half-dead door
  and stalling for roughly 15 seconds. Fixed at the source (retired
  `.env`), at the discovery seam (never runs an ambient round trip
  inside a turn), and by test-fixture isolation.
- `/v1/sessions` no longer 500s on sessions written by an older server
  version.
- Server errors (5xx) now reach the browser as a real error instead of
  an opaque "Failed to fetch".
- A session no longer silently inherits a blueprint or expert-pack
  default it never activated.
- `POST /messages` with a missing `parts[]` returns a typed 400 instead
  of an internal error.
- A namespace-dispatch defect that mis-reported real backend tool
  failures as "unknown tool" is fixed.
- MCP tool-result plumbing: list/bool results reach the UI as JSON
  instead of a Python repr, typed MCP content blocks and tool titles are
  preserved end to end, and turn-end artifact rollups land every mint in
  a turn's tree on the parent message.

## [0.9.0] — 2026-07-29

### Governance surfaces campaign (#1057)

Rebuilds three model-governance surfaces — planning mode, hooks, and
loop/goal/cron autonomy — as ONE declarative, priority-ordered, tighten-only
policy layer on the #1031 `grant_resolver`, plus the P0 permission-engine uplift
they all ride. Deletion-first: the hardcoded plan lock, both old hook systems,
and the `chat` session mode are removed, not extended. Live-gated end-to-end:
all 17 required gates pass on a real self-provisioned CTE (private port, zero
untyped degrade reasons) — plan write-deny, plan-exit approve with no phantom
re-approval, a PreToolUse hook firing on a live tool call, reserved-metadata
400, cron firing a scheduled turn, a loop tripping a sticky bound, and an
LLM-judge goal met + cleared.

### Added

- **New user commands `/loop`, `/goal`, `/cron` (+ `/schedule` alias)** (#1079-#1082).
  Model-callable tools land alongside: `loop_wakeup` (self-paced cross-turn
  iteration), read-only `goal_status`, `cron_create`/`cron_list`/`cron_delete`,
  and `write_todos` (execution-phase checklist). All auto-attached to react
  experts; `create_artifact` and `plan_exit` join the same auto-tool set.
- **Autonomous loop** (#1079). `loop_wakeup(delay_seconds, prompt, reason, stop)`
  arms at most one pending wakeup (delay clamped [60,3600], typed reason) with
  first-class typed bounds — `loop_max_iters` (default 100) and a
  `max_wallclock`/`max_tokens`/`max_usd` budget measured as delta since loop
  start. The first tripped bound ends the loop with a structured reason;
  end/cancel/delete cancels the pending wakeup (store row + daemon entry).
- **Goal conditions** (#1080). A goal is a built-in Stop-hook condition
  (`/goal` or a declared `set_goal` skill-effect only — never a model-callable
  set_goal, RULE 1) evaluated at turn-finalize by a bounded LLM judge and
  re-driven on the loop-inbox seam until met (`goal_met`) or a bound trips
  (`goal_max_iters`/`goal_budget`). The model gets only read-only `goal_status`.
- **Cron scheduler improvements** (#1081). Local-timezone/DST-correct `next_fire`
  (per-schedule IANA tz, system-local default), `run_at`/`delay_s` one-shots,
  exponential-backoff retry, anti-runaway clamps (min-interval, max-lifetime,
  max_fires/until — all typed), explicit overlap policy, and the
  `cron_create`/`list`/`delete` tool triad with cancel-both (store row + daemon
  deferred entry).
- **Planning mode, engine-backed** (#1062-#1068). Plan/architect read-only
  enforcement is now declarative `plan_acl` rows resolved through the one
  `grant_resolver`, structurally unbypassable by any user policy store. Adds: a
  clio-owned per-session plan-file lifecycle with a create-vs-edit reminder that
  survives compaction; the sole writable path is `<repo>/.clio/plans/*.md`; a
  `plan_exit` tool with an N-way approval flow (auto / interactive / exit-only /
  reject-with-feedback, decisionless = reject-safe); a built-in `planning` skill
  entry (declares `enter_mode:plan`); `plan_workflow`/`plan_small` variants that
  shape the reminder; skill-supplied operator playbooks with per-step
  `tools_allowed` allowlists (tighten-only through the resolver); save-and-reuse
  of an approved plan as a provenance-tracked, replayable playbook; and
  stall-triggered replanning suggestions injected next turn.
- **Declarative hooks — one new system** (#1069-#1075). A single subprocess
  hook system driven by `.clio/hooks.json` (user/project/managed scope, required
  stable id) on the industry exit-0 (allow/parse stdout) / exit-2 (deny + stderr
  to model) wire. Events: `PreToolUse`, `PostToolUse`, `PostToolBatch`,
  `UserPromptSubmit`, `Stop` (bounded block-to-re-drive, capped), `BeforeModel`/
  `AfterModel` (per-request LM wrapper: synthesize/route/modify), `SessionStart`/
  `End`, `SubagentStart`/`Stop`, `PreCompact`, `SemanticEvent`. Decisions rank
  most-restrictive-wins (deny > defer > ask > synthesize > modify > allow),
  reads are never gated, and a hook allow can never override a permission deny.
  Read-only `GET /v1/hooks` introspects every loaded hook (id/events/match/
  source/trust/enabled + recent audit); sha256 trust fingerprinting drops a hook
  whose config/script bytes changed (`hook_untrusted_content_changed`); `defer`
  is a durable first-class decision at every yield point including `PreToolUse`.
  Audit rides the semantic highway (`hook.invoked`, trace-only); a `hooks.*`
  config family replaces the old `CLIO_HOOKS_*`/`hooks.backend` keys.
- **Skill privileged-effects substrate** (#1062, #1082). A skill may declare a
  frontmatter-only, runtime-executed effect (never body text or model prose):
  `enter_mode`, `spawn_subagent_with_skill`, `loop`, `set_goal`, `schedule`,
  `plan_workflow`/`plan_small`. Every effect is bound to the same bounded infra
  and cannot escape a restrictive mode, over-grant, or evade the loop/goal/cron
  bounds; malformed/unknown effects raise typed errors.

### Changed

- **Permissions — engine uplift** (#1059-#1061). `resolve()` no longer returns on
  first match: it collects all matching rows, takes the highest-priority band,
  and breaks a same-band tie most-restrictively (deny > defer > ask >
  allow_workspace > allow_session > allow). Rows gain optional `modes` (session
  mode) and `on` (hook event) scope axes and the `plan_acl`/`hook` kind
  discriminators. Built-in fs/shell tools now declare MCP `ToolAnnotations`
  (readOnly/destructive/openWorld) as the single classification source of truth;
  absent/partial annotations are treated most-restrictively. Migration preserves
  legacy first-match behavior exactly.
- **Permissions — grant kind enforcement (#1057, B4).** A workspace domain grant
  is now resolved strictly on the domain axis: a host-bearing (`host_pattern`)
  policy row is stamped `kind="domain"` and no longer carries a stray
  `tool_name_pattern: "*"`, and the resolver admits a row into a resolve only
  when its kind shares the caller's axis. Previously a persisted domain grant's
  `"*"` tool glob bled into every `kind="tool"` resolve, so a fleet-egress allow
  authorized every tool call in the workspace. **Behavior change:** any
  deployment that relied on that domain→tool kind-bleed to authorize tool calls
  loses it — re-grant the tools explicitly. Persisted rows self-heal to the
  corrected shape on the next load/flush.
- **Reserved client-metadata keys are rejected** (#1057, B2). A client-supplied
  message `metadata` carrying an internal turn-control key (e.g.
  `hook_defer_resume`, plan-exit/stop-defer resume markers) is now rejected with
  a typed `400 reserved_metadata_key` — **breaking for any client that sent one**
  — across every client-writable ingest (`POST /messages`, `.../retry`,
  `/agent-tasks/{id}/steer`). Reject, not strip (no silent coercion).
- **Goal completion is LLM-judge only** (#1057, A4). The deterministic
  workflow_state predicate tier was removed as the halt authority — a predicate
  over model-authored state let the model grade its own homework (RULE 1). The
  bounded LLM judge is the sole completion decision; the typed loop/goal bounds
  remain the hard stops. A goal is armed only by the user (`/goal`) or a declared
  `set_goal` skill-effect.
- **Loop hard bounds are sticky** (#1057, A1). After a loop halts on a hard bound
  (`loop_max_iters`/`loop_budget`/session-ended), the model can no longer
  silently re-arm a fresh loop — `loop_wakeup` raises
  `loop_bound_tripped_rearm_denied`; only a user `/loop` clears it (the model's
  own `stop` is not sticky).

### Fixed

- **Plan-exit phantom re-approval** (#1057, B1, blocker). A resolved plan-exit
  request stored as `{}` read as a live request, so the resumed turn minted a
  second approval question and hijacked the turn. The pause seam now treats `{}`
  as absent.
- **Hook stdout multi-object parse** (#1057, B3, blocker). A JSON-shaped banner
  printed before a hook's real tagged-union output shadowed the decision (a real
  deny read as allow). The parser now scans every decodable object and resolves
  most-restrictively (tighten-only — a smuggled allow can never beat a real deny).
- **`create_artifact` in plan mode** (#1057). The content-write gate consulted the
  resolver without the resolved target path, so the `<plans>/*.md` carve-out
  never won over the plan-mode deny — the model could not write its designated
  plan file and `plan_exit` was unreachable end-to-end. The consult now carries
  the resolved path, making plan mode workable end-to-end.
- **`cron_delete` cross-session leak** (#1057, A5). `cron_delete` discarded the
  active session, so a model could cancel another session's schedule and use the
  return as an enumeration oracle. It now cancels only owned schedules (a
  wrong-owner request is indistinguishable from missing; typed
  `cron_delete_not_owner`). The HTTP delete route keeps its cross-session reach.
- **DST fall-back rapid re-fire** (#1057, A6). `next_fire` could return an instant
  before its reference during a fall-back overlap, rapid-refiring through the
  repeated hour. Every candidate is now floored at the reference instant.
- **Plan-ACL anti-lockout narrowed to `plan_exit`** (#1057, B5). A user's explicit
  deny of a plan-safe tool (`ask_user`/`web_fetch`) was overridden by the
  plan-mode allow-band. Only `plan_exit` now keeps the anti-lockout override so
  the user's denies are honored; the mode-aware denial message is shown only when
  leaving plan mode would actually unblock the call.
- **`PreToolUse` ordering** (#1070). The ported `PreToolUse` consumer now fires
  after the read-only fast-allow, fixing the prior reads-gated ordering bug.

### Removed

- **The `chat` session mode** (#1063). It was documented as no-destructive but no
  code enforced it; `mode="chat"` now **422s**. Default mode stays `edit`.
- **Both legacy hook systems** (#1069, #1070, #1075). The dead `/v1/hooks` CRUD +
  `declarative_hooks` registry, `runtime/hooks.py`, blueprint-packaged
  `load_hook_descriptors`, and the dead packaged-hook enable subsystem are
  deleted, replaced by the one declarative system above.
- **The deterministic goal-predicate tier** (#1057, A4) and the unreachable
  `loop_stalled` no-progress bound (#1057, A3) — see Changed for the goal rationale.

## [0.8.1] — 2026-07-24

### Added — post-#974 three-pillar redesign (#1031)

Unifies the permission model, adds a single async loop-inbox, and completes the
provenance DAG for cross-job reproducibility. All three pillars are live-gated
end-to-end (standalone + a composed multi-job/multi-workspace pipeline). GACT
surface:

- **Permissions (P1)**: one `GrantRecord` (subject × decision × scope × grantor)
  resolved by a single `grant_resolver`; reads are structurally never gated. A new
  `session.approval_mode` axis (`ask` · `auto-edits` · `bypass` · `ai-review`),
  orthogonal to the plan/architect read-only lock, set via
  `PATCH /v1/sessions/{sid}`; the unified `POST /v1/workspaces/{wid}/grants` `kind`
  body (`fs_root`/`domain`/`tool`). FS grants apply live at the loop boundary
  (stop→apply→restart). `ai-review` routes an un-granted write to an in-process
  reviewer that resolves the pending row (recorded `grantor=reviewer`; fail-safe →
  escalate to human). An explicit `deny`/`ask` policy always beats the mode.
- **Loop-inbox (P2)**: a single per-session inbox injects async child
  completions/failures AND mid-turn user messages into the running ReAct loop. A
  second POST while a turn runs is now a **202 steer** (the `409 session_busy`
  busy-path is deleted), surfaced mid-turn; a fire-and-forget child completing
  during the parent's turn injects into its next ReAct step (`loop_inbox.drained`)
  rather than only the next turn. Human-facing live handle:
  `GET /v1/agent-tasks/{id}/live` + `POST /v1/agent-tasks/{id}/steer`.
- **Provenance (P3)**: cross-JOB / cross-workspace lineage binds by global path
  identity (not sha) with revision-on-change; an executed `.py`/`.sh` is designated
  a SCRIPT artifact on use; single-artifact reproduce is transitive with a
  complete-closure export policy. `b = transform(a)` now reproduces across job
  boundaries (`used` `cross_workspace_bind`).

### Added — sandboxing campaign (#974)

OS-level write confinement for every process the agent spawns, a network chokepoint that
records all child egress, grants as first-class recorded events, and the provenance-tier
UPGRADES that make #966's honest floor enforcing. Backend ladder srt → Landlock → none;
srt is the Windows path (one-time `clio sandbox setup`). GACT-contract surface:

- **Events** (all trace-only, never on the SSE UI wire): `sandbox.state` (boot conformance),
  `net.egress` (per-child egress), `artifact.policy_violation` (fence-denied out-of-root
  write), and the `boundary.*` grant family.
- **Permissions**: a new `network_egress` request kind (deny-mode grant-on-first-domain)
  reusing the existing gate + policy store; a `host_pattern` domain vocabulary.
- **Routes**: `POST /v1/workspaces/{wid}/grants` — a recorded root or domain grant (a
  user/model decision, `boundary.granted` with grantor + sticky-policy provenance).
- **Provenance**: `gap → policy_violation` (`prevented`/`detected`); the per-edge
  `lease-window → fence_proven` upgrade on generated edges (fence proved output-territory
  exclusivity by construction; `contended` records stay unproven); egress →
  `used web:<domain>@<time>` ingest edges.
- **Doctor**: the `sandbox` row (fence mechanism + typed reason), the census `confinement`
  column (wrapped vs verifiably-excluded seams), and the `sandbox_conformance` row (the
  zero-untyped-degrade guarantee: a typed mechanism/reason per seam on every tier).
- **CLI**: `clio sandbox setup` (one-time self-elevating UAC on Windows) / `clio sandbox
  status`.

### Added — artifacts campaign (#966)

The first-class artifacts campaign lands `b = transform(a)` provenance and gives
every meaningful session output a durable, hash-pinned, versioned record. GACT
surface (vendor `x_clio_artifacts`, SPEC §6.26):

- **Artifact routes**: `GET /v1/sessions/{sid}/artifacts` (+ `?include_children`),
  `GET /v1/workspaces/{wid}/artifacts`, `GET /v1/workspaces/{wid}/artifacts/{name}`
  (`?ref=latest|vN|<alias>`), `GET /v1/artifacts/{id}`, `GET /v1/artifacts/{id}/bytes`
  (hash-verified), `POST /v1/sessions/{sid}/artifacts/pin`,
  `POST /v1/workspaces/{wid}/artifacts/{name}/aliases`,
  `GET /v1/artifacts/{id}/lineage`, `GET /v1/sessions/{sid}/transforms`,
  `GET /v1/transforms/{activity_id}`.
- **RO-Crate export (S7)**: `GET /v1/artifacts/{id}/export` and
  `GET /v1/sessions/{sid}/export/bundle` return an RO-Crate zip — File entities with
  PROV lineage, TransformRecords serialized as schema.org `CreateAction`s, gap
  versions attributed to an unknown Agent — plus a compiled `reproduce.py` /
  `reproduce.ipynb` that re-runs the lineage with executable per-stage `sha256`
  assertions and honest per-stage verdicts (deterministic / write-bytes /
  re-runnable / agentic-only / gap-break). Exports register their content hashes as
  CAS GC roots.
- **Events**: the `artifact.created` / `artifact.version.added` /
  `artifact.alias.moved` family on the SSE UI wire; `artifact.used` /
  `artifact.transform.recorded` trace-only. `artifact.proposed` keeps its payload.
- **Parts**: a `resource_link` part is emitted per generated artifact at turn
  finalize (a plot/report now has outbound wire identity instead of a path string).

### Changed — path-string mechanisms deleted (S7, #973)

- Answer grounding no longer disk-scans `workflow_state.artifact_paths`; it
  validates/rewrites a final answer's fabricated deliverable-path citations against
  the session's **registered artifacts** (registry-sourced, `include_children`
  reach). The `evidence.py` heuristics and the inert `structured_outputs.artifacts`
  field are deleted; a baseline-0 CI guard
  (`scripts/check_no_artifact_scraper_vocabulary.py`) keeps the retired vocabulary
  out. No wire-shape change (grounding is server-internal answer hygiene).

## [0.8.0] — 2026-07-21

The agents-creating-agents campaign (#948) accumulated its GACT-surface changes
per slice; the sub-headers below are slice-scoped so the whole surface story reads
top to bottom (S7 newest first, back to the S2–S3 substrate).

### Added (S6–S7)
- **The `ExpertInvoker` federation seam is minted** (#948 S7 / #671; internal
  infrastructure, no wire change today). `gact/agents/invoker.py` owns the
  transport-abstracted expert-execution boundary — an `ExpertInvoker` Protocol
  (`invoke`/`wait`/`check`/`cancel`) plus the in-process `InProcessExpertInvoker`
  that delegates to the exact spawn/registry/cancel primitives the spawn-runtime
  tools already use (the seam IS the substrate — no second execution pathway). Its
  serializable shapes (`TaskHandle`, `TaskResult`, `TaskEvent`, and the reused
  `TaskSpec`) are relay-compatible: `RELAY_STATE_MAP` records the 1:1 lossless
  mapping to clio-relay's durable job-record vocabulary so a later federation
  campaign swaps the executor BEHIND the seam, not in front of every caller. The
  model-facing tools keep calling the substrate directly in this slice; they
  migrate onto the invoker when federation lands. `TaskResult` carries a RESERVED
  `artifact_ref` (the #670 artifacts campaign fills it).
- **ARC/CTE runtime liveness is hardened with typed degrades** (#948 S7;
  operator-facing knobs). Every `ClioCoreStore` RPC (put/get/exists/scan/delete/
  clear/search) runs under a **progress-based** stall ladder in
  `arc/rpc_liveness.py` — never absolute wall-clock bounds (a call that answers on a
  later attempt succeeds; only a whole-RPC no-response counts as a stall). A stalled
  peer gets N reconnecting retries with growing backoff, then a typed
  `ClioCoreRuntimeLostError` (`reason=clio_core_rpc_stalled`) + gate quarantine so
  subsequent ops fast-fail instead of freezing the event loop (the S4 live-gate
  zombie-daemon incident). Config-first knobs with documented env overrides and
  fail-safe defaults: `CLIO_ARC_LIVENESS_STALL_AFTER_S` (30.0),
  `CLIO_ARC_LIVENESS_RETRIES` (3), `CLIO_ARC_LIVENESS_BACKOFF_INITIAL_S` (2.0),
  `CLIO_ARC_LIVENESS_BACKOFF_MAX_S` (15.0).
- **The memory budget is release-gated with background children active** (#948 S7 /
  #930 discipline; operator note, no wire change). The acceptance load in
  `scripts/mcp_mem_attribution.py` gained a `--children-pack` scenario: 3 concurrent
  claude-haiku sessions on the real CTE where session 0 fans out 2 background
  children (real child sessions via `spawn_agents_parallel`). Measured cold-max peak
  1.01 GB / settled final 0.73 GB (peak = cold max, final = median; 3 recorded runs
  + 1 gate-assert run), recorded in the
  `children` block of `scripts/mcp_mem_budget.json` per the ratchet contract
  (cold-max peak, median final, never raised to pass) — far under the 1.8/1.3
  campaign targets, with zero untyped degrades. Proven, not assumed: children cost
  bounded transcripts (`resident_ledgers`) + provider turns, and the #933 reaper
  cleans their fleets (`workspace_fleet_reaped reason=idle_ttl` every run).
- **Async spawn / wait / observe-later — the model decides** (#948 S6). Every
  model-driven spawn (`spawn_agent_task` / `spawn_agents_parallel`) is now
  fire-and-forget on ONE honest semantic: the handle returns IMMEDIATELY (`status`
  `queued|running` with the typed `queued_reason` at the concurrency cap) and the
  child turn is UNTIED to the spawning turn's lifetime — a parent turn ending never
  cancels its children (only cancelling the parent SESSION cascades). The model
  collects results three ways, and whichever reaches a finished task first CONSUMES
  it exactly once (`consumed_at` + an `agent.task.consumed` event, durable across a
  boot rebuild like `delegation_reported`): (a) `wait_agent_tasks` blocks on the
  children's completion; (b) `check_agent_tasks(task_ids?)` polls non-blocking and
  now returns finished tasks' bounded result excerpt + `message_ref` (+ reserved
  `artifact_ref`); (c) any completed-but-unconsumed child spawned in a PRIOR turn is
  injected as a bounded, clio-marked grounding block into the parent's NEXT turn
  input (task id, child expert, status, result excerpt, child session id) — the
  model reads it and decides; clio never auto-acts on the content. A FAILED child is
  observed-later and injected IDENTICALLY to a completed one (no branch on child
  output content anywhere in the runtime). The injection is bounded
  (`_MAX_NOTIFY_BLOCKS`); overflow stays pending for the following turn with a typed
  truncation note — never dropped.

### Changed (S6–S7)
- **Provider-summary serialization no longer warns every turn** (#948 S7; log
  noise, no wire change). The `PROMPT-CTX provider summary serialize failed` line
  fired each turn because `app.state.lm_config` is a plain dict fed to
  `dataclasses.asdict`; it is now serialized by shape (a `Mapping` directly, a
  dataclass via `asdict`, with the typed-repr fallback preserved for genuinely bad
  inputs).
- **`wait_agent_tasks` now REQUIRES `timeout_s`** (#948 S6 / #670) — BREAKING tool-arg
  change. A wait without a budget is a hang; the model passes its own budget and, on
  timeout, gets the current statuses and decides whether to keep waiting, keep
  working, or finish. The `spawn_agent_task` handle additionally carries the typed
  `queued_reason`. (The declared-workflow runner's internal step waits keep their own
  explicit per-step budgets.) The injected observe-later block is SERVER-composed
  grounding carrying the clio-owned `PENDING_TASK_NOTIFICATION_MARKER` (the #881
  marker discipline) so a presentation-model split keeps it out of the user-text
  lane; when that split machinery lands on this lineage, register the marker in its
  `_SERVER_APPENDED_CONTEXT_MARKERS` set.

### Added (S5)
- **Declared deterministic workflows** (#948 S5). A tier-1 orchestrator blueprint may
  declare a `workflow:` block — a `steps` list describing an `a -> b -> c` child pathway
  gated on typed `workflow_state` predicates (`when_state.<field>.exists` /
  `when_state.<field>.equals` / `when_child_completed`), reviving the retired
  continuation-contract shape. The runner (`gact/workflows.py`) executes the steps
  DETERMINISTICALLY in declaration order — each step is a real `spawn_child_turn` + wait
  with its own `AgentTask` record, evaluating its gate over the ACCUMULATED typed
  `workflow_state` (the declaration is the decision; the model is not in the loop for the
  declared steps). A gate that cannot be satisfied (missing field, a prior child that
  never completed) or a child that FAILS is a typed STALL — the run stops and returns
  `stalled{reason, step, predicate, observed}`, never a guess or a silent continuation.
  A react main enters the workflow via one `run_workflow` tool (present ONLY when a
  workflow is declared, mirroring the children-gated toolset); the tool returns the full
  run record (per-step task ids/results, accumulated `workflow_state`, terminal
  `completed | stalled`) and the model decides how to proceed from a stall. Invalid
  declarations (unknown child, dependency cycle, malformed predicate, an unproduced
  `when_child_completed`, or a `when_child_completed` produced by a LATER step — an
  acyclic-but-misordered workflow that would stall forever) are typed validation errors on
  the expert row that compose with the react-children hierarchy rules. A step whose child
  exceeds the step budget stalls with a distinct `workflow_step_timeout` reason (never the
  child-failed reason) and the orphaned child is cancelled so it stops holding a slot.
- **`fanout.max_workers` is now enforced** (#948 S5). A parent expert's declared
  `fanout: {enabled, max_workers}` bounds `spawn_agents_parallel`'s batch admission: at
  most `max_workers` of the parent's concurrent children at a depth RUN before the next
  spawn queues with the typed `concurrency_cap` reason (queue admission honors the bound
  too); the global per-depth cap remains the overall bound, and an absent/disabled
  declaration leaves the batch unbounded up to that cap.
- **Declarable `dspy.BestOfN` / `dspy.Refine` module variants** (#948 S5). A blueprint
  may widen its `module` from `{kind}` to `{kind, variant, n, threshold, reward}` — where
  `variant` is `best_of_n` or `refine` and `reward` declares an LM-as-judge signature
  (`instructions` + optional `inputs` + `target`). The inner `predict`/`chain_of_thought`/
  `react` program is wrapped in the REAL engine, whose reward is a generated source-backed
  scorer (an out-of-range/unparseable judge score clamps or degrades to `0.0` with a typed
  `variant.reward.parse_failed` log — never a crash). Invalid declarations (unknown
  variant, `n < 1`, missing/malformed reward or threshold) are typed validation errors
  surfaced on the expert row. The selected try's `winning_index` + `winning_score` (and
  every try's score) are stamped, additive, on the prediction as `variant_selection` — and
  carried across the expert boundary onto the assistant message metadata
  (`metadata.variant_selection`) so the winner is observable in the durable trace; each
  try emits a structured `variant.try` / `variant.reward` log. When EVERY try fails (total
  failure), the wrapper raises ONE typed error carrying the last try's real error + a
  per-try summary — identical for any `n` (previously the engine swallowed it to `None` for
  `n<=2` and raised only for `n>=3`). N in-process tries of one
  module in one session are partitioned per try on the ARC live plane + transcript-tap
  KEYS via a new `react_run` discriminator (folded only into keying, never attribution) so
  try N's model input never accumulates try N-1's trajectory.

### Fixed (S5)
- **The ensemble `run_index` now resets per parent turn** (#948 S5 / #953 [2][8]). The
  model-facing spawn paths (`spawn_agent_task` / `spawn_agents_parallel` and the declared-
  workflow runner) now stamp the active turn id on each spawn, so `run_index` restarts at 0
  each turn (it previously accumulated across the whole session because `parent_turn_id` was
  never populated).
- **The cancel cascade is now transitive** (#948 S5 / #953 [3]). Cancelling a parent turn
  cancels the whole descendant tree (grandchildren and deeper), depth-first and cycle-safe,
  so no nested child turn outlives the ancestor that spawned it.
- **Ensemble merge conflict rows carry `agent_id`** (#948 S5 / #953 [1]). Each `winner` /
  `loser_runs` attribution dict in a `workflow_state_merge_conflict` row now includes the
  child expert id, disambiguating a heterogeneous fan-out's same-`run_index` runs; the
  cross-expert tie-break (stable `run_index`, then wait-list order) is documented.

### Changed (S4–S5)
- **The legacy Tier-1 `ClioAgent` planner pathway is deleted** (#948 S4b). The
  planner loop (`ClioAgent.forward` / `_run_agent_loop` and its action-planner /
  answer-synthesizer / chat-tool-loop / ARC-persistence stack) and the turn
  engine's fall-through dispatch to it are gone; `ClioAgent` is now the runtime
  HOST only (provider identity, MCP tool fleet, ARC keystone, agent registry).
  Blueprint react mains are the only mains. This is an internal-engine change;
  the only wire-facing effect is a new typed `no_resolvable_agent` error (with
  `recovery_actions: [install_default_registry, activate_agent_blueprint]`) that
  a default/main session with **no** resolvable Agent Blueprint now returns —
  replacing the previous silent execution of the legacy planner.
- **The `CLIO_AGENT_ENABLE_LEGACY_NATIVE_EXPERTS` /
  `agents.enable_legacy_native_experts` knob is retired** (#948 S4b). It gated
  the deleted legacy native-expert runtime (routing Agent Blueprint experts to
  the tool/prompt user-agent runners instead of the blueprint runtime); with
  that runtime gone there is no configuration under which a blueprint agent
  routes anywhere else, so the flag no longer exists. Setting it now has no
  effect and it is dropped from `docs/ENVIRONMENT.md` / `.env.example`.
- **Mains are react agents; the settle/synthesis layer is deleted** (#948 S4 /
  #952). A tier-1 main runs the retained ReAct loop and its `answer` IS the
  user deliverable; it routes by CALLING the spawn-runtime tools
  (`spawn_agent_task` / `wait_agent_tasks` / `check_agent_tasks` /
  `spawn_agents_parallel`) over real child turns. The wire-facing
  `blueprint.delegation.{started,completed,failed,parent_resumed}` /
  `blueprint.fanout.started` events are re-emitted by the spawn runtime (event
  types unchanged); `parent_resumed` fires once per terminal child so the TUI
  active-agent indicator re-pins to the parent. The spawn runtime also appends
  the `expert_handoff` Parts the deleted sync-delegate path appended — one
  `delegate.started` header Part per spawn and one terminal return Part per
  child (success AND failure conclude on `stage: delegate.completed` with the
  outcome on `status`, #882) — so the canonical transcript renderer shows the
  delegation header / nesting / return row instead of a bare tool row. The
  completed payload now carries `task_id` / `message_ref` / `error_reason`
  alongside `output` / `workflow_state` / `stage` and no longer carries
  `return_to` / `tools_called` / `structured` (those live on the AgentTask
  record and the `agent.task.*` events).
- **Concurrent same-child ensembles + deterministic request-order merge** (#948
  S5 part 1 / #953). The SAME declared child may now be spawned N times
  concurrently in one parent turn (an ensemble): each run mints its own child
  session + `AgentTask` record and runs as its own concurrent child turn on the
  per-depth pool (up to `CLIO_MAX_CONCURRENT_AGENT_TASKS`). Wire-additive shape
  changes (nothing removed):
  - A new `run_index` integer field (0, 1, 2… in spawn order per
    `(parent_turn, child expert)`, durable on the record) rides the `AgentTask`
    record, the `agent.task.*` event payloads, the `blueprint.delegation.started`
    / `.completed` / `.failed` / `.parent_resumed` payloads, the `expert_handoff`
    started/return Part metadata, and the `spawn_agent_task` /
    `spawn_agents_parallel` tool return. It disambiguates an ensemble's otherwise
    identical child-id rows (the ARC `react_scope` deliberately stays the bare
    agent id per the S5 spike — run identity is a field, never a scope suffix).
  - `wait_agent_tasks` now returns two new keys alongside `results`:
    `merged_workflow_state` (the collected runs' typed `workflow_state` merged in
    REQUEST ORDER = `run_index` order, NOT completion order, so the merge is
    timing-independent) and `workflow_state_conflicts` — a list of typed
    `workflow_state_merge_conflict` rows (`{reason, key, winner:{run_index,
    task_id}, loser_runs:[…]}`), one per top-level key two-or-more runs set to
    different values. No silent last-writer: every collision is surfaced on the
    payload and logged structurally for the model to arbitrate.
- Typed blueprint validation: an expert with declared children must declare
  `module.kind: react` (children are reachable only via the spawn-runtime
  tools); `predict` / `chain_of_thought` remain valid for leaf experts only.
- Blueprint expert `answer` output is REQUIRED again — the optional-answer
  default existed so an orchestrator could defer its deliverable to a
  synthesis child; that pathway is deleted.
- Session agent-overlay export now round-trips the `module:` declaration
  (previously dropped, so an exported react parent re-loaded as `predict` and
  failed validation).
- An empty blueprint/prompt-agent `answer` is now a typed failure (raises into
  the `agent_error` ladder like the tool-agent path) instead of returning a
  silent empty deliverable with a "runtime settlement / declared-child handoff
  repair" rationale — the settle layer that consumed those empty answers is gone.
- The `subagents` capability flag is re-keyed to the spawn substrate: it still
  advertises child-agent support, now provided by real agent-task child sessions
  (`session_type=agent_task`) + `blueprint.delegation.*` events rather than the
  retired nanoagent subsessions / `subagent.*` events.

### Removed (S4)
- The settle/synthesis orchestration internals (#948 S4 / #952): the settle
  loop + parent re-invoke resume prompts (`turn_delegation.py`,
  `turn_delegation_arc.py`), the `final_responder` synthesis-child adoption
  (`turn_terminal.py`) and its degradation reasons, `answer_stream_visible`,
  the nanoagent post-hoc materialization (`turn_nanoagents.py`), the
  `next_expert`/`next_task` typed routing signature fields (internal, never
  wire fields), and the inline per-child delegate / fan-out tools. Marketplace
  packs are migrated (mains → react, synthesis children deleted) and the
  submodule pin updated. A baseline-0 CI guard
  (`scripts/check_no_settle_vocabulary.py`) keeps the vocabulary out.
- Fan-out terminal + nanoagent events retired (#948 S4 / #952, no silent
  retirement): the deleted inline fan-out tool emitted both
  `blueprint.fanout.started` AND `blueprint.fanout.completed`, and the deleted
  nanoagent path emitted `subagent.{started,completed}` (with
  `session_type: nanoagent`). The spawn runtime re-emits only
  `blueprint.fanout.started`; the per-child `blueprint.delegation.*` events are
  now the terminal signal for a fan-out batch (`fanout.completed`/`failed` had
  no wire consumer), and the delegation path uses `agent.task.*` with
  `session_type: agent_task` in place of `subagent.*`. Clients that reloaded the
  session list or raised a notification on `subagent.started` should key on
  `agent.task.started` / `blueprint.delegation.started` instead.
- Deletion closure for the settle removal (#948 S4 / #952): the orphaned
  answer-substitution machinery (`substitute_answer_from_delegation_evidence`,
  `_fallback_answer_from_delegation`, the `answer_substituted_from_delegation_evidence`
  turn-degradation reason and its now-unused per-session ledger), the stream-only
  parent-resume duplicate suppressor (its `parent.resumed` Part producer died with
  the settle loop), the dead sync-delegate prompt/state helpers
  (`_delegated_expert_prompt`, `_delegated_expert_public_prompt`,
  `_should_execute_delegated_handoff`, `_delegated_expert_agent_id`,
  `_failed_child_delegation_workflow_state`, `_append_session_workflow_state_context`,
  `_delegate_started_row`, `_public_task_from_composed_prompt`, and their app.py
  re-exports), and the orphaned Tier-3 nanoagent spawn primitive
  (`runtime/nanoagent.py`). The finalize-time stream-provenance assembler
  (formerly `turn_degradation.assemble_stream_and_degradation_metadata`) is
  retained as `turn_stream.assemble_stream_metadata`.

### Added (S2–S3 substrate)
- Child-turn substrate (#948 S3 / #951): `spawn_child_turn` spawns a declared child
  expert as a REAL turn in a REAL child session (projected as an `AgentTask`), on a
  dedicated executor pool (never the default), with depth/declared-child guards,
  FIFO queue admission at the concurrency cap, a completion hook that records the
  child's result, HITL-in-child typed failure, and a parent→children cancel cascade.
  The `#671` federation seam (`TaskSpec` serializable in/out).
- Agent-task API + event family (#948 S2 / #950): `GET /v1/sessions/{sid}/agent-tasks`,
  `GET /v1/agent-tasks/{task_id}`, `POST /v1/agent-tasks/{task_id}/cancel`, and the
  `agent.task.{queued,started,completed,failed,cancelled,consumed}` events
  (published on both the parent and child session channels). The `AgentTask`
  record projects over a child session's metadata (`session_type=="agent_task"`)
  — no new store — and its registry is rebuilt at boot from `sessions.json`.
  The paths are `agent-tasks`: `/v1/sessions/{sid}/tasks` + `/v1/tasks/{tid}`
  remain the #18 per-session manual task CRUD; S2's original same-path claim
  shadowed that GET by registration order (fixed in S4).

## [0.7.12] — 2026-07-17

### Fixed
- **Diagnosable desktop boot failures** (gact-tui#317, pinned via gact-tui
  v0.9.8). A bundled-MSI boot could fail with "sidecar did not report ready
  within 30s" and leave *zero* backend output in `backend-boot.log`, making
  the cause impossible to see. Now the sidecar launcher logs which backend it
  resolved before exec and forces `PYTHONUNBUFFERED` so the backend's boot
  transcript is captured in real time, and the supervisor records *why* the
  probe gave up (launcher exited early vs. never bound) instead of silently
  killing.

### Added
- **Inline, copyable boot log on the failure card** (gact-tui#317): a new
  `read_logs` desktop command surfaces the boot-log transcript inline with a
  Copy button, so a failed boot can be captured without leaving the app (the
  OS "Open logs" reveal stays as a secondary action).

### Changed
- The desktop splash renders the CLIO brand's real logo image instead of the
  placeholder "C" glyph (gact-tui#317). Desktop app 0.7.1 → 0.7.2.

## [0.7.4] — 2026-07-15

### Added
- Skill semantics with progressive disclosure (#916): experts get a tier-1
  skill *metadata* block plus a model-invoked `load_skill` tool (full bodies
  load on demand, never eagerly); `skill.loaded` provenance events stream on
  the wire, and turn provenance carries `resolved_skills` /
  `skill_resolution` so clients can render declared-skill status
  (gact-tui#315 is the client half).

### Removed
- **Legacy-path deletion** (#775 no-accretion; the strangler flags
  became the only semantics): the classic `_RetainingReAct` expert loop
  (`CLIO_REACTV2`), the parallel working-set write (`CLIO_ARC_WORKING_SET_FOLD`),
  the legacy transcript regime + per-session pin (`CLIO_TRANSCRIPT_PROJECTION`),
  the stateful-delta kill-switches (`CLIO_CLAUDE_CODE_STATEFUL_DELTA`,
  `CLIO_CODEX_STATEFUL_DELTA`), the claude_code `exec` batch transport
  (one `claude -p` per call), the codex `exec`/`sdk` batch transports, and
  the codex app-server kill-switch (`CLIO_CODEX_APP_SERVER`) with its
  `app_server_kill_switch` downgrade reason. All six env knobs are gone
  from `docs/ENVIRONMENT.md` / `.env.example`.

### Changed
- `PUT /v1/providers/lm`: `transport` accepts only `sdk` (claude_code) /
  `app_server` (codex); a deleted transport value returns a typed 400
  (`removed in the v0.8.0 cleanup`), never a silent downgrade. The route
  no longer leaked the codex transport default into non-codex binds, and
  the codex catalog preset's registry marker moved `codex://exec` →
  `codex://app-server` (the stale marker silently steered provider swaps
  onto the deleted batch path).
- `turn.completed` durable payloads never embed `final_message` on an
  ARC-backed server (atoms are the only transcript regime); the embed
  survives only in the no-ARC structural case.
- Typed-output repair for react experts now re-drives a forced `submit`
  over the retained History (the V2 repair) from the builders repair
  ladder; the classic extract-only re-run is gone.
- MCP fleets are memory-bounded (#930, #942; server-internal, no wire
  change, noted for operators): namespaces spawn on first tool call, boot
  reads tool definitions from a launcher-anchored cache (warm boots spawn
  zero server processes; cold boots list one namespace at a time), idle
  workspace fleets are reclaimed (TTL + LRU, typed reap reasons), stable
  clio-kit launchers respawn as direct venv interpreters, and the
  3-session acceptance load is budget-gated at release time
  (3.57 GB → 1.42 GB cold peak / 0.72 GB settled).

## [0.7.3] — 2026-07-14

### Fixed
- Portable-runtime prune, final pass: `python*-config` symlinks removed
  (the macOS leg died on a dangling `python3-config` after its target was
  pruned — and the GNU-only `-xtype` sweep silently no-opped on BSD find,
  our own silent-fallback class; the sweep is now portable), and
  distlib/setuptools Windows launcher stubs are pruned from site-packages
  (a 180KB `t64-arm.exe` inside the packed runtime masqueraded as the
  bundled installer and failed the 60MB payload floor on the linux legs).
  Linux validation in-tree before tagging: relocated boot green, zero
  dangling links, zero stray exes.

## [0.7.2] — 2026-07-14

### Fixed
- Portable-runtime prune left dangling `bin/` symlinks (e.g. `2to3` after
  its `2to3-3.12` target was deleted) that failed Tauri's resource walk on
  the unix bundled legs; the prune now removes non-interpreter symlinks
  and sweeps any dangling link. Validated end-to-end on Linux (WSL):
  relocated boot + `/v1/capabilities` green before tagging.

## [0.7.1] — 2026-07-14

### Fixed
- Release engineering: v0.7.0's tag pinned gact-tui at v0.9.6 (a rejected
  tag-fetch silently aborted the &&-chained pin checkout), so its bundled
  desktop legs failed loudly on the retired `clio-runtime` resources glob
  — the guard doing its job. v0.7.1 pins gact-tui **v0.9.7** (verified
  gitlink) and re-ships the identical payload; PyPI 0.7.0 artifacts were
  unaffected (wheel/sdist carry no submodule content).

## [0.7.0] — 2026-07-13

The second resource-usage campaign release (#893): the unified-ARC
highway ships as the default, the desktop bundle works on a fresh
computer end-to-end, and clio-agent's memory is hard-bounded. Pairs
with gact-tui **v0.9.7** and marketplace **v0.5.4**.

### Changed
- **Hard memory budget** (#906, release-gating): clio-core's storage
  runs a disk-only desktop topology — ONE pre-created RAM arena bounded
  at the user's memory budget (`arc.cte.ram_capacity` /
  `CLIO_ARC_CTE_RAM_CAPACITY`, default 1GB) as working memory, and ONE
  file tier at the user-designated dir (`arc.cte.dir`) holding all data.
  "Use 1GB of RAM, and whatever you want of disk." clio-core's own `0g`
  default (up to 80% of system DRAM — an HPC compute-node default) can
  no longer be inherited silently: boot-time typed warnings
  (`clio_core_ram_uncapped`, `clio_core_tier_topology`) and doctor rows
  flag unbounded arenas, legacy two-arena shapes, and undersized final
  layers. Live gate evidence: daemon peak 574MB under the 1GB bound
  through three full EarthScope sessions.
- clio-core blob writes ride a bounded, typed-loud retry
  (`clio_core_put_retry`, 3 attempts): transient PutBlob refusals (an
  eviction race, a post-restart container-restore gap, disk exhaustion —
  all caught live on the #893 gate) no longer turn one failed write into
  a failed turn under the atoms regime's must-succeed ingest.
- The unified-ARC highway regimes are the shipped defaults (#737, #893):
  the working-set fold (`CLIO_ARC_WORKING_SET_FOLD`) and the transcript
  projection (`CLIO_TRANSCRIPT_PROJECTION`) default ON — new sessions run
  single-copy on the canonical `_events` log (byte-equality proven per
  surface; reload==live green on the full real-session corpus). `=0` opts
  back into the legacy regime; existing sessions keep their pinned regime
  and their wire stays byte-identical.
- Stateful session-delta transports default ON for `claude_code` (SDK
  transport) and `codex` (app-server): `CLIO_CLAUDE_CODE_STATEFUL_DELTA` /
  `CLIO_CODEX_STATEFUL_DELTA` `=0` opt out. Live acceptance: delta TTFT
  1.79s vs 7+s cold (claude), 2.95s vs 7.37s (codex), 76.7% cached-input;
  every fallback-to-full-send is a typed reset reason.
- Boot-time environment conformance (#906): clio-core backend init inspects
  the EFFECTIVE `cte.yaml` and emits a typed `clio_core_ram_uncapped`
  warning when the ram cap is unbounded/unparseable/missing (the stale-0g
  incident shape); the doctor ram-cap row also reports the ram bdev
  capacity.

### Fixed
- The bundled desktop runtime is machine-portable (#909): the embedded
  clio-agent now ships its own interpreter (uv's standalone CPython copied
  into the runtime, wheel installed directly — no venv) and self-describes
  via a generic `runtime.json` manifest, invoked as `python -m
  clio_agent.gact`. The previous venv-based runtime hard-pointed
  `pyvenv.cfg home` at the CI runner's Python and could never start on a
  user's machine. `install/build-gact-runtime.{sh,ps1}` build it (moved
  here from gact-tui — the brand owns its runtime) and prove portability
  by booting a relocated copy; the bundle workflow builds the runtime from
  the released checkout and gates on the manifest being present.
- Desktop bundles ship their sidecar launcher again (#907): the CLIO branding
  overlay now declares `bundle.externalBin`, and the bundle workflow asserts
  the merged Tauri config + built launcher before packaging. Every desktop
  installer since v0.5.18 (first post-de-clio gact-tui pin) was missing the
  launcher and failed at boot with "sidecar launcher missing". Pairs with the
  gact-tui lookup fix (iowarp/gact-tui#309) — the installed sidecar is
  triple-stripped by the Tauri bundler. Known residual: the Windows-on-ARM
  leg publishes a raw `--no-bundle` exe with no launcher beside it and
  remains unfixed (noted in #907).

## [0.6.1] — 2026-07-13

### Fixed
- Release container images actually publish: the Docker workflow now runs on
  `v*` tags (its push-to-ghcr step was tag-gated but the workflow had no tag
  trigger, so release images were silently never pushed). v0.6.0's images
  ship under this tag.

## [0.6.0] — 2026-07-13

The resource-usage campaign release. Pairs with gact-tui **v0.9.6**
(pinned submodule) and marketplace **v0.5.4**.

### Added
- `PUT /v1/providers/lm` accepts `thinking_level` (`off|low|medium|high`;
  out-of-vocabulary values are a structured 422). `GET`/`PUT` responses
  report `thinking_level` and the resolved `thinking_effective`
  (including the typed `unsupported (<reason>)` form). SPEC §6.12 (#895).
- `LMProviderInfo.transport` may now report `app_server` — the codex
  warm app-server transport (#896).

### Changed
- The expert loop is dspy ReActV2 (append-only History; provider prompt
  prefix byte-stable across iterations). The SSE wire, semantic-event
  stream, and tree view are contract-identical (byte-equality suites on
  both paths); `CLIO_REACTV2=0` selects the classic loop (#901).
- Typed degradation reasons and doctor probe names that referred to the
  clio-core system as "CTE" are renamed `clio_core_*` (`CTEStore` →
  `ClioCoreStore`, `cte_daemon_not_listening` →
  `clio_core_daemon_not_listening`, etc.). CTE now names only the
  actual clio-core component (config artifacts unchanged).

### Fixed
- Blueprint install from `file:///C:/...` marketplace URIs works from
  native Windows processes (#903).
- The earthscope pack no longer misreports geo-filter tool failures as
  "no coverage"; the no-coverage terminal requires structural filter
  evidence (marketplace v0.5.4, #904).

## [0.5.18] — 2026-07-09

The grounding-demo release. Pairs with gact-tui **v0.9.5** (pinned
submodule) — #233 render parity + #232 protocol convergence on the
client side, matched by the contract-surface changes below on the
server side.

### Added
- `GET /v1/sessions/{id}/messages` now honors pagination
  (`before` / `limit` / `include_system`) and `GET /v1/sessions`
  honors the `parent_session_id` filter. These params were previously
  accepted but ignored (unbounded payloads; subsession UIs saw every
  session); they are now a normative contract with conformance
  coverage on the gact-tui side (#232, #872).

### Changed
- Compaction emits a **structured compaction part** (a typed part
  carrying the summary + metadata) instead of `[compact summary]`
  prose, so clients render it as a first-class part rather than
  scraping text (#832, #873).

### Removed
- Retired the legacy Codex HTTP bridge process. Switch
  `provider=codex` to the LiteLLM CustomLLM path; no bridge process is
  needed. Users pointing at `:18900/v1` should re-pick the `codex`
  preset so config resolves through the registered provider.

> **Tracking gap:** releases `0.3.2` through `0.5.17` were shipped
> without a CHANGELOG entry. For those versions, see the GitHub release
> notes at https://github.com/iowarp/clio-agent/releases. This file is
> owned by the release skill going forward (see
> `.claude/skills/release-clio/SKILL.md`).

## [0.3.1] — 2026-04-27

The "every advertised capability actually works" release. Every flag
in `/v1/capabilities` that's `true` has been verified end-to-end with
either an integration test, a curl trace, or a screenshot — see
`docs/archive/CAPABILITIES_MATRIX.md` for the full matrix. **No silent
downgrades anywhere; if a flag is true, it works.**

### Added
- **Third-party MCP install + use endpoints** (#13).
  - `POST /v1/mcp/servers` (stdio: npx/uvx/raw command, OR http URL).
  - `POST /v1/mcp/servers/{id}/call` (invoke a tool on the installed server).
  - `DELETE /v1/mcp/servers/{id}` (uninstall).
  - `GET /v1/mcp/servers` returns BOTH bundled (fs/hdf5/parquet) AND
    installed third-party servers in one list.
  - Verified end-to-end against `@modelcontextprotocol/server-everything`:
    13 tools enumerated, `echo` + `get-sum` round-tripped via stdio.
- `docs/archive/CAPABILITIES_MATRIX.md` — one row per advertised capability with
  the proof for each (curl evidence / screenshot / integration test).
- New screenshots: `clio_mcp_servers.png` (bundled + third-party), 
  `clio_diff.png` (apply/reject path).

### Fixed
- **Generic tool observer (#2 closed strict).** `_call_tool_function`
  fires the global tool_observer for EVERY tool path — bundled
  in-process, ReAct via MCPToolBridge, AND third-party MCPs. SSE
  events `tool.call.started`/`completed` now arrive before
  `message.completed` for every tool call. `tools_called` metadata
  populated automatically.
- **Context files actually influence answers (#5 closed strict).**
  Removed silent workspace-root filter on attached files (user
  explicitly attached → trust them; write gates still apply via
  `_apply_edit_to_disk`). Binary files (parquet/hdf5) now inlined as
  schema summaries via the bundled tool inspectors instead of useless
  raw bytes.
- **Streaming deltas verified arriving in lifecycle order (#6 closed strict).**
  ClioAgent.acall added so streamify uses `is_async_program=True`
  instead of asyncify-in-executor (which was stripping the send_stream
  ContextVar). Single StreamListener bound to `answer` for clean chat
  output. Live per-token timing depends on the upstream provider —
  some OpenAI-compatible gateways buffer, OpenRouter passes through.
- **Permission audit trail (#7 closed strict).** `_apply_edit_to_disk`
  records an auto-approved permission row (action=allow,
  reason=`user_clicked_apply`) for every diff/apply. `/v1/permissions`
  has a complete audit trail of every destructive operation.
- **Plan mode + edit_modes thread into agent.** `ClioAgent.forward`
  gains `session_mode` + `session_edit_mode` kwargs; `_direct_edit_answer`
  shapes the file_diff Part by mode (diff: unified_diff,
  whole: new_content only, patch: both).
- **Explicit-edit short-circuit.** `_looks_like_explicit_edit` detects
  "propose an edit to /path/file.ext" and bypasses the router so the
  edit handler always fires regardless of which expert the file's
  extension would otherwise pick.
- **fs_server validate signature.** `validate_non_empty_string` was
  being called positionally where it declared `field` as keyword-only.
- **Edit-intent honored across providers.** `_direct_chat_completion`
  timeout 60s → 180s for slower providers.
- **Test infra reliability.** `tests/test_integration_contract/conftest.py`
  httpx client timeout 30s → 90s to absorb proxy tail latency.

### Performance
- Full integration_contract suite is 16/16 strict in ~95s (was ~25min before
  the streaming + adapter fixes earlier in this release cycle).

### Removed
- All `@pytest.mark.xfail` decorators in
  `tests/test_integration_contract/test_real_capabilities.py`. Suite is
  now strict-only.

## [0.3.0] — 2026-04-25

### Added
- GACT contract v0.2 implementation (28/30 capabilities supported;
  only LSP + voice intentionally out of scope).
- Workspaces — group sessions by project root (#12).
- Backend-provided slash commands (#14) and provider listing (#15).
- Session export to JSON (#16) + per-session task tracking (#18).
- Dynamic agent registry — POST/PUT/DELETE on `/v1/agents` (#19).
- User-defined hook subsystem (`pre_tool`, `post_tool`,
  `pre_message`, `post_message`, `on_error`) loaded from
  `~/.config/clio-agent/hooks/<event>.py` (#20).
- Scheduled session turns via cron expressions (#21).
- Session sharing with TTL tokens (#22).
- Skills extraction from past sessions (#23).
- DSPy reasoning trace surfaced as `thinking` Parts (#17).
- LM provider preset for direct OpenAI / ChatGPT (`gpt-4o-mini`
  default) — most-asked-for setup in lab usage.
- `/doctor` Capabilities scorecard tab on the TUI side, sourced
  from `/v1/capabilities`.
- TUI `lm_config` modal exposes Temperature + Max tokens fields
  alongside provider/model/key.

### Changed
- DSPy LM cache is now disabled by default (`cache=False`). Real
  serving wants accurate token accounting; identical-prompt cache
  hits were short-circuiting `dspy.settings.usage_tracker` and
  reporting zero. Trade-off: identical prompts cost real money each
  time. Filed as a setup note in `docs/SETUP.md`.
- `dspy.configure(...)` async-task-ownership guard side-stepped on
  PUT `/v1/providers/lm` so the LM can be swapped mid-session
  (haiku → sonnet → openrouter) without a server restart. Verified
  with cost-meter delta confirming the swap took effect at the LM
  call layer.
- ChatAdapter (with JSONAdapter fallback for cloud providers) wired
  at boot + on PUT `/v1/providers/lm`. Without it, Claude often
  returns plain text and DSPy's default JSONAdapter chokes with
  `LM Response cannot be serialized to a JSON object`.
- Streaming now uses `dspy.streaming.StreamListener` filtered to the
  `answer` field. Previously the user-visible text Part included
  ChatAdapter delimiter markers (`[[ ## answer ## ]]`,
  `[[ ## reasoning ## ]]`) and the router's chain-of-thought trace
  bleeding through.
- Token + cost accounting now snapshots `lm.history` across every
  known LM (main + `_router_lm`) and diffs the slice per turn.
  Required because DSPy contextvars don't propagate to asyncio
  executor threads, so the per-turn `UsageTracker` was unreliable
  from the request handler.
- `message.part.completed` event payload now ships `final_text` so
  the TUI can replace the streamed buffer (which still carries
  upstream markers from the underlying litellm chunks) with the
  parsed clean answer once the part is done growing.

### Fixed
- Router LM occasionally returned malformed Literal values
  (`"None"` as a string, empty strings) which crashed the dispatch
  with "I encountered an issue with the None expert". Coerced to
  one of the known buckets (data/analysis/visualization/none/chat),
  defaulting to chat.
- Doubled provider prefix (`openai/openai/claude-haiku-4-5`) when
  the API was sent a model id that already included the prefix.
  Strip + reapply once.
- 5 of 16 integration tests in `tests/test_integration_contract/` were
  flaking on proxy tail latency. Bumped LM-driven turn timeouts
  from 120s to 300s. Suite is now 11 passed + 5 honest xfails (each
  xfail links the GitHub issue tracking the gap).

### Performance
- Integration suite runtime dropped from ~25 minutes to ~6 minutes
  end-to-end after the streaming + adapter fixes — likely because
  StreamListener avoids buffering router noise + ChatAdapter
  prevents JSON-parse retries.

### Removed
- Old `selected = (None or "chat")` fragility that produced "the
  None expert encountered an issue" answers — see Fixed.

## [0.2.0] — 2026-04-22

Initial v0.2 contract scaffolding:
- `src/clio_agent/gact/` Python module wrapping `ClioAgent` as a
  GACT v0.2 server. FastAPI + SSE.
- `clio-agent-gact` console script (`uv run clio-agent-gact --port
  17800`).
- v0.2 capabilities flipped on as features landed: `sessions`,
  `agent_routing`, `memory`, `structured_errors`,
  `integration_health`, `tool_telemetry`, etc.

(Full details in the gact-tui integration plan items #1–#23.)
