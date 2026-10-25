# CLIO Orchestration Semantics

Oct 7, 2026 · @Jaime

## Summary

The field is moving from declared graphs to orchestration written as code, with every child call checkpointed so a run can be replayed after a crash. That model fits DSPy directly, since a DSPy program is already code in `forward()`, and it fits CLIO's new task layer, which already gives every child call a durable handle. The proposal is that flows become plugin content: a flow is a DSPy module a plugin contributes, the agent selects one by id, and the runtime gives it a small set of primitives that are ordinary CLIO tasks.

- **Code-first won.** Google ADK 2.0 deprecated its Sequential, Parallel and Loop agents in favor of code that calls `ctx.run_node()`. Claude's Workflow tool and Microsoft Agent Framework's functional API work the same way. Each memoizes completed child calls and skips them on resume.
- **Graph engines carry real costs.** Superstep execution, used by LangGraph and Microsoft Agent Framework, makes a fast branch wait for its slowest sibling. That is a poor fit for HPC jobs of very different lengths.
- **Three primitives cover nearly everything.** Calling an agent and getting a value back, handing control to another agent, and sequence, parallel and loop written as ordinary code.
- **Model-written orchestration is a flow like any other.** Recursive Language Models and programmatic tool calling let the model write the orchestration code itself. They win on long-context and parallel work and lose on simple tasks, so they belong as selectable flows, not the default.
- **Durability means a journal, not an orchestrator.** Every engine re-runs the orchestrating function and returns recorded results for completed steps. In CLIO the journal is task history: a call's invocation id fixes its task handle, so replay re-attaches to work instead of submitting it again.
- **CLIO now has the substrate but not the SDK.** The async-tasks branch gives every child call a handle, shared wait and cancel controls, mailbox delivery and checkpointed declared workflows. What is missing is orchestration written as code outside the model, fan-out inside a workflow, and flows as plugin content.

## Where CLIO is today

This section describes branch `codex/agent-facing-async-tasks` at `47b6bd49` (Oct 20), which the team is merging into `develop`. The branch deleted the old task registry and rebuilt the task layer, so most of what an orchestration SDK needs underneath already exists.

**One agent loop.** `ClioReAct` (`gact/agents/clio_react.py`) is a DSPy module and the only agent loop. Declared module variants (`gact/agents/module_variants.py`) can wrap it in `dspy.BestOfN` or `dspy.Refine`.

**One task record.** Long-running work of every kind is a task described by one typed record, `TaskSnapshot` (`arc/task_types.py`), with seven kinds: Shell, Download, Indexing, MCP, Session, Subagent and Question. Accepted work returns an opaque handle at once, and acceptance is not completion. `query_tasks`, `observe_tasks`, `wait_tasks`, `cancel_tasks` and `get_task_result` (`gact/task_operations.py`) accept any mix of handles. Records are written to Core lanes through one writer (`TaskHistory`); the plan rules out a separate workflow database.

**Idempotent spawn.** `Conversations.spawn(caller, assignment, agent_id, invocation_id, ...)` (`gact/conversation_assignments.py`) persists the assignment before inference starts and derives the handle from the session and the invocation id. Spawning again with the same invocation id addresses the same task. It also takes `workflow_id`, `workflow_step`, `execution_deadline`, `inference_concurrency` and `parent_handle`.

**Delivery.** Completion is a mailbox event. A busy agent sees results at its next model iteration, and an idle one is woken by the turn runner without a human message. Stop ends the turn and its wait while accepted tasks keep running. Cancelling a subagent closes admission to its subtree before cancelling it. An uncertain acceptance is never submitted again; `wait_tasks` reports it as `task_state_uncertain`.

**Declared workflows.** `gact/workflows.py` still parses the blueprint `workflow:` block, but its runner now launches each step as an ordinary child assignment, waits through the shared controls and checkpoints the execution in task history under its invocation id. Re-invoking the same workflow continues from the checkpoint. Steps still run one at a time with no branching, and only a declared deadline ends a step; the inactivity driver is gone.

**Goals, loops and schedules.** `/goal` pursues an objective with a judge between turns. In CLIO "loop" already means monitoring at intervals (`/loop`), and schedules run at a time. `wait_for` parks a goal on task handles.

**Relay.** clio-relay's dedicated task, placement and wait paths were removed. Relay now connects as an ordinary MCP server, so an HPC job is a call to a task-capable MCP tool.

**Not there yet.** There is no orchestration code outside the model apart from the linear runner, no fan-out inside a workflow, no model-written orchestration code, and agents are still blueprints.

## Graph and workflow frameworks

Every major graph framework now ships a second, code-first API beside its graph, and the code-first one is where durability is simplest. Versions are as of October 2026.

**LangGraph** (1.2.14). A `StateGraph` has typed state with a reducer per key, nodes as functions, and routing by normal edges, conditional edges or `Command(goto=...)`. It executes in Pregel-style supersteps: all ready nodes run, their writes apply together, and a checkpoint is written at each boundary. `Send` provides dynamic map-reduce. `interrupt()` pauses for a human, and on resume the node restarts from its top, so side effects before the interrupt must be idempotent. The functional API (`@entrypoint`, `@task`) saves task results into the checkpoint and replays forward, with nondeterministic work required to sit inside tasks ([graph API](https://docs.langchain.com/oss/python/langgraph/graph-api), [checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers), [functional API](https://docs.langchain.com/oss/python/langgraph/functional-api)). Thoughtworks moved LangGraph from Adopt to Trial in April 2026, citing global shared state and pointing to simpler, easier-to-debug approaches ([Radar](https://www.thoughtworks.com/radar/languages-and-frameworks/langgraph)).

**Microsoft Agent Framework** (1.20.0). Typed executors joined by edges: direct, conditional, switch-case, fan-out and barrier fan-in that waits for every source. It also runs in supersteps with a checkpoint after each, and its own docs warn that a chained branch cannot advance until a slow sibling finishes. Restoring a checkpoint requires the same topology and executor ids. Its newer functional API caches step results by step name and call index and re-runs from the top on resume ([edges](https://learn.microsoft.com/en-us/agent-framework/concepts/workflows/edges), [execution](https://learn.microsoft.com/en-us/agent-framework/concepts/workflows/builder-and-execution), [functional](https://learn.microsoft.com/en-us/agent-framework/concepts/workflows/functional)).

**LlamaIndex Workflows** (2.25.0). Event-driven: each `@step` declares the event type it consumes, a step fans out by emitting several events, and `collect_events` waits until all expected events arrive. Durability is a context snapshot at step boundaries, and a step that was running re-executes from the top ([concurrency](https://developers.llamaindex.ai/python/llamaagents/workflows/concurrent_execution/), [durability](https://developers.llamaindex.ai/python/llamaagents/workflows/durable_workflows/)).

**Pydantic Graph** (2.54.0). A node's return type annotation defines its outgoing edges, which gives type-checked graphs. Forks broadcast or map over a list, and joins reduce the results. Its docs warn against using a graph unless you need one, and durability has moved to the agent level, where every model and tool call becomes its own durable unit ([graph](https://pydantic.dev/docs/ai/graph/graph/), [joins](https://pydantic.dev/docs/ai/graph/builder/joins/)).

**CrewAI Flows and Mastra.** CrewAI uses `@start`, `@listen` and `@router` decorators over shared state, with persistence to SQLite ([flows](https://docs.crewai.com/en/concepts/flows)). Mastra uses a fluent chain of `.then`, `.parallel`, `.branch`, `.foreach` and `.dountil`, with `suspend()` writing a snapshot ([control flow](https://mastra.ai/docs/workflows/control-flow)).

**The common lesson.** Every framework re-runs the interrupted unit from its beginning, so every one needs idempotent side effects. Checkpoints sit either at superstep boundaries (graph APIs) or on cached step results (functional APIs).

## Composition in agent SDKs

Agent SDKs converge on three primitives: call an agent and get a value back, hand control to another agent, and compose sequence, parallel and loop in ordinary code. DSPy is the only one where an optimizer can tune the parts inside a composed program.

**Google ADK** (2.11.0). ADK 2.0 went GA in May 2026 and deprecated `SequentialAgent`, `ParallelAgent` and `LoopAgent`, telling users to express them as workflows ([ADK 2.0](https://adk.dev/2.0/index.md)). It offers graph workflows with routing events, and dynamic workflows where a `@node` function calls `await ctx.run_node(...)` inside normal loops and `asyncio.gather`. On resume, "successful sub-nodes are automatically skipped" ([dynamic workflows](https://adk.dev/graphs/dynamic/)). A coordinator with `sub_agents` gets generated delegation tools, and `AgentTool` calls an agent as a tool rather than transferring control.

**OpenAI Agents SDK** (0.23.1). It names the split directly: orchestrating via the LLM or via code ([multi-agent](https://openai.github.io/openai-agents-python/multi_agent/)). `Agent.as_tool()` keeps the caller in control and does not pass the parent's conversation; a handoff makes the specialist the active agent for the rest of the turn. Code orchestration is routing on structured output, chaining, evaluator loops and `asyncio.gather`. Durability comes from integrations with Temporal, DBOS, Restate and Dapr. It has no graph primitive.

**Claude Agent SDK** (0.2.164). Subagents are declared with a description, prompt, tools and model; the model invokes them through the `Agent` tool, they receive only a prompt and return only their final message, and they run in parallel in the background with a default depth of 3 ([subagents](https://code.claude.com/docs/en/agent-sdk/subagents)). Its Workflow tool is a script using `agent()`, `parallel()`, `pipeline()` and `phase()` in which "the script", not the model, decides what runs next. On relaunch it replays completed agents' saved results and reruns from the first agent whose prompt changed; `Date.now()` and `Math.random()` throw to keep replay deterministic ([workflows](https://code.claude.com/docs/en/workflows)).

**DSPy** (3.4.0). Composition is a `Module` subclass with sub-modules as attributes and arbitrary Python in `forward()`. Optimizers find tunable parts by walking `named_predictors()`, so a predictor nested inside a composed program is optimizable. Building blocks include `dspy.Parallel`, `BestOfN`, `Refine`, `MultiChainComparison` and async `aforward` ([modules](https://dspy.ai/current/diving-deeper/modules/)). Since 3.3.0 the experimental `Flex` module lets GEPA rewrite a module's control flow itself.

**Others.** AutoGen is in maintenance mode; AG2 1.0 replaced group chat with a hub that keeps a write-ahead log for replay, and offers typed transition graphs ([ag2](https://github.com/ag2ai/ag2)). Strands Agents offers graph, swarm and fixed-DAG workflow patterns with node timeouts and execution limits ([patterns](https://strandsagents.com/docs/user-guide/concepts/multi-agent/multi-agent-patterns/)). smolagents calls managed agents like tools.

## Recursive and programmatic orchestration

Here the model writes the orchestration itself, usually as code in a sandbox that can call tools, sub-models or subagents. It wins clearly on long-context and parallel work and costs more and varies more on simple tasks.

**Recursive Language Models** ([paper](https://arxiv.org/abs/2512.24601), Zhang, Kraska and Khattab). The input is held as a variable in a Python REPL rather than placed in context. The model writes code to inspect and chunk it, and from that code calls itself or a smaller model on the pieces, finishing with `FINAL(answer)`. With GPT-5 it reached 91.3 on BrowseComp+ over 1,000 documents where the base model scored 0, and it handles inputs about 100 times the context window. DSPy ships it as the experimental `dspy.RLM`, running code in a WASM sandbox with `llm_query`, `llm_query_batched` and `SUBMIT` built in and limits on iterations and LLM calls ([dspy.RLM](https://dspy.ai/current/api/modules/RLM/)). An [independent reproduction](https://arxiv.org/abs/2603.02615) found recursion depth 1 helps on complex reasoning, depth 2 lowers accuracy and raised runtime from 3.6 s to 344.5 s, and RLMs do worse than plain models on simple retrieval.

**Code as orchestration.** [CodeAct](https://arxiv.org/abs/2402.01030) found Python actions up to 20% more successful than JSON tool calls across 17 models. Anthropic's [programmatic tool calling](https://platform.claude.com/docs/en/agents-and-tools/tool-use/programmatic-tool-calling) lets model-written code call tools: execution pauses at each call, the client returns the result, and results from code-issued calls do not enter the context. Anthropic reported 37% fewer tokens on average, but about 8% more on small sequential workflows. Exposing MCP servers as code APIs cut one worked example from 150,000 tokens to 2,000 ([code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp)).

**Model-driven decomposition.** [LLMCompiler](https://arxiv.org/abs/2312.04511) plans a DAG of tool calls and runs independent ones in parallel, reporting up to 3.7 times faster and 6.7 times cheaper than ReAct. [ReWOO](https://arxiv.org/abs/2305.18323) plans all calls up front. [ADaPT](https://arxiv.org/abs/2311.05772) decomposes recursively only when the executor fails. Anthropic's [multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system) beat a single agent by 90.2% using about 15 times the tokens of a chat, and found token usage explains 80% of performance variance.

**When each wins.** Anthropic distinguishes workflows, which are predetermined code paths, from agents, which direct their own process, and advises starting with the simplest that works ([Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)). Cognition argues parallel subagents without shared context make conflicting decisions ([Don't build multi-agents](https://cognition.com/blog/dont-build-multi-agents)). The evidence supports having both available: model-driven loops for open-ended, parallel or context-exceeding work, fixed code for well-understood procedures, with hard limits on depth, calls and budget.

## Durable execution and HPC workflows

Every durable engine works the same way at its core: re-run the orchestrating function from the start, and return the recorded result for every step that already completed. Scientific workflow systems instead resume by hashing inputs and reusing cached outputs. CLIO can take the first model without an external engine.

**Durable execution engines.** [Temporal](https://docs.temporal.io/workflow-execution) replays a workflow against its stored event history, with side effects in retried activities; history is capped at 51,200 events or 50 MB. [DBOS](https://docs.dbos.dev/architecture) stores every step output in Postgres, never re-executes a checkpointed step, and uses the workflow id as an idempotency key. [Restate](https://docs.restate.dev/concepts/durable_execution) journals side effects, calls and timers, and [Inngest](https://www.inngest.com/docs/learn/how-functions-are-executed) memoizes each step by id. In all of them, recorded results are exactly-once and side effects inside a step are at-least-once, so steps must be idempotent.

**What resuming requires.** A run id and its inputs, an ordered log of step results keyed by a stable step id, pending timers and waits, received signals, and a pinned code version so replay produces the same sequence of steps. LLM nondeterminism needs no special handling: replay returns the recorded output instead of sampling again.

**How agent frameworks use them.** In the [Temporal integration for the OpenAI Agents SDK](https://docs.temporal.io/develop/python/integrations/openai-agents), the agent loop runs in the workflow and model calls run as activities, so they are not repeated on replay. [Pydantic AI](https://pydantic.dev/docs/ai/capabilities/durable_execution/overview/) supports eight engines, each wrapping every model request and tool call as its own durable unit, and notes that durability is not the same as storing chat history.

**Scientific workflow systems.** [Parsl](https://parsl.readthedocs.io/en/stable/userguide/workflows/checkpoints.html) memoizes app calls by a hash of arguments, with checkpoints written at task exit, periodically or manually. [Nextflow](https://docs.seqera.io/nextflow/cache-and-resume) resumes a task when a hash of its inputs, script, container and environment matches a cached result with valid outputs. [Globus Flows](https://docs.globus.org/api/flows/authoring-flows/actions/) polls long-running actions with exponential backoff capped at 10 minutes.

**Agents on HPC.** [Ma, Foster and colleagues](https://www.arxiv.org/abs/2502.12280) connected LangGraph to Parsl to run molecular dynamics on Polaris. [Academy](https://arxiv.org/html/2505.05428) restores agent state on startup but leaves checkpointing to the application.

**For a Slurm job that outlives the agent.** One durable step submits the job and records its id, using an idempotency key derived from the run and step; a second durable step polls with backoff. This matches the design constraint set for the fall durable-execution work: content-derived idempotency keys, and no claim of exactly-once across the gap between a tool finishing and its result being persisted.

## Comparison

The code-first systems all resume the same way, by replaying with memoized child calls; the graph systems checkpoint at superstep boundaries and pay for it with head-of-line blocking.

| System | Structure expressed as | Parallelism | Resume | Model can write the structure |
| --- | --- | --- | --- | --- |
| LangGraph graph API | Typed state graph | Supersteps, `Send` map-reduce | Checkpoint per superstep | No |
| LangGraph functional API | Code with `@task` | Task futures | Replay with saved task results | No |
| Microsoft Agent Framework | Executors and edges, or functional | Supersteps, barrier fan-in | Checkpoint per superstep or per step | No |
| LlamaIndex Workflows | Events and steps | Emit several events, collect | Context snapshot | No |
| Google ADK 2.0 | Graph, or code calling `run_node` | `asyncio.gather` | Completed nodes skipped | Through delegation tools |
| Claude Workflow tool | Script | `parallel()` | Replay saved agent results | No |
| OpenAI Agents SDK | Code, handoffs, agents as tools | `asyncio.gather` | Through Temporal, DBOS and others | Through handoffs |
| DSPy | Code in `forward()` | `dspy.Parallel`, async | None built in | `dspy.RLM`, `Flex` |
| Recursive Language Models | Model-written REPL code | Batched sub-calls | None | Yes |
| Parsl, Nextflow | Dataflow over apps or processes | Futures and data fan-out | Hash-based memoization | No |
| CLIO today | Linear declared workflow | One child at a time | None | Through spawn tools |

## Proposed semantics for the CLIO SDK

A flow is a DSPy module that a plugin contributes. It orchestrates through a runtime context, and every call it makes is an ordinary CLIO task, so a flow inherits handles, Stop, cancellation, mailbox delivery and recovery from the task layer instead of reimplementing them. Workflows, councils, arenas and recursive decomposition are all flows; there is no separate graph engine and no new store.

**The name.** These are flows rather than loops because CLIO already uses "loop" for interval monitoring.

**Flows are plugin content.** A plugin ships flows in `ai.iowarp.clio/flows/`, and an agent selects one by id the same way it selects plugins. `ClioReAct` becomes the default flow, `react`, contributed by `core`. Because a flow is a `dspy.Module`, predictors inside it stay visible to GEPA.

```python
class Agent:
    flow = "react"                                   # default, from core

class Factorio(ScienceAgent):
    flow = Flow("campaign", max_cycles=5)            # from the manufacturing plugin
```

**The runtime context.** Every flow's `forward(task, ctx)` receives a small set of primitives, each built on something the task layer already provides:

| Primitive | Does | Built on |
| --- | --- | --- |
| `ctx.call(target, input)` | Runs a subagent, agent, peer or tool and returns its result | `Conversations.spawn` for agents; an MCP tool call, task-capable or inline, for tools and peers |
| `ctx.start(target, input)` | The same, but returns the handle at acceptance | The same |
| `ctx.gather(calls, limit=, return_when=)` | Runs calls concurrently, bounded; `ctx.as_completed` yields each result as it settles, with no superstep barrier | `TaskOperations.wait` with `all` or `any` |
| `ctx.ask(question, schema=)` | Asks the human and resumes on the answer | A Question task |
| `ctx.step(fn, *args)` | Records any other side effect | A checkpoint in task history |
| `ctx.permission_receipts()` | Snapshots historical tool-permission decisions for this assignment and confirmed descendants | ARC semantic events, pinned in the flow checkpoint |
| `ctx.workspace` | The session's workspace, for state that outlives one call | The existing workspace |

Permission receipts carry safe ownership/scope identifiers, tool names, historical
decisions and linked policy-refresh provenance; request arguments and policy
resource patterns are omitted. Legacy events without confirmed assignment
identity are excluded, so absence is not evidence that no decision occurred.
Receipts do not authorize a call or establish current authority. Replaying a
snapshot preserves what an earlier call saw. At a new review boundary, after
reattaching earlier work, `ctx.permission_receipts(refresh=True)` observes current
durable history and pins a new content-versioned snapshot. Use that to review new
input explicitly, rather than changing an accepted call's original evidence.

An HPC job is a `ctx.call` on a task-capable MCP tool, now that relay is an ordinary MCP server, so there is no separate job primitive. Predictors called directly in `forward()` are model calls made inside the flow's own assignment.

**Outcomes are typed.** A call returns its task's result or raises a typed outcome carrying the handle: `failed`, `cancelled`, `interrupted` or `task_state_uncertain`. The flow decides what to do with it. The runtime never resubmits an uncertain call, and in a gather one member failing never cancels the others.

**Durability.** Each primitive gets an invocation id derived from the flow run, the call site, a hash of the target and input, and an occurrence index for repeats. That id is passed as the task's invocation id, so the handle is deterministic. The run records its progress as workflow checkpoints in task history, as the declared-workflow runner already does. On restart the runtime re-runs `forward()` from the top, and each call resolves by the state of its task:

- A settled task returns its stored result.
- A running task is re-attached by handle and waited on; nothing is submitted twice.
- A task whose acceptance is uncertain returns `task_state_uncertain` to the flow.
- A call that never reached acceptance is submitted for the first time.

Three rules follow. Predictor calls inside `forward()` may run again after a crash, because CLIO promises recoverable input delivery, not exactly-once model effects. Flow code must be deterministic given its results, with no clock, randomness or I/O outside `ctx`, and the plugin validator flags direct calls. The session pin fixes the code version, which replay requires.

**A running flow is owned work.** A flow runs under its owning assignment, the way a declared workflow does now. When the model starts one through `run_flow` or `run_workflow`, it gets a handle at acceptance and can keep working, and the result arrives through the mailbox. Stop ends the model's wait and leaves the flow and its children running. `cancel_tasks` on the flow cancels its subtree.

**Limits are first-class.** Depth, concurrency, total model calls and budget are set on the flow and enforced by `ctx`, on top of the existing per-depth inference capacity and fan-out budgets, since model-driven orchestration is where runtime and cost become unbounded.

**Model-written orchestration is a flow too.** An `rlm` or `code` flow wraps `dspy.RLM`, or the `run_code` kernel from the programmatic tool calling design, and exposes the same primitives inside the sandbox as functions. Its calls get the same invocation ids, and each records that model-written code issued it.

**The journal is also the trace.** Task history already holds every call as a task with its parent, invocation and result, so the Work and Runs views can draw a flow's executed graph and provenance reads the same records. No declared graph is needed to visualize a run.

**Examples.** A sequence that replaces today's declared workflow:

```python
class Survey(Flow):
    id = "survey"
    async def forward(self, task, ctx):
        sources = await ctx.call("literature-search", task)
        notes = await ctx.gather([ctx.call("reader", s) for s in sources], limit=4)
        return await ctx.call("synthesizer", {"task": task, "notes": notes})
```

A topology optimization campaign with Slurm jobs and a human gate. `slurm.run_case` is a task-capable MCP tool, so the job survives a restart under the same handle:

```python
class Campaign(Flow):
    id = "campaign"
    async def forward(self, goal, ctx):
        design = await ctx.call("setup-tosca", goal)
        for cycle in range(self.max_cycles):
            run = await ctx.call("slurm.run_case", design)
            critique = await ctx.call("process-reviewer", run)
            if critique.acceptable:
                break
            design = await ctx.call("revise-design", {"design": design, "critique": critique})
        if not await ctx.ask("Send this design to Utah?", schema=bool):
            return design
        return await ctx.call("export-results", design)
```

A council asks several specialists the same question and has a synthesizer reconcile their views:

```python
class Council(Flow):
    id = "council"
    members = ("physicist", "numerics-reviewer", "hpc-engineer")
    async def forward(self, question, ctx):
        views = await ctx.gather([ctx.call(m, question) for m in self.members])
        return await ctx.call("synthesizer", {"question": question, "views": views})
```

An arena has contestants attempt the same task and a judge pick one. The votes are a side effect, so they go through `ctx.step`:

```python
class Arena(Flow):
    id = "arena"
    async def forward(self, task, ctx):
        entries = await ctx.gather([ctx.call(c, task) for c in self.contestants], limit=3)
        verdict = await ctx.call("judge", {"task": task, "entries": entries})
        await ctx.step(ctx.workspace.append, "arena/votes.jsonl", verdict.votes)
        return entries[verdict.winner]
```

Recursion is a flow calling itself through `ctx.call`. This one decomposes only when a predictor says the task is not atomic, in the style of ADaPT, and `ctx.depth` together with the flow's limits keeps it bounded:

```python
class Decompose(Flow):
    id = "decompose"
    max_depth = 3
    async def forward(self, task, ctx):
        split = await self.planner(task=task)        # a predictor inside this flow's assignment
        if split.atomic or ctx.depth >= self.max_depth:
            return await ctx.call("worker", task)
        parts = await ctx.gather([ctx.call(self, s) for s in split.subtasks], limit=4)
        return await ctx.call("synthesizer", {"task": task, "parts": parts})
```

**Migration.** Replace the blueprint `workflow:` block, `parse_workflow` and `run_declared_workflow` in `gact/workflows.py` with flows and plans; the spawn tools stay as the model-driven path. What the branch's runner already gets right carries over as semantics of the primitives: steps are ordinary child assignments, executions checkpoint by invocation id, a step that cannot proceed returns a typed stall (reason, step, predicate, observed state) instead of a guess, only a declared deadline ends a step, and concurrent results merge in spawn order with collisions reported as conflicts. The optimizer never rewrites flows, since they are code and sit on the executable side of the trust boundary.

## Workflows CLIO writes itself

The goal is not only that plugin authors can write workflows, but that CLIO composes its own while it works. That needs two forms, because some structure is known when the work starts and some is only discovered while it runs.

**Workflows as data.** A workflow is a set of steps, each naming a target (subagent, agent, peer or tool), its input and the steps it waits on. The `workflow` flow from `core` runs it: every step whose dependencies are met starts as a task, and each step starts as soon as its own inputs finish, with no superstep barrier. A workflow can come from a plugin, from the user, or from the agent itself, which composes one and starts it through a `run_workflow` tool that returns a handle. (It is not Plan mode, which is planning before execution.) A linear workflow is one where each step waits on the one before.

```python
workflow = Workflow(steps=[
    Step("survey", "literature-search", input="$task"),
    Step("design", "setup-tosca", after=["survey"]),
    Step("run", "slurm.run_case", after=["design"]),
    Step("review", "process-reviewer", after=["run"]),
])
result = await ctx.call("workflow", {
    "workflow": workflow,
    "editors": ["process-reviewer"],   # who may change the workflow while it runs
    "approve_edits": True,
})
```

**Workflows can change while they run.** A workflow names its editors, such as a critic. An editor gets `workflow_add_step` and `workflow_cancel_step` tools during its own call, so a reviewer that finds a gap can insert a new phase that waits on its own step. Four rules keep this safe:

- Every edit is checkpointed like any other call, so replay rebuilds the same workflow and re-attaches to steps that already started.
- Growth is open by default, so the model can express the pattern it needs. The caller may bound it (`max_added_steps`, `max_edit_rounds`, `allowed_targets`), the person may cap it in Settings (Workflows), and the tighter bound wins; the bounds in force are recorded when the run starts. Beyond that, Clio's own limits (subagent depth and concurrency) and the person's Stop and cancel end a critic that always finds one more gap.
- An edit can require approval through a Question task. The UI draws the live workflow from task history, and a proposed step shows as pending until approved.
- A failed step first goes to the editors as a typed stall, and they may add a repair step within the same bounds. If nobody edits, the stall returns to the caller.

**Scripts are workflows as code.** When the structure depends on results in ways a workflow cannot express, such as loops over intermediate data, the agent writes a flow instead and runs it through `run_code`. The script gets the same primitives as functions inside the sandbox, which is the programmatic tool calling path. Its calls get invocation ids like any flow's, and each records that the model wrote the code that issued it.

**Which form when.** A workflow is inspectable and editable before it starts, replays exactly, and can be shown to a human for approval, so it is the default for anything with jobs, cost or external side effects. A script is the escape hatch for data-dependent control flow. Both are capabilities: `run_workflow`, `run_flow` and `run_code` come from an orchestration plugin, so an agent without it delegates only through the spawn tools.

**Keeping what worked.** A workflow that succeeded can be saved to the user's personal plugin as a named workflow and reused like a shipped one; it is data, so it goes through the same lesson gates as prompts and skills. A script promoted to a reusable flow is code, so it takes the explicit trust step any git-installed flow takes, and the self-optimization cycle never edits it.

## Open questions

- **A task kind for flows.** A flow run could stay as checkpoints under its owning assignment, as declared workflows do now, or get its own `Flow` kind in `TaskSnapshot` so it appears in `query_tasks` like any other work.
- **Handoff.** The proposal covers calling an agent and getting a value back, but not transferring the rest of the conversation to another agent. Whether CLIO needs that, or whether delegation plus a returned result is enough, is open.
- **Checkpoint size.** A long campaign with many calls needs a cap on one run's checkpoint and a continue-as-new style rollover, the way Temporal caps history at 51,200 events.
- **Replaying model-written code.** Invocation ids make identical calls re-attach, but a model that writes different code on replay makes different calls. Whether `rlm` and `code` flows should resume or restart is open.
- **Context passed to subagents.** Most SDKs pass only a prompt and return only a final message; Cognition argues for sharing full traces. `spawn` already accepts pinned evidence references (`input_task_refs`), and the default for `ctx.call` needs a decision.
- **Visual authoring.** Workflows are already a declarative form, so the UI can draw a workflow before it runs and let a user edit steps. Whether users should also author flows visually is open.
- **Workflow or script by default.** Whether the agent should be steered toward workflows and reach for `run_code` only when a workflow cannot express the control flow, and whether that guidance lives in a skill or in the tool descriptions, is open.
- **Edits from outside the workflow.** Only named editors may change a running workflow. Whether the user or the parent agent may also add or cancel steps mid-run is open.

## Sources

All opened in October 2026.

- **Graph frameworks:** [LangGraph graph API](https://docs.langchain.com/oss/python/langgraph/graph-api), [LangGraph checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers), [LangGraph functional API](https://docs.langchain.com/oss/python/langgraph/functional-api), [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts), [Thoughtworks Radar on LangGraph](https://www.thoughtworks.com/radar/languages-and-frameworks/langgraph), [Agent Framework edges](https://learn.microsoft.com/en-us/agent-framework/concepts/workflows/edges), [Agent Framework execution](https://learn.microsoft.com/en-us/agent-framework/concepts/workflows/builder-and-execution), [Agent Framework functional workflows](https://learn.microsoft.com/en-us/agent-framework/concepts/workflows/functional), [LlamaIndex concurrency](https://developers.llamaindex.ai/python/llamaagents/workflows/concurrent_execution/), [LlamaIndex durability](https://developers.llamaindex.ai/python/llamaagents/workflows/durable_workflows/), [Pydantic Graph](https://pydantic.dev/docs/ai/graph/graph/), [Pydantic Graph joins](https://pydantic.dev/docs/ai/graph/builder/joins/), [CrewAI Flows](https://docs.crewai.com/en/concepts/flows), [Mastra control flow](https://mastra.ai/docs/workflows/control-flow)
- **Agent SDKs:** [ADK 2.0](https://adk.dev/2.0/index.md), [ADK dynamic workflows](https://adk.dev/graphs/dynamic/), [OpenAI multi-agent](https://openai.github.io/openai-agents-python/multi_agent/), [Claude Agent SDK subagents](https://code.claude.com/docs/en/agent-sdk/subagents), [Claude workflows](https://code.claude.com/docs/en/workflows), [DSPy modules](https://dspy.ai/current/diving-deeper/modules/), [AG2](https://github.com/ag2ai/ag2), [Strands patterns](https://strandsagents.com/docs/user-guide/concepts/multi-agent/multi-agent-patterns/)
- **Recursive and programmatic:** [Recursive Language Models](https://arxiv.org/abs/2512.24601), [dspy.RLM](https://dspy.ai/current/api/modules/RLM/), [RLM reproduction](https://arxiv.org/abs/2603.02615), [CodeAct](https://arxiv.org/abs/2402.01030), [programmatic tool calling](https://platform.claude.com/docs/en/agents-and-tools/tool-use/programmatic-tool-calling), [code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp), [LLMCompiler](https://arxiv.org/abs/2312.04511), [ReWOO](https://arxiv.org/abs/2305.18323), [ADaPT](https://arxiv.org/abs/2311.05772), [multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system), [Building effective agents](https://www.anthropic.com/engineering/building-effective-agents), [Don't build multi-agents](https://cognition.com/blog/dont-build-multi-agents)
- **Durability and HPC:** [Temporal workflow execution](https://docs.temporal.io/workflow-execution), [DBOS architecture](https://docs.dbos.dev/architecture), [Restate](https://docs.restate.dev/concepts/durable_execution), [Inngest](https://www.inngest.com/docs/learn/how-functions-are-executed), [Temporal and OpenAI Agents SDK](https://docs.temporal.io/develop/python/integrations/openai-agents), [Pydantic AI durable execution](https://pydantic.dev/docs/ai/capabilities/durable_execution/overview/), [Parsl checkpoints](https://parsl.readthedocs.io/en/stable/userguide/workflows/checkpoints.html), [Nextflow cache and resume](https://docs.seqera.io/nextflow/cache-and-resume), [Globus Flows actions](https://docs.globus.org/api/flows/authoring-flows/actions/), [LangGraph and Parsl on Polaris](https://www.arxiv.org/abs/2502.12280), [Academy](https://arxiv.org/html/2505.05428)
