# Tool calling, progressive disclosure and long-horizon work: implementation plan

This plan covers item (c) of the CLIO 1.0 release, programmatic tool calling, together with the work that decides whether it pays off: the exploration primitives it calls, progressive tool disclosure, step cost, and long-horizon operation. It comes before the RLM plan because RLM is a defined case of `run_code`, in the same way that debate is a defined case of a workflow. RLM, the in-app browser and any later code-driven feature run in the kernel this plan builds.

It turns two design documents into one contract: "CLIO exploration speed: findings for the PTC and tool disclosure campaign" and "Long-horizon exploration in CLIO: an evolving session plan" (both 2026-10-29), plus the `run_code` design from the programmatic tool calling write-up of 2026-10-22. The plan has five workstreams, each building on the ones before it:

1. **Exploration primitives.** Ranged reads with line numbers, a files-only search mode, a glob tool, and spill files the agent can page through.
2. **A fast explorer.** The `explore` role runs on a fast model with read tools and a lean prompt, and the parent can spawn a batch and get the reports back in the same call.
3. **Progressive tool disclosure.** Large tool sets are deferred: the prompt names each deferred server and its tools, and `search_tools` loads their schemas. The task protocol fragment moves into a skill.
4. **Programmatic tool calling.** One native `run_code` tool backed by a persistent, confined Python kernel whose tool stubs call back through the agent's existing tool path.
5. **The session plan.** `write_todos` is replaced by a plan the agent owns and rewrites as it works, kept visible at every step and through compaction.

The work runs to completion without stopping for review. The human reviews the finished branches.

## Contract

This plan is the contract for the five workstreams. The megachange design documents still govern everything they cover, and decisions D1 to D29 in `acceptance-ledger.md`, the F decisions of `delegation-fork-science-plan.md`, the M decisions of `long-term-memory-plan.md` and the model fleet decisions in `model-fleet.md` stay in force. When code and this plan disagree, change the code. If the plan turns out wrong, choose the fix that best keeps its intent, update this document in the same change, and record the decision in the ledger.

| Document | Governs |
| --- | --- |
| This plan | Exploration tools, the explorer role, tool disclosure, `run_code` and its kernel, the session plan |
| [tool-calling-ledger.md](tool-calling-ledger.md) | This plan's decisions, baselines and acceptance evidence |
| `plugins-and-composition.md` | Plugins, the composed prompt, fragments |
| `agent-sdk.md` | `Agent`, `Subagent`, `Plugin`, `plugin check` |
| `orchestration-semantics.md` | The task layer, flows, `ctx` |
| `model-fleet.md` | Model selection, `Prefers`, `model_choice` |
| `plugin-change-delivery.md` | How tool lists change mid-session without breaking the cached prefix |
| `runtime/sandbox.py` and `sandboxing-campaign-2026-07.md` | Confinement; the kernel uses it, does not redefine it |

Base: `develop` at `9a365758` in clio-agent (the merge of #1911, which lists every declared MCP server's tools before the first model request), with the matching gact-tui pin and `clio-agent-marketplace` `develop`. Delegation, fork, the science log, long-term memory and the model fleet have landed there. This plan's decisions use the prefix `TD`; its baseline tasks use `B` and its qualification scenarios use `T`.

## Two phases of qualification

This plan is implemented and qualified in two passes.

- **Cloud phase (this branch).** Claude only: the `claude_code` provider, which uses the text tool engine. Everything is implemented, and every scenario that can run on Claude runs live.
- **Local phase (later).** Open models through the fleet, a real Home Assistant MCP server, the Anthropic and OpenAI native tool-search paths live, the Tauri desktop, Windows and HPC.

| Item | Cloud phase | Local phase |
| --- | --- | --- |
| Baselines B1 to B3 | `claude_code` engine; B1 also on Claude Code itself | Open model |
| Native tool search (Anthropic `defer_loading`, OpenAI `tool_search`) | Implemented; request shape covered by tests | Live |
| Large deferred MCP server (T5) | A synthetic MCP server with about 90 tools | Real HA-MCP |
| T6, T16 | Recorded as local phase | Run |
| Everything else | Live on a freshly restarted stack | Repeat on open models where relevant |

## Rules

The megachange rules carry over unchanged: delete and replace, the task layer is the only execution authority, the model decides, no silent fallback, one mechanism per concept, no fifth store, the repository rules in `AGENTS.md`, and evidence over claims. Five more apply here:

1. **One execution path for tools.** A call from a script, a direct call and a call from a child all run through the same instrumented dispatch: permission gate, file policy, hooks, MCP v2 tasks, cancellation, the observer and presentation. The kernel never gets a second path, an MCP client or credentials.
2. **The cached prefix stays stable.** Nothing in this plan changes the system text or the tool list in the middle of a conversation except through the mechanisms in `plugin-change-delivery.md`. `run_code` has one fixed schema for the life of the process.
3. **Provider independent.** Everything works on every provider, including open models and the `claude_code` text engine. Provider-native tool search is used where it exists as an optimization of the same behavior, never as the only path. Vendor-hosted programmatic tool calling (Anthropic's and OpenAI's) is not used.
4. **Surface reality, never decide.** The runtime may show the agent structured state the agent wrote (its plan, its budget, a spilled file's path). It does not decide that work is unfinished, route calls, or pick tools for the agent.
5. **Measure before and after.** Every workstream is judged against the Phase 0 baseline on the same tasks, not against expectations.

## Out of scope

Do not implement, stub or reserve fields for these:

- RLM semantics (`llm()` leaves, the `rlm` subagent, recursive context handling). That is the next plan; it adds its own kernel binding.
- The in-app browser and its `page` object (item g). It also adds its own kernel binding later.
- Replacing `run_workflow` or declared flows with scripts. Flows stay as `orchestration-semantics.md` defines them.
- Images or other media in `run_code` observations. A script writes a file and the agent calls `view_image` or `view_pdf`.
- Changes to the science log. It stays the user-facing record of a study and is not the session plan.
- Desktop or remote-relay kernels. The kernel runs where the agent's tools run.
- Collapsing a whole server behind one enabling tool. Name lists make deferral cheap enough that a second level is not needed.

## Current state

What exists at the base, verified against `9a365758`, so nothing is rebuilt and nothing is missed:

- **The loop.** `ClioReAct` (`gact/agents/clio_react.py`) runs one model call per step and every call it emitted concurrently in a `ThreadPoolExecutor`, results in call order. `max_iters=0` means no step cap, and a turn ends when the model makes no tool call. The batching hint is `TOOL_USE_NOTE` (line 99), injected once per agent context. `_refresh_tools` reassigns the listed tools mid-turn when a connected server sends `tools/list_changed`.
- **File tools.** `fs_server.read_file` (`tools/servers/fs_server.py`, around line 91) takes only a path and returns `{path, size_bytes, content, truncated}` up to 256 KB, which `mcp_executor` serializes with `json.dumps`, so source code reaches the model as one escaped string with no line numbers. `search_text` is literal, `context_lines=2`, up to 100 matches, with no files-only mode and scan caps of 1,000 files and 3 s. `list_directory` is one level. There is no glob tool.
- **Result bounding.** `bounded_model_tool_result` (`tools/mcp_result_projection.py:62`) cuts MCP results at `limits.model_tool_result_chars` (12,000) and spills the rest to the session spill folder, telling the model to read it in parts, which `read_file` cannot do. Native tools have no equivalent bound. Since `9bb0b117`, a subagent result over `limits.agent_task_output_digest_chars` (8,000) is saved whole to the same spill folder with `result_file` and `result_excerpt`.
- **Delegation.** The marketplace `delegation` plugin defines `explore`, `planner` and `review` (`subagents/roles.py`), all `derive="caller"`, `effects="read"`, with no `Prefers`, so `explore` runs on the caller's model with the caller's whole prompt and every schema. `spawn_agents_parallel` (`gact/spawn_tools.py:109`) accepts a per-assignment `model` and returns handles; collecting reports needs a separate `wait_tasks` call. The fleet (`gact/fleet/selection.py`) records `model_choice` for every child.
- **Tool surface.** `_agent_tools` (`gact/agents/builders.py`) sends every native, gateway and declared MCP tool schema on every step, roughly 55 for the standard agent; HA-MCP alone adds 86. `_clio_unlisted` (`clio_react.py:171`) and `with_tool_introductions` (`gact/agents/prompt_basis.py:154`) already let a tool become callable mid-session without rewriting the prefix; an introduced tool joins the tool list at the next compaction. There is no deferral flag and no tool search. Skills already disclose progressively through `load_skill`. `MCPServerSpec` has no server description.
- **First request (#1911).** A declared MCP server whose tools are not known (no valid cached listing) is prepared and listed before the first model request; the old two-second grace and `wait_for_tools` are deleted. A valid cached listing is used immediately while the session warm-up starts the server. HTTP/OAuth listings are read live per connection and are not cached.
- **Fragments.** `plugins/builtin/core/ai.iowarp.clio/prompt/tasks.md` (`core.tasks`) is 1,564 words, about 2.5k tokens on every request of every agent, children included. `guidelines.md` is 110 words. The only explicit "batch independent calls" instruction is in a science-only marketplace skill.
- **The `claude_code` engine.** `lm/engines/text_tools.py` renders schemas as text in the system prompt; `TURN_REMINDER` (line 87) spends most of its words on fence syntax and on waiting for dependent results.
- **Tool calls to MCP servers.** `ExternalTasks._call` runs `host.client.list_tools()` before every `tools/call` (`gact/external_tasks.py:262`). A direct stdio route opens a connection per call.
- **Observer.** Per-call state is already in `ContextVar`s (`gact/tool_observer.py`), so the thread-local seam the 2026-10-22 write-up flagged is gone. `Part` (`gact/parts.py:108`) has no parent link.
- **Raw lane.** `call_tool_result` uses `_call_tool_inner(..., return_raw=True)` (`tools/execution.py:624`) only for the MCP Apps bridge, returning before PostToolUse.
- **Sandbox.** `runtime/sandbox.py::wrap_confined` (line 435) composes the fence for child processes; `sandbox_conformance.py` probes what a host supports.
- **Planning state.** `write_todos` (`gact/todos.py`) stores a whole-list checklist on `session.metadata['todos']` and is recited at turn start when it changed. Plan mode (`gact/plan_mode.py`) authors a separate plan file and blocks writes; the `planning` skill (704 words) enters it. The stall monitor (`gact/replanning.py`) only runs with an execution playbook. Stop hooks (`gact/hooks/stop_loop.py`) can block a stop. Goal (`gact/goal.py`) re-drives from an independent judge. Compaction (`gact/compaction.py`) never mentions todos.
- **Parked design.** `docs/design/agent-loop-rebuild-2026-09.md` (line 701) proposes a category index plus stable `tool_info`/`call_tool` tools for disclosure. This plan supersedes it.

## Design: exploration primitives

`run_code` scripts call these same tools, so their shape matters twice.

**`fs_read_file`.** New signature `read_file(filepath, offset=1, limit=2000, access_reason="")`, lines 1-based. It returns a TEXT content block: a header line `path  lines a-b of N  (size)`, then the lines prefixed with their numbers. Binary files return a typed error naming the format-specific reader if one exists. The structured result (path, range, total lines, truncated) goes in `structuredContent` for scripts. The old whole-file dict shape is deleted.

**Spill files.** The `result_spilled` note and the subagent `result_file` both name the exact `fs_read_file` call that continues reading, since the spill folder is inside the readable roots.

**`fs_search_text`.** Backed by ripgrep (bundled through the existing package preparation; a missing binary is a typed error at startup, not a Python fallback). Arguments: `query`, `path`, `regex=False`, `glob`, `mode="files"`, `context_lines=0`, `case_sensitive`, `limit`. `files` returns paths and match counts, `content` returns numbered lines with context, `count` returns totals. The 1,000-file and 3 s caps go; the result cap and the explicit truncation report stay.

**`fs_glob`.** `glob(pattern, path, limit=200)` returning paths sorted by modification time, newest first, with an explicit truncation flag. `list_directory` stays one level.

**Native tool bounding.** Native tool results pass through `bounded_model_tool_result` like MCP results do, with the same spill. `run_code` relies on this.

**Fragment `workspace.explore`** (about 60 words): locate with glob or files-mode search, read the ranges you need, cite paths and lines, and make independent reads in one step. `TOOL_USE_NOTE` is folded into it and deleted. `TURN_REMINDER` is rewritten so that parallel independent calls get the same weight as waiting for dependent ones.

**Overheads.** `ExternalTasks._call` caches the tool list per connection era (the executor's `tools_revision`) instead of listing before each call. The direct stdio route keeps its connection for the session if Phase 0 shows per-call spawning costs more than baseline noise.

## Design: the fast explorer

**Model.** `explore` declares `Prefers(model=<fleet fast role>)` through the existing fleet: the fleet gains an `explore` role (beside the memory roles) picked once in Fleet settings, defaulting to the caller's model when unset. `model_choice` records the choice as for any child.

**Tools and prompt.** A subagent can name the fragments it drops. `explore` drops `core.tasks` and the delegation spawn tools; effect gating already refuses writes. Its prompt becomes a strategy prompt (about 150 words): sweep wide with glob and files-mode search, read narrow ranges, follow references, and return conclusions with paths and lines rather than file contents.

**Blocking batch.** `spawn_agents_parallel` gains `wait: bool = False`. With `wait=True` it returns each child's report (or its `result_file` when over the digest limit) in the same observation, after the last child finishes or the call is cancelled. It is a mechanism, so it does not route anything.

**Parent prompt.** `delegation.parent` gains one sentence: a broad survey of a codebase or dataset splits into several `explore` children in one `spawn_agents_parallel(..., wait=True)` call.

## Design: progressive tool disclosure

The shape follows what Claude Code ships and what the Anthropic, OpenAI and MCP client guidance converge on: the model always knows every server and tool that exists, and only the schemas wait until needed. A model that cannot see a tool concludes it does not have it (the failure #1911 fixed for slow servers, and Codex issues #21503 and #56973 for search misses), so deferral must never make a server invisible.

**What the prompt carries.** Each tool is either listed (full schema in the tool list, as today) or deferred. For each deferred server the prompt carries one entry: the server's name, its description, and the names of its deferred tools, without docstrings or schemas. The description is the server's MCP `instructions` when it sends them, otherwise the `description` the plugin declares for the server. `plugin check` warns when a deferrable server has neither. The entry lives in the composed prompt, so it is part of the cached prefix and is built from the real listing that #1911 guarantees before the first request.

**What is deferred.** The `ai.iowarp.clio` namespace gains `defer: true | false` per MCP server declaration and per plugin tool. When a plugin does not say, a server is deferred when its listed schemas exceed `tools.defer_share` (default 5%) of the model's context window, which the handshake already discovers; small servers stay listed in full. Core loop tools (fs, shell, `load_skill`, delegation, `run_code`, `search_tools`, the session plan) are never deferred. Task-control tools beyond `wait_tasks` are deferred by the core plugin's declaration.

**`search_tools`.** One native tool, `search_tools(query, limit=8)`, over the agent's deferred catalog (`gact/tool_catalog.py`, the one catalog). It takes three query forms: `select:a,b` loads exact names; plain keywords rank by BM25 over name, description, argument names and server; `+term rest` requires `term` in the name and ranks by the rest. Names are tokenized on `_` and `__` as well as whole, and an exact name anywhere in a query always returns that tool first, ahead of the ranked results. It returns the matching schemas and makes them callable from the next step through `_clio_unlisted` and `with_tool_introductions`, so the prefix is untouched; a loaded tool joins the tool list at the next compaction, as any introduced tool does. On providers with native deferral (Anthropic `defer_loading` tool search, OpenAI Responses tool search) the same catalog feeds the provider's mechanism instead of the text introduction; on open models and the `claude_code` engine the text path is used.

**Reachability.** If an agent's configuration cannot offer `search_tools` (a tool allowlist without it, or a provider path that drops it), its deferred tools are listed in full instead, and the trace records `tools_deferral_unavailable`. Inside `run_code`, deferred tools have stubs like any other tool, and `tools.search` reads the same catalog, so a script never needs `search_tools` first.

**Tool list changes.** A `tools/list_changed` from a deferred server updates the catalog and the kernel stubs, never the listed tools. A new tool name reaches the model through the plugin-change note, so the prefix stays intact. A change from a listed server behaves as today.

**`core.tasks` into a skill.** The fragment shrinks to three sentences (what a handle is, `wait_tasks` collects, results over the digest limit arrive as files). The rest moves to a `task-protocol` skill in the core plugin, loaded on demand.

**The parked proposal.** The `tool_info`/`call_tool` paragraph in `agent-loop-rebuild-2026-09.md` is deleted with a pointer to this plan.

## Design: `run_code`

**The tool.** A native tool in a new owner package `gact/code_mode/` (`tool.py`, `kernels.py`, `bridge.py`, `dispatch.py`, `stubs.py`, `kernel_main.py`), each under the 800-line cap and built with `tool_instrumentation.native_tool`. Schema: `code: str`, nothing else, fixed for the life of the process. Its description, rendered once at agent construction, carries the calling conventions and a one-line-per-namespace index of the agent's tools, deferred ones included. It is annotated non-read-only.

**The kernel.** One persistent CPython process per `(session, react scope)`, spawned with `python -I kernel_main.py` through `wrap_confined`, so it runs inside the same fence as the shell. State (variables, imports) persists across calls in a scope and is dropped when the scope ends, when the session closes, or after `code.idle_minutes` (default 30) idle. A fork gets a fresh kernel. `kernel_main.py` is stdlib only; the agent's Python environment supplies numpy, h5py and the rest. Per-call wall time is bounded by `code.call_timeout_s` (default 600) and cancellation reaches the kernel through the task layer.

**The bridge.** Newline-delimited JSON-RPC on a dedicated file descriptor, so the script's own prints never mix with protocol messages. Messages: `exec`, `call`, `result`, `cancel`, `output`.

**Stubs.** One async function per tool, generated from the agent's `dspy.Tool` list and its deferred catalog and grouped by namespace (`tools.fs.read_file`, `tools.hdf5.list_datasets`). Parameters are typed from `inputSchema`, the description is the docstring, and the return type comes from `outputSchema` when the server declares one, otherwise `Any` with the result's `structuredContent` or text. `tools.search(query)` and `help(tools.x)` read the same catalog as `search_tools`. The kernel namespace is built from one binding registry whose only entry in this plan is `tools`; RLM and the browser add theirs in their own plans.

**Dispatch.** A stub call goes over the bridge to `dispatch.py`, which runs it through the same instrumented path as a direct call. The structured value returns to the script without passing PostToolUse presentation meant for the model; this replaces the MCP-Apps-only `return_raw` lane with one structured-value lane that both MCP Apps and `run_code` use. Concurrency: stubs are awaitable, `asyncio.gather` works, and dispatch caps concurrent calls per server (`code.per_server_concurrency`, default 8). A nested-call budget per `run_code` call (`code.max_nested_calls`, default 500) and the existing circuit breaker bound runaway loops.

**What the model sees.** Only the script's stdout, stderr and final expression, bounded by the same 12,000-character rule with spill. Nested results stay in the kernel. The observation is text.

**Observability.** `Part` gains `parent_call_id`; `run_code` binds its call id in a `ContextVar`, dispatch copies the context into each nested call, and the observer emits it in `tool.call.started` and the v3 projection (`gact/protocol/v3/event.py`). gact-tui nests child rows under the `run_code` row, each with tool, redacted args, duration, status, result size and a spilled flag. ARC records only the `run_code` call and its bounded observation.

**Permissions.** Running a script is itself a tool call, gated like `shell_bash`: `run_code` is one non-read call, so it is approved before it runs, with the code shown, in every mode except `bypass`. That approval covers what the code does directly (`open()`, `h5py.File()`, subprocesses) inside the `wrap_confined` fence, as a shell approval covers its command. Every nested tool call still goes through the permission gate with its own annotations: explicit deny policies deny it (raised inside the script), plan-mode restrictions apply, and it is audited and shown under the `run_code` row. Nested reads never ask. Whether a nested non-read call asks again depends on the approval mode:

| Approval mode | The script | Nested non-read calls |
| --- | --- | --- |
| `bypass` | Runs | Run |
| `auto-edits`, `ai-review`, `spotter-ai`, `spotter-watcher` | Approved once | Covered by the script's approval |
| `ask` | Approved once | Each asks |

The script's approval is a grant scoped to that `run_code` call; at the prompt boundary the gate allows a nested call when the mode covers it and the grant exists, and otherwise asks as usual. In `ask` mode, a nested call that asks pauses the script until the answer, and the time waiting does not count against `code.call_timeout_s`. Only one question per script is open at a time (other calls that need an answer wait behind it), the question shows the script it came from, and it offers "Allow for the rest of this script", which turns the grant on for the rest of that call. Approval resumes the call; denial raises in the script. Stop cancels the open question and the kernel call. Read-only sessions and plan mode filter the stub set with the same visibility rules as direct tools and spawn the kernel read-only.

**Teaching.** Fragment `code.run` (about 120 words) with one worked example: gather a batch of reads or queries, filter in Python, print a short summary. It says when not to use it: a single call or a short dependent sequence is cheaper as direct calls.

## Design: the session plan

**The tool.** `write_todos` is deleted and replaced by `session_plan` in the core plugin. The plan holds an `objective` (the question in the agent's words and what an answer must contain), `threads` (id, text, status `open`, `active`, `closed` or `dropped`, a reason when closed or dropped, optional task handles working on it), `findings` (id, claim, references: `call:` refs, paths and lines, task handles), and `next` (the few steps intended now). The tool takes keyed operations (set objective, add, update or close a thread, add a finding, set next) and a whole rewrite. It is stored on `session.metadata['plan']`, replacing `todos`, so no store is added.

**Recitation.** The plan is injected as a CLIO addition whenever it changed since the last step and always on the first step after a compaction, so one long turn never loses it. The recitation carries elapsed time and spend against any session limits.

**Compaction.** The compaction prompt is told the plan exists and is kept verbatim, so the summary carries only the narrative. The plan addition is excluded from what compaction summarizes.

**Staleness.** After `plan.stale_steps` (default 25) steps without a plan operation, a one-line reminder that the plan exists is added. The stall monitor counts plan operations as progress and runs in every session, not only playbook ones.

**Plan mode.** The `planning` skill keeps its approval gate but writes the session plan instead of a separate plan file; `plan_exit` approves the current plan. The plan-file path code is deleted.

**Delegation and goal.** A thread can name child handles; their reports arrive as ordinary observations and the parent records findings. The goal judge reads the objective, threads and findings instead of the transcript; goal and stop hooks remain the backstop.

**Stopping.** The runtime does not comment on open threads when the agent stops (TD12). Whether agents stop with open threads is measured in qualification, and that evidence decides whether to revisit.

**Fragment `core.plan`** (about 80 words): write the objective and threads first, rewrite the plan when a finding changes direction, make checking your claims the last thread, and stop when every thread is closed or dropped with a reason.

**UI.** A live plan panel in gact-tui beside the task list: objective, threads with status icons and reasons on hover, findings with reference links. Edits made there arrive as steering attributed to the human.

## Defaults for the open questions

Take these and record each in the ledger.

| Question | Default |
| --- | --- |
| Read window | `offset=1`, `limit=2000` lines |
| Search default mode | `files` |
| Explorer model | Fleet role `explore`, caller's model when unset |
| What a deferred server shows | Server name, MCP `instructions` or the plugin's description, and tool names |
| When a server is deferred | The plugin's `defer`; otherwise schemas over 5% of the context window |
| First request and deferred servers | #1911's listing applies to every declared server, deferred ones included |
| Kernel scope | One per `(session, react scope)`; forks get a fresh one |
| Kernel idle reap | 30 minutes |
| Permissions for a script | `run_code` is approved once like `shell_bash`; nested non-read calls are covered by that approval in every mode except `ask`, where each asks one at a time and may be allowed for the rest of the script |
| Nested-call budget | 500 per `run_code` call, 8 concurrent per server |
| Vendor-hosted PTC | Not used |
| Plan storage | `session.metadata['plan']`, replacing `todos` |
| Plan per agent or per tree | Per session; children report, the parent records |
| Runtime note on open threads at stop | Off; decided again from qualification evidence |
| Plan mode | Keeps its gate, writes the session plan |

## Phase 0: baseline and decisions

No production code changes in this phase.

1. Run the baseline tasks (B1 to B3 below) on the base on the `claude_code` engine, and record per step: model steps, calls per step, prompt and cached tokens (`turn_lm_ledger`), TTFT and decode time, batch wall time against the slowest call, step overhead outside model and tools, and `prefix_mismatch` and `first_call` resets. Run B1 on Claude Code with the same model for comparison. The open-model baselines are taken in the local phase.
2. Time `list_tools` per call, direct stdio per-call spawn, sequential segment writes in `step_done`, and per-call `asyncio.Runner`. Record which exceed baseline noise; only those are fixed.
3. Run `sandbox_conformance.py` on the cloud host and record which fence level a child process gets, since the kernel inherits it.
4. List every caller of `write_todos`, `todos` metadata, the plan file, `return_raw`, `read_file`'s dict shape and `TOOL_USE_NOTE`, for the deletions.
5. Fill in `tool-calling-ledger.md`: decisions TD1 onward, the baseline numbers and the scenarios below.
6. Commit, push, open draft PRs, and continue.

Baseline tasks, each a plain request with no tools or steps named:

| ID | Task |
| --- | --- |
| B1 | "Review `src/clio_agent/tools` and list the five riskiest issues with file and line" |
| B2 | "Across all HDF5 files in this folder, which datasets have NaNs and how many?" on a fixture of 200 small files |
| B3 | A deep diagnosis question on a seeded regression in a fixture repository, with no goal set |

## Phase 1: exploration primitives

1. Replace `read_file`, add ranged reads and the text block; update spill notes and `result_file` to name the continuation call.
2. Back `search_text` with ripgrep and add the modes and regex; add `fs_glob`.
3. Bound native tool results.
4. Add `workspace.explore`, delete `TOOL_USE_NOTE`, rewrite `TURN_REMINDER`.
5. Cache tool lists per connection era; fix the other overheads Phase 0 flagged.

## Phase 2: the fast explorer

1. Add the `explore` fleet role and `Prefers` on the role; let a subagent drop fragments and tools.
2. Rewrite the explore prompt and add the `delegation.parent` sentence.
3. Add `wait=True` to `spawn_agents_parallel`.

## Phase 3: tool disclosure

1. Add `defer` and the server `description` to the namespace schema and `plugin check`; add the size rule and `tools.defer_share`.
2. Write `gact/tool_catalog.py`, the deferred-server prompt entry and `search_tools` with its three query forms and exact-name-first ranking; wire the text path and the Anthropic and OpenAI native paths.
3. List deferred tools directly when `search_tools` cannot be offered; route `tools/list_changed` from deferred servers to the catalog and the plugin-change note.
4. Move `core.tasks` into the `task-protocol` skill; delete the parked `tool_info`/`call_tool` paragraph.
5. Extend `tests/test_gact/test_mcp_initial_request.py` with a deferred server: its entry and tool names are in the first request and its schemas are not.

## Phase 4: `run_code`

1. Write `gact/code_mode/` and the kernel runtime; spawn through `wrap_confined`; registry, reaping and cancellation.
2. Stubs from schemas, deferred tools included, the binding registry with `tools`, in-kernel catalog access.
3. Dispatch through the instrumented path; replace `return_raw` with the structured-value lane and move MCP Apps onto it.
4. `parent_call_id` on `Part`, the observer and the v3 projection; nested rows in gact-tui.
5. Bounds, budgets, permission behavior, read-only filtering, and the `code.run` fragment.

## Phase 5: the session plan

1. Write `session_plan` and its storage; delete `write_todos` and `todos` metadata.
2. Per-step recitation, post-compaction recitation, the compaction prompt change, staleness, and stall monitoring for every session.
3. Move plan mode onto the session plan and delete the plan file path.
4. Point the goal judge at the plan; add `core.plan`.
5. The plan panel in gact-tui.

Verify each UI change as the megachange plan describes: restart the stack, run `node scripts/cloud/ui_check.mjs` with a `--steps` script at 1440 by 900 and 390 by 844, read every screenshot, and check that `consoleErrors` and `failedRequests` are empty.

## Phase 6: qualification

Run focused tests and `ruff` on touched paths while working. Then run these against a freshly restarted stack (`claude_code` provider unless stated) and record each in the ledger with commands, handles, numbers and the screenshots read.

| ID | Scenario | Passes when |
| --- | --- | --- |
| T1 | B1 after Phases 1 to 3 | Fewer model steps and fewer prompt tokens per step than the B1 baseline, and no `sed` through the shell to page a file |
| T2 | Read a 5,000-line file | Ranged reads with line numbers; a spilled result is continued with the named call |
| T3 | Find a symbol in the repository | Files-mode search returns paths; the agent reads ranges, not pages of context |
| T4 | "Survey this repository's three subsystems" | One `spawn_agents_parallel(..., wait=True)` of `explore` children on the fleet `explore` model, reports in the same observation, `model_choice` recorded |
| T5 | A session with a large MCP server connected (cloud: synthetic, about 90 tools; local: HA-MCP) | Its schemas are not in the step's tool list, but its entry and tool names are; a plain request about a light leads to `search_tools`, then a direct call; no `prefix_mismatch` reset |
| T6 | Same as T5 on an open model (local phase) | Same outcome through the text path |
| T7 | B2 after Phase 4 | The agent uses `run_code` with gathered calls, the answer is correct, model steps are a fraction of baseline, nested calls appear under the `run_code` row |
| T8a | `auto-edits`: a script that calls a write tool | One question for the script, showing its code; the nested write runs without another; a nested call to a tool with an explicit deny policy raises in the script |
| T8b | `ask`: a script that makes three nested writes with `asyncio.gather` | The script asks once; then one write question at a time, each showing the script; approval resumes the call, denial raises in the script; waiting does not trip `code.call_timeout_s` |
| T8c | `ask`: the person picks "Allow for the rest of this script" on the first nested write | The remaining nested writes run without asking; the next `run_code` call asks again |
| T9 | `run_code` in plan mode | Write stubs are absent; the kernel is read-only |
| T10 | A script loops on a tool | It stops at the nested-call budget with a typed error in the observation |
| T11 | Cancel a turn during a long script | The kernel call is cancelled and the scope's kernel stays usable |
| T12 | Kernel state across calls | A variable set in one call is visible in the next; a fork starts clean |
| T13 | An MCP Apps view | Still renders through the structured-value lane |
| T14 | B3 after Phase 5, frontier model | The agent writes an objective and threads, closes or drops every thread with a reason, and its final answer covers its own objective |
| T15 | A run that compacts mid-turn | The plan is recited on the next step verbatim, and findings recorded before compaction are cited after it |
| T16 | B3 on an open model, before and after (local phase) | Steps to a self-chosen stop and open threads at stop recorded for both; the result is reported whether or not it improved |
| T17 | Plan mode | The approved plan is the session plan, and execution continues on it without a separate file |
| T18 | Plan panel at both viewport sizes | Objective, threads and findings render and update live; a human edit arrives as steering; no console errors |
| T19 | A broad query that contains an exact deferred tool name (Codex #21503) | `search_tools` returns that tool first |
| T20 | A deferred server adds a tool mid-session (Codex #33266) | `search_tools` and `tools.search` find the new tool; the model learns its name through the plugin-change note; no `prefix_mismatch` reset |
| T21 | A script calls a deferred tool with no prior `search_tools` (Codex #32101) | The stub exists and the call succeeds through the instrumented path |
| T22 | An agent whose configuration cannot offer `search_tools` (Codex #33608) | Its deferred tools are listed in full and `tools_deferral_unavailable` is in the trace |

The cloud session cannot qualify the Tauri desktop, Windows-only paths, HPC login nodes, open models or a real HA-MCP server; those rows are recorded as local phase. Nothing else may be blocked. A failing scenario is fixed, not deferred, except T16, which records evidence for the open-threads decision rather than a pass.

## Done

1. Every phase is implemented, and the old `read_file` shape, `TOOL_USE_NOTE`, `write_todos`, the plan file path, the `return_raw` lane, the full `core.tasks` fragment and the parked disclosure paragraph are gone.
2. Focused tests pass, and `ruff check` and `ruff format --check` are clean on touched paths.
3. Every cloud-phase scenario passes against a freshly restarted stack, each shown with commands, handles, numbers and screenshots read; local-phase rows say so.
4. The ledger holds the baseline and the after numbers for B1 to B3 per phase.
5. The branches are pushed in clio-agent, gact-tui and clio-agent-marketplace, the submodule pins point at the pushed commits, and the draft PRs carry a summary and the evidence.

## Working conventions

- Branch `claude/tool-calling-long-horizon` in clio-agent, gact-tui and clio-agent-marketplace. Open draft PRs against `develop`.
- Small Conventional Commits per step, with test evidence in each PR description.
- Keep runtime state in `.cloud-run/`. Stop the stack when you finish a work block.
