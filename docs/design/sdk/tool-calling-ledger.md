# Tool calling ledger: exploration, disclosure, `run_code` and the session plan

Rows follow [tool-calling-plan.md](tool-calling-plan.md). Status values are the ones [acceptance-ledger.md](acceptance-ledger.md) defines (`passed (live)`, `tests only`, `static`, `done`, `open`, `blocked`), plus `local phase` for a row the plan assigns to the local qualification pass (open models, a real HA-MCP server, native tool search live, the Tauri desktop, Windows and HPC).

Decisions use the prefix `TD`, evaluation tasks `P` (performance) and `H` (long-horizon), and qualification scenarios `T`.

## Decisions

| ID | Decision | Source | Evidence | Status |
| --- | --- | --- | --- | --- |
| TD1 | Qualification runs in two passes: a cloud phase on Claude only (`claude_code`, text tool engine), then a local phase for open models, HA-MCP, live native tool search, Tauri, Windows and HPC. | Planning review 2026-10-30 | Plan section "Two phases of qualification" | done |
| TD2 | Decisions are `TD`, evaluation tasks `P` and `H`, scenarios `T`; the draft plan used `T` for both decisions and scenarios, and `B` for three single-run baselines. | Planning review 2026-10-30 | This ledger | done |
| TD3 | `fs_read_file` reads `offset=1`, `limit=2000` lines by default. | Plan defaults | | open |
| TD4 | `fs_search_text` defaults to `mode="files"`. | Plan defaults | | open |
| TD5 | `explore` runs on the fleet `explore` role, the caller's model when unset. | Plan defaults | | open |
| TD6 | A deferred server's prompt entry is its name, its MCP `instructions` (otherwise the plugin's declared `description`) and its tool names, with no docstrings or schemas. `plugin check` warns when a deferrable server has neither description. This matches Claude Code; the Anthropic API shows nothing, OpenAI shows a namespace description, Codex shows only `tool_search`. | Planning review 2026-10-30 | Research notes in the plan's disclosure section | open |
| TD7 | A server is deferred when its plugin sets `defer: true`, or, when the plugin does not say, when its schemas exceed `tools.defer_share` (5%) of the context window. Replaces the draft's fixed category list. Published thresholds: Anthropic 10 tools or 10k tokens, Claude Code 10% of context, MCP client guidance 1-5%. | Planning review 2026-10-30 | | open |
| TD8 | #1911's listing before the first request applies to every declared server, deferred ones included: the prompt entry needs the real tool names. The cost is paid only without a valid cached listing. | Planning review 2026-10-30 | `tests/test_gact/test_mcp_initial_request.py` (Phase 4 extends it with a deferred server) | open |
| TD9 | `search_tools` takes `select:a,b`, keywords (BM25) and `+term rest`. Names are tokenized on `_` and `__`; an exact name in a query is always returned first. Loaded tools stay callable until compaction, then join the tool list as introduced tools do. | Planning review 2026-10-30 (Codex #21503, #56973) | | open |
| TD10 | When `search_tools` cannot be offered, deferred tools are listed in full and the trace records `tools_deferral_unavailable`. Inside `run_code`, deferred tools always have stubs. | Planning review 2026-10-30 (Codex #33608, #32101) | | open |
| TD11 | `tools/list_changed` from a deferred server updates the catalog and kernel stubs, never the listed tools; new names reach the model through the plugin-change note. | Planning review 2026-10-30 (Codex #33266) | | open |
| TD12 | The runtime adds no note about open plan threads when the agent stops; decided again from T16 evidence. | Plan defaults | | open |
| TD13 | No whole-server collapse behind an enabling tool; name lists are the only deferral level. | Planning review 2026-10-30 | | done |
| TD14 | One kernel per `(session, react scope)`; a fork gets a fresh one. | Plan defaults | | open |
| TD15 | An idle kernel is reaped after 30 minutes. | Plan defaults | | open |
| TD16 | Running a script is a tool call gated like `shell_bash`: `run_code` is approved once, with its code shown, except in `bypass`, and that covers its direct I/O inside the fence. Nested non-read calls are covered by the script's approval in `auto-edits`, `ai-review`, `spotter-ai` and `spotter-watcher` (the reviewer modes approve once); in `ask` each asks, one question at a time, showing the script, with "Allow for the rest of this script". Explicit deny policies, plan-mode restrictions and audit apply to every nested call. Replaces the draft's "pause and ask for every nested call" and the speed doc's "no write stubs unless the mode allows them without asking". | Planning review 2026-10-30 | | open |
| TD17 | 500 nested calls per `run_code` call, 8 concurrent per server. | Plan defaults | | open |
| TD18 | Vendor-hosted programmatic tool calling is not used. | Plan defaults | | open |
| TD19 | The session plan is stored on `session.metadata['plan']`, replacing `todos`. | Plan defaults | | open |
| TD20 | One plan per session; children report and the parent records findings. | Plan defaults | | open |
| TD21 | Plan mode keeps its approval gate and writes the session plan. | Plan defaults | | open |
| TD22 | Evaluation runs the main agent on `claude-sonnet-5-5` and the fleet `explore` role on `claude-haiku-5-5`, through the `claude_code` engine; Claude Code itself on the same model is the reference for P1 to P4. | Planning review 2026-10-30 | Confirmed by the owner 2026-10-31 | done |
| TD24 | The 150-token fragment budget stays a warning for third-party plugins. Core and default-marketplace fragments have no per-fragment cap; each agent's total prompt is measured per phase and targeted after the base. | Planning review 2026-10-31 | | done |
| TD25 | A fragment's `when` gate is evaluated against the agent's final tool list (after effect gating, session posture and allowlists), so a role never receives text about tools it cannot call. Replaces the draft's "a subagent names the fragments it drops". | Planning review 2026-10-31 | | open |
| TD26 | The prompt foundation is its own phase (Phase 1), measured on its own before the tool workstreams. At the base the standard agent sends about 3,800 words of system text and about 20k tokens of tool schemas; `core.tasks` is about 2.8k tokens; `explore` composes 2,488 words, 76 of them its own. Measured by resolving `standard` in an isolated plugin store; the exact dump in Phase 1 replaces this measurement. | Planning review 2026-10-31 | | open |
| TD27 | Each workstream's presentation ships in the same phase as its producer change, extending the shared contract and readers (Design: presentation). | Planning review 2026-10-31 | | done |
| TD23 | The evaluation measures run length and performance. Correctness is recorded where an answer key grades it mechanically but is not an acceptance gate; prompts are tuned for correctness later. A run counts only if it ended with a final answer. No LLM judge. Each task runs three times per configuration. | Planning review 2026-10-30 | | done |

## Evaluation

Three runs per task per configuration (TD23). Each cell holds the median with the range of the three runs, plus the session handles. Targets are written here after the base and before Phase 1.

### Usage per task (Phase 0)

| Task | 5-hour window share per run | 7-day window share per run | Median wall time |
| --- | --- | --- | --- |
| P1 to P10, H1 to H3 | | | |

### Prompt size

| Agent | Phase | System text (words) | Tool schemas (tokens) | Injections per step | Total before the user message (tokens) |
| --- | --- | --- | --- | --- | --- |
| Root (`standard`) | Base | about 3,830 | about 20k | | about 26-27k |
| `explore` child | Base | about 3,330 | about 6-7k | | about 12k |

### Performance

| Task | Configuration | Steps | Calls per step | Prompt tokens per step (uncached / cached) | Output tokens | Wall time | TTFT / decode | Batch wall vs slowest call | Step overhead | Prefix resets | Correct (where graded) | Handles |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| P1 to P10 | Base | | | | | | | | | | | |
| P1 to P4 | Claude Code | | | | | | | | | | | |

### Long-horizon

| Task | Configuration | Steps to self-stop | Wall time | Per-step latency and prompt tokens over the run | Compactions; plan recited; findings carried | Threads open at stop and how each ended | Re-drives | Correct root cause | Handles |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| H1 to H3 | Base | | | | | | | | |

### Targets

| Workstream | Tasks | Target | Met |
| --- | --- | --- | --- |
| Prompt foundation | Prompt size (root, `explore`); P1 to P10, H1 to H3 | | |
| Exploration primitives | P1 to P4 | | |
| Fast explorer | P1 to P4 | | |
| Tool disclosure | P1 to P10 | | |
| `run_code` | P5 to P7, H1 | | |
| Session plan | H1 to H3 | | |

## Overheads (Phase 0 step 3)

| Overhead | Measured | Above baseline noise | Fixed in |
| --- | --- | --- | --- |
| `list_tools` per external call | | | |
| Direct stdio per-call spawn | | | |
| Sequential segment writes in `step_done` | | | |
| Per-call `asyncio.Runner` | | | |

## Scenarios

| ID | Scenario | Evidence | Status |
| --- | --- | --- | --- |
| T1 | P3 after Phases 2 to 4: no shell paging | | open |
| T2 | Read a 5,000-line file | | open |
| T3 | Find a symbol in the repository | | open |
| T4 | Survey three subsystems with `explore` children | | open |
| T5 | Large MCP server deferred (cloud: synthetic, about 90 tools) | | open |
| T6 | T5 on an open model | | local phase |
| T7 | P6 after Phase 5: `run_code` with nested rows | | open |
| T8a | `auto-edits`: one approval covers the script's nested write | | open |
| T8b | `ask`: nested writes ask one at a time, showing the script | | open |
| T8c | `ask`: "Allow for the rest of this script" | | open |
| T9 | `run_code` in plan mode | | open |
| T10 | Nested-call budget | | open |
| T11 | Cancel during a long script | | open |
| T12 | Kernel state across calls; fork starts clean | | open |
| T13 | MCP Apps through the structured-value lane | | open |
| T14 | H1 after Phase 6: objective, threads, every thread ended with a reason | | open |
| T15 | Plan recited verbatim after compaction | | open |
| T16 | H1 on an open model, before and after | | local phase |
| T17 | Plan mode writes the session plan | | open |
| T18 | Plan panel at both viewport sizes | | open |
| T19 | Exact name inside a broad query returns first | | open |
| T20 | Deferred server adds a tool mid-session | | open |
| T21 | Script calls a deferred tool without `search_tools` | | open |
| T22 | Configuration without `search_tools` lists deferred tools | | open |

## Prompt research sources

Read from source on 2026-10-30 for the prompt foundation:

- OpenAI Codex CLI: `codex-rs/core/gpt_5_2_prompt.md`, `gpt-5.2-codex_prompt.md`, `templates/agents/orchestrator.md`, `templates/search_tool/tool_description.md`, `code-mode-protocol/src/description.rs`, `prompts/templates/review/rubric.md`, `prompts/templates/compact/prompt.md`, `ext/goal/templates/goals/continuation.md` in https://github.com/openai/codex
- OpenAI prompting guides for GPT-5, 5.1, 5.2 and Codex: https://github.com/openai/openai-cookbook/tree/main/examples/gpt-5
- opencode: `session/runner/prompt/system.txt`, `plugin/system-prompt/gpt.txt`, `plugin/agent.ts`, `tool/todowrite.txt` in https://github.com/sst/opencode
- Gemini CLI: `packages/core/src/prompts/snippets.ts`, `agents/codebase-investigator.ts` in https://github.com/google-gemini/gemini-cli
- Claude Code prompts as mirrored in https://github.com/Piebald-AI/claude-code-system-prompts (Explore, Plan and general-purpose subagents, TodoWrite, subagent-writing guidance, parallel tool calls, context management)
- Anthropic: https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices, https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-fable-5-1, https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool
