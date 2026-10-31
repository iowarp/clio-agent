# Tool calling, progressive disclosure and long-horizon work: implementation plan

This plan covers item (c) of the CLIO 1.0 release, programmatic tool calling, together with the work that decides whether it pays off: the exploration primitives it calls, progressive tool disclosure, step cost, and long-horizon operation. It comes before the RLM plan because RLM is a defined case of `run_code`, in the same way that debate is a defined case of a workflow. RLM, the in-app browser and any later code-driven feature run in the kernel this plan builds.

It turns two design documents into one contract: "CLIO exploration speed: findings for the PTC and tool disclosure campaign" and "Long-horizon exploration in CLIO: an evolving session plan" (both 2026-10-29), plus the `run_code` design from the programmatic tool calling write-up of 2026-10-22. The plan has six workstreams, each building on the ones before it:

1. **The prompt foundation.** The base agent's and the subagents' prompts are measured, scoped to the tools each agent actually has, and rewritten to explain how to work: parallel steps, exploration, persistence, delegation. Everything else builds on them.
2. **Exploration primitives.** Ranged reads with line numbers, a files-only search mode, a glob tool, and spill files the agent can page through.
3. **A fast explorer.** The `explore` role runs on a fast model with read tools and a lean prompt, and the parent can spawn a batch and get the reports back in the same call.
4. **Progressive tool disclosure.** Large tool sets are deferred: the prompt names each deferred server and its tools, and `search_tools` loads their schemas.
5. **Programmatic tool calling.** One native `run_code` tool backed by a persistent, confined Python kernel whose tool stubs call back through the agent's existing tool path.
6. **The session plan.** The plan made in plan mode becomes the session's living plan: the agent keeps editing it as it works, CLIO keeps it and re-injects it after compaction, and the rail shows it. Todos stay a separate, near-term step list.

Each workstream's presentation in the web UI ships in the same phase as its producer change (Design: presentation).

The work runs to completion without stopping for review. The human reviews the finished branches.

## Contract

This plan is the contract for the five workstreams. The megachange design documents still govern everything they cover, and decisions D1 to D29 in `acceptance-ledger.md`, the F decisions of `delegation-fork-science-plan.md`, the M decisions of `long-term-memory-plan.md` and the model fleet decisions in `model-fleet.md` stay in force. When code and this plan disagree, change the code. If the plan turns out wrong, choose the fix that best keeps its intent, update this document in the same change, and record the decision in the ledger.

| Document | Governs |
| --- | --- |
| This plan | The base agent and subagent prompts, exploration tools, the explorer role, tool disclosure, `run_code` and its kernel, the session plan, and their presentation |
| [tool-calling-ledger.md](tool-calling-ledger.md) | This plan's decisions, baselines and acceptance evidence |
| `plugins-and-composition.md` | Plugins, the composed prompt, fragments |
| `agent-sdk.md` | `Agent`, `Subagent`, `Plugin`, `plugin check` |
| `orchestration-semantics.md` | The task layer, flows, `ctx` |
| `model-fleet.md` | Model selection, `Prefers`, `model_choice` |
| `plugin-change-delivery.md` | How tool lists change mid-session without breaking the cached prefix |
| `runtime/sandbox.py` and `sandboxing-campaign-2026-07.md` | Confinement; the kernel uses it, does not redefine it |

Base: `develop` at `9a365758` in clio-agent (the merge of #1911, which lists every declared MCP server's tools before the first model request), with the matching gact-tui pin and `clio-agent-marketplace` `develop`. Delegation, fork, the science log, long-term memory and the model fleet have landed there. This plan's decisions use the prefix `TD`; its evaluation tasks use `P` (performance) and `H` (long-horizon), and its qualification scenarios use `T`.

## Two phases of qualification

This plan is implemented and qualified in two passes.

- **Cloud phase (this branch).** Claude only: the `claude_code` provider, which uses the text tool engine. Everything is implemented, and every scenario that can run on Claude runs live.
- **Local phase (later).** Open models through the fleet, a real Home Assistant MCP server, the Anthropic and OpenAI native tool-search paths live, the Tauri desktop, Windows and HPC.

| Item | Cloud phase | Local phase |
| --- | --- | --- |
| Evaluation tasks P1 to P10, H1 to H3 | `claude_code` engine; P1 to P4 also on Claude Code itself | Open model |
| Native tool search (Anthropic `defer_loading`, OpenAI `tool_search`) | Implemented; request shape covered by tests | Live |
| Large deferred MCP server (T5) | A synthetic MCP server with about 90 tools | Real HA-MCP |
| T6, T16 | Recorded as local phase | Run |
| Everything else | Live on a freshly restarted stack | Repeat on open models where relevant |

## Rules

The megachange rules carry over unchanged: delete and replace, the task layer is the only execution authority, the model decides, no silent fallback, one mechanism per concept, no fifth store, the repository rules in `AGENTS.md`, and evidence over claims. Six more apply here:

1. **One execution path for tools.** A call from a script, a direct call and a call from a child all run through the same instrumented dispatch: permission gate, file policy, hooks, MCP v2 tasks, cancellation, the observer and presentation. The kernel never gets a second path, an MCP client or credentials.
2. **The cached prefix stays stable.** Nothing in this plan changes the system text or the tool list in the middle of a conversation except through the mechanisms in `plugin-change-delivery.md`. `run_code` has one fixed schema for the life of the process.
3. **Provider independent.** Everything works on every provider, including open models and the `claude_code` text engine. Provider-native tool search is used where it exists as an optimization of the same behavior, never as the only path. Vendor-hosted programmatic tool calling (Anthropic's and OpenAI's) is not used.
4. **Surface reality, never decide.** The runtime may show the agent structured state the agent wrote (its plan, its budget, a spilled file's path). It does not decide that work is unfinished, route calls, or pick tools for the agent.
5. **Measure before and after.** Every workstream is judged against the Phase 0 baseline on the same tasks, not against expectations.
6. **Prompts explain, they do not handcuff.** Prompt text tells the model how to work and what state means. It does not add "exactly once" or "never" rules to suppress a failure seen in one run; that failure is fixed at its cause (`.claude/CLAUDE.md`, principle 3). A prompt change is judged by the evaluation like a code change.

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

## Design: the prompt foundation

Every other workstream depends on what the base agent and its subagents are told. Measured at the base (TD26), the standard agent sends about 3,800 words of system text and about 20k tokens of tool schemas before the user's message. `core.tasks` alone is about 2.8k tokens of task-protocol detail sent to every agent and every child, and it names a `finish/submit` argument the default agent does not have. Subagents inherit the parent's whole prompt: `explore`'s composed prompt is 2,488 words, 76 of them its own, and tells it to delegate, prefer workflows and edit files, none of which it can do. Nothing says to make independent calls together (only a one-off injected note does), how to explore, when to stop, to verify before finishing, that compaction exists, or when and how to delegate. Several rules are handcuffs. This workstream treats the prompts as a product: read in full, measured, and changed against the evaluation. The public agents' prompts it draws on are listed in the ledger.

**Seeing the exact prompt.** `clio-agent plugin resolve <agent> --exact [--subagent <role>] [--provider <name>]` prints what the model receives: the system text with the workspace and skills suffix, the injections a step can carry, and the tool schemas as the provider sends them (JSON, or the `claude_code` engine's text rendering). `GET /v1/sessions/{sid}/agent` gains the same view. Prompt sizes for the root agent and each subagent are recorded per phase. The 150-token fragment budget stays a warning for third-party plugins; core and default-marketplace fragments are judged by each agent's total instead (TD24).

**Fragments follow the tools.** A fragment's `when` gate is evaluated against the agent's final tool list, after effect gating, session posture and allowlists, not against the resolved plugin set. A role that cannot spawn, write or run workflows no longer receives the text describing them (TD25).

**The base agent.** Short sections that explain rather than constrain:
- **Working:** make independent calls together in one step and dependent ones in sequence; an extra turn costs more than a wider step.
- **Exploring:** search first, then read the ranges you need; stop gathering once you can name what to act on, with at most one further round.
- **Persisting and stopping:** keep going until the objective is met with evidence; uncertain or indirect evidence is not done; verify before finishing; the context is compacted automatically, so never wrap up early to save it.
- **Delegating:** delegate parallel independent work, high-volume reading and speculative research, and do small, exact work directly; brief a child like a capable colleague who has just walked in, with the question rather than your conclusion; do not repeat its searches yourself; check what comes back; wait for running children before finishing.

`core.tasks` shrinks to three sentences (what a handle is, `wait_tasks` collects, results over the digest limit arrive as files); the rest moves to a `task-protocol` skill in the core plugin, and the stale `finish/submit` reference goes. The handcuffs go: `write_todos`'s "exactly ONE per step", `TURN_REMINDER`'s list of fence mistakes seen in past runs, `load_skill`'s "Call BEFORE", and the rendering fragment's negation of a removed rule. Narrow fragments (`shell.gh-releases`, `shell.prefer-gh`, `pdf.usage`) gain `when` gates. The skills listing keeps a name and a one-line description per skill. The largest tool descriptions (`create_artifact`, `shell_bash`, `connected_data_connect`, `run_workflow`, `raise_alert_card`) keep their usage rules and lose repetition and long examples, which move to skills.

**The engine reminder.** `TOOL_USE_NOTE` is deleted. `TURN_REMINDER`, sent with each `claude_code` request, keeps only the fence syntax the engine needs and adds the batching nudge Anthropic recommends after each round of tool results: "First privately list what you need next; then request every item that doesn't depend on another's result in this one response."

**Subagent prompts.** Each role has its own prompt of at most about 300 words and receives only the fragments its tools need:
- `explore`: a fast, read-only searcher. Sweep wide with glob and files-mode search in parallel, read narrow ranges, follow references, and match depth to the thoroughness the caller states in the assignment (`quick`, `medium` or `thorough`). Return conclusions with absolute paths and line numbers, not file contents. It cannot spawn.
- `planner`: understand, explore, then design; return the steps and the three to five files or datasets the work turns on.
- `review`: report discrete, actionable issues the change introduced, each with its location, impact and confidence, and an overall verdict.
- `delegation.child`: the report is the only thing the parent reads, so it carries the answer and its evidence.

**Compaction.** The compaction prompt asks for a handoff to another model that will resume the work: goal, constraints, what is known, the state of files and data, and the next step. The session plan is kept verbatim beside it (Design: the session plan).

## Design: exploration primitives

`run_code` scripts call these same tools, so their shape matters twice.

**`fs_read_file`.** New signature `read_file(filepath, offset=1, limit=2000, access_reason="")`, lines 1-based. It returns a TEXT content block: a header line `path  lines a-b of N  (size)`, then the lines prefixed with their numbers. Binary files return a typed error naming the format-specific reader if one exists. The structured result (path, range, total lines, truncated) goes in `structuredContent` for scripts. The old whole-file dict shape is deleted.

**Spill files.** The `result_spilled` note and the subagent `result_file` both name the exact `fs_read_file` call that continues reading, since the spill folder is inside the readable roots.

**`fs_search_text`.** Backed by ripgrep (bundled through the existing package preparation; a missing binary is a typed error at startup, not a Python fallback). Arguments: `query`, `path`, `regex=False`, `glob`, `mode="files"`, `context_lines=0`, `case_sensitive`, `limit`. `files` returns paths and match counts, `content` returns numbered lines with context, `count` returns totals. The 1,000-file and 3 s caps go; the result cap and the explicit truncation report stay.

**`fs_glob`.** `glob(pattern, path, limit=200)` returning paths sorted by modification time, newest first, with an explicit truncation flag. `list_directory` stays one level.

**Native tool bounding.** Native tool results pass through `bounded_model_tool_result` like MCP results do, with the same spill. `run_code` relies on this.

**Fragment `workspace.explore`** (about 60 words): locate with glob or files-mode search, read the ranges you need, cite paths and lines, and make independent reads in one step. It names the tools this workstream adds; the general working and exploring guidance is in `core` (the prompt foundation).

**Overheads.** `ExternalTasks._call` caches the tool list per connection era (the executor's `tools_revision`) instead of listing before each call. The direct stdio route keeps its connection for the session if Phase 0 shows per-call spawning costs more than baseline noise.

## Design: the fast explorer

**Model.** `explore` declares `Prefers(model=<fleet fast role>)` through the existing fleet: the fleet gains an `explore` role (beside the memory roles) picked once in Fleet settings, defaulting to the caller's model when unset. `model_choice` records the choice as for any child.

**Tools and prompt.** The role's prompt and its tool-scoped fragments come from the prompt foundation; effect gating already refuses writes, and the spawn tools are withheld so it cannot spawn.

**Blocking batch.** `spawn_agents_parallel` gains `wait: bool = False`. With `wait=True` it returns each child's report (or its `result_file` when over the digest limit) in the same observation, after the last child finishes or the call is cancelled. It is a mechanism, so it does not route anything.

**Parent prompt.** `delegation.parent` gains one sentence: a broad survey of a codebase or dataset splits into several `explore` children in one `spawn_agents_parallel(..., wait=True)` call.

## Design: progressive tool disclosure

The shape follows what Claude Code ships and what the Anthropic, OpenAI and MCP client guidance converge on: the model always knows every server and tool that exists, and only the schemas wait until needed. A model that cannot see a tool concludes it does not have it (the failure #1911 fixed for slow servers, and Codex issues #21503 and #56973 for search misses), so deferral must never make a server invisible.

**What the prompt carries.** Each tool is either listed (full schema in the tool list, as today) or deferred. For each deferred server the prompt carries one entry: the server's name, its description, and the names of its deferred tools, without docstrings or schemas. The description is the server's MCP `instructions` when it sends them, otherwise the `description` the plugin declares for the server. `plugin check` warns when a deferrable server has neither. The entry lives in the composed prompt, so it is part of the cached prefix and is built from the real listing that #1911 guarantees before the first request.

**What is deferred.** The `ai.iowarp.clio` namespace gains `defer: true | false` per MCP server declaration and per plugin tool. When a plugin does not say, a server is deferred when its listed schemas exceed `tools.defer_share` (default 5%) of the model's context window, which the handshake already discovers; small servers stay listed in full. Core loop tools (fs, shell, `load_skill`, delegation, `run_code`, `search_tools`, the session plan) are never deferred. Task-control tools beyond `wait_tasks` are deferred by the core plugin's declaration.

**`search_tools`.** One native tool, `search_tools(query, limit=8)`, over the agent's deferred catalog (`gact/tool_catalog.py`, the one catalog). It takes three query forms: `select:a,b` loads exact names; plain keywords rank by BM25 over name, description, argument names and server; `+term rest` requires `term` in the name and ranks by the rest. Names are tokenized on `_` and `__` as well as whole, and an exact name anywhere in a query always returns that tool first, ahead of the ranked results. It returns the matching schemas and makes them callable from the next step through `_clio_unlisted` and `with_tool_introductions`, so the prefix is untouched; a loaded tool joins the tool list at the next compaction, as any introduced tool does. On providers with native deferral (Anthropic `defer_loading` tool search, OpenAI Responses tool search) the same catalog feeds the provider's mechanism instead of the text introduction; on open models and the `claude_code` engine the text path is used.

**Reachability.** If an agent's configuration cannot offer `search_tools` (a tool allowlist without it, or a provider path that drops it), its deferred tools are listed in full instead, and the trace records `tools_deferral_unavailable`. Inside `run_code`, deferred tools have stubs like any other tool, and `tools.search` reads the same catalog, so a script never needs `search_tools` first.

**Tool list changes.** A `tools/list_changed` from a deferred server updates the catalog and the kernel stubs, never the listed tools. A new tool name reaches the model through the plugin-change note, so the prefix stays intact. A change from a listed server behaves as today.

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

The session holds two kinds of working state with different jobs, as Claude Code, Codex, Gemini CLI and Manus also separate them (sources in the ledger):

| | The plan | Todos |
| --- | --- | --- |
| Holds | Intent and understanding: the objective, the approach, lines of inquiry with how each ended, and what has been learned | The next few concrete steps, one in progress |
| Changes when | Understanding or direction changes | Every few steps |
| Tools | `plan_enter`, `plan_update`, `plan_exit` | `write_todos` |
| Survives compaction | Re-injected from the plan file | Re-injected from `session.metadata['todos']` |
| On the rail | Plan section | Todos section |

Long-term memory is neither: it holds what a future session needs (`long-term-memory-plan.md`) and never progress. The science log stays the user-facing record of a study.

**The plan is a CLIO-held document.** It is the Markdown file plan mode already writes under CLIO's state folder (`Agent state/workspaces/<hash>/plans/`), its path recorded on `session.metadata['plan_file']`, as today (no new store). Its status is recorded beside the path: `draft` while in plan mode, `approved` after `plan_exit` is approved, then `done` or `abandoned` when the agent says so. A fork copies the plan file. Children do not get plan tools; a parent's plan can name the child handles working on a thread, and the parent records what their reports establish.

**Tools.**
- `plan_enter(reason)` enters plan mode. It replaces the `planning` skill's entry effect; the guidance the skill carried moves into the plan-mode reminder, which already exists. Plan mode keeps its read-only lock and its approval gate.
- `plan_update(content=None, old=None, new=None, status=None)` writes the whole plan or replaces one exact passage, and can set the status to `done` or `abandoned`. It works in plan mode and outside it, so the agent replans at any step without a mode switch. It replaces the plan-mode ACL carve-out that let `fs_write_file` and `fs_edit_file` reach the plans folder.
- `plan_exit(summary, ...)` asks for approval, as today. After approval, execution starts by writing todos from the plan.

**What the plan contains.** A structure hint, not a schema, merging today's epistemic ledger with the long-horizon design: **Objective** (the question, what an answer must contain, what counts as done); **Approach**; **Threads**, each open, active, closed or dropped, with a one-line reason when closed or dropped (the decision log), and the child handles working on it; **Findings**, each a claim with its references (`call:` refs, paths and lines, handles), a correction marking the old finding superseded rather than deleting it. When a plan already exists, the agent first judges whether it still applies (the existing re-entry note).

**Re-injection.** While a plan is `draft` or `approved`, CLIO injects it verbatim on the first step after a compaction and when a session resumes, with one line: if this plan is relevant to the current work and not finished, continue on it, and update it with `plan_update` when your understanding or direction changes. The model judges relevance; CLIO only says the plan exists and shows it (rule 4). A human edit from the rail is injected the same way, marked as the person's. The plan is not recited every step, since the agent's own `plan_update` calls are already in its context. After `plan.stale_steps` (default 25) steps without a `plan_update` or `write_todos`, a one-line note says the plan exists. The stall monitor counts both as progress and runs in every session, not only playbook ones.

**Todos.** `write_todos` stays as the near-term step list: whole-list replace, imperative items, at most one in progress. Its description loses the handcuffs (Design: the prompt foundation). It is recited at turn start when it changed, as today, and re-injected after compaction.

**Compaction.** The compaction prompt asks for a handoff to whoever resumes (narrative, data and file state, constraints, open questions) and is told that the plan and the todos are re-injected verbatim, so it does not restate them. Their injections are excluded from what compaction summarizes.

**Goal and stopping.** The goal judge reads the plan and the todos beside the transcript tail; goal and stop hooks remain the backstop. The runtime does not comment on open threads or todos when the agent stops (TD12); whether agents stop with open threads is measured, and that evidence decides whether to revisit.

**Fragment `core.plan`** (about 80 words): for long or open-ended work, enter plan mode or write the plan first; keep the objective and threads current; rewrite the plan when a finding changes direction; make checking your claims the last thread; stop when every thread is closed or dropped with a reason; use todos for the next concrete steps.

**UI.** The Work rail gets a Plan section that renders the plan with its status in words and updates when `plan_update` runs. The person can edit it there; the edit is saved to the plan file and reaches the agent as steering attributed to them. The Todos section stays.

## Design: presentation

What the person sees is part of each workstream, not a later pass. The work extends the shared producer contract and the GACT readers (`docs/tool-result-presentation.md`); no tool gets its own viewer. The UI rules in gact-tui apply: status is never only a colour, dot or icon; JSON and producer identifiers are technical detail; updates stream at display-frame cadence and honour reduced motion. `web/src/components/clio/tool-invocation.tsx` is at 526 of its 800 lines, so new pieces go in their own modules.

| Phase | Producer and contract | Web |
| --- | --- | --- |
| 2 | Rewrite the `read_file` presenter for ranges (the new result has no `content` key, so today's presenter would fall back to generic output); `start_line` on code blocks; a line on file links; search presenters for `files`, `content` and `count`; a `glob` presenter; spilled results and `result_file` link the file and show the continuation call | `CodeBlock` numbers from `start_line`; `presentation-link.tsx` passes the line to `onOpenFileReference`; mode, pattern and glob in row metadata |
| 3 | The blocking spawn emits wait-style report blocks, one per child | One report-tabs component shared by `wait_tasks` and the blocking spawn: tabs named by child, status in text, the `model_choice` model shown; an interrupted batch is not a failed child |
| 4 | `deferred` and the server description in the resolution schema; a `search_tools` presenter (query and count; matched tools with server and description, exact names first; schemas in technical details) | The agent composition view splits each server's tools into listed and deferred and shows `tools_deferral_unavailable` as text; the plugin-change chip names newly reachable tools |
| 5 | `parent_call_id` on `Part`, the event projection and `part_to_v3_block`; the `run_code` presenter (script as a Python block, live output, final value, "14 calls, 1 failed"); permission payloads carry the script; an `allow_script` action | `parent_call_id` in `packages/core` with a children-by-parent selector; the turn model attaches children to their parent; a nested-rows module under the `run_code` row with a "Show all" control; evidence and call counts group nested calls; the permission prompt shows the script and offers "For the rest of this script" (today an unknown action renders as a disabled button, so producer and UI ship together); "Script paused, waiting for you" on the row |
| 6 | The work state carries the plan text, path and status; a work event on each `plan_update`, `plan_exit` approval and `write_todos`; presenters for `plan_enter`, `plan_update` (the changed passage) and `plan_exit` | A Plan section in the Work rail above the Todos section, rendering the plan's Markdown with its status in words and links for its references, updated on the work event instead of the current 5-second poll; an Edit action that saves the plan and sends the change as steering attributed to the person |

Every item ships with producer tests, frontend fixtures with large, empty, partial, failed and interrupted results, and a `scripts/cloud/checks/` step file for `ui_check.mjs`, run at 1440 by 900 and 390 by 844 with every screenshot read. The link in `docs/tool-result-presentation.md` to a gact-tui section that does not exist is pointed at the guide's own "Adding or changing a tool" section.

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
| Plan storage | The plan-mode Markdown file under CLIO's state folder, path and status on `session.metadata`; todos stay on `session.metadata['todos']` |
| Plan per agent or per tree | Per root session, copied by a fork; children have no plan tools and report, the parent records |
| Runtime note on open threads at stop | Off; decided again from qualification evidence |
| Plan mode | Entered with `plan_enter`, keeps its read-only lock and approval gate; `plan_update` works in and out of plan mode |

## Evaluation

The campaign's goal is long, fast execution: cheap, wide steps, and an agent that keeps going coherently over hundreds of them. The evaluation measures those two things, run length and performance, against the base and after every phase. Correctness is recorded where it can be graded mechanically, but it is not an acceptance gate in this plan; prompts are tuned for correctness later. The qualification scenarios (Phase 7) are separate: they show that each mechanism works, and many of them name tools, so they are not evidence of the agent's choices.

**Tasks.** Every task is a plain request with no tools or steps named, run on a frozen fixture checked into `scripts/eval/tool_calling/fixtures/`, never on the live checkout, which this plan changes.

| ID | Suite | Task |
| --- | --- | --- |
| P1 | Performance | Locate a function and every caller in a frozen copy of a source tree |
| P2 | Performance | Find a fact that sits deep in a 5,000-line file |
| P3 | Performance | "Review this package and list the five riskiest issues with file and line" on a frozen package with seeded bugs |
| P4 | Performance | "Survey this repository's three subsystems" on the frozen tree |
| P5 | Performance | "Across all HDF5 files in this folder, which datasets have NaNs and how many?" on 20 files |
| P6 | Performance | P5 on 200 files |
| P7 | Performance | P5 on 2,000 files |
| P8 | Performance | A request that needs one tool of a synthetic MCP server with about 90 tools, beside distractor servers |
| P9 | Performance | A second P8 request whose wording shares no words with the tool's name |
| P10 | Performance | A control request that needs no deferred tool |
| H1 | Long-horizon | Diagnose a seeded regression in a fixture repository, with no goal set |
| H2 | Long-horizon | "Why is this I/O path slower than it was?" on a fixture with a seeded slowdown, with no goal set |
| H3 | Long-horizon | H1 with the compaction threshold lowered so the run compacts at least once |

**Performance metrics, per run and per step:** model steps; calls per step; prompt tokens per step, uncached and cached (`turn_lm_ledger`); output tokens; wall time; TTFT and decode time; batch wall time against the slowest call; step overhead outside model and tools; `prefix_mismatch` and `first_call` resets; and the share of the subscription's 5-hour and 7-day windows the run used (the `claude_code` `rate_limit_event`). For P8 to P10 also: `search_tools` calls, and how often the agent concludes it lacks a tool it has.

**Prompt size:** system text, injections and tool schemas for the root agent and each subagent, from the exact dump; it needs no live run and is recorded per phase.

**Long-horizon metrics:** steps and wall time until the agent stops on its own; per-step latency and prompt tokens against step number (a long run should stay flat); compactions, and whether the plan and todos were re-injected verbatim after each and findings recorded before it were cited after it; `plan_update` and `write_todos` calls over the run; threads and todos open at the stop and how each thread ended (closed or dropped with a reason, or left open), read from the plan's Threads section when it follows the structure; and how often goal or stop hooks re-drove the agent.

**Completion guard and correctness.** A run counts only if it ended with a final answer, not an error, a cancellation or an empty reply, so that "faster" never means "gave up". Correctness is recorded where an answer key grades it mechanically (P1, P2, P3 by seeded issue, P5 to P8, H1 and H2 by root cause) and reported beside the numbers, without a pass threshold. No LLM judge is used.

**Protocol.**
- Each task runs three times per configuration on the `claude_code` engine: the main agent on `claude-sonnet-5-5` and the fleet `explore` role on `claude-haiku-5-5` (TD22). The runner is `scripts/eval/tool_calling/`, built on the agent-test harness (`tests/test_real_cases/clio_sut.py`) with a `claude_code` cell.
- Configurations: the base; Claude Code itself on the same model for P1 to P4 (the speed doc's reference); after Phase 1 (all, since the prompt changes every request); after Phase 2 (P1 to P4); after Phase 3 (P1 to P4); after Phase 4 (all, since disclosure changes every request); after Phase 5 (P5 to P7 and H1); after Phase 6 (all). Then ablations, one feature off at a time on the tasks it touches: deferral, `run_code`, the fast explorer, and on H1 to H3 the working state three ways (no plan or todo tools, todos only, plan and todos), since newer models may track multi-step work without them. Phases 1 and 2 replace the old prompt and tools, which cannot be switched back on; they are judged against the base.
- The pass for timing runs one task at a time; other runs may go three at a time, since concurrency changes wall time but not steps or tokens.
- Phase 0 measures each task's share of the usage windows and confirms the schedule fits them before Phase 1 starts.

**Targets.** After the base is measured and before Phase 1 starts, the ledger records a target per workstream on the tasks it touches, for example the root agent's and the `explore` child's prompt size after Phase 1, steps and prompt tokens per step on P1 to P4 against the base and against Claude Code, P5 to P7 cost growing much more slowly than file count after Phase 5, prompt tokens per step on P8 to P10 after Phase 4 with no rise in "lacks a tool" conclusions, and on H1 to H3 a flat per-step cost, no re-drives and coherent plans across compaction. Run length on Claude is recorded, not targeted: strong models already run long, and the length gains are measured on open models in the local phase.

## Phase 0: baseline and decisions

No production code changes in this phase.

1. Build the fixtures with their answer keys, the synthetic MCP server and the runner (Evaluation, above).
2. Run every task on the base and Claude Code on P1 to P4, and record the metrics and each task's usage-window share in the ledger. The open-model baselines are taken in the local phase.
3. Time `list_tools` per call, direct stdio per-call spawn, sequential segment writes in `step_done`, and per-call `asyncio.Runner`. Record which exceed baseline noise; only those are fixed.
4. Run `sandbox_conformance.py` on the cloud host and record which fence level a child process gets, since the kernel inherits it.
5. List every caller of the `planning` skill's entry effect, the plan-mode ACL carve-out for the plans folder, `return_raw`, `read_file`'s dict shape and `TOOL_USE_NOTE`, for the deletions.
6. Fill in `tool-calling-ledger.md`: decisions TD1 onward, the base numbers, the targets and the scenarios below.
7. Commit, push, open draft PRs, and continue.

## Phase 1: the prompt foundation

1. Add the exact prompt dump to the CLI and the session agent route; record the root agent's and each subagent's prompt size.
2. Evaluate fragment `when` gates against the final tool list.
3. Rewrite the base agent's sections; move `core.tasks` into the `task-protocol` skill; remove the handcuffs; gate the narrow fragments; trim the skills listing; trim the largest tool descriptions.
4. Delete `TOOL_USE_NOTE`; rewrite `TURN_REMINDER`.
5. Write the `explore`, `planner` and `review` prompts in the marketplace `delegation` plugin; rewrite `delegation.child`.
6. Read the exact dumps for the root agent and each role in full, then run the evaluation's "after Phase 1" configuration.

## Phase 2: exploration primitives

1. Replace `read_file`, add ranged reads and the text block; update spill notes and `result_file` to name the continuation call.
2. Back `search_text` with ripgrep and add the modes and regex; add `fs_glob`.
3. Bound native tool results.
4. Add `workspace.explore`.
5. Cache tool lists per connection era; fix the other overheads Phase 0 flagged.
6. The Phase 2 presentation (Design: presentation).

## Phase 3: the fast explorer

1. Add the `explore` fleet role and `Prefers` on the role.
2. Add the `delegation.parent` sentence on fanning a broad survey out to `explore` children.
3. Add `wait=True` to `spawn_agents_parallel`.
4. The Phase 3 presentation.

## Phase 4: tool disclosure

1. Add `defer` and the server `description` to the namespace schema and `plugin check`; add the size rule and `tools.defer_share`.
2. Write `gact/tool_catalog.py`, the deferred-server prompt entry and `search_tools` with its three query forms and exact-name-first ranking; wire the text path and the Anthropic and OpenAI native paths.
3. List deferred tools directly when `search_tools` cannot be offered; route `tools/list_changed` from deferred servers to the catalog and the plugin-change note.
4. Delete the parked `tool_info`/`call_tool` paragraph.
5. Extend `tests/test_gact/test_mcp_initial_request.py` with a deferred server: its entry and tool names are in the first request and its schemas are not.
6. The Phase 4 presentation.

## Phase 5: `run_code`

1. Write `gact/code_mode/` and the kernel runtime; spawn through `wrap_confined`; registry, reaping and cancellation.
2. Stubs from schemas, deferred tools included, the binding registry with `tools`, in-kernel catalog access.
3. Dispatch through the instrumented path; replace `return_raw` with the structured-value lane and move MCP Apps onto it.
4. `parent_call_id` on `Part`, the observer and the v3 projection.
5. Bounds, budgets, permission behavior, read-only filtering, and the `code.run` fragment.
6. The Phase 5 presentation.

## Phase 6: the session plan

1. Add `plan_enter` and `plan_update`; record the plan status; delete the `planning` skill's entry effect and the plans-folder ACL carve-out; copy the plan file on fork.
2. Re-inject the plan after compaction and on resume while it is `draft` or `approved`; re-inject todos after compaction; the compaction prompt change; the staleness note; stall monitoring for every session.
3. Rewrite the plan-mode structure hint (objective, approach, threads, findings) and the `write_todos` description.
4. Point the goal judge at the plan and todos; add `core.plan`.
5. The Phase 6 presentation.

Verify each UI change as the megachange plan describes: restart the stack, run `node scripts/cloud/ui_check.mjs` with a `--steps` script at 1440 by 900 and 390 by 844, read every screenshot, and check that `consoleErrors` and `failedRequests` are empty.

## Phase 7: qualification

Run focused tests and `ruff` on touched paths while working. Then run these against a freshly restarted stack (`claude_code` provider unless stated) and record each in the ledger with commands, handles, numbers and the screenshots read.

| ID | Scenario | Passes when |
| --- | --- | --- |
| T1 | P3 after Phases 2 to 4 | No `sed` through the shell to page a file; ranged reads and files-mode search are used |
| T2 | Read a 5,000-line file | Ranged reads with line numbers; a spilled result is continued with the named call |
| T3 | Find a symbol in the repository | Files-mode search returns paths; the agent reads ranges, not pages of context |
| T4 | "Survey this repository's three subsystems" | One `spawn_agents_parallel(..., wait=True)` of `explore` children on the fleet `explore` model, reports in the same observation, `model_choice` recorded |
| T5 | A session with a large MCP server connected (cloud: synthetic, about 90 tools; local: HA-MCP) | Its schemas are not in the step's tool list, but its entry and tool names are; a plain request about a light leads to `search_tools`, then a direct call; no `prefix_mismatch` reset |
| T6 | Same as T5 on an open model (local phase) | Same outcome through the text path |
| T7 | P6 after Phase 5 | The agent uses `run_code` with gathered calls, and nested calls appear under the `run_code` row |
| T8a | `auto-edits`: a script that calls a write tool | One question for the script, showing its code; the nested write runs without another; a nested call to a tool with an explicit deny policy raises in the script |
| T8b | `ask`: a script that makes three nested writes with `asyncio.gather` | The script asks once; then one write question at a time, each showing the script; approval resumes the call, denial raises in the script; waiting does not trip `code.call_timeout_s` |
| T8c | `ask`: the person picks "Allow for the rest of this script" on the first nested write | The remaining nested writes run without asking; the next `run_code` call asks again |
| T9 | `run_code` in plan mode | Write stubs are absent; the kernel is read-only |
| T10 | A script loops on a tool | It stops at the nested-call budget with a typed error in the observation |
| T11 | Cancel a turn during a long script | The kernel call is cancelled and the scope's kernel stays usable |
| T12 | Kernel state across calls | A variable set in one call is visible in the next; a fork starts clean |
| T13 | An MCP Apps view | Still renders through the structured-value lane |
| T14 | H1 after Phase 6 | The agent writes a plan with an objective and threads, keeps it current with `plan_update`, and every thread at its stop is closed or dropped with a reason |
| T15 | A run with an approved plan that compacts mid-turn | The plan and todos are re-injected verbatim on the next step, and findings recorded before compaction are cited after it |
| T16 | H1 on an open model, before and after (local phase) | Steps to a self-chosen stop and open threads at stop recorded for both; the result is reported whether or not it improved |
| T17 | Plan mode | `plan_enter`, a written plan and `plan_exit` approval; execution starts by writing todos and later replans with `plan_update` without re-entering plan mode |
| T18 | Plan and Todos sections at both viewport sizes | The plan renders with its status and updates on `plan_update`; a person's edit is saved and arrives as steering; todos render separately; no console errors |
| T19 | A broad query that contains an exact deferred tool name (Codex #21503) | `search_tools` returns that tool first |
| T20 | A deferred server adds a tool mid-session (Codex #33266) | `search_tools` and `tools.search` find the new tool; the model learns its name through the plugin-change note; no `prefix_mismatch` reset |
| T21 | A script calls a deferred tool with no prior `search_tools` (Codex #32101) | The stub exists and the call succeeds through the instrumented path |
| T22 | An agent whose configuration cannot offer `search_tools` (Codex #33608) | Its deferred tools are listed in full and `tools_deferral_unavailable` is in the trace |

The cloud session cannot qualify the Tauri desktop, Windows-only paths, HPC login nodes, open models or a real HA-MCP server; those rows are recorded as local phase. Nothing else may be blocked. A failing scenario is fixed, not deferred, except T16, which records evidence for the open-threads decision rather than a pass.

## Done

1. Every phase is implemented, and the old `read_file` shape, `TOOL_USE_NOTE`, the `planning` skill's entry effect, the plans-folder ACL carve-out, the `return_raw` lane, the full `core.tasks` fragment and the parked disclosure paragraph are gone.
2. Focused tests pass, and `ruff check` and `ruff format --check` are clean on touched paths.
3. Every cloud-phase scenario passes against a freshly restarted stack, each shown with commands, handles, numbers and screenshots read; local-phase rows say so.
4. The ledger holds the evaluation numbers for every configuration, the targets set after the base, and whether each was met.
5. The branches are pushed in clio-agent, gact-tui and clio-agent-marketplace, the submodule pins point at the pushed commits, and the draft PRs carry a summary and the evidence.

## Working conventions

- Branch `claude/tool-calling-long-horizon` in clio-agent, gact-tui and clio-agent-marketplace. Open draft PRs against `develop`.
- Small Conventional Commits per step, with test evidence in each PR description.
- Keep runtime state in `.cloud-run/`. Stop the stack when you finish a work block.
