# Leg C2 — the EXPANDED synthetic-v2 live-verification avenues (#1286, C1-S6)

**Owner ruling this leg answers:** "every avenue of the mcp v2 needs to be
live tested, to find bugs and problems." Leg C (`leg_c_synthetic_session.py`)
proved ONE avenue end to end through a real gact session: task=required
plumbing (`task_echo`) plus form-mode MRTR (`guarded_input`). This leg
(`leg_c2_v2_avenues.py`) covers the REMAINING avenues the C1-S0 exerciser
(`tests/test_tools/mcp_exerciser.py`) and the declared-path client can reach,
structured as eleven INDEPENDENT sub-legs, each producing its own
`{avenue, status: pass|fail|blocked, evidence, error}` verdict inside one
`out/live-verification/leg_c2_verdict.json`. One red or blocked avenue never
blocks another's verdict — a red or blocked result is DESIRED output here: it
becomes failing-first slice work, not something to hide.

**Status:** PREPARED, not yet run live (same posture as `RUNBOOK.md`'s
leg B/C — runs on owner go-ahead). `--dry-run` and `--plumbing-only` have
both been exercised while building this package (zero LM spend, zero server
boot for `--dry-run`).

## Study first

- `_common.py` — the shared harness (boot_server, wait_health, allow-all
  policies-first, `materialize_testing_pack`, install/activate blueprint,
  `resolved_agent_tools`). Unmodified.
- `leg_c_synthetic_session.py` — the pattern this leg extends: readiness
  gate, headless HITL answer via
  `POST /v1/sessions/{sid}/questions/{question_id}/answer`, verdict JSON to
  `out/`.
- `tests/test_tools/mcp_exerciser.py` — the v2ex server: 9 tools
  (`task_echo`, `task_optional_echo`, `plain_echo`, `forbidden_echo`,
  `guarded_input`, `plain_guarded_input`, `staller`, `plain_staller`,
  `silent_sleeper`); form-mode MRTR only; no resources/prompts; no
  cache/listChanged/ui-resource arms — see each blocked avenue below for the
  exact citation.
- `tests/test_tools/test_mcp_v2_conformance.py` — how the exerciser is
  driven in-process (this leg drives it through a REAL session instead).
- `src/clio_agent/gact/mcp_task_events.py` — the `mcp_task.wait` SSE event
  (avenue 5); `src/clio_agent/gact/routes/misc.py::session_events` — the SSE
  route itself (transient events are fanned out LIVE but never replayed —
  why avenue 5 needs a live SSE subscriber, not a poll-after-the-fact).
- `src/clio_agent/gact/routes/mcp.py` — `POST /v1/mcp/servers` +
  `POST /v1/mcp/servers/{sid}/call` (the REST-install lane avenue 10 rides —
  a DIRECT, non-gateway connect via `make_mcp_client`, deliberately NOT the
  declared/gateway path this campaign otherwise targets, because avenue 10 is
  a transport-layer question the REST lane answers just as validly, headless
  and LM-free).
- `src/clio_agent/gact/mcp_apps.py` — the MCP Apps host (avenue 11's
  citations: `_resource_uri` ~104-108, `_resource_payload` ~174-193,
  `_append_live_assistant_part` 434-448, the `GET /v1/sessions/{sid}/
  mcp-apps/{app_id}` route 617-637).

## New files this leg adds

- `_sse_collector.py` — a small background-thread SSE subscriber
  (`GET /v1/sessions/{sid}/events`), needed because `mcp_task.wait` is
  published `transient=True` (fanned out live, never recorded into replay
  history — `gact/events.py::EventBus._deliver`), so only a subscriber
  attached DURING the call can observe it.
- `_header_capture_server.py` — a minimal FastMCP HTTP server + Starlette
  middleware that logs every request's raw headers to a JSONL file.
  Standalone-runnable; avenue 10 boots it as a real subprocess.
- `agents/v2ex-avenues/` — a SIBLING of `agents/v2ex-testing/` (not an edit
  to it), exposing all 9 exerciser tools on one `main` expert so avenues 1
  and 5 can share a single pack/session.
- `leg_c2_v2_avenues.py` — the runner.

## How to run

```bash
# Print the avenue plan (id/needs_lm/expected outcome) and exit. Boots nothing.
uv run python scripts/live_verification/leg_c2_v2_avenues.py --dry-run

# Boot the server, materialize+install+activate the pack, run every headless
# avenue (2,3,4,6,7,8,9,10,11) AND the readiness gate; avenues 1 and 5 are
# recorded status="blocked" (reason: "plumbing-only run"). Zero LM spend.
uv run python scripts/live_verification/leg_c2_v2_avenues.py --plumbing-only

# Full run: everything above PLUS avenues 1 (task-modes) and 5 (waits-cancel)
# through two directed claude_code/sonnet turns on one session.
uv run python scripts/live_verification/leg_c2_v2_avenues.py --provider claude_code --model sonnet
```

`--port` (gact server, default 17982), `--hcap-port` (header-capture server,
default 17983), `--ws-dir`, `--out`, `--turn-timeout-s`,
`--wait-event-timeout-s`, `--cancel-timeout-s` are all overridable — see
`--help`. Verdict JSON always lands at
`out/live-verification/leg_c2_verdict.json` (default `--out`); per-turn
message dumps and the SSE audit log land beside it.

## Avenue table — expected outcome today, grounded

| # | Avenue | Needs LM? | Expected today | Why (citation) |
|---|---|---|---|---|
| 1 | task-modes | Yes | **pass** | optional/plain succeed like leg C's task_echo already proves; forbidden-explicit is asserted only to be terminal-fast (never hung) — see "On avenue 1's forbidden-explicit design" below for why a hard "-32021/-32022 or bust" assertion would be presumptuous. |
| 2 | mrtr-url | No | **blocked** | `mcp_exerciser.py`'s only elicit helper, `_one_elicit()` (lines 56-64), always builds `mcp_types.ElicitRequestFormParams` — no URL-mode arm exists anywhere in this repo's exerciser. The only `ElicitRequestURLParams` construction in the repo is `tests/test_gact/test_elicitation_hitl.py`, fed DIRECTLY into `handle_elicitation()` in isolation (no real MCP tool round-trip). |
| 3 | mrtr-methods | No | **blocked** | (a) the exerciser declares no `@server.resource`/`@server.prompt` at all — 9 `@server.tool`s and nothing else; (b) even if it did, the declared session/turn surface only exposes an expert's frontmatter `tools:` list — prompts/resources reach a session only via the SEPARATE REST-install-lane inventory routes (`gact/routes/mcp.py` ~898-960), a different (direct, non-gateway) connection than the path this campaign targets. |
| 4 | cache | No | **blocked** | repo-wide grep for `cache_ttl`/`cache_scope`/`CacheConfig`/`cache_hint` found zero hits inside `src/clio_agent` or `tests/test_tools` — no client support and no exerciser arm exist yet. C1-S5 territory (`docs/design/mcp-client-unification-2026-08.md` line ~62-63). |
| 5 | waits-cancel | Yes | **pass** | `staller` is `task=required` with a 50ms poll interval; `gact/mcp_task_events.py::publish_mcp_task_wait` fires `mcp_task.wait` on the session's SSE bus while it runs (transient, live-only — a NEW `_sse_collector.py` subscribes DURING the call to observe it); `POST /v1/sessions/{sid}/cancel` then ends the turn `cancelled` per `gact/routes/session_cancellation.py::cancel_session_state`. |
| 6 | pagination | No | **pass (indirect)** | no `list_page_size`/page-size control exists anywhere in `clio_agent` (repo-wide grep: zero MCP-tools/list-paging-related hits) — this leg cannot FORCE multi-page traversal. It instead reuses the readiness gate: all 9 exerciser tools resolving onto the agent's toolset is indirect proof that whatever paging fastmcp's `Client.list_tools()` did internally (SDK-covered per obligations doc row B1) worked correctly. |
| 7 | list-changed | No | **blocked** | the exerciser's tool set is fixed at server-build time — no tool adds/removes a tool or fires `notifications/tools/list_changed`. Repo-wide grep for `listChanged`/`list_changed`: only unrelated hits. C1-S5 territory. |
| 8 | extensions | No | **blocked** (for the direct assertion) | `gact/routes/mcp_rows.py::handshake_server_row` — the only wire shape `GET /v1/mcp/handshake` returns — never surfaces `ServerCapabilities.extensions`; no other HTTP route exposes the era/capability registry either. `execution_era` (which SHOULD read `"modern"` for v2ex) is recorded as the closest available indirect signal — C1-S3(a)'s generic extension registry hasn't landed. |
| 9 | adversarial | No | **blocked** | no standalone MUST-violating raw-responder/ASGI-shim fixture exists anywhere in this repo (searched `tests/test_tools/*` for "raw responder"/"ASGI shim": zero hits). The C1-S0 slice built well-behaved fixtures only. |
| 10 | headers | No | **genuinely probed live** | a NEW `_header_capture_server.py` is booted as a real subprocess and probed via `POST /v1/mcp/servers` + `POST /v1/mcp/servers/{sid}/call`. An isolated, out-of-band smoke test (outside this repo, not part of any run this package makes) already showed a BARE `fastmcp.Client` sends `mcp-method`/`mcp-name`/`mcp-protocol-version` headers on a real `tools/call` today — B2 is "library-covered" exactly as the obligations doc's own B2 row says, with ZERO clio_agent code involved (grepped `src/clio_agent` for `Mcp-Method`/`Mcp-Param`: no hits). `mcp-param-*` mirroring (B3) is a SEPARATE, still-open question this probe tool cannot exercise either way (see the avenue's own evidence note — B3 only mirrors ANNOTATED header-worthy params, and no tool anywhere declares one). Do not assume this avenue's number pre-decides pass/fail — read the live verdict. |
| 11 | apps-ui | No | **blocked** | no exerciser tool carries a `_meta.ui.resourceUri` (`ui://...`) at all — `mcp_apps.py::_resource_uri` (~104-108) would return `''` for all 9 tools, so no MCP App would ever admit. Full citation of what an arm would need (resource shape, `mimeType`, the `protocol_version: "2026-01-26"` literals at `mcp_apps.py:446`/`:626`) is recorded in the avenue's own evidence for C1-S3 to build against, failing-first. |

## On avenue 1's forbidden-explicit design (why not a hard -32021/-32022 assertion)

`forbidden_echo` is a normal, non-elicit, non-task PLAIN tool
(`_forbidden_echo` just returns `f"forbidden:{payload}"`); its ONLY special
property is `Tool.execution = ToolExecution(task_support="forbidden")` —
metadata saying the SERVER refuses a `tasks/create` against it, not that a
PLAIN `tools/call` refuses. `test_declared_path_plain_tools_work_on_a_task_
capable_server` (`test_mcp_v2_conformance.py`) already proves whole-namespace
routing calls a task-capable namespace's PLAIN tools through the direct
client via an ordinary `tools/call`, not `tasks/create` — so a well-behaved
client should just call `forbidden_echo` plainly and it should SUCCEED, never
refuse. Hard-asserting a `-32021`/`-32022` refusal would presuppose a
mechanism (the client forcing task mode on an explicitly-forbidden tool) this
package found no evidence for. The avenue instead asserts what the
`#1275`/C1-S2 fix actually guarantees — terminal-fast, NEVER a 15-minute
spin — and records whatever the live run actually observes (success or a
typed refusal) as evidence either way. That is the honest test of the
concern in play, not a guess dressed as a fixed expectation.

## Exerciser gaps found (for C1-S3..S5 slice scoping)

- **mrtr-url**: needs a new tool (e.g. `url_guarded_input`) whose
  `InputRequiredResult` carries `mcp_types.ElicitRequestURLParams` instead of
  `_one_elicit`'s form params.
- **mrtr-methods**: needs at least one `@server.resource`/`@server.prompt`
  handler with an MRTR-capable `InputRequiredResult` path.
- **cache**: needs a tool returning a result annotated with a cache hint
  (`ttlMs`/`cacheScope` per the 2026-07-28 spec).
- **list-changed**: needs a tool that mutates the server's own tool registry
  at runtime and fires `notifications/tools/list_changed`.
- **adversarial**: needs a hand-rolled ASGI app (bypassing fastmcp's own
  protocol correctness) emitting deliberately MUST-violating frames,
  servable stand-alone as a second declared MCP server.
- **apps-ui**: needs a tool declaring `_meta.ui.resourceUri = "ui://..."`
  plus a matching `@server.resource("ui://...")` handler returning
  `text/html;profile=mcp-app` content (see the avenue's own evidence for the
  exact admission requirements, cited against `mcp_apps.py`).
- **headers (partial)**: even with the new capture server, `mcp-param-*`
  mirroring (B3) needs a tool with an ANNOTATED header-worthy param —
  neither the capture tool nor any exerciser tool declares one today.

None of these were added to `tests/` (existing-file rule) — each is recorded
here, grounded, as a finding for a future slice.
