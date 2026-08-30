# MCP client unification — one client, negotiated v1/v2, no privileged integrations

**Umbrella:** iowarp/clio-agent#1274 (+ #1275 refusal semantics). **Status:** ACTIVE — kickoff Monday 2026-08-31.
**Protocol references (this repo):** `docs/design/mcp-v2-understanding-2026-08.md` (the working
understanding) and `docs/design/mcp-client-obligations-2026-07-28.md` (per-item obligations table).

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

## EXECUTION ORDER (Monday kickoff)

1. **clio-relay + MCP v2 adaptation first**: build (a) on the feature branch
   (`feat/mcp-client-unification`; owner ruling — develop is user-consumed, the campaign lands on
   develop only at its final proof), migrate the relay onto the unified client, and DELETE the
   relay's special path (the inventory's dissolve/keep/delete verdicts below).
2. (d), (e), (f) land as slices on the same branch as the generic client grows.
3. **Final testing grounds: clio-web-search and the clio-kit web MCP** — the acceptance is
   explicitly ordered: FIRST clio-relay works properly with no special path (the full relay
   workload through the unified client), THEN the web MCPs prove generality end-to-end (the
   `task=required` fetch scenario that exposed the fork — a real user's exact failure).
4. (b) runs in parallel in jarvis-cd/clio-kit (different repos); (c) gates on everything and
   closes the campaign with the v1.7.0 release.

PRECONDITION (owner hold, 2026-08-29): the other development teams' outstanding week of work
merges into develop first; the campaign branches from the true integrated base. Known conflict
surfaces vs this session's pushed merges: gact/relay_wiring.py, tools/gateway.py, agent.py relay
fields.

## The defect, precisely

clio-agent reaches every declared MCP server through a proxy whose client class pins
`_auto_internal_extensions = False`, so the tasks extension is suppressed (typed:
`mcp_tasks_declaration_suppressed`, #1119) — while `tools/relay_transport.py:256`
builds a direct default client and gets the tasks extension implicitly. All MCP v2
task support is therefore trapped in a bespoke relay pathway (~7,945 production LOC,
inventoried below), and a real user's `task=required` server was structurally
unreachable (-32021 on every call).

The fix key is already on the wire and unread: `Tool.execution.task_support`
("forbidden" | "optional" | "required", SEP-1686) is present on every tools/list
entry, is copied intact through FastMCP's proxy (`ProxyTool.from_mcp_tool` preserves
`execution` even while pinning its own `task_config=forbidden`), and `grep
task_support src/` returns zero hits. Additionally the -32021 refusal is
self-describing: `requiredCapabilities` names exactly what to re-dial with.

## Target architecture

ONE client pathway for every server (built-in, declared, relay):

1. **Negotiated capability, never probed**: at discovery/mount, read each tool's
   `execution.taskSupport`; record the server's task capability on the per-server
   connection-era record (`tools/mcp_connection_era.py` — the existing per-server
   registry with the right degrade-reason conventions).
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

## Slices (dependency order; implement → adversarial review → merge, per slice)

Letter mapping: S1–S8 deliver (a); (d)/(e)/(f) slot in as marked.

- **S1 — capability-keyed task routing (the unlock).** Read `taskSupport` at
  discovery; record per-server capability; `tasks_declaration` keyed on capability;
  direct task-capable route in `_proxy_for_spec`/`_connect_namespace` for
  task-capable servers; v1 path byte-identical (regression-proven).
- **S2 — refusal + wait semantics (#1275):** protocol-refusal class terminal-fast
  on every path; progress-aware ladder on the direct route; visible waiting.
  Includes (e)'s MRTR per-path verification.
- **S3 — task machinery unification:** drive-to-terminal, handle registries
  (`relay_install_jobs` → shared registry), ONE state vocabulary.
- **S3b — (a) extension registry:** generic extension negotiation (declare + read
  server extensions); tasks becomes an entry; **(d) the ui extension declares here**;
  enumerate the other official extensions (oauth-client-credentials for headless/CI,
  enterprise-managed-auth).
- **S4 — console generalization:** `mcp_task_console.py` + stream observer factory.
- **S5 — curated tool overlay + relay as declared server:** `tool_overlay.py`;
  `relay_factory` deleted; relay enters `load_mcp_servers()`.
- **S6 — artifact fetch generalization** + origin-schema-keyed edges.
- **S6b — (f) protocol-surface completion:** subscriptions/listen + listChanged→cache
  invalidation, server cache hints, resultType/x-mcp-header/era-gated removals,
  auth specifics + CIMD deployment artifact, fastmcp b1→b5 + verification probes.
- **S7 — glue deletion sweep** with content-accounted deletion verification.
- **S8 — the ordered acceptance:** FIRST the full relay workload through the unified
  client, no special path (this is where the campaign may land on develop); THEN the
  final testing grounds — clio-web-search / clio-kit web MCP `task=required` fetch
  end-to-end through the generic declared-server path (the exact real-user failure
  that exposed the fork). Relay-specific tests migrate or die with their subjects.

Rules that bind every slice: no accretion (owner modules, ratchets); no silent
fallback (typed reasons); protocol-negotiation only; the five #1274 wait
constraints; deletion inventories verified by content accounting.
