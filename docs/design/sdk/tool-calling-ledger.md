# Tool calling ledger: exploration, disclosure, `run_code` and the session plan

Rows follow [tool-calling-plan.md](tool-calling-plan.md). Status values are the ones [acceptance-ledger.md](acceptance-ledger.md) defines (`passed (live)`, `tests only`, `static`, `done`, `open`, `blocked`), plus `local phase` for a row the plan assigns to the local qualification pass (open models, a real HA-MCP server, native tool search live, the Tauri desktop, Windows and HPC).

Decisions use the prefix `TD`, baseline tasks `B` and qualification scenarios `T`.

## Decisions

| ID | Decision | Source | Evidence | Status |
| --- | --- | --- | --- | --- |
| TD1 | Qualification runs in two passes: a cloud phase on Claude only (`claude_code`, text tool engine), then a local phase for open models, HA-MCP, live native tool search, Tauri, Windows and HPC. | Planning review 2026-10-30 | Plan section "Two phases of qualification" | done |
| TD2 | Decisions are `TD`, baselines `B`, scenarios `T`; the draft plan used `T` for both decisions and scenarios. | Planning review 2026-10-30 | This ledger | done |
| TD3 | `fs_read_file` reads `offset=1`, `limit=2000` lines by default. | Plan defaults | | open |
| TD4 | `fs_search_text` defaults to `mode="files"`. | Plan defaults | | open |
| TD5 | `explore` runs on the fleet `explore` role, the caller's model when unset. | Plan defaults | | open |
| TD6 | A deferred server's prompt entry is its name, its MCP `instructions` (otherwise the plugin's declared `description`) and its tool names, with no docstrings or schemas. `plugin check` warns when a deferrable server has neither description. This matches Claude Code; the Anthropic API shows nothing, OpenAI shows a namespace description, Codex shows only `tool_search`. | Planning review 2026-10-30 | Research notes in the plan's disclosure section | open |
| TD7 | A server is deferred when its plugin sets `defer: true`, or, when the plugin does not say, when its schemas exceed `tools.defer_share` (5%) of the context window. Replaces the draft's fixed category list. Published thresholds: Anthropic 10 tools or 10k tokens, Claude Code 10% of context, MCP client guidance 1-5%. | Planning review 2026-10-30 | | open |
| TD8 | #1911's listing before the first request applies to every declared server, deferred ones included: the prompt entry needs the real tool names. The cost is paid only without a valid cached listing. | Planning review 2026-10-30 | `tests/test_gact/test_mcp_initial_request.py` (Phase 3 extends it with a deferred server) | open |
| TD9 | `search_tools` takes `select:a,b`, keywords (BM25) and `+term rest`. Names are tokenized on `_` and `__`; an exact name in a query is always returned first. Loaded tools stay callable until compaction, then join the tool list as introduced tools do. | Planning review 2026-10-30 (Codex #21503, #56973) | | open |
| TD10 | When `search_tools` cannot be offered, deferred tools are listed in full and the trace records `tools_deferral_unavailable`. Inside `run_code`, deferred tools always have stubs. | Planning review 2026-10-30 (Codex #33608, #32101) | | open |
| TD11 | `tools/list_changed` from a deferred server updates the catalog and kernel stubs, never the listed tools; new names reach the model through the plugin-change note. | Planning review 2026-10-30 (Codex #33266) | | open |
| TD12 | The runtime adds no note about open plan threads when the agent stops; decided again from T16 evidence. | Plan defaults | | open |
| TD13 | No whole-server collapse behind an enabling tool; name lists are the only deferral level. | Planning review 2026-10-30 | | done |
| TD14 | One kernel per `(session, react scope)`; a fork gets a fresh one. | Plan defaults | | open |
| TD15 | An idle kernel is reaped after 30 minutes. | Plan defaults | | open |
| TD16 | A nested call that needs a permission answer pauses the script, asks the ordinary question and resumes on the answer. | Plan defaults | | open |
| TD17 | 500 nested calls per `run_code` call, 8 concurrent per server. | Plan defaults | | open |
| TD18 | Vendor-hosted programmatic tool calling is not used. | Plan defaults | | open |
| TD19 | The session plan is stored on `session.metadata['plan']`, replacing `todos`. | Plan defaults | | open |
| TD20 | One plan per session; children report and the parent records findings. | Plan defaults | | open |
| TD21 | Plan mode keeps its approval gate and writes the session plan. | Plan defaults | | open |

## Baselines

Recorded per phase on the `claude_code` engine. Columns follow Phase 0 step 1.

| Task | Phase | Model steps | Calls per step | Prompt tokens per step | Cached tokens per step | TTFT / decode | Batch wall vs slowest call | Step overhead | Prefix resets | Handle |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| B1 | Base | | | | | | | | | |
| B1 | Base, Claude Code | | | | | | | | | |
| B2 | Base | | | | | | | | | |
| B3 | Base | | | | | | | | | |

## Overheads (Phase 0 step 2)

| Overhead | Measured | Above baseline noise | Fixed in |
| --- | --- | --- | --- |
| `list_tools` per external call | | | |
| Direct stdio per-call spawn | | | |
| Sequential segment writes in `step_done` | | | |
| Per-call `asyncio.Runner` | | | |

## Scenarios

| ID | Scenario | Evidence | Status |
| --- | --- | --- | --- |
| T1 | B1 after Phases 1 to 3 | | open |
| T2 | Read a 5,000-line file | | open |
| T3 | Find a symbol in the repository | | open |
| T4 | Survey three subsystems with `explore` children | | open |
| T5 | Large MCP server deferred (cloud: synthetic, about 90 tools) | | open |
| T6 | T5 on an open model | | local phase |
| T7 | B2 after Phase 4 | | open |
| T8 | Permission pause inside a script | | open |
| T9 | `run_code` in plan mode | | open |
| T10 | Nested-call budget | | open |
| T11 | Cancel during a long script | | open |
| T12 | Kernel state across calls; fork starts clean | | open |
| T13 | MCP Apps through the structured-value lane | | open |
| T14 | B3 after Phase 5, frontier model | | open |
| T15 | Plan recited verbatim after compaction | | open |
| T16 | B3 on an open model, before and after | | local phase |
| T17 | Plan mode writes the session plan | | open |
| T18 | Plan panel at both viewport sizes | | open |
| T19 | Exact name inside a broad query returns first | | open |
| T20 | Deferred server adds a tool mid-session | | open |
| T21 | Script calls a deferred tool without `search_tools` | | open |
| T22 | Configuration without `search_tools` lists deferred tools | | open |
