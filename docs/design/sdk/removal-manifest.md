# Removal manifest: blueprints and declared workflows

This manifest traces every module the plugins, Agent SDK and flows change removes, rewrites or keeps. It follows [megachange-plan.md](megachange-plan.md) Phase 0. Production callers were enumerated with Python AST over `src/clio_agent`, including function-local and relative imports, at clio-agent `2c100d5` (branch `claude/sdk-megachange-setup`). Module paths are relative to the repository root; caller names are relative to `clio_agent`.

The guard test `tests/test_plugins/test_removal_guard.py` rejects imports of every module in the **Removed** tables and the listed symbols. Removal is complete when that test passes and no file below remains.

## Removed: the blueprint system

AGENT.md parsing, `clio-pack.yaml` expert packs, the blueprint installer, sources, coordination, drafts, revisions, updates, activation and their REST routes. Replaced by the plugin loader, importer, trust store and two-stage resolver in `clio_agent/plugins/` and the SDK classes in `clio_agent/sdk/`, with plugin and agent routes in `gact/routes/plugins.py`.

| Module | Production callers at `2c100d5` |
| --- | --- |
| `src/clio_agent/blueprint_server_resolution.py` | `agent` |
| `src/clio_agent/gact/agent_blueprint_files.py` | `gact.blueprint_drafts`, `gact.routes.blueprint_file_read`, `gact.routes.blueprint_file_write` |
| `src/clio_agent/gact/agent_blueprint_refresh.py` | `gact.agent_blueprint_sources`, `gact.agent_blueprints`, `gact.blueprint_installer`, `gact.blueprint_mutations`, `gact.default_registry_migration` |
| `src/clio_agent/gact/agent_blueprint_requires.py` | `gact.agent_blueprints`, `gact.blueprint_activation`, `gact.blueprint_installer`, `gact.routes.blueprint_selection`, `gact.routes.blueprint_session_activation`, `gact.routes.blueprints` |
| `src/clio_agent/gact/agent_blueprint_sources.py` | `gact.blueprint_catalog`, `gact.blueprint_identity`, `gact.blueprint_installer`, `gact.blueprint_source_configuration`, `gact.default_registry_relocation`, `gact.routes.blueprint_updates`, `gact.routes.blueprints`, `gact.version_info` |
| `src/clio_agent/gact/agent_blueprints.py` | `agent`, `gact.a2ui_catalogs.activation`, `gact.a2ui_catalogs.blueprint`, `gact.a2ui_catalogs.doctor`, `gact.a2ui_catalogs.registry`, `gact.agent_blueprint_files`, `gact.agent_blueprint_refresh`, `gact.agent_blueprint_sources`, `gact.agents.resolution`, `gact.agents.skill_runtime`, `gact.agents.unresolved_blueprint`, `gact.app`, `gact.blueprint_activation`, `gact.blueprint_catalog`, `gact.blueprint_descriptor_reload`, `gact.blueprint_drafts`, `gact.blueprint_identity`, `gact.blueprint_install_revision`, `gact.blueprint_installer`, `gact.blueprint_runtime_preparation`, `gact.default_registry_migration`, `gact.default_registry_relocation`, `gact.routes.agents`, `gact.routes.blueprint_candidates`, `gact.routes.blueprint_catalog`, `gact.routes.blueprint_file_write`, `gact.routes.blueprint_selection`, `gact.routes.blueprint_session_activation`, `gact.routes.blueprints`, `gact.routes.expert_packs`, `gact.routes.mcp`, `gact.routes.mcp_specs`, `gact.spawn_context`, `gact.spotter_arming`, `gact.spotter_availability`, `runtime.mcp_launcher` |
| `src/clio_agent/gact/agents/blueprint_commission.py` | `gact.task_declarations` |
| `src/clio_agent/gact/agents/execution_blueprint.py` | `gact.agents.resolution` |
| `src/clio_agent/gact/agents/github_shell_guidance.py` | `gact.agents.clio_react` |
| `src/clio_agent/gact/agents/task_guidance.py` | `gact.agents.clio_react` |
| `src/clio_agent/gact/agents/unresolved_blueprint.py` | `gact.turn` |
| `src/clio_agent/gact/blueprint_activation.py` | `blueprint_server_resolution`, `gact.agent_blueprint_requires`, `gact.agents.unresolved_blueprint`, `gact.app`, `gact.blueprint_reload`, `gact.blueprint_runtime_preparation`, `gact.routes.blueprint_selection`, `gact.routes.mcp`, `gact.routes.mcp_specs`, `gact.spotter_arming` |
| `src/clio_agent/gact/blueprint_cas.py` | `gact.blueprint_coordination`, `gact.blueprint_coordination_admin`, `gact.routes.blueprint_file_write` |
| `src/clio_agent/gact/blueprint_catalog.py` | `gact.routes.blueprint_catalog`, `gact.routes.blueprint_selection`, `gact.routes.blueprint_session_activation` |
| `src/clio_agent/gact/blueprint_coordination.py` | `gact.agent_blueprint_refresh`, `gact.blueprint_coordination_admin`, `gact.blueprint_drafts`, `gact.blueprint_install_revision`, `gact.blueprint_mutations`, `gact.default_registry_migration`, `gact.default_registry_relocation` |
| `src/clio_agent/gact/blueprint_coordination_admin.py` | `runtime.blueprint_coordination_cli` |
| `src/clio_agent/gact/blueprint_descriptor_reload.py` | `gact.blueprint_runtime_preparation`, `gact.routes.blueprints` |
| `src/clio_agent/gact/blueprint_drafts.py` | `gact.routes.blueprint_file_read`, `gact.routes.blueprint_file_write` |
| `src/clio_agent/gact/blueprint_git.py` | `gact.blueprint_installer`, `gact.blueprint_source_revision` |
| `src/clio_agent/gact/blueprint_git_publish.py` | `gact.blueprint_drafts` |
| `src/clio_agent/gact/blueprint_identity.py` | `gact.a2ui_catalogs.activation`, `gact.agent_blueprint_files`, `gact.agent_blueprint_refresh`, `gact.agent_blueprint_sources`, `gact.agent_blueprints`, `gact.agents.resolution`, `gact.agents.unresolved_blueprint`, `gact.blueprint_activation`, `gact.blueprint_catalog`, `gact.blueprint_descriptor_reload`, `gact.blueprint_drafts`, `gact.blueprint_installer`, `gact.default_registry_migration`, `gact.routes.blueprint_identity`, `gact.routes.blueprint_selection`, `gact.routes.blueprint_session_activation`, `gact.routes.blueprints`, `gact.spotter_arming` |
| `src/clio_agent/gact/blueprint_install_files.py` | `gact.agent_blueprints`, `gact.blueprint_descriptor_reload`, `gact.blueprint_drafts`, `gact.blueprint_install_revision`, `gact.blueprint_installer`, `gact.blueprint_mutations`, `gact.blueprint_runtime_preparation`, `gact.default_registry_migration`, `gact.default_registry_relocation` |
| `src/clio_agent/gact/blueprint_install_revision.py` | `gact.agent_blueprints`, `gact.blueprint_installer` |
| `src/clio_agent/gact/blueprint_installer.py` | `gact.agent_blueprints` |
| `src/clio_agent/gact/blueprint_ledgers.py` | `gact.agent_blueprint_refresh`, `gact.agent_blueprint_sources`, `gact.blueprint_coordination_admin`, `gact.blueprint_drafts`, `gact.blueprint_install_revision`, `gact.blueprint_reload`, `gact.default_registry_migration` |
| `src/clio_agent/gact/blueprint_mutations.py` | `gact.blueprint_coordination`, `gact.blueprint_coordination_admin`, `gact.blueprint_descriptor_reload`, `gact.blueprint_install_revision`, `gact.blueprint_reload`, `gact.blueprint_source_configuration`, `gact.routes.blueprints` |
| `src/clio_agent/gact/blueprint_operations.py` | `gact.blueprint_reload`, `gact.routes.blueprint_operations` |
| `src/clio_agent/gact/blueprint_paths.py` | `gact.agent_blueprints` |
| `src/clio_agent/gact/blueprint_placeholders.py` | `gact.blueprint_activation`, `gact.spotter_arming` |
| `src/clio_agent/gact/blueprint_reload.py` | `gact.routes.blueprint_selection`, `gact.routes.blueprints` |
| `src/clio_agent/gact/blueprint_revision.py` | `gact.blueprint_reload`, `gact.routes.blueprint_source_configuration`, `gact.routes.blueprints` |
| `src/clio_agent/gact/blueprint_runtime_preparation.py` | `gact.blueprint_installer`, `gact.blueprint_reload` |
| `src/clio_agent/gact/blueprint_source_configuration.py` | `gact.agent_blueprint_refresh`, `gact.blueprint_descriptor_reload`, `gact.blueprint_drafts`, `gact.routes.blueprint_source_configuration`, `gact.routes.blueprints` |
| `src/clio_agent/gact/blueprint_source_revision.py` | `gact.agent_blueprint_sources` |
| `src/clio_agent/gact/blueprint_update_check.py` | `gact.routes.blueprint_updates` |
| `src/clio_agent/gact/default_registry_migration.py` | `gact.agent_blueprint_refresh`, `gact.app`, `gact.blueprint_coordination`, `gact.blueprint_coordination_admin`, `gact.blueprint_install_revision`, `gact.blueprint_mutations` |
| `src/clio_agent/gact/default_registry_relocation.py` | `gact.agent_blueprint_refresh` |
| `src/clio_agent/gact/expert_packs.py` | `gact.agent_blueprints`, `gact.agents.composition`, `gact.agents.resolution`, `gact.app`, `gact.routes.expert_packs` |
| `src/clio_agent/gact/routes/blueprint_candidates.py` | `gact.agent_blueprint_sources` |
| `src/clio_agent/gact/routes/blueprint_catalog.py` | `gact.routes.blueprints` |
| `src/clio_agent/gact/routes/blueprint_file_read.py` | `gact.routes.blueprints` |
| `src/clio_agent/gact/routes/blueprint_file_write.py` | `gact.routes.blueprints` |
| `src/clio_agent/gact/routes/blueprint_identity.py` | `gact.routes.blueprints` |
| `src/clio_agent/gact/routes/blueprint_operations.py` | `gact.routes.blueprints` |
| `src/clio_agent/gact/routes/blueprint_selection.py` | `gact.routes.blueprints`, `gact.routes.search_assistant`, `gact.routes.session_creation` |
| `src/clio_agent/gact/routes/blueprint_session_activation.py` | `gact.routes.blueprints` |
| `src/clio_agent/gact/routes/blueprint_source_configuration.py` | `gact.routes.blueprints` |
| `src/clio_agent/gact/routes/blueprint_updates.py` | `gact.app` |
| `src/clio_agent/gact/routes/blueprints.py` | `gact.app` |
| `src/clio_agent/gact/routes/expert_packs.py` | `gact.app` |
| `src/clio_agent/gact/user_agents.py` | `gact.app` |
| `src/clio_agent/gact/workflows.py` | `gact.conversation_recovery`, `gact.expert_packs`, `gact.task_declarations` |
| `src/clio_agent/runtime/blueprint_coordination_cli.py` | `ui.cli_commands` |

The `gact.agents.task_guidance` and `gact.agents.github_shell_guidance` rows are prompt snippets appended by `clio_react._system`. They become fragments: `core.tasks` in the `core` plugin and `shell.prefer-gh` in the `shell` plugin.

`gact.user_agents` and the `/v1/agents` CRUD over raw `AgentDef` rows are the old user-defined agent path. User agents are now a parent plus a delta (`extends`, `add`, `remove`), saved from the + menu.

### Removed symbols in retained modules

| Module | Symbol | Replacement |
| --- | --- | --- |
| `gact/catalog.py` | `_builtin_agents`, `_builtin_main_agent`, `builtin_main_catalog_source` | Every session runs a resolved plugin agent; the default is `standard` |
| `gact/agents/resolution.py` | `_runtime_active_agent_blueprint_rows`, `_runtime_active_agent_blueprint_root_id`, `_runtime_active_agent_blueprint_agent_ids`, `_apply_agent_blueprint_mcp_descriptor_validation`, `_apply_enabled_agent_blueprint_mcp_tools`, `_runtime_apply_session_agent_overlay`, `_resolve_dynamic_agent`, `_merge_agent_def_rows` | `gact/agents/resolution.py` rewritten over the session's pinned resolution |
| `gact/agents/composition.py` | `_apply_prompt_registry_to_agent`, `_runtime_dynamic_agent_children_context` | Composed main prompt (`plugins/compose.py`) |
| `gact/agents/builders.py` | `_build_blueprint_dspy_module`, `BlueprintExpertModule`, `_build_tool_user_agent_module`, `_build_prompt_user_agent_module` | `AgentModule` built from a resolved agent |
| `gact/skills.py` | `pack` and `builtin` scopes, `_BUILTIN_SKILLS_ROOT` | `plugin` scope over the resolved plugins' `skills/` |
| `gact/builtin_skills/` | All twelve skills | Moved into the plugin that owns each (see below) |
| `gact/agents/skill_runtime.py` | `agent_pack_root`, default-root auto-declared skills | Skills come only from resolved plugins |
| `gact/task_declarations.py` | `run_workflow` declaration | `run_flow` and `run_workflow` (workflows as data) in the `orchestration` plugin |
| `gact/agents/module_variants.py` | `wrap_module_variant`, `with_session_strategy` (declared `module.variant`) | Removed with the blueprint `module:` field. `strategy_module` stays for model-requested spawn strategies |
| `gact/turn_state.py` | `DEEP_RESEARCH_BLUEPRINT_ID` overlay | Deep research selects the `deep-researcher` agent by id |
| `prompt_packs/builtin/runtime/` | `clio.runtime.tasks`, `clio.runtime.github_shell`, `clio.runtime.tool_user_agent`, `clio.runtime.prompt_user_agent`, and agent prompts `clio.chat`, `clio.orchestrator`, `clio.main.planner` | Fragments in `core` and `shell`; agent identity comes from `Agent.role` |

## Removed: the declared-workflow runner

| Module or symbol | Production callers at `2c100d5` | Replacement |
| --- | --- | --- |
| `src/clio_agent/gact/workflows.py` (`parse_workflow`, `run_declared_workflow`, `repair_workflow_reference`, `workflow_validation_errors`) | `gact.conversation_recovery`, `gact.expert_packs`, `gact.task_declarations` | Flows (`clio_agent/flows/`) and the `workflow` flow (named `plan` until the rename). Typed stalls move into `ctx.gather` and the workflow flow; `conversation_recovery` repairs flow checkpoint references |
| `workflow:` block parsing in expert files | `gact.expert_packs` | None. The one shipped `workflow:` (wildfire `main`) becomes a `Plan` in the wildfire plugin |
| `run_workflow` tool in `build_spawn_runtime_tools` | `gact.agents.builders`, `gact.catalog` | `run_workflow` (workflows as data) and `run_flow` in the `orchestration` plugin |
| `merge_run_workflow_states` in `gact/workflow_state/merge.py` | none (unused) | Spawn-order merge with conflict reports inside `ctx.gather` |
| `gact/agents/spawn_runtime_declarations.py` | n/a | Did not exist at `2c100d5`; nothing to remove |

## Rewritten

These modules stay but their blueprint coupling is replaced in the same change.

| Module | Change |
| --- | --- |
| `src/clio_agent/agent.py` | Gateway server discovery reads the resolved plugins' MCP servers instead of blueprint `mcp_servers` |
| `src/clio_agent/tools/fleet_blueprint_merge.py` | Renamed `tools/fleet_server_merge.py`; merges plugin server sets into a resident workspace fleet |
| `src/clio_agent/gact/agents/blueprint_tool_recording.py` | Renamed `agents/tool_recording.py`; generic recorded-tool wrapper |
| `src/clio_agent/gact/a2ui_catalogs/blueprint.py` | Replaced by `a2ui_catalogs/plugin.py`: one catalog declaration source per resolved plugin |
| `gact/a2ui_catalogs/activation.py`, `declarations.py`, `registry.py`, `doctor.py`, `gact/a2ui_capability_selection.py` | Catalog sources come from the resolved plugin set |
| `gact/agents/resolution.py`, `composition.py`, `builders.py`, `skill_runtime.py`, `gact/skills.py` | Build the running agent from the pinned resolution |
| `gact/turn.py`, `turn_forward.py`, `spawn_context.py`, `task_declarations.py` | Turns and spawns resolve plugin agents and defined subagents |
| `gact/agents/clio_react.py` | `_system` uses the composed prompt only |
| `gact/spotter_arming.py`, `spotter_availability.py`, `spotter_watcher.py` | Watch the `spotter-ai` plugin agent |
| `gact/routes/agents.py`, `session_creation.py`, `search_assistant.py`, `mcp.py`, `mcp_specs.py`, `session_defaults.py` | Agent selection by agent id; MCP specs from plugins |
| `gact/app.py`, `gact/version_info.py`, `ui/cli_commands.py`, `runtime/mcp_launcher.py` | Install plugin routes and the `plugin` CLI; drop blueprint bindings |
| `gact/conversation_recovery.py` | Repairs flow checkpoint references |
| `gact/workflow_state/schema.py` | Kept as the typed `workflow_state` vocabulary, now declared by a plugin (`ai.iowarp.clio.workflow_state`) |
| `gact/hooks/` | Adds an in-process adapter for SDK `@hook` functions and a plugin hook source; `hooks.json` scopes stay |

## Kept

`gact/workflow_state/state_merge.py`, `gact/workflow_state/merge.py` (`_merge_workflow_state_mapping`), the task layer (`conversations.py`, `conversation_assignments.py`, `task_operations.py`, `task_history.py`, `questions.py`), `prompts.py` for non-agent prompts, `gact/hooks/` events and dispatcher.

## Tests

Tests that exercise only removed modules are deleted with them: every `tests/**/test_*blueprint*`, `test_*expert_pack*`, `test_user_agent*` and `test_declared_workflows*` file, plus fixtures under `tests/fixtures/` that hold AGENT.md trees. Behaviour they covered that survives (MCP server launch, A2UI catalog validation, skill progressive disclosure, spawn admission) is covered by new tests in `tests/test_sdk/`, `tests/test_plugins/` and `tests/test_flows/` against public contracts. The acceptance ledger lists the required behaviours.

## Marketplace inventory and plugin split

`external/clio-agent-marketplace` holds 28 AGENT.md agents: 14 at the top level and 14 under `old/`. `cluster-operator/` has no AGENT.md (its relay manifest was removed earlier). No top-level agent declares hooks; the only hook is `old/hook-smoke/hooks/pre_message.py`. The only `workflow:` block is wildfire's `experts/main.md`.

Shared capability plugins replace the copies. `create-pdf-report` exists byte-identically in four agents and moves to the `pdf` plugin. The twelve runtime built-in skills move to the plugin that owns each capability.

| Plugin | Contents | Requires |
| --- | --- | --- |
| `core` (ships in clio-agent) | Operating guidelines, rendering section from the UI catalog, `clio.runtime.tasks` fragment, task controls, `ask_user`, `react` and `plan` flows, `planning` skill, `clio-workspace` A2UI catalog | none |
| `orchestration` (ships in clio-agent) | `run_workflow`, `run_flow`, `council` and `arena` flows | `delegation` |
| `fleet` (ships in clio-agent) | `list_models`, `usage`, `refresh_provider_models`; `fleet.models` fragment; `update-models` skill (moved from `core`) | none; `core` requires it |
| `workspace` | `fs_read_file`, `fs_list_directory`, `fs_search_text`, `fs_write_file`, `fs_edit_file`, `view_image`; grounding and edits fragments (the staged `fs_propose_edit` and `fs_apply_edit_write` are deleted, Phase 7) | none |
| `shell` | `shell_bash`, `prepare_execution_runtime`; `shell.prefer-gh` fragment | none |
| `web` | `mcp.json`: `web: clio-kit mcp-server web` | none |
| `document-runtime` | `prepare_document_runtime`, `prepare_document` | `shell` |
| `pdf`, `docx`, `xlsx`, `pptx` | `view_pdf`, `create-pdf-report`, `work-with-pdfs`; Word, spreadsheet and presentation skills (runtime built-ins plus document-production's OOXML skills) | `document-runtime` |
| `documents` (pack) | `pdf`, `docx`, `xlsx`, `pptx`, plus `latex`, `opendocument` | pack |
| `visuals` | `present-interactive-analysis`, `create-dashboard`, `review-visual-presentation`, `create-diagram` | none |
| `delegation` | `spawn_agent_task`, `spawn_agents_parallel` (general subagents) | none |
| `connected-data` | `connect-data`, `work-with-ndp` skills | none |

| Agent (AGENT.md) | Agent class | Domain plugin and split |
| --- | --- | --- |
| `base-agent` | `StandardAgent` (`standard`) | Prompt cut into identity, `core` guidelines, and `workspace`, `shell`, `visuals`, `pdf` fragments |
| `appl-core` | `ApplCore(ScienceAgent)` | `appl-core` (12 skills, `pandas`/`parquet`/`plot` MCP servers, `.lint-l3`) |
| `clio-search` | `ClioSearch(Agent)` | `clio-search` (search tools fragment) |
| `data-semantics` | `DataSemantics(Agent)` | `data-semantics` with subagents `data`, `analysis`, `visualization`, `hdf5`; `hdf5` skills |
| `deep-researcher` | `DeepResearcher(Agent)` | `deep-research` with subagents `researcher`, `critic` |
| `document-production` | `DocumentProduction(Agent)` | Uses the `documents` pack; its 8 skills move into `pdf`/`docx`/`xlsx`/`pptx`/`latex`/`opendocument`; review skill in `documents-review` |
| `earthscope-flat`, `earthscope-gnss-region` | `EarthscopeFlat`, `EarthscopeGnss` | `earthscope` (shared `workflow_state`) with their subagent trees |
| `earthscope-single-agent` | `Earthscope(ScienceAgent)` | `earthscope-skills` (8 skills, `earthscope-stations` A2UI catalog) |
| `factorio`, `factorio-flat` | `Factorio`, `FactorioFlat(ScienceAgent)` | `manufacturing` pack: `abaqus`, `topology`, `materials`, `am-process`, `characterization`, `mechanical-testing`, `fatigue`, `scientific-method`; subagents per expert |
| `phenotype` | `Phenotype(Agent)` | `phenotype` (MCP from `impl/`) |
| `spotter-ai` | `SpotterAI(Agent)` | `spotter` (MCP from `impl/`) |
| `wildfire-smoke-impact-review` | `WildfireReview(Agent)` | `wildfire` with subagents and the former `workflow:` as a `Plan` |
| 14 `old/` agents | One class each | One plugin each. Tools that no MCP server in the repository provides were never resolvable and are not carried; `hook-smoke`'s `pre_message` becomes an SDK `@hook("prompt_submit")` |

After the migration the marketplace kept only the flat agents of the two domain families: `factorio-flat` became `factorio` (agent and plugin, 0.5.0) and `earthscope-single-agent` became `earthscope` (0.5.0). The earlier Factorio agent, `earthscope-flat`, `earthscope-gnss-region` and the region depth and width variants were removed with the plugins only they used.

**Second removal round (2026-10-24, marketplace `b23ec87`).** The marketplace was rebuilt around its `ARCHITECTURE.md` and now ships ten agents: `standard`, `science`, `deep-researcher`, `document-production`, `clio-search`, `clio-data`, `factorio`, `earthscope`, `appl-core` (shown as "OPAL") and `spotter-ai` (shown as "SPOTTER"). Removed: the blueprint-era demo agents `genomics-review`, `geospatial-field-review`, `hpc-io-regression`, `materials-crystal-review`, `ndp-environmental-hazards`, `proteomics-mzml-review`, `remote-sensing-ogc-review`, `seismic-waveform-review`, `terrain-suitability` and `wildfire-smoke-impact-review` (with the `wildfire` plugin and its smoke-review flow), with their plugins; `phenotype`; `data-semantics` and `format-bridge` (folded into the new `clio-data`); `documents-review` (folded into `document-production`); `topology` (split into `fea-methods`, `abaqus`, which now carries the `abaqus-topology` catalog, and `morphorm`). Renamed: `earthscope-skills` to `earthscope`, `am-process` to `manufacturing-processes`. SPOTTER's MCP server dropped its eight phenotype campaign tools; alerts go through CLIO's native `raise_alert_card` and containment through CLIO's SPOTTER clearance. The table above records the first round and is left as it was.
