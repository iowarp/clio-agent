# Tool calling ledger: exploration, disclosure, `run_code` and the session plan

Rows follow [tool-calling-plan.md](tool-calling-plan.md). Status values are the ones [acceptance-ledger.md](acceptance-ledger.md) defines (`passed (live)`, `tests only`, `static`, `done`, `open`, `blocked`), plus `local phase` for a row the plan assigns to the local qualification pass (open models, a real HA-MCP server, native tool search live, the Tauri desktop, Windows and HPC).

Decisions use the prefix `TD`, evaluation tasks `P` (performance) and `H` (long-horizon), and qualification scenarios `T`.

## Decisions

| ID | Decision | Source | Evidence | Status |
| --- | --- | --- | --- | --- |
| TD1 | Qualification runs in two passes: a cloud phase on Claude only (`claude_code`, text tool engine), then a local phase for open models, HA-MCP, live native tool search, Tauri, Windows and HPC. | Planning review 2026-10-30 | Plan section "Two phases of qualification" | done |
| TD2 | Decisions are `TD`, evaluation tasks `P` and `H`, scenarios `T`; the draft plan used `T` for both decisions and scenarios, and `B` for three single-run baselines. | Planning review 2026-10-30 | This ledger | done |
| TD3 | `fs_read_file` reads from `offset=1`, at most `limit=2000` lines, cut to the model result bound with a continuation offset in its header, so a default read never spills. | Plan defaults; corrected 2026-11-01 | | open |
| TD4 | `fs_search_text` defaults to `mode="files"`. | Plan defaults | | open |
| TD5 | `explore` runs on the fleet `explore` role, the caller's model when unset. | Plan defaults | | open |
| TD6 | A deferred server's prompt entry is its name, its MCP `instructions` (otherwise the plugin's declared `description`) and its tool names, with no docstrings or schemas. `plugin check` warns when a deferrable server has neither description. This matches Claude Code; the Anthropic API shows nothing, OpenAI shows a namespace description, Codex shows only `tool_search`. | Planning review 2026-10-30 | Research notes in the plan's disclosure section | open |
| TD7 | A server or tool group is deferred when its plugin declares `defer` (in `plugin.json`'s `ai.iowarp.clio.servers` or on the tool), or, when it does not say, when its schemas exceed the smaller of `tools.defer_share` (5%) of the context window and `tools.defer_tokens` (4,000). Each default-marketplace plugin gets an explicit decision from Phase 0's measurements. A share alone defers nothing on large-context models, and the standard agent's weight is in CLIO's own plugin tools. | Planning review 2026-10-30; corrected 2026-11-01 | | open |
| TD8 | #1911's listing before the first request applies to every declared server, deferred ones included: the prompt entry needs the real tool names. The cost is paid only without a valid cached listing. | Planning review 2026-10-30 | `tests/test_gact/test_mcp_initial_request.py` (Phase 4 extends it with a deferred server) | open |
| TD9 | `search_tools` takes `select:a,b`, keywords (BM25) and `+term rest`. Names are tokenized on `_` and `__`; an exact name in a query is always returned first. Deferred tools stay callable under `_clio_unlisted`; `search_tools` returns schemas in its observation, the tool rules say loaded tools may be called, and loaded names persist in `metadata["loaded_tools"]` to join the tool list at the next compaction re-basis (`with_tool_introductions` only attaches to plugin-change notes). | Planning review 2026-10-30 (Codex #21503, #56973) | | open |
| TD10 | When `search_tools` cannot be offered, deferred tools are listed in full and the trace records `tools_deferral_unavailable`. Inside `run_code`, deferred tools always have stubs. | Planning review 2026-10-30 (Codex #33608, #32101) | | open |
| TD11 | `tools/list_changed` from a deferred server updates the catalog and kernel stubs, never the listed tools; new names reach the model through the plugin-change note. | Planning review 2026-10-30 (Codex #33266) | | open |
| TD12 | The runtime adds no note about open plan threads when the agent stops; decided again from T16 evidence. | Plan defaults | | open |
| TD13 | No whole-server collapse behind an enabling tool; name lists are the only deferral level. | Planning review 2026-10-30 | | done |
| TD14 | One kernel per `(session, react scope)`; a fork gets a fresh one. | Plan defaults | | open |
| TD15 | An idle kernel is reaped after 30 minutes. | Plan defaults | | open |
| TD16 | Running a script is a tool call gated like `shell_bash`: `run_code` is approved once, with its code shown, except in `bypass`, and that covers its direct I/O inside the fence. Nested non-read calls are covered by the script's approval in `auto-edits`, `ai-review`, `spotter-ai` and `spotter-watcher` (the reviewer modes approve once); in `ask` each asks, one question at a time, showing the script, with "Allow for the rest of this script". Explicit deny policies, plan-mode restrictions and audit apply to every nested call. Replaces the draft's "pause and ask for every nested call" and the speed doc's "no write stubs unless the mode allows them without asking". | Planning review 2026-10-30 | | open |
| TD17 | 500 nested calls per `run_code` call, 8 concurrent per server. | Plan defaults | | open |
| TD18 | Vendor-hosted programmatic tool calling is not used. | Plan defaults | | open |
| TD19 | The plan and todos are separate. The plan is the plan-mode Markdown file under CLIO's state folder, its path and status (`draft`, `approved`, `done`, `abandoned`) on `session.metadata`, made with `plan_enter`, `plan_update` and `plan_exit`; `plan_update` works in and out of plan mode. Todos stay `write_todos` on `session.metadata['todos']`. Replaces the draft's single `session_plan` record that deleted `write_todos` and the plan file. | Owner direction 2026-10-31; plan, todo and memory research | | open |
| TD20 | The root session owns the plan, copied by a fork; subagents and workflows work on it, a thread names their handles or run, and the parent records what they establish; no child writes its parent's plan. A child has its own plan only when its agent definition includes the plan tools (long-running roles such as a deep-research subagent); `explore`, `review` and `planner` do not. Todos are per session. | Owner direction 2026-10-31 (option 3) | | open |
| TD21 | While a plan is `draft` or `approved`, it is re-injected verbatim after compaction and on resume with a note that the model judges its relevance; it is not recited every step. Todos are re-injected after compaction too. | Owner direction 2026-10-31 | | open |
| TD22 | Evaluation runs the main agent on `claude-sonnet-5-5` and the fleet `explore` role on `claude-haiku-5-5`, through the `claude_code` engine; Claude Code itself on the same model is the reference for P1 to P4. | Planning review 2026-10-30 | Confirmed by the owner 2026-10-31 | done |
| TD24 | The 150-token fragment budget stays a warning for third-party plugins. Core and default-marketplace fragments have no per-fragment cap; each agent's total prompt is measured per phase and targeted after the base. | Planning review 2026-10-31 | | done |
| TD25 | A fragment's `when` gate is evaluated against the agent's final tool list (after effect gating, session posture and allowlists), so a role never receives text about tools it cannot call. Replaces the draft's "a subagent names the fragments it drops". | Planning review 2026-10-31 | | open |
| TD26 | The prompt foundation is its own phase (Phase 1), measured on its own before the tool workstreams. At the base the standard agent sends about 3,800 words of system text and about 20k tokens of tool schemas; `core.tasks` is about 2.8k tokens; `explore` composes 2,488 words, 76 of them its own. Measured by resolving `standard` in an isolated plugin store; the exact dump in Phase 1 replaces this measurement. | Planning review 2026-10-31 | | open |
| TD27 | Each workstream's presentation ships in the same phase as its producer change, extending the shared contract and readers (Design: presentation). | Planning review 2026-10-31 | | done |
| TD23 | The evaluation measures run length and performance. Correctness is recorded where an answer key grades it mechanically but is not an acceptance gate; prompts are tuned for correctness later. A run counts only if it ended with a final answer. No LLM judge. Each task runs three times per configuration. | Planning review 2026-10-30 | | done |
| TD28 | Model input for every MCP result is text first: the text content blocks when there are any, the JSON of `structuredContent` only when there is no text. `structuredContent` stays for the UI and `run_code`. Matches Gemini CLI and opencode; Claude Code and Codex drop the text on the assumption, from the MCP specification, that it repeats the JSON. | Owner decision 2026-11-01 | | open |
| TD29 | Providers are inference only. No provider-native tool search (Anthropic `defer_loading`, OpenAI `tool_search`), no vendor-hosted programmatic tool calling, no use of Claude Code's or Codex's own tools. `search_tools` is the one disclosure mechanism on every provider. | Owner decision 2026-11-01 | | done |
| TD30 | `explore` selects its model with `Prefers(role="explore")`, resolved by the fleet's model choice; the fleet gains an `explore` role; unset inherits the caller with a recorded reason. A literal pin would fail the marketplace's model-pin check. | Planning review 2026-11-01 | | open |
| TD31 | `run_code` calls the permission gate itself (native tools do not reach it), dispatches nested calls on worker threads in a copy of its context (never on the application loop), talks to the kernel over stdin and stdout with the kernel's own output captured, runs the execution runtime's Python with the shell's write roots and environment scrub, and is withheld in plan mode unless the sandbox backend enforces confinement. | Planning review 2026-11-01 | | open |
| TD32 | Working state (the plan while `draft` or `approved`, and the todos) is offered at every turn start and right after any compaction, including mid-turn; it is excluded from compaction. A fork copies the plan file to its own path and copies the todos. The stall monitor is not extended. | Planning review 2026-11-01 | | open |
| TD33 | Phase 0 ships observability (exact prompt dump, `lm.call` duration and time to first token) and a standalone REST runner, so the base and every phase are measured with one instrument. P5 to P7 use a tool-only `records` MCP server instead of the HDF5 NaN count, which one shell call answers. Evaluation stops at 93% of the 7-day usage window and resumes after the reset; implementation does not wait for it. | Owner direction 2026-11-01 | | open |
| TD34 | `spawn_agents_parallel` spawns concurrently; with `wait=True` each child's report is cut to an equal share of the result bound with its own `result_file`. | Planning review 2026-11-01 | | open |
| TD35 | The evaluation's base is develop plus the instrumentation commit `69f5f915` (prompt dump and `lm.call` timing, no behaviour change), so the base and every phase are measured with one instrument. It runs from a worktree of that commit with the harness from this branch. | Implementation 2026-11-01 | `git show --stat 69f5f915` | done |
| TD36 | Trimming the five largest tool descriptions moves from Phase 1 to Phase 4: a deferred tool's description costs nothing per step, so only tools that stay listed are trimmed, by their measured size. | Implementation 2026-11-01 | | open |
| TD37 | ripgrep 14.1.1 is pinned with the sha256 of each official asset (Linux x86_64 musl and aarch64, macOS x86_64 and arm64, Windows x86_64), each verified against the published checksum; package preparation installs it, the tools use it, then a host `rg`, then a verified download, else `ripgrep_missing`. No Windows arm64 asset is published. | Implementation 2026-11-01 | `src/clio_agent/runtime/ripgrep.py`; `tests/test_runtime/test_ripgrep_install.py` | tests only |
| TD38 | Native tool results are bounded once, when the step is recorded; `load_skill`, collected task results and media stay whole (skill bodies reach 23k characters). | Implementation 2026-11-01 | `tests/test_gact/test_native_result_bound.py` | tests only |
| TD39 | Deferral rules: a plugin's own native and Python tools defer only when declared (`native_tool(name, defer=True)`, `tool(defer=True)`, or the extension's `defer`); an MCP server defers when declared (`servers.<name>.defer`) or, undeclared, when its schemas exceed `min(tools.defer_tokens=4000, tools.defer_share=0.05 × context window)`. `fs_*`, `shell_*`, `search_tools`, `load_skill`, `submit`, the spawn tools, `wait_tasks`, `write_todos` and the plan tools never defer. Without `search_tools` nothing defers (`tools_deferral_unavailable`); with nothing deferred `search_tools` itself is left out of the list. | Implementation 2026-11-01 | `tests/test_gact/test_tool_disclosure.py` | done |
| TD40 | A loaded tool joins the tool list at the next prompt rebuild (compaction or an applied plugin change): `search_tools` records it as pending in `metadata["loaded_tools"]` and `settle_basis` moves pending to listed, so the list stays byte-identical within a cache epoch. | Implementation 2026-11-01 | `test_loaded_tools_are_listed_after_the_prompt_is_rebuilt` | tests only |
| TD41 | Provisional declarations, until the base run gives call frequencies: core defers `observe_tasks`, `cancel_tasks`, `get_task_result`, `pause_agent`, `resume_agent`, `restart_agent` and the cron tools; visuals defers inspect, capture, delete and publish (building a surface stays listed); connected-data defers the NDP organization, endpoint and upload tools. Revisit with Phase 0's per-tool frequencies. | Implementation 2026-11-01 | marketplace `2b6fc38` | provisional |
| TD42 | The catalog drops common English stopwords before BM25: a query such as "dim the light" otherwise ranks a tool by "the" in its description. | Implementation 2026-11-01 | `TestCatalog::test_keywords_rank_by_bm25` | tests only |
| TD43 | MCP `initialize.instructions` are not captured yet (correction 10). A deferred server is described by `servers.<name>.description`, else by its plugin's description when the plugin has one server; `plugin check` warns (`deferred_server_undescribed`) otherwise. Capture instructions only if a real server needs them. | Implementation 2026-11-01 | `tests/test_plugins/test_disclosure_declarations.py` | open |
| TD44 | The permission answers and policy actions are defined once (`gact/runtime/permission_actions.py`): `STANDARD_ANSWERS`, `ALLOWING`, `POLICY_ACTIONS`; an answer outside the standard set is valid only where the question's `actions` offer it (`offered`). `allow_script` is never a policy. | Implementation 2026-11-01 | `tests/test_gact/test_code_mode.py::TestGrants` | tests only |
| TD45 | A script's structured values come from the observer's completed notification (`tool_observation.RESULT_SINK`, a ContextVar a nested call binds), not a new executor lane: the frozen `execution.py` is unchanged, and MCP Apps keep `call_tool_result`. A native tool's return value is passed as is (JSON text decoded). | Implementation 2026-11-01 | `test_mcp_calls_return_structured_content` | tests only |
| TD46 | Each script's output goes to a temporary file the kernel points fds 1 and 2 at (no pipe, so a large print cannot block the protocol); the kernel returns at most 2,000,000 characters of it, and the model's text is bounded at 12,000 with spill like any native result. | Implementation 2026-11-01 | `TestKernel` | tests only |
| TD47 | `run_code` refuses in plan mode unless the sandbox enforces the write fence (`code_plan_mode_unconfined`), at call time as `write_todos` does, rather than being withheld from the tool list. | Implementation 2026-11-01 | `gact/code_mode/tool.py` | open: no test on an active backend |
| TD48 | Deviations kept for now: the plans-folder ACL carve-out stays (it is wired into the resolver's priority bands and playbook narrowing) while the plan-mode reminder steers to `plan_update`; the `planning` skill keeps its `enter_mode` effect beside `plan_enter`. Both are removed once runs show agents using `plan_update`/`plan_enter`. | Implementation 2026-11-01 | | open |
| TD49 | Compaction never summarizes the latest copy of each working-state injection (`plan_mode`, `plan`, `todos`); older copies are folded, and the compaction prompt says the plan and todos come back verbatim. After a mid-turn auto compaction the loop offers the working state again (`working_state.reinject_after_compaction`); the recorder adds only what changed. | Implementation 2026-11-01 | `test_compaction_keeps_the_latest_working_state_verbatim` | tests only |
| TD50 | Not built yet: the `plan.stale_steps` note, the rail's Edit action (an edit sent as the person's steering), the UI consuming `session.work.updated` (the rail still polls every 5 s), and the plan-mode structure hint rewrite (objective, approach, threads, findings; `core.plan` carries it meanwhile). | Implementation 2026-11-01 | | open |
| TD51 | The long tool descriptions stay whole (owner, 2026-11-01: they generally work well) but follow the alignment rules: capitals used for emphasis became plain sentences and each prohibition keeps its reason (`create_artifact`, `shell_bash`, `raise_alert_card`, `plan_exit`, `loop_wakeup`, `goal_status`, `update_a2ui_data_model`, `refresh_provider_models`, `load_skill`). Facts and steers are unchanged; the Windows shell text also lost a mis-encoded dash. Trimming still waits for call frequencies (TD36). | Owner 2026-11-01 | `tests/test_tools/test_shell_server.py` | done |

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
| T14 | H1 after Phase 6: plan kept current, every thread ended with a reason | | open |
| T15 | Plan and todos re-injected verbatim after compaction | | open |
| T16 | H1 on an open model, before and after | | local phase |
| T17 | `plan_enter`, approval, todos, replan with `plan_update` | | open |
| T18 | Plan and Todos sections at both viewport sizes | | open |
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

## Plan, todo and memory research sources

Read on 2026-10-31 for the session plan:

- Claude Code: [what survives compaction](https://code.claude.com/docs/en/context-window#what-survives-compaction), [task tool availability](https://code.claude.com/docs/en/tools-reference#task-tool-availability), [memory](https://code.claude.com/docs/en/memory), and the plan-file and tasks-vs-memory reminders in https://github.com/Piebald-AI/claude-code-system-prompts
- Anthropic: [effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents), [effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents), [memory tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool)
- OpenAI: Codex [`update_plan`](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/plan_spec.rs) and [goal continuation](https://github.com/openai/codex/blob/main/codex-rs/ext/goal/templates/goals/continuation.md); [ExecPlans](https://developers.openai.com/cookbook/articles/codex_exec_plans)
- [Manus: context engineering lessons](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus)
- Gemini CLI: [tracker](https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/tracker.md), [plan mode](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/plan-mode.md)
- OpenHands: [task_tracker](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-tools/openhands/tools/task_tracker/definition.py)

## Implementation notes

- **Phase 0** (`69f5f915`, `219dcf86`, `247bc756`): `lm.call` carries `started_at`, `duration_ms`, `ttft_ms` and a `prompt_shape`; `GET /v1/sessions/{sid}/agent?exact=1` serves the last request sent, rendered as the provider saw it and sized per tool and plugin. The harness under `scripts/eval/tool_calling/` (fixtures, synthetic MCP servers, REST runner with a 93% usage guard, metrics, grading, Claude Code reference) was checked against a live server without model calls; turn completion, real `lm.call` payloads, usage-window readings and stream-audit rows are first exercised in the base run.
- **Phase 1** (`f1039fdb`, marketplace `74fbf24`): tests only so far; prompt sizes are measured from the exact dump in the first evaluation run.
- **Phase 2** (`02dbe742`, marketplace `0cf0611`, gact-tui `8e0c4aac`): a live turn on `claude_code` (haiku, session `sess_def3fe5c51e5`, plain request "Where is the ranged file read implemented in this repository?") made 5 calls: files-mode search, content-mode search, then ranged reads ("Lines 100–159 of 182", "Lines 40–119 of 119"); presentations reached the client with `start_line` and `line`; console errors and failed requests were empty. One run on a small model: an observation, not evidence of judgement.
- **Text-first risk (TD28):** a server whose text block is a summary while its data sits only in `structuredContent` now shows the model the summary. `test_root_data_result_is_publicly_projected_without_private_metadata` models one such server (a repr text block); it now asserts the text. No per-tool override is built until a real server needs one.
- **UI observation to resolve in the UI pass:** on gact-tui `develop` the final answer of the live turn also appeared once as unrendered text in the activity timeline, and clicking a tool group switched the answer to that unrendered view. The backend sends one final text part (checked through `/messages`); this predates this plan's UI changes and is investigated with the Phase 7 UI checks.
- **Phase 4** (`34e9b46c`, marketplace `2b6fc38`, gact-tui composition view): one live P8 run on `claude_code` (sonnet, session `sess_27f222333fbe`, plain request "Set the guest bedroom thermostat to 19.5 °C.", n=1) with `clio-eval/eval-home`: the 95-tool `synthetic-home` server was deferred by the size rule, the 15-tool calendar and inventory servers stayed listed. The agent called `search_tools` with `select:` on three climate tools, then the tools themselves; 6 calls, 54 s, graded correct. Listed schemas were still about 92.6k characters (core 22.2k, connected-data 9.3k, visuals 8.5k), the target for TD36 trimming and TD41. One run: an observation, not a measurement. The composition view's "Loaded when needed" row was checked at 1440×900 and 390×844 (`scripts/cloud/checks/deferred_tools.mjs`), with no console errors. The 7-day usage window read 75% before the Monday reset, so live runs wait for the reset.
- **Phase 1 test leftovers** (`c26c1e55`): seven gact test files still expected the deleted tool-use note at message 0, JSON spacing from `structuredContent`, and the old `write_todos` and skills-block wording.
- **Phase 5** (`run_code`): `gact/code_mode/` (tool, kernels, kernel_main, stubs, dispatch, grants, presentation); a persistent kernel per `(session, react scope)` started through `wrap_confined` with the shell's profile, write roots and environment scrub (`code_kernel` seam in sandbox conformance); nested calls run through the agent's instrumented tools on worker threads with the script's grant bound, so the gate decides them (covered in auto-edits, ai-review and the SPOTTER postures; asked one at a time with the script shown and `allow_script` in `ask`); `parent_call_id` on the live tool-call part, `tool.call.started` and the v3 tool entity; gact-tui nests the calls under the script's row and shows the script in the question. Tested with real kernel processes (gathered calls, per-scope state, errors raised in the script, the call budget, timeout recovery, session close) and through the real permission gate in each mode. No live model run yet: the 7-day window read 75%.
- **Phase 6** (session plan): `plan_enter`, `plan_update` and the plan status (`draft`, `approved`, `done`, `abandoned`); the plan offered as working state outside plan mode and to the goal judge; latest working state kept through compaction and re-offered after a mid-turn compaction; a fork copies the plan file to its own path and the todos; `GET /v1/sessions/{sid}/work` returns the plan and the Work rail shows it above the todos; `session.work.updated` is published on plan and todo writes; `core.plan` fragment. See TD48 and TD50 for what is deferred.
- **Focused regression run after Phases 4-6** (`01317ac6`, tests matching the touched areas across `test_gact`, `test_plugins`, `test_tools`, `test_docs`, `test_client`, `test_runtime`): the real failures were tests pinned to the old reminder wording, the core tool table, the removed tool-use note and the transcript goldens, fixed in `1cd1f583`. Three failures reproduce identically on `ef077541` (before Phase 4) and come from this container: `test_compaction_visible::test_a_record_whose_turn_settled_before_the_fold_is_still_shown_live` stops the worker's shared clio-core daemon and hangs at session teardown, and the two Landlock tests in `test_sandbox_fence.py` find the write fence not enforced here. Twelve errors need the `external/clio-agent-marketplace` submodule, which this container does not check out. A full suite run on the final commit is still owed; in this container it does not finish within two hours.
- **Correction to the entry above:** the three failures were not the container's. (1) `test_compaction_visible::test_a_record_whose_turn_settled_before_the_fold_is_still_shown_live` lost its only foldable content when Phase 1 removed the recorded tool-use note, so the compaction reported `nothing_new_since_last_compaction`; and on any failure before the fold its tool waited forever, which hung the run and took the worker's clio-core daemon down with it. The test now records a CLIO note before the question and releases the tool in a `finally`. (2) The two Landlock tests wrote their "outside" file under `Path.home()`, but the test runtime puts `HOME` inside the checkout, which is a write root; the fence correctly allowed it. They now choose a directory verified to be outside every write root. The fence itself denies the write (checked by hand and by the tests). (3) The twelve errors were the marketplace submodule, now checked out at the branch and pinned (`DEFAULT_MARKETPLACE_COMMIT` moves with it); the marketplace's own CI steps (unit tests, model-pin and skill-literal checks, `plugin check` for both marketplaces, 338 plugin tests with trimesh/h5py/adios2/netCDF4, the spotter implementation's ruff, pyright and tests) all pass against this clio-agent branch.

