# clio-agent 0.5.1 → 0.5.3 — interface change summary (TUI / Desktop / Web)

For the gact-tui team. This is everything in clio-agent **0.5.2** and **0.5.3** that
affects the TUI / Desktop / Web clients, with the concrete contract and the UI action
for each. Use it to scope the matching interface release.

Legend: 🟥 breaking/behavior change to adapt to · 🟩 new capability to surface ·
🟦 fixed (a previously-broken UI surface now works) · ⚙️ deploy/config.

---

## 0. TL;DR — what the UI must do

1. **File preview**: read `Content-Type` from `/files/read` — images now come back as
   real binary (`image/png`), not `text/plain`. (#673/#676)
2. **Streaming**: a turn may now finish with **no token deltas** (blocking path) on
   purpose; treat a final assistant message without prior deltas as success. New
   `stream_fallback` reasons explain why. (#639 + 0.5.2)
3. **MCP reconnect** works again: `POST /v1/mcp/servers/{id}/reconnect` (+ new error
   codes/events). (#636)
4. **Expert packs** have a lifecycle: `/v1/expert-packs/install|update|delete`, and every
   blueprint/pack row now carries a `kind` (`blueprint`|`pack`). (#663)
5. **File edits**: `fs_propose_edit` now produces `file_diff` parts + pending `/diffs`
   rows — render/apply them. (#674)
6. ⚙️ Set `CLIO_GACT_CORS_ORIGINS` for the web/desktop origin so SSE works in a browser. (#675)

---

## 1. Released in 0.5.2

### 1.1 🟥 Streaming is no longer guaranteed per turn
clio added a config-gated bypass that routes some turns through a **blocking** (no live
token deltas) path — the answer arrives as one final assistant message. This happens for:
- reasoning models whose provider streams the answer on the `reasoning_content` channel
  (content-only stream listeners can't fold it), and
- when guided/structured output or the streaming bypass is enabled.

**Contract**: the assistant part metadata carries a `stream_source` (`live` | `batch`) and,
when not streamed live, a structured `stream_fallback` reason. The full audited reason set
(GET surfaces it under `x_clio_stream_fallback_reasons`):
`stream_disabled_live_streaming`, `stream_disabled_guided_output`,
`provider_streaming_unsupported`, `streaming_dependency_unavailable`, `agent_not_available`,
`agent_not_streamable`, `stream_setup_failed`, `stream_failed_before_output`,
`stream_no_prediction`, `stream_completed_without_chunks`, `sync_execution_path`,
`dynamic_prompt_stream_unavailable`, `dynamic_tool_stream_unavailable`.

**UI action**: do not treat "no deltas" as a hang/failure. Optionally render a subtle
"responded without live streaming" hint from the reason.

### 1.2 🟩 Unified semantic trace / ARC (S0–S7)
One canonical, full, live, durable semantic-event stream per turn (capture vs. projection
split; ARC is now a live projection). SSE `semantic.event` payloads stay **redacted**
(projection); the durable file trace carries full detail. New/enriched event types include
per-child `expert.response.completed` (correlated via `parent_span_id`), `turn.completed`
carrying the final message, and richer `llm.*`/`tool.call.*` spans.

**UI action**: none required (SSE projection shape is backward-compatible); optionally
consume the richer correlated events for a better trace/inspector view. Durable trace is
opt-in via `CLIO_SEMANTIC_TRACE_BACKEND=file` / `CLIO_SEMANTIC_TRACE_PATH`.

---

## 2. Released in 0.5.3 (branch `fix/0.5.3-tui-backend`) — the TUI-filed fixes

### 2.1 🟥🟦 #673 / #676 — workspace file read returns real binary content-types
`GET /v1/workspaces/{id}/files/read?path=…` previously decoded **all** files as
`text/plain; charset=utf-8`, corrupting binary bytes (UTF-8 replacement chars) so image
previews were undecodable.
- **Now**: text files → `text/plain` (decoded, as before). Binary files (detected by
  extension + content sniff) → **raw bytes** with the real content type (`image/png`, else
  `application/octet-stream`). `Content-Length` equals the on-disk size.
- **UI action**: branch on `Content-Type`. Render `image/*` from the raw bytes (object
  URL); keep the text path for `text/*`.

### 2.2 🟥🟦 #639 — Argonne reasoning models route to the blocking path
ALCF/Argonne reasoning models (e.g. nemotron) stream their answer on the `reasoning_content`
channel, which broke live streaming (TaskGroup error / empty output). They now run the
robust blocking path and report `stream_fallback = provider_streaming_unsupported`.
Non-reasoning ALCF models (gpt-oss/gemma) still stream; LM Studio reasoning models (qwopus)
unchanged.
- **UI action**: same as §1.1 — a no-delta turn from these models is normal.

### 2.3 🟦 #649 — blueprint display name
The default-registry blueprint no longer shows "… Agent Blueprint **Blueprint**". No action;
expect the corrected catalog title.

### 2.4 🟦 #636 — `POST /v1/mcp/servers/{id}/reconnect` restored
The reconnect route (dropped in the registry merge) is back. It re-probes the stored
stdio/http transport spec, re-lists tools, updates the registry row in place, and is
non-destructive (no permission prompt).
- **Responses**: `200` `{id,name,status:"ready",transport,tools_count,tools,spec}` on
  success; `404` unknown/bundled server; `422` `mcp_spec_invalid` (malformed stored spec);
  `502` `upstream_unavailable` (probe failed); `504` `mcp_reconnect_timeout`.
- **Events** (global, `session_id=""`): `mcp.server.reconnected` on success,
  `mcp.server.error` on failure/timeout.
- **Config**: probe timeout via `CLIO_GACT_MCP_RECONNECT_TIMEOUT_S` (default 15s).
- **UI action**: wire the Reconnect action to this route; handle the new status codes/events.

### 2.5 🟩 #663 — expert-pack lifecycle + `kind` discriminator
Expert packs and agent blueprints now share **one** install/update/delete engine.
- **New routes** (thin aliases of the agent-blueprint lifecycle, same request/response
  shapes, provenance, and structured errors):
  - `POST /v1/expert-packs/install` (body: `source`|`url`|`path`, `scope`,
    `workspace_id`, optional `ref`/`pinned_commit`)
  - `POST /v1/expert-packs/{pack_id}/update`
  - `DELETE /v1/expert-packs/{pack_id}?scope=&workspace_id=`
- **New field**: every blueprint/pack wire row now carries `kind`:
  - `"blueprint"` = structured workflow with a root orchestrator (`root_expert` set)
  - `"pack"` = loose collection of experts (no orchestrator root)
- **UI action**: render/filter by `kind`; you may keep using `/v1/agent-blueprints/*` or the
  new `/v1/expert-packs/*` aliases interchangeably. (Note: true multi-root loose packs aren't
  installable yet — single-root packs work.)

### 2.6 🟦 #674 — `fs_propose_edit` now materializes diffs
When a dynamic tool agent calls `fs_propose_edit`, clio now promotes the result into a
`file_diff` part on the assistant message **and** a pending row in
`GET /v1/sessions/{sid}/diffs` (and emits `artifact.proposed`). Previously the proposal was
invisible to the diff/apply UI.
- `file_diff` part fields: `path`, `unified_diff`, `new_content`, `edit_mode`,
  `lines_added`, `lines_removed`, `status:"pending"`.
- Apply/reject via the existing `POST /v1/sessions/{sid}/diffs/apply|reject`.
- **UI action**: render the diff and the review/apply flow for tool-agent edits. (Caveat:
  the assistant prose may still say "updated" even though it's a proposal — rely on the
  `file_diff`/`pending` state as the source of truth, not the prose.)

### 2.7 ✅ #635 — global SSE fanout (already in 0.5.2)
`EventBus.publish(session_id="")` already fans global lifecycle events out to per-session
subscribers; the UI sees MCP/provider status on the session stream. No backend change —
**issue can be closed**.

### 2.8 ⚙️ #675 — browser CORS for SSE
`CORSMiddleware` is wired; set `CLIO_GACT_CORS_ORIGINS` (comma-separated origins, or `*`) on
the backend so a pure-web `EventSource` to `/v1/sessions/{sid}/events` works. Deploy/UI-config
action, not a code gap.

### 2.9 ⏸️ #672 — `_UnsupportedSessionAgent` in the isolated gate
Deploy/config (CLIO_KIT_PATH / blueprint install in the isolated config), not a backend bug
— pending close confirmation with the team.

---

## 3. Not in 0.5.3 scope (future — see `docs/TUI_ADAPTATION_0.5.x.md`)
The new per-provider tuning knobs (live streaming, guided output, reasoning, GPU/flash
attention, watchdog liveness) are **env-only** today and are NOT yet settable via
`PUT /v1/providers/lm`. Surfacing them in the providers menu needs a backend step
(extend `LMProviderRequest`) first — deferred past 0.5.3 (alignment-only).

---

## 4. Endpoint/field quick reference

| Area | Change | Where |
|---|---|---|
| File read | binary content-types | `GET /v1/workspaces/{id}/files/read` |
| Streaming | `stream_source` + `stream_fallback` reasons | assistant part metadata |
| MCP | reconnect route + codes/events | `POST /v1/mcp/servers/{id}/reconnect` |
| Packs | install/update/delete + `kind` | `/v1/expert-packs/*`, all blueprint rows |
| Diffs | tool-agent `file_diff` parts | assistant parts + `GET /v1/sessions/{id}/diffs` |
| CORS | browser SSE | `CLIO_GACT_CORS_ORIGINS` (env) |
