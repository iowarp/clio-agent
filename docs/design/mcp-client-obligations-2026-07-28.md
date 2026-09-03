# MCP 2026-07-28 — complete client-side obligations checklist (research record, 2026-08-29)

Companion to iowarp/clio-agent#1274 (comments carry the decision summaries; this file preserves the
full per-item table). PIN FACT (corrected vs the sweep's stale premise): clio-agent develop is
ALREADY on fastmcp/fastmcp-tasks 4.0.0b1 + mcp 2.0.0 (uv.lock-verified) — the 2026-07-28-capable
line; upstream fastmcp is at 4.0.0b5. Library slice = beta-track hardening b1→b5, not a 3.x migration.
"LIBRARY-GAP→v4" rows below therefore read as ALREADY-COVERED-ON-OUR-PIN unless b1 predates the item.

Pre-covered by other campaign work: MRTR (SEP-2322), MCP Apps (SEP-1865, host exists at rev
2026-01-26 in gact/mcp_apps.py), formal extensions framework (the registry gap is the campaign's core).

| # | Item | Client obligation | Spec status | fastmcp coverage | Classification |
|---|------|-------------------|-------------|------------------|----------------|
| A1 | Per-request `_meta` protocol fields | MUST send protocolVersion + clientCapabilities on EVERY request; SHOULD clientInfo; missing ⇒ -32602/400 | Required (SEP-2575) | v4 | library-covered on pin |
| A2 | `resultType` handling | absent ⇒ "complete"; unrecognized ⇒ invalid; "input_required" ⇒ MRTR | Required (SEP-2322) | v4 | library-covered; never break |
| A3 | `server/discover` | server MUST implement; client MAY call; stdio SHOULD use as dual-era probe | Required-to-handle | v4 mode="auto" | library-covered |
| A4 | -32022 version retry | retry with a version from data.supported | Required | v4 | library-covered |
| A5 | Dual-era compat | probe + cache era per server/origin; fallback MUST NOT key on one error code; legacy HTTP+SSE endpoint-event fallback | Required if supporting old servers | v4 auto/legacy; ClientGroup (b5) per-server | library-covered; our config must not force one era |
| A6 | New error codes | handle -32020 HeaderMismatch, -32021 MissingRequiredClientCapability (data.requiredCapabilities), -32022; never emit reserved codes | Required | v4 types | covered; our typed-rejection mapping ours-to-not-break |
| A7 | JSON Schema 2020-12 | default dialect; graceful error on unsupported; MUST NOT auto-deref network $ref (opt-in allowlist); bound composition cost | Required (SEP-2106) | pydantic/jsonschema | verified: clio never implements its own JSON-Schema resolver/validator (repo-wide grep, pinned `test_mcp_verification_probe_shortlist.py::test_a7_...`) — satisfied by construction, not an explicit refusal check |
| A8 | OTel trace context in `_meta` | traceparent/tracestate/baggage reserved keys | Optional (SEP-414) | on by default | library-covered |
| A9 | Statelessness discipline | stdio process lifetime not scoped to one conversation; state via explicit handles only | Required posture | structural in v4 | OURS (client pooling must not assume connection=session) |
| B1 | POST + Accept | every message own POST; Accept both json + event-stream | Required | v4 | library-covered |
| B2 | Standard headers | MCP-Protocol-Version, Mcp-Method, Mcp-Name (tools/call, resources/read, prompts/get); Base64 sentinel for non-ASCII | Required (SEP-2243) | v4 | verify sentinel encoding |
| B3 | `x-mcp-header` | mirror annotated params into Mcp-Param-{name}; MUST drop tools with invalid values from tools/list; re-list-and-retry on HeaderMismatch | Required on HTTP | v4 (mirroring/dropping are SDK `ClientSession` behavior; verified by reading `mcp/client/session.py`, not duplicated) | done (#1285 C1-S5 item 1): mirroring + invalid-tool dropping exercised end to end (`test_mcp_header_family.py`, live-verification avenue 10 headers) and re-list-and-retry on -32020 is CLIO's own new `tools/mcp_header_mismatch.py::call_tool_with_header_retry`, bounded to exactly one retry (also proven against a hostile always-refusing server, `test_mcp_adversarial.py`) |
| B4 | No sessions | no Mcp-Session-Id; list results not per-connection | Removed (SEP-2567) | v4 sessionless | delete any session-id plumbing of ours |
| B5 | No SSE resumability | broken stream ⇒ re-issue as NEW request id; Last-Event-ID gone | Removed (SEP-2575) | v4 | OUR ladders must re-issue, never resume; verified (zero resumption_token/on_resumption_token hits in clio_agent, `test_mcp_verification_probe_shortlist.py::test_b5_...`) |
| B6 | SSE keep-alive comments | ignore `:` lines | Required | parser | library-covered |
| C1 | stdio framing/hygiene | NDJSON; nothing non-MCP to stdin; stderr ≠ errors | Required | covered | library-covered |
| C2 | Shutdown ladder | stdin close → wait → SIGTERM → SIGKILL | Required | covered | library-covered |
| C3 | Crash restart | SHOULD restart, retry in-flight, re-establish subscriptions/listen | Required posture | v4 b3 reconnect | wiring policy OURS |
| C4 | stdio cancellation | notifications/cancelled (HTTP: closing stream IS cancel) | Required | SDK | library-covered |
| D1 | subscriptions/listen | replaces GET stream + resources/subscribe; filters (tools/prompts/resourcesListChanged, resourceSubscriptions); check acknowledged filter; subscriptionId correlation on stdio; MUST re-subscribe after stdio reconnect | Optional feature, required shape | v4 method (client side); resources/updated still UNVERIFIED (clio never subscribes to resources — no resource-subscription surface exists) | tools-filter half DONE (#1285 C1-S5 item 2): `tools/mcp_listen.py::watch_list_changed` drives the raw SDK `mcp.client.subscriptions.listen(session, tools_list_changed=True)` (fastmcp's `Client` has no `.listen()` of its own) → `listing_cache.invalidate_namespace`; **verified library gap**: fastmcp 4.0.0b1's SERVER implements zero `subscriptions/listen` support (`-32601`, pinned in `test_mcp_listen.py`), so `list_changed_message_handler` covers today's real fastmcp fleet via the legacy unsolicited-notification path while `watch_list_changed` stays the spec-correct client for servers that do implement SEP-2575 |
| D2 | listChanged handling | invalidate + re-fetch | Optional | handler hooks | DONE (#1285 C1-S5 item 2): wired into `tools/listing_cache.py` via both `watch_list_changed` and `list_changed_message_handler`; live-verified (leg_c2 avenue "list-changed") |
| E1 | tools/list + call | core; deterministic ordering; name collisions ⇒ prefix-disambiguate (serverInfo.name NOT unique) | Required | covered; ClientGroup namespacing | our namespace dispatch: check vs spec text |
| E2 | Tool annotations | untrusted unless server trusted | Required trust rule | passthrough | OURS (trust policy/consent) |
| E3 | outputSchema/structuredContent | SHOULD validate; structuredContent may be ANY JSON value | Optional-strength | .data hydration 2.10+; v4 | library-covered |
| E4 | isError routing | tool-execution errors SHOULD reach the LLM for self-correction | Required distinction | ToolError | routing OURS |
| E5 | Content types | text/image/audio/resource_link/embedded resource + annotations | Required | covered | rendering OURS |
| E6 | Resources | -32602 not-found (MUST also accept legacy -32002); https resources may be client-fetched | Required if used | covered; legacy-code acceptance UNVERIFIED | verify |
| E7 | Resource templates | templates/list + RFC 6570 expansion client-side | Optional | list covered | expansion OURS if surfaced |
| E8 | Prompts | list/get, user-controlled invocation intent | Optional | covered | surfacing OURS |
| E9 | Completions | completion/complete; SHOULD debounce + cache | Optional | client.complete() | library-covered; clio never surfaces it (no @server.prompt/argument-completion UI anywhere), confirmed (`test_mcp_verification_probe_shortlist.py::test_e9_...`) |
| E10 | Pagination | cursors opaque; EMPTY STRING is a valid cursor (only null/missing ends) | Required rule | v4 auto-paginate | library-covered |
| E11 | Cacheable results (SEP-2549) | ttlMs + cacheScope on discover/list/read; key by method+params; MUST NOT cache MRTR-retry results; "private" scope per authorization context; invalidate on notifications; no TTL polling without jitter | Required fields | v4 respects hints; pluggable store | DONE (#1285 C1-S5 item 3): `tools/mcp_runtime.py::make_mcp_client` opts execution-path clients into the SDK's own `mcp.client.caching` mechanism via `response_cache_enabled()` (config `tools.mcp.response_cache_enabled`, default `False` — opt-in); the MUST-NOTs are structural (only list/read methods are ever cacheable — `tools/call` and any MRTR retry are excluded by construction; fastmcp's own eviction wrapper invalidates on listChanged/resourceUpdated), not something the factory enforces. Live-verified (leg_c2 avenue "cache"; `test_mcp_response_cache.py`) — separate from and in addition to `tools/mcp_listen.py`'s push-invalidation of the boot listing cache |
| E12 | Icons (SEP-973) | optional render; if rendering: png/jpeg MUST, svg/webp SHOULD; https/data: only, no credentials, same-origin, magic bytes, size caps | Optional | passthrough | OURS (host UI) |
| F1 | Elicitation capability | declare elicitation {form,url} per-request; ≥1 mode | Required shape | v4 both modes | library-covered |
| F2 | Form-mode features | flat primitive schemas; SEP-1034 defaults pre-populate; SEP-1330 multi-select enums; validate responses; user review before send | Required if form | dataclass conversion; enums/defaults UNVERIFIED | verify |
| F3 | URL-mode consent | MUST NOT pre-fetch; MUST NOT open w/o explicit consent; show FULL URL; open in uninspectable surface; punycode warning; manual retry/cancel (client learns outcome only by retrying); elicitationId + completion notification REMOVED | Required | plumbing v4 | consent surface OURS |
| F4 | Three-action model | accept/decline/cancel distinct | Required | ElicitResult | library-covered |
| F5 | Server-identity UI | make clear WHICH server asks | Required | — | OURS |
| F6 | Roots | DEPRECATED (SEP-2577); if kept: MRTR-delivered; roots/list_changed REMOVED | Deprecated | covered | do not deepen; pinned zero-hit (#1285 C1-S5 item 5, `test_mcp_era_gated_removals.py`) |
| F7 | Sampling (+tools) | DEPRECATED; we never implemented — MUST NOT add | Deprecated | full handler exists | owner-ruled: absent stays absent |
| F8 | Logging | DEPRECATED; per-request io.modelcontextprotocol/logLevel in _meta replaces logging/setLevel | Deprecated | legacy handler; per-request replacement mechanism itself still UNVERIFIED (only the deprecated `logging/setLevel`/`session.set_logging_level` call surface is pinned zero-hit) | era-gate half DONE (#1285 C1-S5 item 5, `test_mcp_era_gated_removals.py`); migrate to stderr/OTel |
| F9 | Ping | REMOVED from 2026-07-28 core | Removed | legacy client.ping | remove modern-era ping assumptions (OURS); pinned zero-hit (#1285 C1-S5 item 5, `test_mcp_era_gated_removals.py`) |
| G1 | Progress | unique progressToken; monotonic; MAY reset timeout clock w/ max cap | Optional | per-call handler | ladder OURS (owner-ruled in #1274) |
| G2 | Timeouts | per-request, configurable; timeout ⇒ cancel | Required posture | per-call timeout | library-covered |
| G3 | Cancellation | HTTP: close stream; stdio: notification; ignore late responses; handle races | Required | SDK | library-covered |
| H1 | PRM discovery | WWW-Authenticate on 401; fallback well-known order | Required w/ auth | covered (bug history #2104/#2419) | caveats |
| H2 | AS metadata discovery | RFC 8414 AND OIDC; priority order; issuer==URL check | Required | 8414 covered; order+issuer UNVERIFIED | verify |
| H3 | OAuth 2.1 + PKCE | PKCE S256; REFUSE if code_challenge_methods_supported absent | Required | S256 covered; refusal NOT implemented | VERIFIED SDK GAP (#1285 C1-S5): the installed SDK unconditionally sends `code_challenge_method: S256` and never reads the AS's `code_challenge_methods_supported` — pinned as a finding, not silently assumed compliant (`test_mcp_verification_probe_shortlist.py::test_h3_...`); implementing the refusal needs intercepting the SDK's own OAuth flow, out of scope for a smalls item |
| H4 | Resource indicators | RFC 8707 resource param on authz AND token requests | Required | present (path bug #2419) | verified fixed on our pin (b1): `resource` rides auth/token/refresh requests (`test_mcp_verification_probe_shortlist.py::test_h4_...`) |
| H5 | iss validation (SEP-2468) | record expected issuer; exact-compare before code redemption; on mismatch act on NOTHING | Required (new) | v4.0.0b1 | library-covered; confirmed (`test_mcp_verification_probe_shortlist.py::test_h5_...`) |
| H6 | CIMD | https client_id URL + hosted metadata doc; check *_supported | SHOULD (recommended) | since v3.0; private_key_jwt fixed 3.4.7 | HOSTING the doc is OURS (deployment artifact, cluster-parameterized) — DONE (#1285 C1-S5 item 5): `docs/deploy/CIMD.md`, wired through the existing `MCPAuthConfig.client_metadata_url` code hook (already library-covered; only the deployment-side artifact was missing) |
| H7 | DCR | DEPRECATED; if used send application_type (SEP-837) | Deprecated | covered; SEP-837 in v4 | library-covered |
| H8 | Credential↔issuer binding (SEP-2352) | key stored credentials by AS issuer; never reuse across ASes; priority pre-reg→CIMD→DCR→prompt | Required | storage backends exist; issuer-keying confirmed (`credentials_match_issuer`, `test_mcp_verification_probe_shortlist.py::test_h8_...`) | verified; storage policy OURS (`tools/mcp_oauth_storage.py::DurableFileTokenStorage`, #1285 item 5 — keyed by server_url, the identity available at construction time; H8's ideal per-AS-issuer keying needs metadata discovery that happens later) |
| H9 | Scope + step-up | challenge scope authoritative; 403 insufficient_scope ⇒ union step-up, retry-limited | Required | SEP-2350 in v4 | library-covered |
| H10 | Token hygiene | bearer header only; per-server AS binding; secure storage; localhost/https redirects; state param | Required | covered | storage encryption config OURS |
| I1-I3 | Host consent | human-in-loop tool consent, data-privacy consent, prompt-injection posture for annotations | Required posture (spec MUSTs) | — | OURS (permission_gate exists; extend to new surfaces; UI lane inherits as requirements) |
| J1 | OAuth Client Credentials ext | io.modelcontextprotocol/oauth-client-credentials (M2M; secret or JWT assertion) | Optional official ext | flows achievable; declaration UNVERIFIED | relevant to headless/CI runs |
| J2 | Enterprise-Managed Authorization ext | ID-JAG token exchange, org config | Optional official ext | not in fastmcp | only if enterprise demands |
| J3 | Skills over MCP | working group, not in registry | Incubating | v3 server-side provider | watch |

Verification-probe shortlist (sweep's UNVERIFIED items) — CLOSED by #1285 C1-S5 (items 1/3/5,
`test_mcp_header_family.py` + `test_mcp_verification_probe_shortlist.py`): A7 network-$ref
refusal (satisfied by construction); B3 header emission + invalid-tool rejection; H3 PKCE-absence
refusal (VERIFIED GAP, pinned as a finding, not fixed — out of scope for a smalls item); H4
resource indicators; H5 iss validation; H8 credential↔issuer binding; B5 SSE
re-issue-never-resume; E9 completions non-use — all pinned as regression-locking tests, not
one-off manual checks. STILL OPEN (untouched by C1-S5, a future slice's scope): B2 sentinel
encoding (Base64 for non-ASCII header values specifically — B2's core header presence IS
live-verified); D1 resources/updated (no resource-subscription surface exists to probe; the
tools-listChanged half of D1 is done, see that row); E6 legacy -32002 acceptance; F2
enums/defaults; F8's per-request logLevel replacement mechanism itself (only the deprecated
call surface is pinned absent); H2 AS metadata discovery order+issuer.

Sources: modelcontextprotocol.io 2026-07-28 spec (changelog, transports, patterns, features,
authorization, deprecated registry), extensions registry, SEP-2243/2322/2352/2468/2549/2567/2575/
2577/1034/1330/973/837/414, gofastmcp.com docs + PrefectHQ/fastmcp releases v4.0.0b1–b5.
