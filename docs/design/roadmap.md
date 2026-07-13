# ARC live context plane — roadmap beyond v1

v1 (the single-node *logical* plane) is DONE and green on real ALCF: the DSPy ReAct
loop reads its prompt FROM ARC each iteration; the four ops + per-expert 90%
auto-compaction + ARC/Trace separation are proven (byte-equality, mutation-propagation,
needle-in-haystack, real provider-driven compaction). See `GOAL.md`,
`docs/archive/arc-live-context-plane.md`, `docs/archive/implementation-spec.md`.

This file captures the larger arc we scoped early but the v1 GOAL did not — written as a
dependency graph of threads, not a phased timeline. Threads marked *(independent)* can run
in parallel with the rest.

## Thread B — clio-core backend via an `ARCStore` factory  *(the core idea / seam #2)*

The `ARCStore` protocol (`put/get/exists/scan/delete/clear`) is already the swap seam; v1
uses `LocalFSStore`. Introduce a **factory** resolved from config:
`make_arc_store(config) -> clio-core (default, gold standard) | LocalFSStore (files) |
<future>`. Targets **clio-core** (IOWarp CTE, `pip install iowarp-core`, `clio_cee`) as the
canonical backend so the live plane runs on distributed, tiered storage — the original
"ARC ⇄ CTE" idea. Unblocks Threads D and E. Keep files as a config fallback (and the unit
default) so nothing requires a clio-core daemon to test.

## Thread A — expose ARC context over the REST API for the TUI  *(independent)*

Make the live plane observable + controllable in gact-tui:
- **GET** context state per session/scope: % of window used, live segment (block) count,
  per-kind/per-scope **token categorization** (already have `segment_tokens_by_kind`), and
  the current render.
- **POST** context operations: manual compact/`summarize`/`delete`/`insert` (the `apply`
  surface) so a human can shape an agent's context live.
- **Stream** `arc.op` events (already emitted) so the TUI shows insertions/deletions/
  compactions as they happen.
This turns ARC from an invisible substrate into a first-class, inspectable surface.

## Thread D — connect the clio-core CEE MCP  *(needs B)*

Once clio-core is the backend, expose/consume the CEE MCP (`iowarp-cei-mcp`) as the agent's
context + retrieval tool surface over clio-core: semantic (BM25) + temporal + tag queries become
discovery primitives. This is how an expert finds another expert's published context.

## Thread C — enrich testing  *(independent, ongoing)*

Server-based real-case trace-audit (replay the durable JSONL, reconstruct ARC@each LM call,
assert == the captured prompt); an exhaustive parallel needle/edit battery (positions,
partial deletes, as-of-T time-travel, needle-survives-compaction); multi-turn sessions;
concurrent-writer/as-of-T; more providers/models.

## Thread E — the distributed team + the physical KV plane  *(the far horizon, needs B/D)*

The team-of-agents vision from early in the design:
- **Cross-agent shared plane**: agent-scope as a blackboard; `poll_telemetry_log` change-feed
  for async pickup ("one produces, another finds it days later"); as-of-T semantics for
  concurrent shared-scope writers.
- **Physical KV plane**: the KV-surgery backend behind the stable `apply(op, scope)`
  interface (clio-core `context-transfer-engine/llm-hooks/kvcache`) so mid-context edits
  (delete/insert/summarize) avoid a recompute storm — a backend swap, the student's work.

## Suggested lead

**B** (clio-core backend) is the highest-leverage next move — it's the core idea, it
connects the clio-core work we explored first, and it unblocks D and E. **A** (REST/TUI
exposure) is the best *parallel* companion: independent, immediately useful, and it makes B
and everything after it visible.
