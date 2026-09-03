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
- `tests/test_tools/mcp_exerciser.py` — the v2ex server: 12 tools
  (`task_echo`, `task_optional_echo`, `plain_echo`, `forbidden_echo`,
  `guarded_input`, `plain_guarded_input`, `staller`, `plain_staller`,
  `silent_sleeper`, `ui_echo` (C1-S3, #1283, bound to the `ui://v2ex/panel`
  resource), and — since C1-S4 (#1284) — `url_guarded_input`/
  `plain_url_guarded_input`, the url-mode MRTR arm); one MRTR-capable
  `@server.prompt` (`guarded_prompt`) and `@server.resource`
  (`guarded_resource`, `res://v2ex/guarded`); a synthetic, non-built-in
  `ServerExtension` (`x-clio-agent/exerciser-echo`); no cache/listChanged
  arms — see each blocked avenue below for the exact citation.
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
  to it), exposing every exerciser tool on one `main` expert so avenues 1, 2,
  5, and 11 can share a single pack/session.
- `leg_c2_v2_avenues.py` — the runner.

## How to run

```bash
# Print the avenue plan (id/needs_lm/expected outcome) and exit. Boots nothing.
uv run python scripts/live_verification/leg_c2_v2_avenues.py --dry-run

# Boot the server, materialize+install+activate the pack, run every headless
# avenue (3,4,6,7,8,9,10) AND the readiness gate; avenues 1, 2, 5, and 11 are
# recorded status="blocked" (reason: "plumbing-only run"). Zero LM spend.
uv run python scripts/live_verification/leg_c2_v2_avenues.py --plumbing-only

# Full run: everything above PLUS avenues 1 (task-modes), 2 (mrtr-url), 5
# (waits-cancel), and 11 (apps-ui) through directed claude_code/sonnet turns
# on one session.
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
| 2 | mrtr-url | Yes | **pass** (C1-S4, #1284, landed) | the exerciser gained `url_guarded_input` (task=required) and `plain_url_guarded_input`, whose `InputRequiredResult` embeds a genuine `mcp_types.ElicitRequestURLParams` (`_one_url_elicit`) instead of `_one_elicit`'s form params. The avenue drives `v2ex_url_guarded_input` through a real turn (url-mode elicitation is only wired through the production tool-call path, `agents/builders.py::make_elicitation_client` — never the REST-install lane, so this needs a model in the loop like avenues 1/5/11) and asserts the resulting question's `metadata.elicitation` carries the FULL url + the `punycode_warning`/`punycode_host` fields (build item 3, `gact/elicitation_schema.py::build_url_metadata`/`punycode_warning`). Its readiness-gate plumbing is live-verified in `--plumbing-only` mode (recorded `blocked`, reason "plumbing-only run"); the LM-driven assertion itself awaits a live run under owner go-ahead. |
| 3 | mrtr-methods | No | **pass** (C1-S4, #1284, landed) | the exerciser gained an MRTR-capable `guarded_prompt`/`guarded_resource` (mirroring `guarded_input`'s one-round shape). The DECLARED session/turn surface still cannot reach them (finding (b) below is architectural, unaffected by this landing) — so the avenue drives them via the REST-install lane instead (`POST /v1/mcp/servers` + `POST .../prompts/get`, a bare `make_mcp_client(transport, server_id=sid)` with NO elicitation handler wired, `gact/routes/mcp.py::_external_mcp_inventory`): `prompts/get` genuinely dispatches the SDK's MRTR loop and fails typed + terminal-fast (a 502 `upstream_error` citing "Elicitation not supported" — never a hang); `resources/read` genuinely has NO REST route in this repo (a LIVE 404, not assumed from source — `gact/routes/mcp.py` only lists resources). Headless, LM-free, live-verified in `--plumbing-only` mode. The FULL round-trip (asked → answered → terminal) for both methods, through a properly elicitation-wired client on BOTH the direct and proxy routes, is proven in `tests/test_tools/test_mcp_v2_conformance.py` — the house pattern for per-path MRTR verification. |
| 4 | cache | No | **blocked** | repo-wide grep for `cache_ttl`/`cache_scope`/`CacheConfig`/`cache_hint` found zero hits inside `src/clio_agent` or `tests/test_tools` — no client support and no exerciser arm exist yet. C1-S5 territory (`docs/design/mcp-client-unification-2026-08.md` line ~62-63). |
| 5 | waits-cancel | Yes | **pass** | `staller` is `task=required` with a 50ms poll interval; `gact/mcp_task_events.py::publish_mcp_task_wait` fires `mcp_task.wait` on the session's SSE bus while it runs (transient, live-only — a NEW `_sse_collector.py` subscribes DURING the call to observe it); `POST /v1/sessions/{sid}/cancel` then ends the turn `cancelled` per `gact/routes/session_cancellation.py::cancel_session_state`. |
| 6 | pagination | No | **pass (indirect)** | no `list_page_size`/page-size control exists anywhere in `clio_agent` (repo-wide grep: zero MCP-tools/list-paging-related hits) — this leg cannot FORCE multi-page traversal. It instead reuses the readiness gate: all 11 declared exerciser tools resolving onto the agent's toolset is indirect proof that whatever paging fastmcp's `Client.list_tools()` did internally (SDK-covered per obligations doc row B1) worked correctly. |
| 7 | list-changed | No | **blocked** | the exerciser's tool set is fixed at server-build time — no tool adds/removes a tool or fires `notifications/tools/list_changed`. Repo-wide grep for `listChanged`/`list_changed`: only unrelated hits. C1-S5 territory. |
| 8 | extensions | No | **pass** (C1-S3, #1283, landed) | `gact/routes/mcp_rows.py::handshake_server_row` now surfaces the recorded server-declared extension SET directly (`"extensions"` field, `None` when genuinely unobserved — never conflated with a real empty list, `"extensions_era"` alongside it). The avenue asserts the v2ex handshake row's `extensions` contains BOTH the well-known tasks id AND the exerciser's synthetic, non-built-in `x-clio-agent/exerciser-echo` id — proving the read side is generic, not a tasks/ui shortlist. Headless (the handshake was already fetched for the readiness gate); live-verified in `--plumbing-only` mode. |
| 9 | adversarial | No | **blocked** | no standalone MUST-violating raw-responder/ASGI-shim fixture exists anywhere in this repo (searched `tests/test_tools/*` for "raw responder"/"ASGI shim": zero hits). The C1-S0 slice built well-behaved fixtures only. |
| 10 | headers | No | **genuinely probed live** | a NEW `_header_capture_server.py` is booted as a real subprocess and probed via `POST /v1/mcp/servers` + `POST /v1/mcp/servers/{sid}/call`. An isolated, out-of-band smoke test (outside this repo, not part of any run this package makes) already showed a BARE `fastmcp.Client` sends `mcp-method`/`mcp-name`/`mcp-protocol-version` headers on a real `tools/call` today — B2 is "library-covered" exactly as the obligations doc's own B2 row says, with ZERO clio_agent code involved (grepped `src/clio_agent` for `Mcp-Method`/`Mcp-Param`: no hits). `mcp-param-*` mirroring (B3) is a SEPARATE, still-open question this probe tool cannot exercise either way (see the avenue's own evidence note — B3 only mirrors ANNOTATED header-worthy params, and no tool anywhere declares one). Do not assume this avenue's number pre-decides pass/fail — read the live verdict. |
| 11 | apps-ui | Yes | **pass** (C1-S3, #1283, landed) | the exerciser now carries a real ui-serving arm: `ui_echo` (`_meta.ui.resourceUri` bound to `ui://v2ex/panel`, built with fastmcp's native `fastmcp.apps` support) + a matching `@server.resource` handler serving `text/html;profile=mcp-app`. The avenue drives `v2ex_ui_echo` through a real turn and asserts an `mcp_app` Part is minted on the persisted message stream (`_append_live_assistant_part`, `mcp_apps.py:434-448` — the SAME single-writer transcript ledger a real assistant reply uses, not the SSE-only transient path avenue 5 rides) AND `GET /v1/sessions/{sid}/mcp-apps/{app_id}` (`mcp_apps.py:617-637`) actually serves the resource. Needs a model in the loop (unlike avenue 8) — its readiness-gate plumbing (the tool resolves onto the `v2ex-avenues` pack) is live-verified in `--plumbing-only` mode; the LM-driven assertion itself awaits a live run under owner go-ahead, matching this package's existing posture. |

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

## Exerciser gaps found (for C1-S5 slice scoping)

- **cache**: needs a tool returning a result annotated with a cache hint
  (`ttlMs`/`cacheScope` per the 2026-07-28 spec).
- **list-changed**: needs a tool that mutates the server's own tool registry
  at runtime and fires `notifications/tools/list_changed`.
- **adversarial**: needs a hand-rolled ASGI app (bypassing fastmcp's own
  protocol correctness) emitting deliberately MUST-violating frames,
  servable stand-alone as a second declared MCP server.
- **headers (partial)**: even with the new capture server, `mcp-param-*`
  mirroring (B3) needs a tool with an ANNOTATED header-worthy param —
  neither the capture tool nor any exerciser tool declares one today.

**Closed since this list was written:** **extensions** and **apps-ui** both
landed in C1-S3 (#1283) — the exerciser gained a synthetic `ServerExtension`
(`x-clio-agent/exerciser-echo`) and a `ui_echo`/`ui://v2ex/panel` pair;
`gact/routes/mcp_rows.py::handshake_server_row` now surfaces the recorded
extension set. **mrtr-url** and **mrtr-methods** both landed in C1-S4
(#1284) — the exerciser gained `url_guarded_input`/`plain_url_guarded_input`
(a genuine `ElicitRequestURLParams` arm) and `guarded_prompt`/
`guarded_resource` (MRTR-capable prompt/resource). All four avenues are REAL
assertions now (rows 2/3/8/11 above), not blocked findings.

**Still architectural, not an exerciser gap:** mrtr-methods finding (b) is
unaffected by C1-S4 — the DECLARED session/turn surface still exposes only an
expert's frontmatter `tools:` list; prompts/resources reach a session only
through the REST-install lane (`gact/routes/mcp.py`), which has a
`prompts/get` route but genuinely NO `resources/read` route at all (live-
confirmed by a 404 in the avenue's own evidence, not assumed from source).

None of these were added to `tests/` (existing-file rule) — each is recorded
here, grounded, as a finding for a future slice.
