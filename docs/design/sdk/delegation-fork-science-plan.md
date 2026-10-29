# Default delegation, fork, the science log, export and two published studies: implementation plan

This plan builds two items of the CLIO 1.0 release, the default agent and the science log, on the plugins, Agent SDK and flows change now on `develop` (#1825). It has seven workstreams, each building on the ones before it:

1. **Default delegation.** A spawn with no target runs a copy of the caller, and one plugin ships generic `explore`, `planner` and `review` roles for every agent.
2. **Fork.** Any agent can fork itself. The fork carries the conversation so far, runs beside the original under the same parent, and whoever waits on the original also waits on the fork. Inside a workflow this lets a running step add a sibling to the diagram without editor rights.
3. **The science log.** Questions, hypotheses, experiments, observations, evidence, reframes and conclusions become typed, linked entries. Studies run as workflows, and branches grow by fork. Every Observation cites the recorded tool calls that produced it, the agent gets the measured values as data to analyze in its own code, and a study can run with each node writing only to its own folder.
4. **The Wordle study.** The science agent answers a bare question about Wordle played on a generated word list.
5. **The hidden-law study.** The science agent finds the law behind a simulated oscillator whose law is generated, not taken from a textbook, and is scored on measurements it never saw.
6. **Export of flows and subagents.** A session export carries every flow run (council, arena, workflow), every subagent and every fork, readable offline.
7. **Publication.** Every live verification session is exported. Once a session passes and reads well, it is published on clio.iowarp.ai: the two studies as one worked example with the workflows they ran, and the plain-request sessions beside the user docs this change adds for the science log, the default subagent roles and fork.

Neither study adds anything to the agent that was written for its domain. The point of having two is to show that the same generic machinery serves both.

Work under `/goal`: the goal is met when every item under **Done** holds. Run to completion without stopping for review. Use the browser yourself to check every UI change, and do the live verification yourself. The human reviews the finished, installable branches.


> **Addenda.** [Addendum A](delegation-fork-science-addendum-a.md) (sources, data access, isolated workspaces) is folded in. [Addendum B](delegation-fork-science-addendum-b.md) (study views: agent-built dashboards attached to the study, with live study data) and [Addendum C](delegation-fork-science-addendum-c.md) (issue #1823: the dashboard-export crash, selection-driven viewers, links, no component cap) extend it; each lists its phase, scenarios and done criteria.

## Contract

This plan is the contract for the six workstreams. The SDK design documents still govern everything they cover, and the decisions in [acceptance-ledger.md](acceptance-ledger.md), [model-fleet-ledger.md](model-fleet-ledger.md) and [memory-ledger.md](memory-ledger.md) stay in force. When code and this plan disagree, change the code. If the plan turns out wrong, choose the fix that best keeps its intent, update this document in the same change, and record the decision in the science ledger.

| Document | Governs |
| --- | --- |
| This plan | Default delegation, fork, the science log, both studies, their publication |
| [delegation-fork-science-addendum-a.md](delegation-fork-science-addendum-a.md) | Sources, data access and isolated workspaces. Its content is folded into this plan, which now governs them; the addendum file is kept as the record of the decision |
| [plugins-and-composition.md](plugins-and-composition.md) | Plugins, packs, resolution, composed prompt, trust |
| [agent-sdk.md](agent-sdk.md) | `Agent`, `Subagent`, `Plugin`, `plugin check` |
| [orchestration-semantics.md](orchestration-semantics.md) | Flows, `ctx`, the task layer, workflows |
| [model-fleet.md](model-fleet.md) | Model choice for spawns, flow calls and workflow steps |
| `site/WRITING.md` and `site/README.md` | Every page and asset published on the site |

Base: `develop` in clio-agent at `8159955f5`, with gact-tui at its pinned `158e60ba` and clio-agent-marketplace `main` at its pinned `d0d92981`. Rebase onto `develop` if it moves while you work.

## Rules

1. **Delete and replace.** Keep no dual path, compatibility shim, feature flag or dormant copy. A path judged wrong is deleted in the same change that replaces it.
2. **The task layer is the only execution authority.** Spawns, forks, flows and steps submit and wait through the task layer. Add no registry, scheduler, store or polling loop of your own.
3. **The model decides.** Follow the superseding principles in `.claude/CLAUDE.md`. Core never routes, completes or retries on a heuristic.
4. **No silent fallback.** Every refusal, stall or degraded path returns a typed reason that reaches the API and the trace.
5. **One mechanism per concept.** A fork reuses the user fork's context copy. A read-only role reuses the side-session read-only profile. A study runs on the `workflow` flow. Publication reuses session export and the site's published-conversation path. Do not build a second version of any of them.
6. **No new store.** The science log is written through an existing store. It is not the long-term memory system and shares nothing with it.
7. **Nothing domain-specific in generic pieces.** The `delegation` and `science-log` plugins, the task layer and the generic UI never mention Wordle, words, games, guesses, oscillators or periods. A test greps for it.
8. **Method lives in the skill, not the prompt.** How to run a study belongs in the `science-log` method skill. A kickoff prompt is what a scientist would type, and nothing more.
9. **Hidden stays hidden.** The Wordle answer set and the bench's law are derived at server start from a secret seed in the server's environment. They are never written to the workspace or to plugin files the agent can read.
10. **The model points, the record supplies.** The agent never types a command, parameter or hash into an Observation. It cites recorded tool calls, and the log copies what they did from the records that already exist.
11. **The platform provides facts and data, not judgements.** The log records what ran and hands the values to the agent. It does not compute statistics, rank results, decide that one result beats another, or check the agent's numbers against a rule. Whether A beats B is the agent's judgement, recorded as a claim with its sources. A capable model chooses the right analysis for an experiment and weighs trade-offs between metrics better than a fixed rule would, and a fixed rule breaks the next experiment that does not fit it.
12. **Every number in a claim can be reproduced from the record.** A quantitative claim cites the analysis call that produced it. That call's record already holds the script's hash, its input files' hashes and the environment it ran in, so anyone can run the same analysis on the same data.
13. **Repository rules.** Follow `AGENTS.md` for resource ownership and cleanup, ruff, tests, the file-size and nested-class guards, and Conventional Commits.

## Out of scope

Do not implement, stub or reserve fields for these:

- Remote agents (A2A, ACP, Academy) and distributed or federated branches. Every branch runs on one CLIO server.
- Programmatic tool calling, `run_code`, and the `rlm` and `code` flows. Code during a study runs through the existing shell.
- Statistical tooling inside the science log. The agent computes statistics with its normal tools and records the result as Evidence.
- A solver or fitter the agent can call for either study. The reference solver and the scorer exist only to check results.
- Objective, promotion or master semantics in the log.
- Git worktrees per node.
- A remote layout for nodes that run on other machines.
- A performance-tuning study such as HDF5 chunk size; a candidate for a later study.

## Current state

What exists on `develop`, so nothing is rebuilt:

- **Spawning.** The `delegation` plugin (marketplace `plugins/delegation`) claims `spawn_agent_task`, `spawn_agents_parallel` and `spawn_skill_task`, built by `build_spawn_runtime_tools` in `gact/task_declarations.py`. Both spawn tools take `agent`, `task`, `name`, `model`, `input_task_ids` and `strategy`. `agent` is required, and `prepare_and_spawn` rejects an empty one. It names a defined subagent or an installed agent, which starts with its own fixed composition.
- **`self`.** `resolve_child_agent` in `gact/spawn_context.py` resolves `self` to the parent's exact resolution and delta, and `choose_child_model` treats it as the same agent. Flows reach it through `ctx.call("self")`, `run_flow` and `run_workflow` (D14). The spawn tools accept it but do not offer it. A spawn's launch context carries `workflow_id` and `workflow_step`.
- **Subagents.** `Subagent` in `sdk/subagent.py` has `plugins`, an optional `tools` allowlist (D11), `prefers`, `flow`, `structured_outputs` and `internal`. `core` always stays (D19). No generic explore, plan or review role exists. Defined subagents that look generic: `critic` in deep-research, `independent_reviewer` and `evidence_critic` in factorio, `data_analysis_expert` in scientific-method.
- **Read-only.** `is_read_only` in `gact/runtime/grant_resolver.py` holds when a tool declares `readOnlyHint` or carries the catalog `read` tag. Side sessions (`gact/side_sessions.py`) run under `READ_ONLY_SIDE_PROFILE`: every other tool is denied with a typed reason, and spawn and workflow controls are not offered.
- **Prompt fragments.** `when` supports `tools` and `plugins` (`plugins/loader.py`, `plugins/compose.py`). Nothing distinguishes a root session from a delegated one, so children receive the rendering section and A2UI guidance meant for the person. The `delegation` plugin ships no fragment.
- **Workflows.** The `workflow` flow in `core` starts each step as soon as its inputs finish. Named editors may add or cancel steps, but only through their own settled result (D16), and an added step waits on the step that added it (D25). `approve_edits` puts each proposal behind a Question. Growth bounds come from the caller and the person's settings. A step may target a flow, including `workflow`, which nests a run drawn inline in the railroad (D28), and may name a model.
- **Flow limits.** A flow class may declare `max_concurrency`, `max_model_calls`, `budget_tokens` and `max_depth`, which `ctx` checks on the calls the flow makes. A subagent that a step's agent spawns on its own is not counted.
- **Forks.** `POST /v1/sessions/{sid}/fork` (`gact/routes/session_fork.py`) copies a conversation through a chosen message into a new `branch` session with the agent ref, delta and resolution, without task ownership or pending work, using `copy_session_context`, and records `fork_origin`. It refuses anything but conversations and branches (`fork_not_allowed`). No agent can fork.
- **Tool calls.** Every tool call is a row in its session's transcript, keyed by the tool observer's `call_id`, with its tool, arguments, result and times. Only background and task-capable calls are also task records (`TaskKind`); an ordinary call such as a measurement is a transcript row only.
- **Artifacts.** The artifact registry (`gact/artifacts/`) keeps one `TransformRecord` per producing tool call, keyed by the same `call_id`. It carries the instrument (tool and arguments, or the command and the script's hash), the time span, the tiered execution environment, and `used` and `generated` edges to artifact versions, each with its SHA-256 and evidence. Versions dedup by content hash and record revisions, and the registry is persisted in clio-core. A file becomes an artifact by designation, never by scanning: through a tool's declared output paths, `fs_write_file`, or the designation tool. A script's output file that nobody designates is not in the record. `gact/file_changes.py` is the in-memory Undo ledger for `fs_write_file` and `fs_edit_file`; it is not provenance.
- **Write territory.** The write territory is keyed by the workspace root (`runtime/sandbox_roots.py`) and shared by every session in the workspace, for both the OS fence (`sandbox.py`) and the tool-boundary check (`tools/file_policy.py`). A tool's declared output path (`output_path` and the other names in `gact/artifacts/designation.py`) is grounded against the workspace root before the call.
- **Science agent.** `ScienceAgent` in the marketplace's `agents/standard.py` is the standard agent plus `scientific-method`. `maintain-scientific-dossier` lives in the factorio plugin and keeps decisions, evidence and superseded status as prose.
- **Export and the site.** Session export (`gact/session_export*.py`) produces one standalone HTML or a ZIP. It includes delegated children's transcripts through the descendant walk and projects saved A2UI parts with the production validator. It has no rendering of flow runs: no railroad, no step states, no editor proposals or approvals. Phase 0 records exactly what a council, an arena, a workflow and a deep subagent tree look like in today's export. The site publishes native exports as GitHub release assets pinned by size and sha256 in `site/src/lib/ndp-conversations.ts`, served by `site/src/pages/conversations/ndp/[case]`, tested by `site/e2e/ndp-conversations.spec.ts`, and linked from `docs/examples/ndp-science.mdx`. The site deploys from `main`.
- **Cloud tooling.** `scripts/cloud/stack.sh` runs the backend on 8787 and the web UI on 5173 with the `claude_code` provider. `scripts/cloud/ui_check.mjs` drives the UI in headless Chromium and prints screenshots, console errors and failed requests.

## Design: default delegation

**The default target is the caller.** `agent` becomes optional in `spawn_agent_task` and in each item of `spawn_agents_parallel`. An omitted agent resolves through the existing `self` path: the caller's exact resolution and delta, pinned at spawn, running `react` (D14), on the caller's model unless `model` says otherwise. A Factorio child keeps manufacturing and Abaqus; a standard-agent child gets the standard set. Naming an installed agent or a defined subagent works as before. The tool description says that omitting `agent` delegates to a copy of the caller, and lists the roles and defined subagents as the alternatives.

**Generic roles.** The `delegation` plugin ships three defined subagents that derive from the caller instead of carrying a fixed plugin list:

| Role | Runs as | Returns |
| --- | --- | --- |
| `explore` | The caller's resolution under the read-only profile | Findings with paths, handles and entry ids, and no proposals |
| `planner` | The caller's resolution under the read-only profile | A workflow in the shape `run_workflow` accepts, so the caller can start it unchanged |
| `review` | The caller's resolution under the read-only profile | An independent verdict on the work or claim it was given, with the evidence for each point |

`Subagent` gets two fields. `derive="caller"` makes the child's plugins the caller's resolution; `plugins` must then be empty (`subagent_derive_conflict`). `effects="read"` runs the child under the side-session read-only profile, so the same gate denies writes with the same typed reason. `core` stays (D19). `plugin check` validates both fields. After this change there is one read-only mechanism, not two.

**Delegated prompt scope.** Fragment `when` gains `session: root | delegated`; omitted means both. The resolver decides it at session creation. `core.rendering` and A2UI catalog guidance become `session: root`. The `delegation` plugin ships two fragments within the 150-token budget:

- `delegation.child` (`session: delegated`): the session was delegated, no person reads it, stay inside the assignment, and end with a report the delegating agent can use.
- `delegation.parent` (`when: {tools: [spawn_agent_task]}`): delegate work that is separable or would flood the context, write a self-contained task because a child starts without the conversation, and prefer a role when one fits.

**What a child keeps.** Children keep `core`, Question tasks included. Phase 1 verifies that a child's Question appears in the root session the person is looking at, attributed to the child, and fixes delivery if it does not. Record the result as a decision.

**Marketplace cleanup.** Compare each generic-looking subagent with the roles. One whose prompt adds no domain knowledge is deleted, and its callers move to a role or the default. One that carries domain knowledge or a `structured_outputs` contract stays. Each verdict goes in the ledger with a one-line reason. Do not keep a deleted subagent as an alias.

## Design: fork

**The rule.** A fork is a copy of the caller, conversation included, that runs as a sibling under the caller's parent. Whoever waits on the caller also waits on the fork. Delegation to `self` starts a child with only its assignment. A fork starts where the caller is, which is the reason to fork rather than spawn.

**Where a fork lands.**

| Caller | The fork | Who waits |
| --- | --- | --- |
| A root conversation | A new conversation in the left panel, marked with its origin like a person's fork | Nobody; the person reads both |
| A delegated child | A sibling child of the same parent | The parent's wait now covers both |
| A workflow step | A sibling step with the same inputs, drawn beside the original | Every step that waited on the original also waits on the fork |

The fork never touches anything outside the caller and the tasks waiting on it. That is why a workflow step may fork without editor rights: it cannot rewrite the diagram, only duplicate itself in place.

**The tool.** The `delegation` plugin claims `fork(note, title=None)`. `note` is the divergence: what the fork should do differently ("pursue H4 instead of H2", or a reframed question). The original receives the fork's handle as the tool result and continues. A fork may fork again.

**What is copied.** The caller's transcript up to its last completed tool result, through `copy_session_context`, followed by the note as the fork's next input. Like the person's fork, it copies configuration without task ownership or pending work, so the fork does not inherit the original's running tasks. The fork runs the caller's exact resolution, delta and model. In an isolated run, the fork also starts with a copy of its forker's folder at the moment of the fork (see **Design: isolated workspaces**); outside one, no folder is copied.

**Task layer.** A fork is an ordinary task of the caller's kind (D1: no new `TaskKind`). `TaskSnapshot` gains `fork_of`, the handle it was forked from, and the fork takes the caller's `parent_handle`. One rule in `TaskOperations` covers every case above: waiting on a handle waits on it and on every task whose `fork_of` chain reaches it, and its result is the family's results, each labelled by handle. The workflow flow gets "dependents wait on the fork" from the same rule, because a dependent waits on its step's handle. A `$<step>` reference resolves to the family's answers, labelled `<step>`, `<step>~1`, `<step>~2` in fork order.

**Durability.** The fork is accepted through the idempotent spawn path, keyed by the `fork` call's invocation id. A replay of the caller re-issues the same call, which addresses the same fork. Nothing new is checkpointed.

**Bounds and approval.** A fork inside a workflow counts as an added step against the run's growth bounds and is drawn as one. With `approve_edits` set, the fork first raises the same Question an edit would ("Fork step 'h2' as 'h2~1': pursue H4. Allow?"), and a rejection is the tool's typed result. Outside workflows, Settings → Execution & history gains **Forks per task** (0 means unlimited, as for the workflow limits), and `fork_limit_reached` is the typed refusal. A fork sits at its caller's spawn depth.

**Cancel and failure.** Cancelling the original leaves its forks running. Cancelling the parent or the workflow run settles the whole family. A failed fork in a workflow is offered to the editors as a stall, like any failed step.

**The person's fork stays.** The fork route keeps serving people, and agent forks of a root conversation go through it. `fork_not_allowed` is lifted for agent forks only: a person still branches from the parent conversation.

## Design: the science log

**Entries.** The log is append-only. Each entry has an id, study id, branch, authoring session, timestamp and status.

| Type | Written when | Fields | Links |
| --- | --- | --- | --- |
| Question | A study starts or a branch reframes | statement, scope; on the root question, an optional budget (amount and unit) | the question it reframes |
| Hypothesis | The agent proposes an answer | claim, refuting prediction | its question |
| Experiment | The agent designs a test | method, sample size | its hypothesis |
| Observation | A run finishes | `from_calls`, `measurements`, optional `derived_from`, cost in the budget's unit; the log adds `lineage` and `artifacts` (see **Design: sources**) | its experiment; the calls it cites |
| Evidence | The agent interprets observations | interpretation, strength | observations; supports or refutes a hypothesis |
| Reframe | A branch decides the question should change | rationale | old question, new question, triggering evidence |
| Conclusion | A branch or the root finishes | claims, each with its evidence ids; confidence; caveats; relation to the parent question (answers, refutes its premise, replaces it) | evidence and merged child conclusions |
| Conflict | A merge finds branches disagreeing | description | the disagreeing entries on both sides |

A status change is a new entry naming the entry it changes. Nothing is edited in place.

**Branches follow the task tree.** A study belongs to the session that wrote its root Question. Every session in that session's task tree is a branch, and its branch id is its session id. A fork is a branch of its own from the fork on. A person's fork of a conversation in a study is a branch the same way.

**Whole-study visibility.** Every branch reads the whole study's entries, siblings and cousins included, while they run, and writes only its own branch. With many branches exploring in parallel, each can see what the others have measured and claimed before it claims anything. The cost is independence: branches that read each other's claims can converge early, so the method skill tells a branch to form its own hypothesis and design before reading the others, and the Evidence says when it relied on another branch's result. When two branches claim incompatible results, the merge records a Conflict citing both and resolves it, or leaves it open.

**Validation.** The log validates schema and references, which the superseding principles allow. A link must point at an existing entry its author can see. Every Conclusion claim cites at least one entry. An Observation must cite recorded calls its author may cite, and is rejected with `lineage_missing`, `lineage_call_unknown`, `lineage_call_outside_branch` or `measurement_field_unknown` otherwise (see **Design: sources**). When a study declares a budget, Observations charge it, and one that would overspend it is rejected with `budget_exhausted`. Rejections are typed tool errors. The log never judges whether a claim or a value is right.

**Store.** The log lives in clio-core, through the same writer the task layer uses. Each entry is written to the event log of the session that wrote it, which is its branch, through `TaskHistory` and its ARC/Core lanes. Study-wide queries (every Hypothesis in a study, everything citing an entry) go through derived index records built the way `gact/task_index_records.py` builds task lookups. Nothing is replayed at boot; a study's entries load on demand from its sessions. A study therefore travels with its sessions: the export's descendant walk reaches every branch's entries with no extra mechanism. `observe_tasks` can show a session's entries.

**Tools**, in a `science-log` plugin that `ScienceAgent` carries:

- `log_entry(type, fields, links)` writes an entry and returns its id.
- `link(from_id, relation, to_id)` adds a relation later, such as a merge connecting one branch's evidence to another's hypothesis.
- `set_status(entry_id, status, reason)` records a status change as a new entry.
- `query_log(filter)` returns entries by type, branch, status or relation from the caller's study. It returns an Observation's `measurements` as structured rows, not prose, with its lineage. A result too large for the context spills to file like any tool result (see **Design: data access**).
- `export_measurements(filter, format="parquet"|"csv")` writes the matching measurements to a data file next to a manifest, both designated as artifacts.

Every tool result the model sees, from any tool, carries a call reference that an Observation can cite.

There is no branch, reframe or merge tool. A branch is a step, child or fork. A reframe is an entry followed by a fork or an editor's step. A merge is a step that reads its inputs' entries and writes a cited Conclusion, with a Conflict entry for each disagreement it does not resolve.

**Running a study.** Execution and its limits belong to the workflow; the log holds only what was learned. The method skill `run-a-study` tells the agent how to work, in any domain:

- write the question, then a hypothesis for each competing answer, each with a prediction that would refute it;
- run the study as a workflow started isolated: one step per hypothesis targeting `self`, a merge step after them, and `review` as an editor;
- in a branch, form its own hypothesis and design before reading other branches, then check what they have measured and claimed before claiming anything;
- log every run as an Observation citing the calls behind it, and interpret it as Evidence, saying so when a sample is too small to separate two hypotheses;
- designate the files its analysis writes, so they enter the record;
- analyze exported data in code with the method the experiment needs, recording the seed of a random analysis, and cite that analysis in every quantitative claim;
- when a branch finds a new hypothesis or decides the question should change, write the entry and fork with a note;
- close with a merged Conclusion in which every claim cites entries, and a comparison chart.

A short fragment, `science-log.method` (`when: {tools: [log_entry]}`), points to the skill. Neither names a domain.

**Dossier replacement.** `maintain-scientific-dossier` in the factorio plugin is rewritten to keep decisions, assumptions and artifact status as log entries, and factorio gains `science-log`. No skill keeps a parallel prose record.

## Design: sources

Sources are built from records CLIO already keeps: transcript rows, task records and the artifact registry's `TransformRecord`s (see **Current state**). Nothing here adds a store or parses command text. The model cites; the record supplies the facts.

**Citable calls.** Every tool result the model sees carries a short call reference (`call:<session>/<call_id>`) built from the tool observer's `call_id`. If a result envelope already exposes that id, it is used; otherwise the reference is added to the envelope, once, for every tool.

**Observation fields.**

| Field | Written by | Meaning |
| --- | --- | --- |
| `from_calls` | The model (required, at least one) | The recorded calls that produced the result: a measurement, a game, a script run |
| `lineage` | The log | For each cited call: tool, arguments, start and end time, result digest, the authoring session's agent and model, from the transcript row and, when present, the task record |
| `artifacts` | The log | The artifact versions the cited calls used or generated, with their SHA-256, from the calls' `TransformRecord`s |
| `derived_from` | The model (optional) | The Observation or artifact version this candidate came from |
| `measurements` | The model | Structured values: metric, value, unit, and the condition keys they were taken under (settings, seed, strategy, node, repeat index). A value may instead name a field of a cited call's structured result (`{"from": "call:…", "field": "…"}`), and the log copies the value from the record, so values a tool returned are never retyped |

Lineage reads the transcript row for every cited call, and the task record as well when there is one. It never reads the Undo ledger.

**Checks.** A cited call must exist and the author must be allowed to cite it, or the entry is rejected with `lineage_call_unknown` or `lineage_call_outside_branch`. The author may cite calls in its own branch's task tree, and a fork may also cite its forker's calls up to the fork point, which its copied transcript contains. An Observation with no cited call is rejected with `lineage_missing`. A `derived_from` must resolve, and a measurement that names a result field must name one the cited call returned (`measurement_field_unknown`). The log does not judge whether the values are right.

**Where it shows.**

- In the log panel, an Observation's sources open to each cited call's turn in the transcript, with its arguments and result.
- The study tree draws `derived_from` edges, so a candidate's ancestry shows beside the hypothesis tree.
- Exports carry sources, and every link resolves offline.

## Design: data access

The agent gets the measured values programmatically, as data it can analyze in its own code with whatever statistics the experiment needs. The log computes no statistics.

**Values as data.** `query_log` returns an Observation's `measurements` as structured rows with its lineage. A result too large for the context takes the path every tool result already takes (`tools/mcp_result_projection.py`): it is written to the session's tool-output folder and the agent is told where, so `query_log` needs no size rule of its own. `export_measurements(filter, format="parquet"|"csv")` writes the matching measurements to a data file next to a manifest. The manifest lists the filter, the Observation ids, the condition keys and the file's SHA-256. Both files are designated as artifacts by the tool's result, so they are versioned and hashed like any other output. In an isolated run the files go to the caller's node folder; otherwise they go to `<workspace>/.clio/science-log/<session_id>/`, the same per-session layout the shell uses for oversized output, and are deleted with the session. The agent then analyzes the file with its own code: a mean for one experiment, a paired t-test for another, a bootstrap or a fitted model for a third.

**Claims cite their analysis.** An Evidence or Conclusion claim that rests on numbers cites the analysis call (the shell or Python run) that produced them. That call's `TransformRecord` already records the script's hash, the exported file it used with its hash, and the environment tier. When the analysis is random (a bootstrap, a permutation test), the Evidence records its seed. "H2 beats H1 on shared seeds, mean difference 0.4, paired 95% interval 0.2 to 0.6" is the agent's claim, backed by a script anyone can rerun.

**Other branches' data.** Because every branch reads the whole study (see **Whole-study visibility**), a branch can query and export what its siblings and cousins have measured.

**Later, through code.** When programmatic tool calling lands (a separate 1.0 item), analysis code will call `query_log` directly. Until then, the exported file is how values reach code. Nothing is built or reserved for that path now.

## Design: isolated workspaces

**On request.** Isolation is off by default, so existing workflows and skills that write to known workspace paths keep working. A workflow started with `isolate: true` gets `<workspace>/runs/<run-id>/`, and every node gets its own subfolder: `<run-id>/<step-id>/`, forks as `<step-id>~1/` and so on, and children nested under their parent's folder. The `run-a-study` skill tells the agent to start studies isolated, since that is where parallel branches collide.

**How a node knows its folder.** A node's shell runs with its folder as the working directory, and its launch context names the folder, so a relative path such as `results.csv` lands in it.

**Enforcement.** Isolation adds a per-session narrowing to the one territory source that the OS fence and the tool-boundary check share, so both narrow together: a node's session may write only its own folder, and a write outside it is refused with the fence's typed reason. A node reads the whole workspace.

Plugin tools are covered at CLIO's tool boundary, which runs per call and knows the calling session. In an isolated run a tool's declared output path is grounded against the node's folder instead of the workspace root, so a relative output lands in the node's folder, and an absolute one outside it is refused with the same typed reason. What the boundary cannot see is a file a plugin's server writes on its own, with no output path in the call's arguments. Those servers (the `fleet` profile) run once per workspace and serve every session, so the OS fence cannot confine them to one node either: two nodes using such a tool may write the same file. Neither study's plugin writes files. The limit is recorded as a decision, and plugin authors are told to take output paths as arguments. Separately, an isolated run whose fence backend cannot narrow (no Landlock, srt or Windows fence available) returns a typed reason at start instead of running unconfined in silence.

**Forks.** A fork in an isolated run starts with a copy of its forker's folder at the moment of the fork, as it starts with a copy of the transcript. Large files are cloned copy-on-write where the filesystem supports it and copied otherwise, never hard-linked, because a hard link is the same file and an edit through one name changes the other. Cloning uses `FICLONE` on Linux (btrfs, XFS), `clonefile` on macOS (APFS) and block cloning on Windows (ReFS, Dev Drive); NTFS, ext4 and other filesystems copy. The method used is recorded on the fork. A fork outside an isolated run copies no folder.

**What the person sees.** The run's folder is an ordinary folder in the workspace, browsable in the file view, and each node's folder is linked from its node in the railroad and the study tree.

## Design: the Wordle study

**Kickoff prompt.** The only Wordle text the agent sees:

```text
What is the best approach to solving Wordle? Use the word list from the
wordle tools, not the official one. Play every game yourself instead of
writing a solver.
```

**Agent.** The science agent with the `wordle-demo` plugin added. No agent class, skill or prompt fragment is written for the study, and `wordle-demo` ships no fragment.

**`wordle-demo` plugin** (marketplace):

| Tool | Returns | Notes |
| --- | --- | --- |
| `word_list()` | Every valid guess | Answers come from this list; which words are answers stays hidden |
| `new_game(seed)` | Game id | The same seed always gives the same answer |
| `guess(game_id, word)` | Per-letter feedback, turns left | Wrong length or an unknown word is a tool error that costs no turn |
| `game_state(game_id)` | Guesses, feedback, solved or failed | Lets a resumed agent or a fork recover |

- The word list is generated: a few thousand pronounceable five-letter pseudo-words whose letter frequencies deliberately differ from English, so recalled Wordle knowledge does not help. The generator is seeded and versioned; the list ships as a data file with its version. The answer subset is derived from the secret seed (rule 9).
- Each game's Observation records one game as its cost, so the log shows the sample behind every claim.
- Every guess emits a UI event (game id, branch, guess, feedback) that drives the board, so the agent does not narrate games for them to appear.
- The board is an A2UI catalog in the plugin. Generic views stay in the workspace kernel, following the A2UI layering rule.

**What the audience sees.** The study runs many games at once, so the board view is a wall rather than one grid. Each running game is a small board whose tiles flip as feedback lands, tinted by its branch, grouped under its hypothesis. A finished game shrinks into its Observation's thumbnail and the wall reflows. Beside the wall, each branch keeps a live tally (games played, mean guesses, failures), so the moment one strategy pulls ahead is visible before the merge. When a fork appears, its group opens on the wall next to the branch that spawned it.

**Reference.** An information-maximizing solver in `scripts/showcase/wordle_reference.py`, outside every plugin, plays the same list and seeds offline. Its numbers are used only to check and publish the result.

**Code during play.** The shell stays available for analysis. The prompt forbids a solver, and the audit (below) confirms every guess came from a model decision.

## Design: the hidden-law study

The agent is given a simulated bench apparatus and asked what governs it. The law is generated rather than taken from physics, so a model that recites the textbook answer is measurably wrong. Scoring uses settings the agent never measured, so success is objective rather than judged.

**Kickoff prompt.**

```text
What determines the period of the oscillator on the bench? Find the law
that predicts it.
```

**Agent.** The science agent with the `hidden-law-bench` plugin added. Nothing else.

**`hidden-law-bench` plugin** (marketplace):

| Tool | Returns | Notes |
| --- | --- | --- |
| `apparatus()` | The controllable inputs with units and ranges (arm length, bob mass, release angle, ambient temperature), what is measured (time for ten cycles), and the stated measurement noise | Static description; costs nothing |
| `measure(settings, repeats=1)` | One noisy reading per repeat | Out-of-range settings are a tool error |
| `history()` | Every reading this study has taken, by branch | Lets a resumed agent or a fork recover |

**The law.** A seeded generator in the plugin's server draws one law per secret seed (rule 9), from a family with four properties:

- the period depends on two inputs through non-textbook exponents (for example arm length to about 0.6 rather than 0.5);
- one input has no effect at all;
- one input changes the regime above a threshold, so a single power law fits half the range and fails on the other half;
- readings carry Gaussian noise at the stated level.

This family is chosen because it produces the behavior branched science is for. One branch per input is the natural start. The decoy input tests whether the agent concludes "no effect" from evidence rather than from intuition. The regime change is the natural reframe ("is this one law or two?"), and it should arrive as a fork from the branch that finds it.

**Code during the study.** Fitting with the shell (numpy, scipy) is expected here. Unlike Wordle, analysis code is part of doing this science well; the agent may not call a fitter shipped by the plugin, because there is none.

**What the audience sees.** The plugin ships one A2UI catalog, the bench, which shows the real apparatus and its shadow:

- every measurement animates the real oscillator at the measured period, labelled with its settings and branch;
- once a branch states a law (a Hypothesis with a formula, or a Conclusion), a translucent shadow oscillates beside the real one at the period that law predicts for the same settings;
- while a law is wrong, the shadow visibly drifts out of phase, and past the regime threshold it breaks away until a branch finds the second regime;
- a strip of past measurements shows real and shadow phase error per setting, so convergence is visible as the strip settles.

The widget renders only what measurements returned and what laws the agent wrote. It never shows the hidden law. After the study, the scorer's held-out settings replay with real and shadow side by side; that replay is produced for the export and the site, not shown to the agent. Fits and residuals stay in the generic chart, and the study tree is the generic one.

**Scoring.** `scripts/showcase/hidden_law_score.py`, outside every plugin, takes the merged Conclusion's law and evaluates it against 50 held-out settings across the full range, regime included, drawn from the same secret seed. It reports prediction error in units of the stated noise, whether the decoy was declared without effect, and the threshold the agent reported against the true one. It also scores the textbook pendulum law on the same settings, as the baseline a recalled answer would give.

## Design: running the showcases

The two studies are demos as well as tests, so they run the way a demo should look.

- **Model.** Every session in a study tree runs on a Sonnet-class Claude model through the `claude_code` provider. Choose it at run time from `list_models` and set it on the root session; children, steps and forks inherit it. Do not write a model id into code, a plugin or a skill. Record the model in the ledger.
- **Parallelism.** Branches run as parallel workflow steps, and a branch spreads its own work across children through the default delegation (for Wordle, players each running a batch of games under the branch's strategy; for the bench, measurement series). Nothing caps this artificially: no flow concurrency limit, workflow growth bounds at 0, **Forks per task** at 0.
- **No budget.** Neither study declares a budget. The agent decides when its evidence is enough, and the log records the sample size behind every claim. The budget mechanism is still built and tested (F12), on small mechanism studies only.
- **Provider limits.** Wide parallelism will meet the subscription's rate limits. A child that hits one waits and resumes through the model fleet's typed usage reasons; it never fails silently or switches model on its own. A study that stalls on limits is reported with the reason, not trimmed to fit.
- **Recording.** Record each study's live run continuously with the site's demo pipeline (`site/video/README.md`): real interaction, accelerated work intervals labelled with their speed, nothing scripted. Before relying on it, check that the cloud browser can record continuously; if it cannot, record the row as blocked on the recording browser and leave the raw take for the human.

## Design: export of flows and subagents

A study is only worth publishing if its export shows how it ran. Today's export shows the conversations; it does not show the orchestration. After this change an exported session reads like the live session does.

**What an export must show, offline:**

| Work in the session | What the export shows |
| --- | --- |
| A subagent | The spawn in the parent's transcript, linked to the child's full transcript; the child's agent, model, assignment and pinned evidence; its result where the parent received it |
| A subagent tree | Every descendant to the export's depth limit, navigable both ways, with the relationship of each (delegated, role, fork) |
| A fork | The fork point in the original's transcript, the note, and the fork's transcript from there; in a workflow, the fork beside its step |
| A flow run (council, arena, workflow) | The railroad as drawn live, with each step's final state; selecting a step opens its instructions, answer or verdict, time, and its transcript |
| A workflow's edits | Editor proposals, the approval Questions with the person's answers, rejected proposals, and stalls with their typed reason |
| A study | The study tree and every log entry, with citations followable to the entry and to the transcript turn that wrote it; each Observation's sources, followable to the cited calls' turns; the exported data files and analysis scripts each claim cites; and each node's folder in an isolated run |
| A2UI from a plugin | Rendered from the plugin's catalog recorded with the session, so the Wordle boards render without the plugin installed |
| The final chart | Rendered by the same generic chart component |

**One renderer.** The export viewer reuses gact-tui's own railroad, study tree and A2UI components through the standalone build the site already uses for the widget gallery. Do not draw a second railroad for exports. The run's data comes from what the session recorded (the flow checkpoint view, task history, log entries), never from a live server.

**Size.** Exports already carry their viewer and saved results. Keep the published study exports within the size the NDP exports set (about 60 MB). If a study's export goes past that, find what is duplicated rather than dropping content.

## Design: publication

**What is published.** The passed plain-request sessions (see **Which sessions**), and one worked example on clio.iowarp.ai, "Run a branched study", with the two studies as its two sections. For each study the example carries:

- the native session export of the qualification run, which opens in a browser without CLIO and shows the conversation, the workflow railroad with its forks, the study tree, the charts, the board wall for Wordle and the bench with its shadow for the hidden law;
- the edited recording of the live run, from the site's demo pipeline;
- the workflow the agent composed, as the JSON the run recorded, so a reader sees the steps, editors and forks in a form they could pass to `run_workflow`;
- how to repeat it: the plugin to install, the generator version, and the kickoff prompt (the secret seed is not published; a reader's run draws its own);
- the reference result beside the agent's: the solver's numbers for Wordle, and the score table with the textbook baseline for the hidden law.

**When.** A session is published only after its row has passed, independent checks included, and after the export scenarios F19 and F20 have passed. A session that has not passed is not published in a weaker form.

**Which sessions.** Only sessions started from a plain request, because a published conversation is read as the agent's own choices. That means the two studies (F13, F15), the third-domain study (F11) and the delegation sessions (F17, F18), plus a plain-request fork session if one of these did not already fork. Tool-directed mechanism sessions stay as evidence in the ledger and are not published.

**Where they go.** An export under 20 MB is committed under `site/public/conversations/<collection>/`, as the task conversations already are. A larger one is a release asset pinned by size and sha256, served through the general conversation route.

**Site.** `site/src/lib/ndp-conversations.ts` and `site/src/pages/conversations/ndp/[case]` are generalized into one published-conversations list and one `/conversations/[collection]/[case]/` route serving the NDP conversations and every published session from this change. The NDP-specific module and route are deleted in the same change, the NDP URLs stay the same, and `ndp-conversations.spec.ts` becomes the spec for the general route. Large exports are release assets under one tag (for example `science-log-<date>`), pinned by size and sha256. The page is `site/src/content/docs/docs/examples/branched-study.mdx`, with a `LinkCard` in `docs/examples/index.mdx`, a sidebar entry in `astro.config.mjs`, and the conversation routes excluded from the sitemap as the NDP routes are. It follows `site/WRITING.md`: written for someone using CLIO, no internal vocabulary, captures only from the real runs. Each conversation page is checked in the production build the way the NDP pages are.

**User docs.** The site gains docs for what this change adds, written to `site/WRITING.md` with captures from the real runs:

| Page | What it covers |
| --- | --- |
| `docs/science-log.mdx` (new, in the sidebar) | Asking CLIO to study a question; what the log records; the Science log tab, reading its study tree and log panel; following a citation; budgets; asking to approve forks and new steps; exporting a study; links to the published F11 study |
| `docs/tasks/subagents.mdx` | Delegating without naming an agent; the explore, planner and review roles; what a child keeps and what it does not see |
| `docs/tasks/recorded-conversations.mdx` | The published F17 and F18 sessions and the fork session, each with the plain request that started it |
| `docs/flows.mdx` | Fork inside a workflow: what it adds to the diagram, who waits on it, how approval and limits apply |
| `docs/branch-and-rewind.mdx` | Forks the agent makes, how they appear in the left panel, and how they differ from yours |
| `docs/exports.mdx` | What an export shows for flows, subagents, forks and studies |
| `docs/examples/branched-study.mdx` (new) | The two published studies, as above |

**Pushing it.** Once a session's preconditions hold, commit its export or pin, its page and its captures, and push the branch. For exports over 20 MB, create the release with `gh release create`. If the session's credentials cannot create the release, leave those assets and their sums in `.cloud-run/publish/`, record the row as blocked on release rights, and say so in the final report. Verify the pages with a local production build against the assets staged in the build cache. Merging to `main`, which deploys the site, is the human's step.

## Defaults for the open questions

Take these and record each in the science ledger.

| Question | Default |
| --- | --- |
| Where the log lives | A Core lane through the `TaskHistory` writer, confirmed in Phase 0 |
| Branch visibility | Every branch reads the whole study; each writes only its own branch |
| Reframe approval | None by default; a study that wants it sets `approve_edits`, which also gates forks |
| Budget | Optional; one shared pool per study when declared, charged by Observations |
| Fork naming | `<step>~<n>` in workflows; `<title> (fork)` for conversations |
| Agent forks of root conversations | Shown in the left panel like a person's fork, with the agent as author |
| Model for roles and forks | The caller's, unless the spawn names one |
| Showcase model | A Sonnet-class Claude model chosen from `list_models` at run time |
| Invalid guess or out-of-range setting | A tool error |
| Isolated workspaces | Off unless the workflow is started with `isolate: true`; the method skill starts studies isolated |
| Export format | Parquet, with CSV on request |
| Export location | The node folder in an isolated run; otherwise `<workspace>/.clio/science-log/<session_id>/` |
| Hashing | The artifact registry's SHA-256 of the file's content, recorded when the call that wrote it completes |
| Large files on fork | Copy-on-write clone where the filesystem allows, otherwise a copy; never a hard link |

## Verification

These rules decide when anything counts as done. They apply to every phase, not only the last one.

**Evidence.** A ledger row passes only with recorded evidence: the test names and results, the commands run, the session ids and task handles, the paths of saved transcripts and exports under `.cloud-run/<row>/`, and for UI work the screenshots you read. A claim without evidence is `pending`.

**The ledger.** `docs/design/sdk/science-ledger.md` uses the columns of the existing ledgers (ID, required behavior, evidence, status). Decisions are `FD1` onward; scenarios are `F1` onward. Status is `pending`, `passed`, `failed` or `blocked`.

**UI checks.** After every UI change, not only at the end: run `bash scripts/cloud/stack.sh restart`, then `node scripts/cloud/ui_check.mjs` with a `--steps` script for the flow you changed, at 1440 by 900 and again at 390 by 844. Read every screenshot it prints. `consoleErrors` and `failedRequests` must be empty. Show status as icons with detail on hover, not text boxes. A change a person sees in the app (the Science log tab, the railroad's forks, the left panel's agent forks, the board wall, the bench) is also checked in the native Tauri desktop app on Linux under Xvfb with the `verifier-clio` skill's `native.sh` (`deps` and `build` once per container, then `run` with a step file), attached to the same stack through `CLIO_GACT_URL`, and its screenshots are read.

**Live scenarios.** Run each against a freshly restarted stack with the `claude_code` provider. Drive them through the real UI or the real API, never by calling internals.

**Every live session is exported.** After each live scenario, export its session through the conversation menu's exporter, as HTML and as a ZIP with session artifacts, into `.cloud-run/<row>/`. Open the HTML offline in Chromium and check it against the export table for what that session did: its flows, subagents and forks must be there. The export is part of the row's evidence, and a session whose export is missing something fails the row until the export is fixed. Running every scenario through the exporter is how export support for flows and subagents gets exercised beyond F19 and F20.

**Mechanism versus judgement.** A prompt that names a tool, its arguments or the steps verifies the mechanism only, as `CLAUDE.md` says. Record such a result as "mechanism verified with a tool-directed prompt; not evidence of agent judgement". Only a plain request with no tool or step named counts as evidence that the agent chooses well, and so as evidence for the roles, fragments and method skill that steer it. The Prompt column below says which kind each scenario is. A judgement scenario that fails is fixed at its cause (a description, fragment or skill that does not steer), never by naming the tool in the prompt.

**Audits.** Machine-checkable criteria are checked by a committed script, not by reading. `scripts/showcase/audit_study.py` reads a study's exported transcript and log and reports:

- every log citation resolves to an entry its author could see;
- no reading of hidden material: no tool call or shell command touched the secret seed, the answer subset or the law parameters;
- for Wordle, every `guess` call follows a model turn, and no shell process produced a guess;
- forks, their notes, and the steps that waited on them;
- the sample size behind each claim, and the budget charged when one was declared;
- every Observation's cited calls, and whether each lies in its branch's task tree or its forker's history;
- for every quantitative claim, the analysis call it cites, with that call's script hash, input hashes, environment tier and recorded seed.

The audit reports these as facts. It does not judge the numbers.

**Independent check.** Each study's acceptance is judged by a fresh agent that did not build the feature. Give it only the criteria, the export path, the audit output and the scorer or reference output. It returns pass or fail per criterion with the entry ids or handles behind each. Record its verdict in the ledger. If it fails a criterion, fix the cause and rerun the study; do not argue with the verdict.

**Regression.** Run focused tests while working: `uv run --frozen --no-sync python -B scripts/run_tests.py tests/<area> -m "not integration"`. Before finishing, run the full suite the same way on `tests/`, then `uv run ruff check`, `uv run ruff format --check`, `scripts/check_file_size.py` and `scripts/check_no_class_in_function.py`. For gact-tui, run its test suite. For the site, run `pnpm test`, `pnpm check`, `pnpm build` and the Playwright specs you touched.

**Blocked rows.** The cloud session qualifies the Tauri desktop on Linux under Xvfb, so desktop rows are not blocked. It cannot qualify the desktop on Windows or macOS, Windows-only paths or SSH to HPC systems; only those rows may be `blocked`. A failing scenario is fixed, not deferred.

## Phase 0: inventory and decisions

No production code changes in this phase.

1. Confirm which `TaskHistory` lane holds a session's log entries and how the derived study index is built, and record the decision.
2. Inventory every defined subagent in the marketplace with its prompt size, plugins, `structured_outputs` and the agents that reach it, and mark the generic ones.
3. List every fragment and A2UI guidance block that should be `session: root`.
4. Export sessions that ran a council, an arena, a workflow with an editor and a nested flow, a subagent tree three levels deep, and an A2UI widget. Open each offline and record what the viewer does not show, against the table in the export design.
5. Write the science ledger with the decisions above and every scenario below.
6. Commit, push, open draft PRs, and continue to Phase 1.

## Phase 1: default delegation

1. Make `agent` optional in both spawn tools, resolve an omitted agent through `self`, and rewrite both descriptions.
2. Add `derive` and `effects` to `Subagent`, the resolver and `plugin check`, with `effects="read"` running under the side-session read-only profile.
3. Add `session` to fragment `when`. Move `core.rendering` and A2UI guidance to `session: root`.
4. Ship `explore`, `planner`, `review` and the two fragments in `delegation`.
5. Verify how a child's Question reaches the person, fix delivery if needed, and record the decision.
6. Apply the marketplace cleanup verdicts, deleting generic duplicates and moving their callers in the same change.

## Phase 2: fork

1. Add `fork_of` to `TaskSnapshot` and the family rule to `TaskOperations` waits and results.
2. Add the `fork` tool to `delegation`, accepted through the idempotent spawn path with `copy_session_context` and the note.
3. In the `workflow` flow: count forks against growth bounds, gate them with `approve_edits`, resolve `$<step>` to the family, publish fork steps in the live view, and offer a failed fork to the editors.
4. Add **Forks per task** to the settings, with `fork_limit_reached`.
5. Route agent forks of root conversations through the fork route, and lift `fork_not_allowed` for agent forks only.

## Phase 3: the science log

1. Add the `science-log` plugin with the entries, validation, visibility and tools above, on the store chosen in Phase 0.
2. Write `run-a-study` and `science-log.method`.
3. Add `science-log` to `ScienceAgent`. Rewrite `maintain-scientific-dossier` onto the log and add `science-log` to factorio.
4. Add the domain grep test for `delegation`, `science-log`, the task layer and the generic views.

## Phase 3b: sources, data access and isolated workspaces

If Phase 3 is already done, this phase runs before the UI and qualification phases continue; otherwise it is built as part of Phase 3. Record its decisions in the science ledger as the next `FD` numbers. Any of F11, F13 and F15 that ran before this phase is run again after it, because its sessions must show sources. Earlier runs stay in their `.cloud-run/` folders as superseded evidence and are not published.

1. Add the call reference to tool result envelopes where a result lacks one.
2. Add the Observation fields, filling lineage from transcript rows and task records and artifacts from `TransformRecord`s, measurements copied from named result fields, and the validation errors.
3. Return measurements as rows from `query_log`, and add `export_measurements` with its manifest and artifact designation.
4. Add `isolate` to the workflow flow: create the run and node folders, set each node's working directory and launch context, narrow the shared territory source per session for both the fence and the tool boundary, ground and check declared output paths against the node's folder, refuse a run whose fence cannot narrow with a typed reason, and clone or copy the forker's folder on fork. Unit-test the macOS and Windows clone paths with the platform call faked; they are qualified live on Linux only.
5. Update the method skill and fragment.
6. Extend `audit_study.py` with the source and analysis reports.

## Phase 4: UI in gact-tui

1. **Fork in the railroad.** A fork step sits beside its original with a fork mark, and its tooltip names the note. Dependents show the extra wait.
2. **Forks in the left panel.** An agent's fork of a conversation appears under its origin with the agent as author.
3. **Science log tab.** The right-hand panel gains a **Science log** tab beside Work, Artifacts, Context, Files and Activity. It appears only when the session belongs to a study (it or an ancestor in its task tree wrote a root Question) and is absent otherwise. The study tree sits at the top of the tab and the selected branch's log panel below it. Selecting a node in the tree opens that branch's entries; it does not navigate away from the session.
4. **Study tree.** A live tree of questions, hypotheses and branches with status on each node (running, supported, refuted, superseded). Reframes and forks are drawn differently from steps, and `derived_from` edges show a candidate's ancestry beside the hypothesis tree. Nodes link to their task rows and, in an isolated run, to their folders, and open their entries.
5. **Log panel.** A branch's entries in order, with links followable both ways. Selecting a citation jumps to the cited entry, opening its branch in the tab if it is another branch's, or to the transcript turn that wrote it. An Observation shows its sources, each opening to the cited call's turn in the transcript with its arguments and result.
6. **Chart.** The kernel's generic chart serves both studies (distributions, scatter, fit against data, residuals). If the kernel lacks one of these, add it there, never in a plugin.
7. **Board wall.** The `wordle-demo` catalog: the wall of live boards grouped by branch, tiles flipping as feedback lands, per-branch tallies, finished games collapsing into their Observation's thumbnail.
8. **Bench.** The `hidden-law-bench` catalog: the real oscillator and the shadow of the current law, the phase-error strip, and the held-out replay for exports.
9. **Export viewer.** Add whatever Phase 0 found missing.

## Phase 5: export of flows and subagents

1. Record what each export needs from the session: the flow checkpoint views, task relationships, log entries with their sources, the data files and analysis scripts claims cite, and node folders, alongside the transcripts it already carries.
2. Build the export viewer's flow, subagent, fork and study views from gact-tui's components through the standalone build.
3. Carry each session's resolved A2UI catalogs into the export so plugin widgets render without the plugin.
4. Add export round-trip tests for each row of the export table, and an offline Playwright check of an exported workflow run.

## Phase 6: the study plugins

1. `wordle-demo`: the word list generator and the generated list, seeded and versioned; the MCP server with its four tools, strict validation and UI events; the board catalog.
2. `hidden-law-bench`: the law generator; the MCP server with its three tools and strict validation; the bench catalog and its measurement events.
3. Both: derive hidden material from the secret seed in the server environment (rule 9), and add a test that greps the plugin files and a fresh workspace for it.
4. `scripts/showcase/wordle_reference.py`, `scripts/showcase/hidden_law_score.py` and `scripts/showcase/audit_study.py`, with tests.
5. `clio-agent plugin check` passes on both plugins.

## Phase 7: qualification

| ID | Scenario | Prompt | Passes when |
| --- | --- | --- | --- |
| F1 | Factorio spawns with no `agent` | Mechanism | The child's resolution digest equals the parent's, and it calls an Abaqus tool |
| F2 | The standard agent delegates to `explore` | Mechanism | A write tool is denied with the read-only reason; findings come back |
| F3 | `planner` output goes to `run_workflow` | Mechanism | The workflow starts unchanged and its steps run |
| F4 | Compare a root and a delegated prompt in the prompt view | Mechanism | Rendering and A2UI guidance appear only in the root; `delegation.child` only in the child |
| F5 | A child raises a Question | Mechanism | It appears in the root session attributed to the child, and the answer reaches the child |
| F6 | An agent forks a root conversation | Mechanism | A new conversation appears in the left panel with the copied history and origin; the original continues |
| F7 | A delegated child forks | Mechanism | The parent's wait covers both, and the result lists both by handle |
| F8 | A workflow step forks mid-run, with `approve_edits` | Mechanism | The approval Question appears; after approval the fork is drawn beside its step, the dependent waits on both, and its input lists both answers |
| F9 | Restart the server after the fork in F8 | Mechanism | No duplicate fork; the run resumes on the same handles |
| F10 | Forks beyond **Forks per task** | Mechanism | `fork_limit_reached` is returned and nothing is created |
| F11 | A small study in a third domain, asked as a plain question | Judgement | Entries of every type the study needed appear in the tree (a Reframe or Conflict only where the evidence called for one, FD35); every claim cites entries; a citation of a missing entry is a typed error; every Observation cites calls, and every quantitative claim cites an analysis over an exported file |
| F12 | A study that runs out of budget | Mechanism | The next Observation is rejected with `budget_exhausted`, and the branch concludes with what it has |
| F13 | The Wordle study, unbounded, on a Sonnet-class model | Judgement | The audit is clean, the independent check passes every Wordle criterion below, and the recording exists |
| F14 | Restart during a Wordle run | Kickoff prompt | Steps and forks resume on the same handles, games resume through `game_state`, and the log has no duplicate entries |
| F15 | The hidden-law study, unbounded, on a Sonnet-class model | Judgement | The audit is clean, the scorer's output is recorded, the independent check passes every hidden-law criterion below, and the recording exists |
| F16 | Build the site with every published session | No prompt | Every published export opens offline and shows everything in the export table for its run; site tests, type check, build and the conversation spec pass; the example page, the new and updated docs pages, the task conversations and the NDP pages render |
| F17 | A plain request in a large workspace that is easiest to answer by surveying many files first | Judgement | The agent delegates the survey on its own, to `explore` or the default, and the child's report, not the file contents, enters the root context |
| F18 | A plain request whose separable parts would flood one context | Judgement | The agent spawns children with no `agent` named or a role, writes each a self-contained task, and merges their reports |
| F19 | Export a session that ran a council, an arena and a workflow with an editor, an approval, a nested flow and a fork | Mechanism | Offline, each run's railroad matches the live one, every step opens its transcript and answer, and the proposals, approvals and fork appear where they happened |
| F20 | Export a session with a subagent tree three levels deep that includes a role, a fork and a model override | Mechanism | Offline, every descendant is reachable from its spawn and back, with its agent, model, assignment and result |
| F21 | Open the F11 study, and a session that is not in a study, in the web UI and in the native desktop app under Xvfb | No prompt | The Science log tab appears only for the study; the tree sits above the selected branch's entries; selecting a node opens that branch's entries; a citation jumps to the cited entry and to the transcript turn that wrote it; screenshots at 1440 by 900 and 390 by 844 in the web UI and from the desktop app are read |
| F22 | Write an Observation citing a real measurement call, with one value named by result field | Mechanism | Its sources list the call's tool, arguments, times, result digest and artifact versions, all matching the transcript and the artifact registry, and the named value equals the call's result |
| F23 | Write an Observation with no cited call, one citing a call from another branch, and, in a fork, one citing the forker's call from before the fork | Mechanism | `lineage_missing` and `lineage_call_outside_branch` are returned and nothing is written for the first two; the third is accepted |
| F24 | `export_measurements` on a known set of Observations, in and outside an isolated run | Mechanism | The file's rows match the log, the manifest lists the Observation ids and condition keys, the hash matches the file and its artifact version, and each file is where the defaults put it |
| F25 | An isolated workflow: two parallel steps write a file with the same name; one tries to write outside its folder; a step forks; a merge reads its inputs | Mechanism | Both files exist in their own folders; the outside write is refused with the fence's reason; the fork's folder starts as a copy, cloned or copied as recorded; the merge reads both inputs and writes only its own folder |

**Wordle criteria (F13):**

- at least three hypotheses proposed without being given any, run as workflow steps;
- where a branch finds something no hypothesis offered, or the premise proves doubtful, it is pursued (a fork, a reframe or a further step) and the merge waits on it; a study whose results raise neither needs no fork or reframe (FD35);
- every claim in the merged Conclusion cites log entries;
- the merged ranking agrees with the reference solver on the same seeds, or the log explains the disagreement by sample size;
- strategy comparisons are paired over shared seeds in the agent's own analysis, cited in the merged Conclusion;
- every guess was a model decision;
- branches ran in parallel and spread their games across children, as the task tree shows;
- screenshots read during the run show the board wall filling across branches and a fork's group opening on it;
- no Wordle-specific code, skill or fragment outside `wordle-demo`.

**Hidden-law criteria (F15):**

- one branch per candidate input, without being told which inputs matter;
- the decoy input concluded to have no effect, citing the evidence, with the decoy conclusion citing an analysis that shows no effect within its uncertainty;
- the regime change found and pursued (a fork, a reframe or a further step, which the merge waited on, when a branch first finds it), with the regime claim citing Observations on both sides of the threshold (FD35);
- the merged law predicts the held-out settings within twice the stated noise on both sides of the threshold, and beats the textbook baseline;
- every claim cites log entries, and the reported threshold is within 10 percent of the true one;
- screenshots read during the run show the shadow drifting while a law is wrong and settling once the merged law holds, and the held-out replay shows real and shadow together;
- no hidden-law-specific code, skill or fragment outside `hidden-law-bench`.

## Phase 8: publication

1. After F19 and F20 pass, generalize the site's published-conversation module and route, and delete the NDP-specific ones.
2. Publish each passed plain-request session as it passes: commit or pin its export, and add it to its page.
3. Write the example page and the user docs pages above, with captures from the real runs, and add them to the sidebar and the examples index.
4. Create the release for the large exports, and push the site changes.
5. Run F16 against the production build.

## Done

1. Every phase is implemented; the deleted subagents, the prose dossier and the NDP-specific site module and route are gone; the domain grep and hidden-material tests pass.
2. The full test suite, ruff, the file-size and nested-class guards, the gact-tui suite and the site checks pass, each run shown in the transcript.
3. Scenarios F1 to F21 pass against a freshly restarted stack with the evidence the Verification section requires.
4. F22 to F25 pass with the evidence the Verification section requires, and F11, F13 and F15 have passed again after Phase 3b landed.
5. Every published study's export shows sources for every Observation, the data and analysis behind every quantitative claim, and each node's folder.
6. The science ledger has every row passed, apart from the platform rows named above.
7. The branches are pushed in clio-agent, gact-tui and clio-agent-marketplace, the submodule pins point at the pushed commits, and the draft PRs carry a summary and the evidence.
8. Every live scenario's export is in its `.cloud-run/<row>/` folder. Every passed plain-request session is published on the site branch, committed or pinned as a release asset, or, if the release could not be created, its asset and sum are in `.cloud-run/publish/` with the row blocked on release rights. The workflow JSONs, audit and scorer outputs and independent verdicts are in `.cloud-run/f13/` and `.cloud-run/f15/`.
9. Each study's raw take, action timestamps and edited recording are in `.cloud-run/f13/` and `.cloud-run/f15/`, or the row is blocked on the recording browser.
10. A final report in the transcript lists every ledger row with its status and evidence.

## Working conventions

- Branch `claude/delegation-fork-science` in clio-agent, gact-tui and clio-agent-marketplace. Open draft PRs against `develop` in all three: the marketplace pin `d0d92981` is on its `develop`, not `main`.
- Small Conventional Commits per step, with test evidence in each PR description.
- Keep runtime state in `.cloud-run/`. Stop the stack when you finish a work block, and clean up every process and file you created, as `AGENTS.md` requires.
