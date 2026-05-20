# docs/tui — TUI frontend integration reference

This folder is the spec and reference for building a first-class terminal UI on top of CLIO. It documents CLIO's external surface end-to-end from the perspective of a frontend integrator, and ends with a concrete phased plan for connecting [gact-tui](https://github.com/iowarp/gact-tui) — a Bubbletea-based TUI with a pluggable backend contract (GACT v0.1) — as CLIO's primary interactive UI.

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

- CLIO ships **`clio-agent-gact`**, a FastAPI backend that speaks the native GACT `/v1/...` contract used by `gact-tui`.
- The legacy **`clio-agent-api`** surface still exists with `POST /query`, `GET /health`, `GET /experts`, and `GET /metrics`, but it is not the primary TUI integration path.
- One GACT turn = `POST /v1/sessions/{sid}/messages`; the request acks quickly and progress streams over `GET /v1/sessions/{sid}/events` as `message.created`, `message.part.*`, tool telemetry when available, and `message.completed`.
- Routing is a one-pass DSPy planner over live tools and registered experts; it selects a tool, `expert:data|analysis|visualization`, `answer`/chat, or an explicit `none` route.
- CLIO's GACT backend owns sessions through `/v1/sessions`.
- Cancellation is available as best effort through `POST /v1/sessions/{sid}/cancel`.
- Text deltas carry explicit stream provenance. `stream_source="live"` means the text came through the live DSPy/LiteLLM streaming path; `stream_source="synthetic_posthoc"` means the backend chunked a completed answer for rendering continuity. Expert paths can still be synthetic; see [REAL_GAPS.md](REAL_GAPS.md).

## What this folder does NOT cover

- SIMBA optimiser internals — enough in [08-semantics-and-lifecycle.md](08-semantics-and-lifecycle.md) for integration; deeper context in [`../SELF_IMPROVEMENT.md`](../SELF_IMPROVEMENT.md).
- DSPy signatures in detail — treat as implementation detail per [`../../CLAUDE.md`](../../CLAUDE.md) Rule 3.
- IOWarp CTE storage tiers — automatic; not the TUI's concern.

## How these docs were produced

A systematic read-through of `src/clio_agent/` (agent + experts + ARC + tools), `docs/` (architecture + expert-system + memory), and `tests/` (behavioural pins). Citations give exact file paths and line ranges so the next person doesn't have to re-derive anything.

Produced from the gact-tui side during scoping; now lives here as the authoritative integration spec. Tracked against the `develop` branch; updates land via PRs referencing the TUI integration issue.
