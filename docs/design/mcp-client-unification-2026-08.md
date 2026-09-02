# MCP client unification — one client, negotiated v1/v2, no privileged integrations

**Umbrella:** iowarp/clio-agent#1274 (+ #1275 refusal semantics). **Status:** ACTIVE —
branch `feat/mcp-client-unification` (cut 2026-09-01 from develop 96d5bdcc, the integrated
post-landing base; the merge-first precondition is satisfied).
**Protocol references (this repo):** `docs/design/mcp-v2-understanding-2026-08.md` (the working
understanding) and `docs/design/mcp-client-obligations-2026-07-28.md` (per-item obligations table).

## REORIENTATION (owner-ruled, 2026-09-01 — supersedes the execution order below)

The campaign was motivated by clio-relay owning a bespoke task-capable path instead of the
main client doing the proper handshake. That fork has now bitten real users repeatedly:
MCP v2–compliant servers are structurally unusable on clio (the #1274 -32021 failure), and
more complaints have arrived. Priorities therefore invert:

- **First and foremost: a proper clio MCP client** — ONE client, official negotiation,
  full v2 protocol surface — proven against BOTH eras: real v1 servers (the existing
  fleet, byte-identical) and real v2 servers. The relay's special path is left UNTOUCHED
  AND WORKING while this is built (never break baseline); it is not the driver, the
  proving ground, or a gate anymore.
- **Testing grounds for v2:** (1) the clio-kit web MCP — v2-native, `task=required`
  fetch, the exact real-user failure — with the marketplace deep-research expert working
  end-to-end on clio-agent as the user-visible proof; (2) a SYNTHETIC v2-exerciser MCP
  server in tests that FORCEFULLY uses the v2 semantics (task=required, MCP Apps/ui,
  MRTR, elicitation, subscriptions, cache hints, …) as the per-slice conformance bed;
  public internet v2 servers may serve as extra non-gating sanity legs.
- **clio-relay becomes the after-fact:** the special-path removal (the dissolve/keep/
  delete inventory below), letters (b) and (c), and the v1.7.0 release move to a SEPARATE
  follow-on plan-execute process (campaign 2) that starts only after campaign 1 has
  landed and we can confidently say clio supports the full MCP v2 protocol.
- **Landing:** campaign 1 lands on develop at its own acceptance (C1-S6) — the user fix
  ships without waiting for the relay migration.

## THE CAMPAIGN LETTERS (owner-ruled, 2026-08-29 — the authoritative frame)

- **(a) The unified client, with the official handshake mechanism.** ONE client for every MCP
  server and every tool — no exceptions, no enumerated consumers, no privileged pathways. v1 vs v2
  is decided by the protocol's own negotiation only: `server/discover`, per-request `_meta`
  protocol fields, the dual-era probe model, and generic extensions negotiation (a real registry,
  reading server-declared extensions — not today's hardcoded tasks special case). Never probed by
  behavior, never classified by timing.
- **(b) Artifact pathway semantics done properly for IOR, Darshan, and ParaView** — the capture
  contract (declared outputs from the package's own configuration parameters + the loud
  uncaptured-files signal, clio-relay#293) proven on those three packages
  (grc-iit/jarvis-cd#211, clio-relay#294, jarvis-cd#209/PR#210). LAMMPS is the verified reference
  implementation of both halves (live progress stream + configured-root/script-declared files).
- **(c) Re-verify EVERYTHING under the finished stack** — with the acceptance frame that
  clio-relay is a tool of EASE for the agent: single-command install, easy deployment, clear
  artifact tracking, SSE updates — never demanding the agent jump through hoops. Runs under
  production enforcement (dev mode off, both clusters), ends in the formal v1.7.0 release.
- **(d) MCP Apps support** — align the EXISTING Apps host (`gact/mcp_apps.py`, built at revision
  2026-01-26) to the 2026-07-28 extensions framework: declare `io.modelcontextprotocol/ui` through
  the generic extension registry, align revision metadata, keep the proven
  tolerate-unknown-metadata behavior.
- **(e) MRTR support** — verify and complete: the retry/`requestState` loop is the mcp-2.0 SDK's;
  clio's contribution (round bound, typed exhaustion, the single HITL surface all three input
  shapes converge on) must survive unification intact; plus the elicitation completeness items
  (per-mode form/url capability declaration, URL-mode consent MUSTs, SEP-1034 defaults,
  SEP-1330 multi-select enums, and never carrying forward the removed elicitationId constructs).
- **(f) The rest of the v2 protocol surface** — the completeness-sweep items (#1274 comments):
  `resultType` handling, `subscriptions/listen` + listChanged as the listing-cache invalidation
  signal, server cache hints (`ttlMs`/`cacheScope` + the caching MUST-NOTs), SSE
  re-issue-never-resume, `x-mcp-header` duties, era-gating the removals (ping / logging/setLevel /
  roots list_changed), authorization specifics by name (iss validation, credential-per-issuer,
  PKCE-absence refusal, RFC 8707, CIMD hosting as a cluster-parameterized deployment artifact),
  JSON-Schema 2020-12 posture, pagination/completions small MUSTs, and the fastmcp beta-track
  hardening (develop is already on 4.0.0b1; upstream b5) with the verification-probe shortlist.

## EXECUTION ORDER (reoriented 2026-09-01; the 2026-08-31 order is superseded)

**Campaign 1 — this branch (`feat/mcp-client-unification`):**
1. Build (a) on the generic declared-server path only — capability-keyed routing over the
   already-generic task machinery (`tools/mcp_tasks.py` / `mcp_task_extension.py`); the
   relay's special path stays untouched and working.
2. (d), (e), (f) land as slices on the same branch as the generic client grows, each proven
   against the synthetic v2-exerciser as its surface grows.
3. Acceptance (C1-S6): v1 fleet regression + exerciser full-surface + clio-kit web MCP
   `task=required` fetch + the deep-research expert working on clio-agent. On pass, the
   campaign lands on develop and the full-v2-support claim is made.

**Campaign 2 — separate follow-on plan-execute process (relay after-fact):** migrate the
relay onto the unified client as consumer #1, execute the dissolve/keep/delete inventory
below (special path DELETED, content-accounted), re-verify the relay workload through the
unified client, then letters (b) and (c) close with the v1.7.0 release. Gets its own
kickoff, plan pass, and issue tree when campaign 1 has landed.

PRECONDITION — SATISFIED 2026-09-01: the outstanding week of work (#1255 + gact-tui#380)
merged; the branch is cut from the integrated base (develop 96d5bdcc). Known parallel lane:
`codex/clio-composer-pipeline` (PR #1278, gact-0.3/composer/resources territory) overlaps
only on gact/relay_wiring.py (+22, a provider_config passthrough into
construct_agent_with_relay) and tools/mcp_executor.py (~20 lines) — absorb on the periodic
develop→campaign merges; the relay_wiring.py deletion happens in campaign 2, where the
passthrough must survive into whatever replaces that seam.

## The defect, precisely

clio-agent reaches every declared MCP server through a proxy whose client class pins
`_auto_internal_extensions = False`, so the tasks extension is suppressed (typed:
`mcp_tasks_declaration_suppressed`, #1119) — while `tools/relay_transport.py:256`
builds a direct default client and gets the tasks extension implicitly. All MCP v2
task support is therefore trapped in a bespoke relay pathway (~7,945 production LOC,
inventoried below), and a real user's `task=required` server was structurally
unreachable (-32021 on every call).

The fix key is already on the wire and unread — but WHERE it rides is era-split.
**C1-S0 PROBE VERDICT (2026-09-01, empirical, fastmcp 4.0.0b1 + mcp_types 2.0.0):**
- **Modern era (2026-07-28):** per-tool `Tool.execution.taskSupport` is REMOVED from the
  wire model (`mcp_types/_v2026_07_28` has no `execution` field; listing returns
  `execution=None` for every tool). The negotiation key is the SERVER-DECLARED
  extensions: `ServerCapabilities.extensions` carries `io.modelcontextprotocol/tasks`
  (read off `client.server_capabilities` after initialize/discover). Note fastmcp
  splices `io.modelcontextprotocol/ui: {}` onto every modern server by default — the
  read must key on the tasks id, not extension presence generally.
- **Legacy era (2025-11-25):** the reverse — `capabilities.extensions` is stripped by
  the version sieve, but per-tool `execution.taskSupport` ("forbidden" | "optional" |
  "required", SEP-1686) IS present on tools/list entries.
- **The proxy front strips the backend's tasks declaration entirely** (a
  `create_proxy(ProxyClient(backend))` front re-advertises only its own extensions):
  the declared path today cannot even SEE that a backend speaks tasks. Both reads
  therefore happen at the direct-connecting choke point `gateway._list_declared_tools`
  (which connects AND lists), never through the mounted proxy.
`grep task_support src/` returns zero hits either way. Additionally the -32021 refusal
is self-describing: `requiredCapabilities` names exactly what to re-dial with.

## Target architecture

ONE client pathway for every server (built-in, declared, relay):

1. **Negotiated capability, never probed**: at discovery/mount, read the era-split
   key (probe verdict above — modern: `server_capabilities.extensions` ∋ the tasks id;
   legacy: per-tool `execution.taskSupport`); record the server's task capability on
   the per-server connection-era record (`tools/mcp_connection_era.py` — the existing
   per-server registry with the right degrade-reason conventions).
2. **Capability-keyed routing**: a server with task-capable tools gets a DIRECT
   task-capable client route (the machinery in `relay_transport` today); v1 servers
   keep today's path byte-for-byte. Decision point: `tasks_declaration()`
   (`tools/mcp_task_extension.py:293` — already owns the typed suppression reason).
   Mount seam: `gateway._proxy_for_spec` (injectable `proxy_factory` already exists
   and is tested). Call seam: `mcp_executor._connect_namespace`.
3. **Wait semantics (owner-ruled, pinned on #1274)**: typed protocol refusals
   (-32021/-32022) propagate immediately — the ONLY fail-fast; slowness is never a
   verdict; per-exchange retry with increasing backoff to a cap; intermediate
   progress RESETS the clock; every wait names what it waits on. Template:
   `arc/rpc_liveness` stall ladder.
4. **The relay becomes consumer #1**: a declared server entry (`mcp_config`
   `MCPServerSpec` + auth) plus a small transport adapter for what is honestly
   relay-only (held-channel/session identity headers, its HTTP door endpoints, the
   CLI receipt parser, the expert-invoker deployment target).

## Inventory verdict (full table: scout report 2026-08-28, condensed here)

~7,945 LOC bespoke production layer + ~7,900 LOC relay-specific tests.
- **GENERIC-IN-DISGUISE (~4,600, 58%) — dissolves into generic owners:**
  - `relay_transport.py` core submit/poll/wait/resume/cancel/message → generic
    task-capable client in `mcp_tasks`/`mcp_task_extension`.
  - `relay_install_jobs.py` (700) → literal duplicate of `AgentTaskRegistry` +
    `ledger_retention`; residue ZERO.
  - `jarvis_jobs.py` + `remote_mcp.py` projection machinery → declarative curated
    tool overlay (`tools/tool_overlay.py`) usable by any declared server.
  - `relay_console.py` + `relay_console_stream.py` → `tools/mcp_task_console.py`
    hung off the already-generic `task_observers` registry (the template seam).
  - `relay_artifact_fetch.py` → `tools/mcp_artifact_fetch.py` keyed on a
    server-declared artifact capability; transform edges keyed on the origin
    schema version, not tool names.
  - `relay_contract.py` validation + session resolution → `mcp_executor`/
    `mcp_task_records.resolve_task_session_id` (exists).
  - `relay_timeline.py` → `gact/mcp_task_events.py` + EventBus.
  - `relay_status.py` → `providers/handshake/mcp.py` + `/v1/mcp/*` rows.
  - FOUR duplicate task-state vocabularies (`invoker.RELAY_STATE_MAP`,
    `remote_mcp._TASK_TO_JOB_STATE`, `run_registry._RELAY_LIVE_STATES`,
    display mapping) → one, in `mcp_task_records`.
- **TRANSPORT-SPECIFIC (~2,150, 27%) — the legitimate residue:** relay CLI receipt
  parsing (`relay_cli_runner.py`), HTTP door endpoints (SSE timeline, artifact
  bytes), owner-session identity headers, catalog-revision meta key, the
  relay expert invoker pair (`relay_expert_invoker.py`, `relay_invoker_runtime.py`)
  + `relay:<cluster>` placement.
- **GLUE — DELETE (~1,200, 15%), first-class deletion inventory:**
  `gact/relay_wiring.py` (286 — the worst privileged seam: mutates singleton agent
  internals because relay is not a declared server), `tools/relay_factory.py` (241 —
  bespoke re-implementation of `MCPServerSpec`), `gact/routes/relay.py`,
  `gact/agent_message_transport.py`, `agent.py` relay fields + federation epoch,
  `gateway.py` reserved-namespace mounts + `list_relay/jarvis_tool_definitions`
  (collapse into `list_builtin_tool_definitions` once relay is declared),
  18 `relay.*` config keys → declared-server entry.

## Campaign 1 status (2026-09-02)

- **C1-S0 MERGED** (2bcde2f9; #1280): exerciser + frozen v1 fixture + dual-era suite; the
  era-split probe verdict recorded above.
- **C1-S1 MERGED** (954f8b9f; #1281): capability-keyed routing — the #1274 defect fixed,
  adversarially proven under production wiring both directions (readiness-ordered
  first-call success; cold-race typed refusal then one-shot heal).
- **C1-S2 MERGED** (15a03f24; #1282, resolves #1275): the hang's root cause was the
  vendored ReActV2 tool-exception swallow (typed refusals became LM-retry fuel; swept
  through BestOfN/Refine too); refusals terminal-fast with typed reasons on the parent
  surface + re-dial hints; every MCP wait activity-driven, typed, surfaced (transient
  throttled `mcp_task.wait`; rendering = gact-tui#384), cancellable. OWNER-EYE TRADE: a
  progressing call holds the per-namespace call lock while it progresses (accepted cost
  of never killing live work; #1225 precedent).
- Composer wave ABSORBED (develop 61708fae → c43a2ca8, clean). Deep-researcher case
  scaffolded (case14; #1286 leg iii).
- **REORDER (owner, 2026-09-02): full live verification is PREPARED NOW on the campaign
  branch** (scripts/live_verification/: preflight + web-fetch smoke + synthetic-on-a-
  session + fleet/deep-researcher runbooks) and RUNS ON OWNER GO-AHEAD — before S3-S5,
  not after. A live session against the exerciser (incl. an MRTR round through the real
  HITL surface) is added to the #1286 gate; the exerciser-as-test-surface re-affirmation
  stays as well.

## Campaign 1 slices (dependency order; implement → adversarial review → merge, per slice)

- **C1-S0 — the v2 exerciser + dual-era conformance bed.** A synthetic in-repo MCP
  server (fastmcp 4 + fastmcp-tasks server side; in-memory `Client(server)` where
  possible) that FORCEFULLY uses v2: `task=required` tools (-32021 with
  `requiredCapabilities` on plain calls), declared extensions, MRTR/input_required
  rounds, elicitation (form and url modes), subscriptions/listen + listChanged, cache
  hints, resultType variants, pagination — plus a frozen v1-era fixture server. Each
  later slice EXTENDS the exerciser with its surface; the conformance suite runs the
  unified client against both eras. Starts with the tasks surface (what S1 needs).
- **C1-S1 — capability-keyed task routing (the unlock; fixes the #1274 defect).**
  Read `execution.taskSupport` at discovery; record per-server capability on
  `mcp_connection_era`; `tasks_declaration` keyed on capability; direct task-capable
  route in `_proxy_for_spec`/`_connect_namespace` for task-capable servers; v1 path
  byte-identical (regression-proven).
- **C1-S2 — refusal + wait semantics (#1275):** protocol-refusal class terminal-fast
  on every path; progress-aware ladder on the direct route; visible waiting.
  Includes (e)'s MRTR per-path verification. USER-AGENCY rule (owner, 2026-09-01):
  this is a user-facing system — the user is the unstuck mechanism. No wait may
  degrade or block the system on a clock's verdict (NFS latency runs beyond any
  general expectation and such timeouts have literally locked users out); the
  system retries with expanding windows and SURFACES the wait (what it waits on,
  attempt count, next retry) so the user can see, judge, and cancel; the terminal
  give-up on slowness belongs to the user.
- **C1-S3 — (a) generic extension registry:** extension negotiation (declare + read
  server-declared extensions); tasks becomes a registry entry, not a special case;
  **(d) the ui extension declares here** — align `gact/mcp_apps.py` to the 2026-07-28
  extensions framework, revision metadata, tolerate-unknown-metadata preserved;
  enumerate the other official extensions (oauth-client-credentials for headless/CI,
  enterprise-managed-auth).
- **C1-S4 — (e) MRTR + elicitation completeness:** round bound, typed exhaustion, the
  single HITL surface all three input shapes converge on — surviving unification
  intact; per-mode form/url capability declaration, URL-mode consent MUSTs, SEP-1034
  defaults, SEP-1330 multi-select enums, no removed elicitationId constructs.
- **C1-S5 — (f) protocol-surface completion:** subscriptions/listen + listChanged→cache
  invalidation, server cache hints (`ttlMs`/`cacheScope` + caching MUST-NOTs),
  resultType/x-mcp-header/SSE re-issue-never-resume/era-gated removals, auth specifics
  by name (iss validation, credential-per-issuer, PKCE-absence refusal, RFC 8707, CIMD
  as a cluster-parameterized deployment artifact), JSON-Schema 2020-12 posture,
  pagination/completions MUSTs, fastmcp b1→b5 + the verification-probe shortlist.
- **C1-S6 — acceptance + landing:** (i) v1 fleet regression byte-identical; (ii)
  exerciser full-surface green; (iii) clio-kit web MCP `task=required` fetch
  end-to-end through the generic declared-server path (the exact real-user failure);
  (iv) the marketplace deep-research expert works on clio-agent; optional non-gating
  sanity leg against a public internet v2 server. On pass: land on develop, make the
  full-v2-support claim.

## Campaign 2 slices (relay after-fact — separate process, re-planned at its kickoff)

Held from the original plan, content unchanged: task-machinery dissolution into shared
owners (`relay_install_jobs` → shared registry, ONE state vocabulary), console
generalization (`mcp_task_console.py` + stream observer factory), curated tool overlay +
relay as declared server (`relay_factory` deleted, relay enters `load_mcp_servers()`),
artifact fetch generalization + origin-schema-keyed edges, the glue deletion sweep
(content-accounted; `relay_wiring.py` deletion must preserve the #1278 provider_config
passthrough semantics), and the relay-workload acceptance through the unified client with
no special path. Relay-specific tests migrate or die with their subjects. Letters (b) and
(c) close it with the v1.7.0 release.

Rules that bind every slice: no accretion (owner modules, ratchets); no silent
fallback (typed reasons); protocol-negotiation only; the five #1274 wait
constraints; deletion inventories verified by content accounting.
