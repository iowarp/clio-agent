# Acceptance ledger: plugins, Agent SDK and flows

Rows follow [megachange-plan.md](megachange-plan.md). A row passes only with recorded evidence: test names and results, commands, task handles, and for UI work the screenshots that were read. Status is `pending`, `passed`, `failed` or `blocked`. Only the platform rows the plan names (Tauri desktop, Windows-only paths, SSH to HPC systems) may be `blocked`.

## Decisions

| ID | Decision | Source |
| --- | --- | --- |
| D1 | No new `TaskKind`. A flow run is checkpoints under its owning assignment. | Plan defaults |
| D2 | Handoff is not implemented; delegation returns a value. | Plan defaults |
| D3 | One flow run is capped at 10,000 checkpoint entries; past it the run ends with the typed outcome `flow_checkpoint_limit`. | Plan defaults |
| D4 | `ctx.call` passes the assignment prompt plus the evidence references (`input_task_refs`) the flow passes explicitly. | Plan defaults |
| D5 | Plans only; `run_code`, `rlm` and `code` flows are out of scope. | Plan defaults |
| D6 | Only a plan's named editors may edit it. The human cancels nodes through the existing task controls. | Plan defaults |
| D7 | The name is `flow` everywhere. | Plan defaults |
| D8 | The shipped default marketplace (the bundled submodule, or the pinned default URL) is trusted as part of the product, like the pip-installed `core` and `orchestration` plugins. Every other git or local marketplace needs the explicit trust step. | Phase 1 |
| D9 | The `external-agents` pack (`a2a`, `acp`, `academy`) from the design's `StandardAgent` example is not built: remote agents are out of scope. | Out of scope |
| D10 | SDK hook events use snake-case names that map one to one onto CLIO hook events: `tool_call`→`PreToolUse`, `tool_result`→`PostToolUse`, `tool_batch`→`PostToolBatch`, `prompt_submit`→`UserPromptSubmit`, `stop`→`Stop`, `session_start`→`SessionStart`, `session_end`→`SessionEnd`, `subagent_start`→`SubagentStart`, `subagent_stop`→`SubagentStop`, `pre_compact`→`PreCompact`, `before_model`→`BeforeModel`, `after_model`→`AfterModel`. | Phase 1 |
| D11 | A `Subagent` may name an optional tool allowlist, which the Claude Code importer needs for `agents/*.md`. Each listed tool must be provided by the subagent's plugins. The allowlist narrows only what the subagent's plugins add: `core`'s tools (task controls, questions, skills) always stay, since `core` is never removable. | Phase 1 |
| D12 | Plugin stdio MCP servers start in the session workspace (CLIO's write-confinement boundary), not the plugin root; `PLUGIN_ROOT` and `PLUGIN_DATA` are expanded and also set in the server's environment. CLIO additionally supplies `${CLIO_PROVENANCE_CONFIG}` (the provenance handoff file) to a declaration that references it, replacing the blueprint placeholders (`${CLIO_BLUEPRINT_DIR}` becomes `${PLUGIN_ROOT}`). | Phase 2 |
| D13 | `clio_agent.sdk` is the authoring SDK the designs name; the typed HTTP client that held that package name moved to `clio_agent.client`. | Phase 1 |
| D14 | `ctx.call("self", ...)` runs this agent as a worker on its `react` flow (pinned to the caller's exact resolution and delta), never its selected flow, so a flow's helpers cannot re-enter the flow; a flow recurses only through `ctx.call(<flow>)`. `run_flow`/`run_plan` start the flow in such a child, which is how a flow started by the model gets a handle at acceptance without a new `TaskKind` (D1). | Phase 3 |
| D15 | Call targets resolve in this order: a flow object, `self`, a defined subagent, a resolved flow id, a resolved tool, an installed agent (`marketplace/id`). The plan flow keys each node's invocation by node id (`ctx.call(..., key=)`), since nodes start in completion order. | Phase 3 |
| D16 | A plan editor's proposals are read from its own settled result (`tools_called` rows of `plan_add_node`/`plan_cancel_node`), so replay applies exactly the same edits with no extra store; the editor tools are granted by the child's `plan_editor` metadata, not claimable by a plugin. | Phase 3 |

## Phase rows

| ID | Required behavior | Evidence | Status |
| --- | --- | --- | --- |
| P0-1 | Removal manifest lists every blueprint and declared-workflow module with its production callers | `docs/design/sdk/removal-manifest.md`: 53 removed modules with AST callers, removed symbols, declared-workflow runner, rewritten and kept modules | passed |
| P0-2 | Marketplace inventory with a plugin and pack split for every agent | Manifest, marketplace section: 28 AGENT.md agents (14 top level, 14 `old/`), shared capability plugins and one domain plugin per agent | passed |
| P1-1 | `clio_agent.sdk` exports `Plugin`, `Pack`, `Agent`, `Subagent`, `Flow`, `Requires`, `Prefers`, `PromptFile`, `hook` | | pending |
| P1-2 | Loader reads `plugin.json` against Agent Plugins 1.0.0 (ten top-level fields) plus the `ai.iowarp.clio` extension and folder conventions | | pending |
| P1-3 | Claude Code importer maps marketplace, plugin, `.mcp.json`, `skills/`, `hooks/hooks.json`, `agents/*.md`, dependency-only packs and federated marketplaces into the same plugin record, with typed reasons for anything unmapped | | pending |
| P1-4 | Trust: pip and default marketplace trusted; git and external marketplaces need the trust step; an update that changes executable parts needs it again; a text-only update does not | | pending |
| P1-5 | Stage 1: `spec()` imports the agent once and stores a frozen spec with its checksum | | pending |
| P1-6 | Stage 2: pack expansion, user delta, cascade of required plugins, model requirement union, tool collision error, hook cycle error, hook order child to parent with `before`/`after`, five-section composed prompt, pinned to the session | | pending |
| P1-7 | `clio-agent plugin check` and the pytest fixture run the install resolver and fail on a missing prompt file, a tool collision and a hook cycle, and warn above 150 tokens per fragment | | pending |
| P1-8 | `core` plugin holds the guidelines, the rendering section generated from the UI catalog, the `clio.runtime.tasks` fragment and the `react` flow wrapping `ClioReAct` | | pending |
| P2-1 | Every marketplace agent is converted to plugins, packs and Python agent classes; the base prompt is cut into identity, `core` guidelines and capability fragments | | pending |
| P2-2 | Plugin and agent REST routes replace the blueprint routes and GACT bindings | | pending |
| P2-3 | AGENT.md loader, `clio-pack.yaml` path and every manifest module are deleted; the guard test rejects their imports | | pending |
| P3-1 | `Flow` is a `dspy.Module` with `forward(task, ctx)`; `ctx.call`, `start`, `gather`, `as_completed`, `ask`, `step`, `workspace`, `depth` map onto the task layer | | pending |
| P3-2 | Invocation ids derive from run, call site, target and input hash, and occurrence; handles are deterministic; replay re-attaches; typed outcomes raise | | pending |
| P3-3 | Limits on depth, concurrency, model calls and budget are enforced in `ctx` | | pending |
| P3-4 | Checkpoint cap returns `flow_checkpoint_limit` (D3) | | pending |
| P3-5 | Agents select a flow by id; `react` is the default | | pending |
| P3-6 | `plan` flow with `Plan`, `Node`, editors `plan_add_node`/`plan_cancel_node`, growth bounds and approval through a Question task | | pending |
| P3-7 | `orchestration` plugin ships `run_plan`, `run_flow` (handle at acceptance), `council`, `arena` | | pending |
| P3-8 | Declared-workflow runner, `workflow:` parsing and `run_workflow` are deleted; spawn-order merge and typed stalls live in `ctx.gather` and the plan flow | | pending |
| P4-1 | Agent + menu adds and removes plugins, shows pack membership, previews the cascade and saves a user agent (parent plus delta) | | pending |
| P4-2 | Plugin provenance (core, standard, science, pack, user) is shown | | pending |
| P4-3 | Composed prompt view attributes each fragment to its plugin | | pending |
| P4-4 | Flow run view draws the live plan from task history, with proposed nodes awaiting approval and typed stalls | | pending |
| Q-1 | Focused tests for every touched area pass through `scripts/run_tests.py` | | pending |
| Q-2 | `ruff check` and `ruff format --check` are clean on the touched paths | | pending |

## Live scenarios

Run against a freshly restarted `bash scripts/cloud/stack.sh start` with the `claude_code` provider. UI checks use `node scripts/cloud/ui_check.mjs --steps <file>` at 1440x900 and 390x844.

| ID | Scenario | Passes when | Evidence | Status |
| --- | --- | --- | --- | --- |
| L1 | The standard agent answers a question | The prompt view shows each fragment attributed; the answer arrives | | pending |
| L2 | Remove a required plugin in the + menu | Its dependents leave too, the UI showed them first, and the new session reflects it | | pending |
| L3 | Run `council` on a question | Three child handles exist, then one synthesized answer | | pending |
| L4 | A plan with a critic editor | The critic proposes a node, the approval Question appears, and the node runs after approval | | pending |
| L5 | Restart the server mid-plan | The run resumes on the same handles, and `query_tasks` shows no duplicate tasks | | pending |
| L6 | Stop during a flow, then `cancel_tasks` on it | Stop leaves the flow running; cancel settles its subtree | | pending |
| L7 | `clio-agent plugin check` on a broken plugin | A missing prompt file, a tool collision and a hook cycle each fail with a clear message | | pending |
| L8 | Install clio-kit `main` through the importer, including its federated clio-coder marketplace after the trust step | Check passes per plugin or reports a typed reason; `clio-hdf5` HDF5 tool runs `clio-kit mcp-server hdf5`; `clio-dataset-report`'s hook fires; a `clio-agents` subagent runs with mapped tools | | pending |
| L9 | Compose a user agent: standard + `clio-hpc` pack + `clio-hdf5`, minus `xlsx` | Stored as parent plus delta; new session shows `clio-hpc` members, no xlsx tools, attributed fragments; removing `clio-slurm` removes only it; removing a hard requirement shows and applies the cascade; bumping standard's version reaches the saved agent's next session | | pending |

## Platform rows

| ID | Required behavior | Evidence | Status |
| --- | --- | --- | --- |
| X-1 | Tauri desktop shell shows the plugin UI | A cloud session cannot run the Tauri desktop | blocked |
| X-2 | Windows-only paths (Windows sandbox fence, Windows install layout) with plugins | A cloud session has no Windows host | blocked |
| X-3 | HPC plugins reach a real cluster over SSH (`clio-slurm` jobs) | A cloud session cannot reach HPC systems over SSH | blocked |
