# TUI / Web / Desktop adaptation guide — clio-agent 0.5.2 → 0.5.3

Audience: the `gact-tui` (TUI + web + desktop) team. This is the backend-side view
of what changed and what the UI needs to do to align with the latest clio-agent
release. Two parts: **(A)** contract changes to adapt to, and **(B)** the new
configuration surface the providers menu should expose (with the backend work that
must land first).

Status legend: ✅ shipped in clio-agent · 🛠️ backend work needed in 0.5.3 · 🎨 UI work.

---

## A. Backend contract changes to adapt to (already shipped)

1. **Workspace file read now returns binary content-types** ✅ (#673, #676)
   `GET /v1/workspaces/{id}/files/read?path=...` no longer forces
   `text/plain; charset=utf-8`. Text files are still served decoded as
   `text/plain`; **binary files (PNG, etc.) are served as raw bytes with their
   real content type** (`image/png`, else `application/octet-stream`).
   - 🎨 The preview rail should branch on the response `Content-Type`: render
     `image/*` as an image (raw bytes / object URL), `text/*` as code. Do **not**
     assume `text/plain` anymore. `Content-Length` now equals the on-disk size.

2. **Not every turn streams live deltas** ✅ (#639 + 0.5.2 streaming work)
   Reasoning models on some providers now run the **blocking** path (no token
   deltas) on purpose — their answer arrives in one assistant message at the end.
   New audited `stream_fallback` reasons the UI may observe (all benign, surfaced
   on the assistant part metadata / `x_clio_stream_fallback_reasons`):
   - `stream_disabled_live_streaming` — operator disabled streaming via config.
   - `stream_disabled_guided_output` — guided/structured output is on.
   - `provider_streaming_unsupported` — e.g. an Argonne reasoning model routed to
     the blocking path (answer comes on the reasoning channel).
   - 🎨 Treat a turn that produces a final assistant message **without** prior
     deltas as success, not a hang. Reason metadata can drive a small "responded
     without live streaming" hint if desired.

3. **Default-registry blueprint display name fixed** ✅ (#649)
   No longer "… Agent Blueprint **Blueprint**". No UI action; just expect the
   corrected catalog title.

4. **Global lifecycle events already fan out to per-session subscribers** ✅ (#635)
   `EventBus.publish(Event(session_id="", ...))` delivers to all open per-session
   SSE subscribers (MCP/provider lifecycle status). No backend change needed —
   the UI can rely on seeing global events on the session stream. (Issue can be
   closed.)

5. **Browser CORS for SSE is supported via config** ✅ (#675)
   `CORSMiddleware` is wired; set `CLIO_GACT_CORS_ORIGINS` (comma-separated
   origins, or `*`) on the backend and `EventSource` against
   `/v1/sessions/{sid}/events` works from a pure-web client.
   - 🎨/deploy: launch the backend with `CLIO_GACT_CORS_ORIGINS` set to the web
     app origin(s). This is a deploy/UI-config adaptation, not a code gap.

---

## B. New configuration surface for the providers menu

The user-facing goal: the providers menu should expose the new tuning knobs
(streaming on/off, guided output, GPU/flash-attention, reasoning, watchdog/liveness).

### What `PUT /v1/providers/lm` accepts today (`LMProviderRequest`)
`provider, api_base, model, api_key, temperature, max_tokens, top_p, top_k,
min_p, presence_penalty, context_length, parallel, turn_timeout_s, transport,
thinking_budget`

### New knobs that exist but are ENV-only (not in the PUT body yet) 🛠️
To expose these in the menu, the backend must first add them to `LMProviderRequest`
and apply them per-provider (today they only read process env / conf):

| Knob | Env / conf key | Meaning |
|------|----------------|---------|
| Live streaming | `CLIO_LIVE_STREAMING` / `runtime.live_streaming` (default ON) | Off → robust blocking path; useful for reasoning models whose provider streams on the reasoning channel. |
| Guided/structured output | `CLIO_LM_GUIDED_OUTPUT` / `lm.guided_output` (default OFF) | Constrain generation to the typed schema (vs. text protocol). |
| Reasoning-model flag | `CLIO_LM_REASONING_MODEL` | Force/disable reasoning-model handling (content←reasoning_content recovery, parse re-sample, stop-sequences). Normally auto from handshake `is_reasoning`. |
| Parse re-sample attempts | `CLIO_LM_PARSE_RETRY_ATTEMPTS` | Bounded re-draws on an unparseable typed output (reasoning models). |
| Extract repair attempts | `CLIO_EXTRACT_REPAIR_ATTEMPTS` | Re-run only the typed extraction over the retained trajectory (no full agentic retry). |
| Token liveness | `CLIO_LM_TOKEN_LIVENESS` (default ON) | Stream-token heartbeat keeps the no-progress watchdog alive on long reasoning calls. |
| Inter-token idle | `CLIO_LM_INTER_TOKEN_IDLE_S` (default 120) | Idle-between-tokens ceiling for the watchdog. |
| Flash attention (LM Studio) | `CLIO_LMSTUDIO_FLASH_ATTENTION` | LM Studio load param. |
| Router/planner temps | `CLIO_LM_ROUTER_TEMPERATURE`, `CLIO_LM_PLANNER_TEMPERATURE` | Separate temps for routing/planning vs. generation. |

GPU offload / KV-cache offload: LM Studio's load-time params. Today only
flash-attention is plumbed; deeper LM Studio load knobs (n_gpu_layers, kv offload,
batch) are **not** yet exposed by the backend — see backlog #35/#36 and provider
handshake tuning (#652).

### Server-level config (not per-provider; surface in a settings panel)
| Knob | Env key | Meaning |
|------|---------|---------|
| Trace backend | `CLIO_SEMANTIC_TRACE_BACKEND` (`file`/`none`) | Durable semantic trace on/off. |
| Trace path | `CLIO_SEMANTIC_TRACE_PATH` | Where the per-session JSONL trace is written. |
| Trace detail | `CLIO_SEMANTIC_TRACE_DETAIL` | SSE projection detail level. |
| CORS origins | `CLIO_GACT_CORS_ORIGINS` | Browser origins allowed for web/desktop. |
| Turn timeout | `CLIO_GACT_TURN_TIMEOUT_S` | Per-turn no-progress ceiling. |

### Recommended split
- 🛠️ **0.5.3 backend:** extend `LMProviderRequest` (+ `GET /v1/providers/lm`
  echo) with the per-provider knobs above so they're settable/observable via API,
  persisted on `app.state.lm_config`, and applied at bind. Then:
- 🎨 **TUI/web/desktop:** add menu controls bound to those fields; render binary
  previews by content-type; treat no-delta turns as success.

---

## Remaining backend bugs (0.5.3 candidates, not yet done)
- 🛠️ **#636** — restore `POST /v1/mcp/servers/{id}/reconnect` (route is currently
  missing) + re-probe stored stdio/http specs + emit `mcp.server.reconnected` /
  `mcp.server.error` + port the reconnect tests.
- 🛠️ **#674** — `fs_propose_edit` tool-agent turns don't materialize a `file_diff`
  part / `/v1/sessions/{id}/diffs`, and may claim a file was updated (larger;
  needs the tool-agent diff pipeline).
- ⏸️ **#672** — `_UnsupportedSessionAgent` in the isolated TUI gate: deploy/config
  (CLIO_KIT_PATH / blueprint install in the isolated config), likely to be closed.
- 📋 **#663** — expert-pack install/update/delete endpoints (API feature).
- 📋 **#642** — SSE replay vs. live hydration contract for stable session revisits
  (design; partially mitigated by the authoritative `GET /v1/sessions/{id}`).

## Done in 0.5.3 so far (branch `fix/0.5.3-tui-backend`)
- #673/#676 binary file read, #649 blueprint name, #639 Argonne reasoning streaming.
