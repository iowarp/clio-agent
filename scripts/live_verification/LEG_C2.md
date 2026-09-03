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
- `tests/test_tools/mcp_exerciser.py` — the v2ex server: 16 tools
  (`task_echo`, `task_optional_echo`, `plain_echo`, `forbidden_echo`,
  `guarded_input`, `plain_guarded_input`, `staller`, `plain_staller`,
  `silent_sleeper`, `ui_echo` (C1-S3, #1283, bound to the `ui://v2ex/panel`
  resource); since C1-S4 (#1284) — `url_guarded_input`/
  `plain_url_guarded_input`, the url-mode MRTR arm, plus (Opus review
  addendum) `url_guarded_input_idn`, the IDN/`xn--` counterpart that proves
  `punycode_warning=True`; and since C1-S5, #1285 — `header_annotated_echo`,
  `invalid_header_echo` (deliberately x-mcp-header-INVALID, dropped by the
  SDK client on list), `list_changed_target`, `mutate_and_notify_list_changed`,
  plus optional `cache_ttl`/`cache_scope` constructor params); one
  MRTR-capable `@server.prompt` (`guarded_prompt`) and `@server.resource`
  (`guarded_resource`, `res://v2ex/guarded`, C1-S4); a synthetic, non-built-in
  `ServerExtension` (`x-clio-agent/exerciser-echo`) — see the avenue table
  below for what each tool proves.
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
| 2 | mrtr-url | Yes | **pass** (C1-S4, #1284, landed; extended Opus review addendum) | the exerciser gained `url_guarded_input`/`plain_url_guarded_input` (plain-ASCII origin) and, per the review addendum, `url_guarded_input_idn` (an `xn--` ACE-encoded IDN origin) — all `task=required`, all embedding a genuine `mcp_types.ElicitRequestURLParams` (`_one_url_elicit`) instead of `_one_elicit`'s form params. The avenue drives BOTH `v2ex_url_guarded_input` and `v2ex_url_guarded_input_idn`, in order, through one real turn (url-mode elicitation is only wired through the production tool-call path, `agents/builders.py::make_elicitation_client` — never the REST-install lane, so this needs a model in the loop like avenues 1/5/11) and asserts EACH question's `metadata.elicitation` carries the FULL url + the `punycode_warning`/`punycode_host`/`punycode_host_raw` fields (build item 3 + the B5 homograph fix, `gact/elicitation_schema.py::build_url_metadata`/`punycode_warning`) — critically, `punycode_warning` must be `False` on the ASCII arm AND `True` on the IDN arm: a leg that only ever drove the always-false ASCII tool could never prove the warning branch fires at all. Its readiness-gate plumbing is live-verified in `--plumbing-only` mode (recorded `blocked`, reason "plumbing-only run"); the LM-driven assertion itself awaits a live run under owner go-ahead. |
| 3 | mrtr-methods | No | **pass** (C1-S4, #1284, landed; re-shaped Opus review B1/B2/B3) | the exerciser gained an MRTR-capable `guarded_prompt`/`guarded_resource` (mirroring `guarded_input`'s one-round shape). The DECLARED session/turn surface still cannot reach them (finding (b) below is architectural, unaffected by this landing) — so the avenue drives them via the REST-install lane instead (`POST /v1/mcp/servers` + `POST .../prompts/get`, a bare `make_mcp_client(transport, server_id=sid)` with NO elicitation handler wired, `gact/routes/mcp.py::_external_mcp_inventory`). **`pass` keys ONLY on `prompts/get`**: a typed, terminal-fast 502 `upstream_error` (checked structurally — `error.error == "upstream_error"` plus the JSON-RPC `-32600` code, never the free-text "Elicitation not supported" prose, which a reword could silently flip false — B2). **`resources/read` is INFORMATIONAL ONLY** (B1): this repo currently has no REST route for it (a LIVE 404, not assumed from source), but a future route existing would be a GOOD change — feature absence is NEVER a pass criterion either way; only a genuine hang/untyped status there would fail this avenue. The verdict JSON carries a `pass_means` string spelling out exactly what `pass` does and does not prove (B3), so it cannot be misread as "MRTR-on-prompts/resources works end to end" — that full round-trip (asked → answered → terminal), for both methods, on BOTH the direct and proxy routes, is proven separately (offline) in `tests/test_tools/test_mcp_v2_conformance.py` — the house pattern for per-path MRTR verification. Headless, LM-free, live-verified in `--plumbing-only` mode. |
| 4 | cache | No | **pass** (C1-S5, #1285, landed) | `tools/mcp_runtime.py::make_mcp_client` opts execution-path clients INTO SEP-2549 response caching when `response_cache_enabled()` is true (config `tools.mcp.response_cache_enabled` / env `CLIO_MCP_RESPONSE_CACHE_ENABLED`, default `False` — deliberately opt-in, see that function's docstring); `mcp_exerciser.py::build_exerciser_server` accepts `cache_ttl`/`cache_scope` (fastmcp applies the hint server-wide, there is no per-tool knob). The avenue asserts a hinted server's second `tools/list` is served from cache (a recording store proves exactly one `set`, not two). |
| 5 | waits-cancel | Yes | **pass** | `staller` is `task=required` with a 50ms poll interval; `gact/mcp_task_events.py::publish_mcp_task_wait` fires `mcp_task.wait` on the session's SSE bus while it runs (transient, live-only — a NEW `_sse_collector.py` subscribes DURING the call to observe it); `POST /v1/sessions/{sid}/cancel` then ends the turn `cancelled` per `gact/routes/session_cancellation.py::cancel_session_state`. |
| 6 | pagination | No | **pass (indirect)** | no `list_page_size`/page-size control exists anywhere in `clio_agent` (repo-wide grep: zero MCP-tools/list-paging-related hits) — this leg cannot FORCE multi-page traversal. It instead reuses the readiness gate: all 16 declared exerciser tools resolving onto the agent's toolset is indirect proof that whatever paging fastmcp's `Client.list_tools()` did internally (SDK-covered per obligations doc row B1) worked correctly. |
| 7 | list-changed | No | **pass** (C1-S5, #1285, landed) | the exerciser's `mutate_and_notify_list_changed` hides `list_changed_target` via fastmcp's own `ctx.disable_components` (a REAL registry mutation firing an unsolicited `notifications/tools/list_changed`); `tools/mcp_listen.py::list_changed_message_handler` invalidates `tools/listing_cache.py` on receipt. Uses the message_handler path, not the spec-correct `subscriptions/listen` (`watch_list_changed`): fastmcp's SERVER implements ZERO `subscriptions/listen` support (live-verified `-32601 Method not found`, reconfirmed unchanged across the b1->b5 bump, pinned as a regression lock in `tests/test_tools/test_mcp_listen.py`) — a fastmcp-specific gap, not a protocol-wide one. |
| 8 | extensions | No | **pass** (C1-S3, #1283, landed) | `gact/routes/mcp_rows.py::handshake_server_row` now surfaces the recorded server-declared extension SET directly (`"extensions"` field, `None` when genuinely unobserved — never conflated with a real empty list, `"extensions_era"` alongside it). The avenue asserts the v2ex handshake row's `extensions` contains BOTH the well-known tasks id AND the exerciser's synthetic, non-built-in `x-clio-agent/exerciser-echo` id — proving the read side is generic, not a tasks/ui shortlist. Headless (the handshake was already fetched for the readiness gate); live-verified in `--plumbing-only` mode. |
| 9 | adversarial | No | **pass** (C1-S5, #1285, landed) | `mcp_adversarial_fixture.py` wraps a real fastmcp app in a pure ASGI middleware that short-circuits four requests with hand-built malformed JSON-RPC frames (bad `resultType`, `-32021` with no `requiredCapabilities`, always-`-32020`, empty-string pagination cursor). The avenue asserts clio's typed handling of each — including a VERIFIED fastmcp CLIENT bug: `Client.list_tools()`'s `if not result.next_cursor: break` treats an empty-string cursor as terminal even though E10 says only null/missing ends pagination; pinned as a finding, not a clio defect (clio never implements its own pagination). |
| 10 | headers | No | **pass** (C1-S5, #1285, landed) | a NEW `_header_capture_server.py` is booted as a real subprocess and probed via `POST /v1/mcp/servers` + `POST /v1/mcp/servers/{sid}/call`. B2 (`mcp-method`/`mcp-name`/`mcp-protocol-version`) was already confirmed library-covered with zero clio_agent code involved. B3 (`mcp-param-*` mirroring) is now genuinely exercised: the capture server gained `probe_with_header` (an `x-mcp-header`-annotated `Trace-Id` param) and the exerciser gained `header_annotated_echo` + a deliberately INVALID `invalid_header_echo` (proving the SDK's own `_absorb_tool_listing` drops it) — the avenue asserts the mirrored `Mcp-Param-Trace-Id` header's VALUE, not just presence. |
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

All gaps this list originally named are CLOSED as of C1-S5 (#1285): C1-S4
(#1284) closed mrtr-url and mrtr-methods; C1-S5 closed cache, list-changed,
adversarial, and headers (partial, B3). See "Closed since this list was
written" below for what each landing actually asserts.

**Closed since this list was written:**

- **extensions** and **apps-ui** both landed in C1-S3 (#1283) — the exerciser
  gained a synthetic `ServerExtension` (`x-clio-agent/exerciser-echo`) and a
  `ui_echo`/`ui://v2ex/panel` pair; `gact/routes/mcp_rows.py::
  handshake_server_row` now surfaces the recorded extension set. Both
  avenues are REAL assertions now (rows 8/11 above), not blocked findings.
- **mrtr-url** and **mrtr-methods** both landed in C1-S4 (#1284) — the
  exerciser gained `url_guarded_input`/`plain_url_guarded_input` (a genuine
  `ElicitRequestURLParams` arm) and `guarded_prompt`/`guarded_resource`
  (MRTR-capable prompt/resource). Both avenues are REAL assertions now (rows
  2/3 above), not blocked findings. **Still architectural, not an exerciser
  gap:** mrtr-methods finding (b) is unaffected by C1-S4 — the DECLARED
  session/turn surface still exposes only an expert's frontmatter `tools:`
  list; prompts/resources reach a session only through the REST-install lane
  (`gact/routes/mcp.py`), which has a `prompts/get` route but genuinely NO
  `resources/read` route at all (live-confirmed by a 404 in the avenue's own
  evidence, not assumed from source).
- **cache**, **list-changed**, **adversarial**, and **headers (partial, B3)**
  all landed in C1-S5 (#1285) — see rows 4, 7, 9, 10 above for what each
  avenue now asserts. `list-changed` uncovered that fastmcp's SERVER has zero
  `subscriptions/listen` support (a library gap, pinned as a regression
  lock, reconfirmed unchanged across the b1->b5 bump); `adversarial`
  uncovered a genuine fastmcp CLIENT pagination bug (empty-string cursor
  treated as terminal, also reconfirmed unchanged on b5). Both are
  documented findings, not clio defects, since clio owns neither the fastmcp
  server nor its client-side pagination loop.

No avenues remain blocked after C1-S4 + C1-S5: all eleven have a REAL
pass/fail assertion -- seven (mrtr-methods, cache, pagination, list-changed,
extensions, adversarial, headers) are headless and already live-verified in
`--plumbing-only` mode; four (task-modes, mrtr-url, waits-cancel, apps-ui)
need a model in the loop for their LM-driven half, which awaits a live run
under owner go-ahead per this package's existing posture.
