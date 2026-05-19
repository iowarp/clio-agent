# Changelog

All notable changes to clio-agent's GACT-contract surface are
documented in this file. Internal changes that don't affect the
TUI/HTTP surface aren't tracked here.

## [0.3.1] — 2026-04-27

The "every advertised capability actually works" release. Every flag
in `/v1/capabilities` that's `true` has been verified end-to-end with
either an integration test, a curl trace, or a screenshot — see
`docs/CAPABILITIES_MATRIX.md` for the full matrix. **No silent
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
- `docs/CAPABILITIES_MATRIX.md` — one row per advertised capability with
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
- **Test infra reliability.** `tests/test_integration_v0_2/conftest.py`
  httpx client timeout 30s → 90s to absorb proxy tail latency.

### Performance
- Full integration_v0_2 suite is 16/16 strict in ~95s (was ~25min before
  the streaming + adapter fixes earlier in this release cycle).

### Removed
- All `@pytest.mark.xfail` decorators in
  `tests/test_integration_v0_2/test_real_capabilities.py`. Suite is
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
- 5 of 16 integration tests in `tests/test_integration_v0_2/` were
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

(Full details in PLAN-CLIO-BBBBBBBBBB items #1–#23.)
