# Plugins, Agent SDK and flows: implementation plan

This plan turns three design documents into code. Agents become Python classes that select plugins. Plugins follow Agent Plugins 1.0.0 with CLIO content under `ai.iowarp.clio`. Orchestration becomes flows: DSPy modules whose calls are ordinary CLIO tasks. The blueprint system and the declared-workflow runner are deleted and replaced, not kept beside the new code.

## Contract

The designs in this folder are the contract. When code and design disagree, change the code. If the design itself turns out wrong, choose the fix that best keeps the design's intent, update the document in the same change, and record the decision in the acceptance ledger. The work runs to completion without stopping for review; the human reviews the finished branch.

| Document | Governs |
| --- | --- |
| [plugins-and-composition.md](plugins-and-composition.md) | Plugin format, packs, agents as plugin sets, resolution, coexistence rules, composed main prompt, trust |
| [agent-sdk.md](agent-sdk.md) | The Python classes, ownership, two-stage resolution, validation and `clio-agent plugin check` |
| [orchestration-semantics.md](orchestration-semantics.md) | Flows, the `ctx` primitives on the task layer, durability, plans, the `workflows.py` replacement |
| [remote-agents-and-protocols.md](remote-agents-and-protocols.md) | Context only; out of scope for this change |

Base: `codex/agent-facing-async-tasks` in clio-agent (this setup branch sits on `55d06ee5`; the design review was done at `47b6bd49`), with gact-tui at the commit the submodule pins, which is on gact-tui's `codex/agent-facing-async-tasks`. That branch rebuilt the task layer. Read `docs/design/task-replacement-plan.md` and `docs/design/agent-facing-async-tasks.md` before Phase 1. When the team merges it, rebase onto `develop`.

## Rules

1. **Delete and replace.** Keep no dual loader, compatibility shim, feature flag or dormant copy of the blueprint system or the declared-workflow runner. A path judged wrong is deleted in the same change that replaces it. Do not stub anything listed as out of scope.
2. **The task layer is the only execution authority.** Flows submit through `Conversations.spawn`, task-capable MCP calls and Question tasks. They wait through `TaskOperations` and checkpoint in `TaskHistory`. Add no registry, store, scheduler or polling loop of your own.
3. **The model decides.** Follow the superseding principles in `.claude/CLAUDE.md`. Core never routes, completes or retries on a heuristic. A flow's control flow is code written by a plugin author or by the model, and core executes only what that code calls.
4. **No silent fallback.** Every refusal, stall or degraded path returns a typed reason that reaches the API and the trace.
5. **Repository rules.** Follow `AGENTS.md` for resource ownership and cleanup, ruff, tests and Conventional Commits. Do not touch the protected human files named in `task-replacement-plan.md`.
6. **Evidence over claims.** A phase is done when its ledger rows pass with recorded evidence: test names and results, and for UI work the screenshots you read.

## Out of scope

Do not implement, stub or reserve fields for these:

- Remote agents: `Peer`, `Expose`, A2A, ACP and Academy.
- Self-optimizing plugins: `evolved_from`, override slots, lessons and the optimizer gate.
- `rlm` and `code` flows, plus `run_code`. These depend on the separate programmatic tool calling work.
- Visual authoring of flows.

## Defaults for the open questions

Take these defaults and note each in the ledger.

| Open question | Default for this change |
| --- | --- |
| Task kind for flows | No new `TaskKind`. A flow run is checkpoints under its owning assignment, as declared workflows are now. |
| Handoff | Not implemented. Delegation returns a value. |
| Checkpoint size | Cap one run at 10,000 checkpoint entries and return a typed `flow_checkpoint_limit` outcome past it. |
| Context passed to subagents | The assignment prompt plus pinned evidence references (`input_task_refs`) the flow passes explicitly. |
| Plan or script by default | Plans only, since `run_code` is out of scope. |
| Edits from outside the plan | Only named editors may edit. The human cancels nodes through the existing task controls. |
| Naming | `flow` everywhere, because "loop" already means interval monitoring in CLIO. |

## Phase 0: inventory and removal manifest

No production code changes in this phase.

1. Map the blueprint system with production callers, using AST like the task replacement inventory did: `gact/agent_blueprints.py`, `gact/agent_blueprint_*.py`, `gact/blueprint_*.py`, `gact/expert_packs.py`, `gact/agents/blueprint_commission.py`, `gact/agents/execution_blueprint.py`, `gact/agents/unresolved_blueprint.py`, AGENT.md parsing, `clio-pack.yaml`, and their REST routes and GACT bindings.
2. Map the declared workflow: `gact/workflows.py`, `gact/workflow_state/`, `run_workflow` in `gact/task_declarations.py` and `gact/agents/spawn_runtime_declarations.py`, and module variants in `gact/agents/module_variants.py`.
3. Inventory `external/clio-agent-marketplace`. It has 28 AGENT.md agents. For each, record its experts, skills, MCP servers, A2UI catalogs, hooks and any `workflow:` block, and propose its plugin and pack split.
4. Write `docs/design/sdk/removal-manifest.md`, listing each module as removed, rewritten or kept with its callers. Write `docs/design/sdk/acceptance-ledger.md` with these columns: ID, required behavior, evidence, status. Include the live scenarios from Phase 5.
5. Commit, push the branch and open a draft PR, then continue straight to Phase 1. The manifest is for traceability, not a review gate.

## Phase 1: plugin standard, resolver and composed prompt

1. Add the public SDK package `clio_agent/sdk/` with `Plugin`, `Pack`, `Agent`, `Subagent`, `Flow`, `Requires`, `Prefers`, `PromptFile` and the `hook` decorator.
2. Add the plugin loader:
   - Read `plugin.json` against the Agent Plugins 1.0.0 schema, plus the `ai.iowarp.clio` extension.
   - Read the folder conventions: `skills/`, `mcp.json`, and `ai.iowarp.clio/` with `plugin.py`, `tools/`, `hooks/`, `subagents/`, `commands/`, `flows/`, `prompt/` and `a2ui/`.
   - Add one importer that brings Claude Code plugin layouts and marketplaces into the same internal record. clio-kit `main` is the main real-world example: 72 plugins in `.claude-plugin/marketplace.json`, each with `.claude-plugin/plugin.json` and some of `.mcp.json`, `skills/`, `hooks/hooks.json` and `agents/*.md`. It gets no special treatment. The importer maps that layout as follows:
     - A plugin whose only content is `dependencies` (such as `clio-hpc` or `clio-skills`) becomes a pack, so its members stay individually removable. `dependencies` on a plugin with content of its own become hard requirements.
     - `.mcp.json` entries become the plugin's MCP servers, started lazily.
     - `skills/` stays portable as is.
     - `hooks/hooks.json` events map onto CLIO hook events. An event with no CLIO equivalent is reported with a typed reason, never dropped silently. Other clients' hook files, such as `codex.json` or `opencode.js`, are ignored.
     - `agents/*.md` become defined subagents of that plugin. Claude Code tool names map to CLIO tools, and an unmappable tool is a typed import error. A `model:` pin becomes a model preference, not a requirement.
     - `federated-marketplaces.json` entries pinned by commit are followed as git sources and go through the trust step.
     - Client-specific index files outside the Claude Code format, such as `.clio-kit/catalogue.json`, are not read.
3. Trust:
   - pip-installed plugins are trusted.
   - Git and external marketplaces need an explicit trust step.
   - An update that changes executable parts needs the trust step again.
4. Resolution happens in two stages:
   - **Stage 1 (install):** `spec()` imports the agent once and stores a frozen spec with its checksum.
   - **Stage 2 (session start):** expand packs, apply the user delta, cascade removal of required plugins, and build the union of model requirements. Treat tool-name collisions and hook-ordering cycles as errors. Order hooks child to parent, adjustable with `before=`/`after=`. Compose the main prompt in its five sections, then pin the result to the session.
5. Add `clio-agent plugin check` and a pytest fixture that run the same resolver as install.
6. Create the `core` plugin. It holds:
   - the operating guidelines;
   - the rendering section generated from the UI catalog;
   - the `clio.runtime.tasks` lifecycle fragment;
   - the `react` flow wrapping `ClioReAct`.

## Phase 2: migrate shipped agents and delete blueprints

1. Convert every marketplace agent to plugins, packs and Python agent classes on a branch of `iowarp/clio-agent-marketplace`. Cut the base agent's prompt apart into identity, `core` guidelines and one fragment per capability plugin.
2. Replace the blueprint REST routes and GACT bindings with plugin and agent routes.
3. In the same change, delete the AGENT.md loader, the `clio-pack.yaml` path and every module the manifest marks for removal. Add a guard test that rejects imports of the removed symbols.

## Phase 3: flows on the task layer

1. Add `Flow` as a `dspy.Module` with `forward(task, ctx)`. Implement `ctx.call`, `ctx.start`, `ctx.gather`, `ctx.as_completed`, `ctx.ask`, `ctx.step`, `ctx.workspace` and `ctx.depth` exactly as the orchestration document's table maps them onto the task layer.
2. Durability:
   - Derive invocation ids from the run, the call site, a hash of the target and input, and an occurrence index.
   - Pass each id to `spawn` so the handle is deterministic, and checkpoint the run through `TaskHistory`.
   - On replay, re-attach to existing handles.
   - Surface `task_state_uncertain` and the other terminal outcomes as typed exceptions.
3. Limits: enforce depth, concurrency, total model calls and budget in `ctx`, on top of the existing inference capacity.
4. Agents select a flow by id. `react` is the default.
5. Ship the `workflow` flow in `core`, with `Workflow`, `Step`, editors (`workflow_add_step`, `workflow_cancel_step`), growth bounds the caller or the person sets, and approval through a Question task. (Named `plan` until Phase 5, when it was renamed so it is not mistaken for Plan mode.)
6. Ship an `orchestration` plugin with `run_workflow` and `run_flow`, both of which return a handle at acceptance, plus `council` and `arena` flows.
7. Delete the declared-workflow runner, the `workflow:` parsing and `run_workflow`. Move spawn-order merging and typed stalls into `ctx.gather` and the plan flow.

## Phase 4: UI in gact-tui

Commit UI work in `external/gact-tui` on a branch of `iowarp/gact-tui`, and update the submodule pin in clio-agent.

1. Add the agent "+" menu. It adds and removes plugins, shows pack membership, previews the cascade when a required plugin is removed, and saves the result as a user agent (parent plus delta).
2. Show each plugin's provenance: core, standard, science, a pack, or the user.
3. Add a composed prompt view with each fragment attributed to its plugin.
4. Add a flow run view. It draws the live plan from task history, shows proposed nodes awaiting approval, and shows typed stalls with their reason.

Verify every UI change yourself:

1. Run `bash scripts/cloud/stack.sh restart`.
2. Capture the screen with `node scripts/cloud/ui_check.mjs`, passing `--steps` with a small script for the flow you changed. Capture it at 1440 by 900 and again at 390 by 844.
3. Read every screenshot it prints, and check that `consoleErrors` and `failedRequests` are empty.

## Phase 5: qualification

Run focused tests while working:

- `uv run --frozen --no-sync python -B scripts/run_tests.py tests/<area> -m "not integration"`
- `uv run ruff check` and `uv run ruff format` on the touched paths

Run these live scenarios against `bash scripts/cloud/stack.sh start`, which defaults to the `claude_code` provider. Record each in the ledger with its handles and screenshots.

| ID | Scenario | Passes when |
| --- | --- | --- |
| L1 | The standard agent answers a question | The prompt view shows each fragment attributed; the answer arrives |
| L2 | Remove a required plugin in the + menu | Its dependents leave too, the UI showed them first, and the new session reflects it |
| L3 | Run `council` on a question | Three child handles exist, then one synthesized answer |
| L4 | A plan with a critic editor | The critic proposes a node, the approval Question appears, and the node runs after approval |
| L5 | Restart the server mid-plan | The run resumes on the same handles, and `query_tasks` shows no duplicate tasks |
| L6 | Stop during a flow, then `cancel_tasks` on it | Stop leaves the flow running; cancel settles its subtree |
| L7 | `clio-agent plugin check` on a broken plugin | A missing prompt file, a tool collision and a hook cycle each fail with a clear message |
| L8 | Install clio-kit `main` as a marketplace through the importer, including its federated clio-coder marketplace after the trust step | `clio-agent plugin check` passes for every imported plugin, or reports a typed reason per plugin. An agent with `clio-hdf5` added calls an HDF5 tool that runs `clio-kit mcp-server hdf5`. `clio-dataset-report`'s hook fires. A `clio-agents` subagent runs with mapped tools. |
| L9 | Compose a user agent in the + menu: start from the standard agent, add the `clio-hpc` pack and `clio-hdf5`, remove `xlsx` from the documents pack, and save | The saved agent is stored as parent plus delta. A new session shows `clio-hpc`'s members, no xlsx tools, and fragments attributed to their plugins. Removing `clio-slurm` removes only it, since it is a pack member. Removing a hard requirement shows and applies the cascade. Bumping the standard agent's version reaches the saved agent's next session. |

The cloud session cannot qualify the Tauri desktop, Windows-only paths, or SSH to HPC systems. Mark those rows blocked, not passed. Nothing else may be marked blocked: a failing scenario is fixed, not deferred.

## Done

The change is done when all of these hold, each shown in the session transcript, not only claimed:

1. Every phase is implemented and every module in the removal manifest is gone, with the guard test passing.
2. The focused tests for every touched area pass, and `ruff check` and `ruff format --check` are clean on the touched paths.
3. Live scenarios L1 to L9 pass against a freshly restarted stack. For each one the transcript shows the commands, the task handles, and the screenshots that were read.
4. The acceptance ledger has every row passed, apart from the platform rows named above as blocked.
5. The branches are pushed in clio-agent, gact-tui and clio-agent-marketplace. The clio-agent submodule pins point at the pushed commits, and the draft PRs are updated with a summary and the evidence.
6. A final report in the transcript lists each ledger row with its status and evidence.

## Working conventions

- Branch `claude/sdk-plugins-flows` in clio-agent, gact-tui and clio-agent-marketplace. Open draft PRs against the base branch.
- Make small Conventional Commits per step, with test evidence in each PR description.
- Keep runtime state in `.cloud-run/`. Stop the stack when you finish a work block.
