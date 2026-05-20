# GAPS - GACT / CLIO Coverage Map

Bookkeeping document for TUI-facing capability truth. Runtime truth comes
from `GET /v1/capabilities`, the tests under `tests/test_gact/`, and the
end-to-end caveats in `REAL_GAPS.md`.

## Currently Unsupported

These are intentionally advertised as `false` by `/v1/capabilities`:

| Capability | Reason |
|---|---|
| `lsp` | CLIO is a scientific-data agent, not a code editor. LSP servers are out of scope. |
| `voice` | Voice IO needs platform audio, transcription, and playback plumbing outside the current CLIO runtime. |

## Implemented and Advertised

These capabilities are now implemented on the CLIO GACT surface and are
advertised as `true`. Some remain partial in the sense described in
`REAL_GAPS.md`, especially when a real `ClioAgent` does not yet emit the
same rich event stream as the smoke/fake agents.

| Capability | Current runtime semantics |
|---|---|
| `agent_routing` | One-pass planner selects tool, expert:data, expert:analysis, expert:visualization, answer/chat, or no-action/error. |
| `structured_errors` | Planner, provider, expert, and cancellation failures surface as structured `error_info`. |
| `diffs` | `file_diff` parts can be applied or rejected through `/v1/sessions/{sid}/diffs/*`; writes use the shared file-policy path. |
| `permissions` | Permission rows/events exist; destructive tool/write paths are audited and can be denied. |
| `cancellation` | `/v1/sessions/{sid}/cancel` is best-effort. The GACT envelope settles as cancelled; executor-thread provider/tool work may continue and is flagged. |
| `tool_telemetry` | `tool.call.started/completed` events are emitted only by live tool observers. `metadata.tools_called` may still summarize post-turn provenance, but it is not reconstructed into lifecycle events. |
| `cost_tracking` | Per-session token/cost fields exist and are populated from DSPy history where available. |
| `subagents` | Real expert nanoagent spawns propagate through `ClioAgent`, GACT child sessions, `subagent.*` events, and ARC invocation records. |
| `session_branching` | Session fork endpoints copy conversation state for alternate analysis paths. |
| `search_messages` | Message search endpoint returns stored conversation matches. |
| `files` | Workspace/context-file endpoints exist with workspace-root checks. |
| `plan_mode` | `session.mode=plan` blocks destructive writes. |
| `edit_modes` | `session.edit_mode` controls diff/whole/patch file-diff shape. |
| `scheduled_sessions` | Cron-style schedules can fire stored backend commands. |
| `session_sharing` | Share tokens expose read-only shared session views. |
| `agent_write` | User and skill agent definitions can be created, listed, updated, deleted, persisted, and executed with prompts, optional provider/model metadata, and declared MCP tool lists. |
| `skills_extraction` | Past-session tool usage can be mined into a user agent definition. Extracted agents execute with prompt-only or declared-tool semantics. |

## Partial / Real-Driver Gaps

Use `REAL_GAPS.md` for the honest "does a real CLIO turn drive this?"
audit. In short:

- Streaming can be live for supported chat paths and provider-backed
  expert synthesis. Deterministic tool-result summaries can still emit
  `stream_source="synthetic_posthoc"` because no provider tokens exist
  to stream.
- Cancellation is truthful but best-effort; Python executor work may keep
  running after the GACT envelope settles.
- Some capabilities have correct endpoints/events but limited real-agent
  drivers.
- User, skill, and extracted agents can execute when selected by a
  session, either prompt-only or through a tool-scoped ReAct runner for
  declared MCP tools.

## Maintenance Rules

- When a new gap is discovered, file a GitHub issue framed around CLIO's
  mission and link it from the relevant doc.
- When runtime behavior changes, update this file in the same PR or file a
  docs issue immediately.
- Cross-check this file against `/v1/capabilities`, `tests/test_gact/`, and
  `REAL_GAPS.md` before declaring a release-ready capability set.
