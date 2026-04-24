# docs/tui — TUI frontend integration reference

This folder is the spec and reference for building a first-class terminal UI on top of CLIO. It documents CLIO's external surface end-to-end from the perspective of a frontend integrator, and ends with a concrete phased plan for connecting [gact-tui](https://github.com/JaimeCernuda/gact-tui) — a Bubbletea-based TUI with a pluggable backend contract (GACT v0.1) — as CLIO's primary interactive UI.

**North-star**: CLIO is the gold standard of compatibility. Everything CLIO can do, the TUI must be able to surface. The GACT contract evolves to match CLIO's semantics, not the other way around.

## Reading order

| # | Doc | When to read |
|---|---|---|
| 01 | [Overview](01-overview.md) | What CLIO is, who it's for, 30-second mental model |
| 02 | [Agent graph](02-agent-graph.md) | How a single turn flows end-to-end |
| 03 | [Experts](03-experts.md) | What an Expert is, current roster, routing |
| 04 | [ARC memory](04-arc-memory.md) | Persistent memory layout + what to surface in the TUI |
| 05 | [Tools](05-tools.md) | The FastMCP gateway + every tool catalogued |
| 06 | [Endpoints](06-endpoints.md) | CLI + REST API + MCP surface |
| 07 | [Providers + config](07-providers-config.md) | LM providers matrix + env-var reference |
| 08 | [Semantics + lifecycle](08-semantics-and-lifecycle.md) | Behavioural pins from the test suite |
| 09 | [Integration plan](09-integration-plan.md) | How gact-tui actually hooks in |

## TL;DR for somebody building the adapter

- CLIO ships **`clio-agent-api`** — FastAPI server on `:8000` with `POST /query`, `GET /health`, `GET /experts`, `GET /metrics`. That's the TUI's main interface.
- One turn = `POST /query {question, session_id}` → `{answer, selected_expert, duration_ms, error_info}`. Add `stream: true` for an SSE feed with `routing` / `chunk` / `done` events.
- Routing is deterministic-first (filename heuristics) with a DSPy LM router fallback; selects one of `data` / `analysis` / `visualization` / `chat` / `none`.
- CLIO doesn't issue `session_id`s — the adapter/TUI owns them.
- Cancellation, per-tool SSE events, and token streaming are **not** available today. Plan to fall back to post-hoc rendering, and upstream these as Phase 4 of the integration (see [09-integration-plan.md](09-integration-plan.md)).

## Provider path for dev without paying for API credits

DSPy's default Anthropic provider expects an API key. For local development on Claude Max subscriptions, route through **[Meridian](https://github.com/rynfar/meridian)** — a proxy that bridges Anthropic's official SDK (OAuth) to an OpenAI-compatible endpoint. CLIO then treats Claude Max like any custom `openai`-compatible backend:

```sh
# Launch meridian (see its own README for OAuth setup).
meridian serve --port 4141 &

# Point CLIO at it.
export CLIO_LM_PROVIDER=openai
export CLIO_LM_API_BASE=http://127.0.0.1:4141/v1
export CLIO_LM_API_KEY=any-placeholder   # meridian handles real auth
export CLIO_LM_MODEL=claude-sonnet-4-5
```

Already proven with Crush / OpenCode / Aider / Cline per Meridian's own README — same pattern applies here. See [07-providers-config.md](07-providers-config.md#provider-path-for-claude-max-via-meridian) for the TUI-side knobs.

## What this folder does NOT cover

- SIMBA optimiser internals — enough in [08-semantics-and-lifecycle.md](08-semantics-and-lifecycle.md) for integration; deeper context in [`../SELF_IMPROVEMENT.md`](../SELF_IMPROVEMENT.md).
- DSPy signatures in detail — treat as implementation detail per [`../../CLAUDE.md`](../../CLAUDE.md) Rule 3.
- IOWarp CTE storage tiers — automatic; not the TUI's concern.

## How these docs were produced

A systematic read-through of `src/clio_agent/` (agent + experts + ARC + tools), `docs/` (architecture + expert-system + memory), and `tests/` (behavioural pins). Citations give exact file paths and line ranges so the next person doesn't have to re-derive anything.

Produced from the gact-tui side during scoping; now lives here as the authoritative integration spec. Tracked against the `develop` branch; updates land via PRs referencing the TUI integration issue.
