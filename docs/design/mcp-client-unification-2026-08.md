# MCP client unification — one client, negotiated v1/v2, no privileged integrations

**Umbrella:** iowarp/clio-agent#1274 (+ #1275 refusal semantics). **Status:** ACTIVE.
**Owner ruling (2026-08-28, verbatim intent):** "there is no review of this, you know
what is wrong, a relay specific path, and you know what is right, a unified proper mcp
client that can handshake and operate with both v1 and v2 mcps." The relay is JUST an
MCP; capability built for one integration lands as a generic mechanism or not at all.

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
   `arc/rpc_liveness` stall ladder. (See also
   docs anchor: feedback memories handshakes-not-global-timeouts,
   protocol-by-negotiation-never-timing.)
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

- **S1 — capability-keyed task routing (the unlock).** Read `taskSupport` at
  discovery; record per-server capability; `tasks_declaration` keyed on capability;
  direct task-capable route in `_proxy_for_spec`/`_connect_namespace` for
  task-capable servers; v1 path byte-identical (regression-proven). ACCEPTANCE:
  the real clio-kit web server's `task=required` fetch called end-to-end through
  the GENERIC declared-server path (live locked-stdio test against the pinned kit
  wheel), plus zero behavior change for v1 servers.
- **S2 — refusal + wait semantics (#1275):** protocol-refusal class terminal-fast
  on every path (no retry, typed propagation to run surfaces); progress-aware
  ladder on the direct route; visible waiting.
- **S3 — task machinery unification:** drive-to-terminal, handle registries
  (`relay_install_jobs` → shared registry), ONE state vocabulary.
- **S4 — console generalization:** `mcp_task_console.py` + stream observer factory.
- **S5 — curated tool overlay + relay as declared server:** `tool_overlay.py`;
  `relay_factory` deleted; relay enters `load_mcp_servers()`.
- **S6 — artifact fetch generalization** + origin-schema-keyed edges.
- **S7 — glue deletion sweep** with content-accounted deletion verification;
  `relay_wiring` dies; agent.py loses its relay fields.
- **S8 — relay-on-generic migration proof:** the relay live workload (case13
  surface) runs entirely through the unified client; relay-specific tests migrate
  or die with their subjects.

Rules that bind every slice: no accretion (owner modules, ratchets); no silent
fallback (typed reasons); protocol-negotiation only; the five #1274 wait
constraints; deletion inventories verified by content accounting.
