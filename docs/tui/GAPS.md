# GAPS — v0.1 ↔ CLIO coverage map

Bookkeeping document. Each gap corresponds to a native-merit GitHub issue on this repo that describes the capability as it benefits CLIO's mission. Rows include the issue number + which downstream TUI-integration PLAN item each unblocks (tracked in [gact-tui's PLAN.md](https://github.com/iowarp/gact-tui/blob/clio/PLAN.md), phase `CLIO-BBBBBBBBBB`).

This document is **bookkeeping only** — the canonical artefacts are the issues themselves. If an entry here diverges from an issue's scope, the issue wins. If a row here is missing an issue reference, it means we haven't filed one yet.

## Capabilities v0.1 exposes that CLIO doesn't implement today

| Issue | Capability | TUI-integration item unblocked |
|---|---|---|
| [#2](https://github.com/iowarp/clio-agent/issues/2) | Granular tool execution telemetry events over SSE | `CLIO-BBBBBBBBBB16` — per-tool telemetry |
| [#3](https://github.com/iowarp/clio-agent/issues/3) | Cooperative cancellation for long-running analysis turns | `CLIO-BBBBBBBBBB18` — cancellation |
| [#4](https://github.com/iowarp/clio-agent/issues/4) | Two-phase edit workflow for reversible file changes | `capabilities.diffs` (v0.1) |
| [#5](https://github.com/iowarp/clio-agent/issues/5) | User-managed session context files | `capabilities.files` (v0.1) |
| [#6](https://github.com/iowarp/clio-agent/issues/6) | Real token-level streaming from the LM through to SSE | `CLIO-BBBBBBBBBB17` — real token streaming |
| [#7](https://github.com/iowarp/clio-agent/issues/7) | Interactive safety gate for destructive data operations | `capabilities.permissions` (v0.1) |
| [#8](https://github.com/iowarp/clio-agent/issues/8) | Per-session cost + token tracking | `capabilities.cost_tracking` (v0.1) |
| [#9](https://github.com/iowarp/clio-agent/issues/9) | Parallel sub-task execution via Nanoagents | `capabilities.subagents` (v0.1) |
| [#10](https://github.com/iowarp/clio-agent/issues/10) | Session forks for exploring alternate analysis paths | `capabilities.session_branching` (v0.1) |
| [#11](https://github.com/iowarp/clio-agent/issues/11) | Full-text message search across conversation history | `capabilities.search_messages` (v0.1) |

## v0.2 additions CLIO supports natively

The v0.2 bump in the GACT contract ([gact-tui/contract/SPEC.md](https://github.com/iowarp/gact-tui/blob/clio/contract/SPEC.md)) promotes several CLIO-native primitives to first-class. CLIO implements them by default — no new issue needed on this side:

| v0.2 capability | CLIO's native implementation |
|---|---|
| `agent_routing` | Tier-1 → Tier-2 experts (DataExpert / AnalysisExpert / VisualizationExpert / chat) via RouterSignature |
| `memory` | ARC cache + session context retrieval surface |
| `structured_errors` | `ClioError` hierarchy (ProviderError / RoutingError / ExpertError / ToolError / ConfigError) |
| `integration_health` | CLIO's `/health` integrations array (lm / gateway / arc / file_policy / clio_core) |
| `tool_telemetry` | `Invocation.tools_called[].{cached, duration_ms}` already stored in ARC |

The work on the CLIO side for these is limited to *exposing them* on the new `/v1/*` routes (the underlying data is already there). Tracked by the CLIO-BBBBBBBBBB4–9 PLAN items in gact-tui (Phase 1: smoke path).

## v0.1 capabilities intentionally unsupported in CLIO

Some v0.1 surface doesn't map to CLIO's mission and will stay declared `unsupported` via `/v1/capabilities`:

- `lsp` (§6.8) — CLIO isn't a code editor; LSP servers are out of scope.
- `voice` (§6.14) — out of scope.
- `scheduled_sessions` (§6.15) — CLIO runs interactively; cron-schedule is a deployment concern, not an agent concern.
- `session_sharing` — multi-user sharing is a deployment-layer concern (auth, ACLs) not part of the agent core.
- `edit_modes` (Aider-style) — CLIO's experts already specialise; a mode switch would be redundant.
- `plan_mode` (Gemini-style) — CLIO's ReAct loop already separates Thought from Action; a dedicated read-only mode isn't a natural fit.

These stay `false` in CLIO's capabilities response; the TUI hides UI for them when driving CLIO.

## How this file stays accurate

- When a new gap is discovered, file an issue framed around CLIO's mission + add a row here pointing at it.
- When an issue closes, strike through the row (don't delete — keeps the trail intact).
- When v0.3+ lands on the spec side, add a section above "v0.2 additions CLIO supports natively" summarising what the new version covers.
- Cross-check against [gact-tui's PLAN.md](https://github.com/iowarp/gact-tui/blob/clio/PLAN.md) on each iteration.
