# Summoning ledger: commands, direct summoning and plugin compatibility

Rows follow [direct-summoning-plan.md](direct-summoning-plan.md) (decisions `DS`, scenarios `N`), plus plugin compatibility (`PC`) and the wtf-p test bed and fork (`W`).

- **Branch:** `claude/direct-summoning` in clio-agent, gact-tui and clio-agent-marketplace, cut from `claude/tool-calling-long-horizon` (clio-agent `a9257457`, gact-tui pin `57813b22`, marketplace pin `7a7ddf00`). Draft PRs target `claude/tool-calling-long-horizon` until it merges into `develop`.
- **Fork:** `JaimeCernuda/wtf-p`, branch `claude/direct-summoning`, from `main` at `eff98baf` (the same as `akougkas/wtf-p`).

A row passes only with recorded evidence. Status uses the [acceptance ledger](acceptance-ledger.md)'s vocabulary:

- `passed (live)`: exercised against a running CLIO (sessions, task handles, CLI output or screenshots that were read).
- `tests only`: covered by the named tests; not exercised live.
- `static`: checked by reading the tree; nothing to run.
- `done`: a recorded decision with no behaviour of its own.
- `open`, or `blocked` with the reason it cannot run in a cloud session (Tauri desktop and Windows only).

Live scenarios run on `claude-sonnet-5-5` through the `claude_code` provider (finding L41 of the tool-calling ledger). A prompt that names the tool or steps verifies the mechanism only; only a plain request counts as evidence of the agent's choices.

## Decisions

| ID | Decision | Source | Evidence | Status |
| --- | --- | --- | --- | --- |
| DS1 | Built-ins live in the core plugin's `commands/` as files with `handler:`; a `handler:` outside a built-in plugin is a `plugin check` error. | Plan defaults | | open |
| DS2 | Built-ins kept: `/clear`, `/cache-stats`, `/dump-trace`, `/cron` (alias `/schedule`), `/loop`, `/goal`. Each was run on a freshly started stack at the base and works. `/optimize` answers 501 `optimizer_not_implemented` and is deleted. | Phase 0 item 1 | Session `sess_10e9ab87b251`: `POST /v1/sessions/{sid}/commands/{id}` for each built-in returned its result part (`/loop` stop, `/goal` clear, `/cron` list, `/clear` with the destructive-action guard); `/optimize` returned 501 | passed (live) |
| DS3 | Skills as commands fail at the base: a workspace skill declaring `command:` is listed with `agent_id: main` and dispatch answers 404 "command agent not found: main". The `main` routing is in `gact/runtime/commands.py::command_defs_from_skill` and `gact/command_presentation.py`; both go. | Phase 0 item 3; Part A item 3 | Workspace `ws_9fb6e2c65e85` (`.cloud-run/ds-ws`), skill `probe-skill`: `GET /v1/commands` lists `/probe-skill` with `agent_id: main`; dispatch returned 404 | passed (live) |
| DS4 | Deletion inventory. `BACKEND_COMMANDS`: `gact/runtime/commands.py`, tests `test_cron_tools`, `test_loop`, `test_goal`, `test_optimizer_stub`. `agent_invocable`: `prompts.py`, `gact/runtime/commands.py`, `gact/catalog.py`, `gact/app.py`, `gact/routes/catalog.py`, `test_prompts_api`, `test_commands`. `planner_command_rows`: the same plus `test_interaction_read_responsiveness`. Per-turn override: `agent_override` (`turn.py`, `turn_finalize.py`, `test_post_messages`), `extract_agent_id` (`message_submission.py`, `message_contract.py`), `turn_agent_id` (15 files). `/optimize`: `optimizer/stub.py`, `gact/runtime/capabilities.py`. Old prompt routes: `gact/routes/mcp.py`, `gact/routes/mcp_rows.py`, `test_mcp_servers`, `test_decomposition_guardrails`, the MCP conformance tests. gact-tui: the "For agents" badge (`settings-prompts.tsx`), `CommandDefinition.agent_invocable`/`user_invocable`, `agent_id` in `message-schemas.ts`. | Phase 0 item 2 | `grep -rn` over both trees at the base | static |
| DS5 | `conversations.spawn` has five callers, not one: `prepare_and_spawn` (`gact/spawn_tools.py`), `flows/dispatch.py`, `gact/agent_fork.py`, `gact/agents/skill_effects.py`, `gact/flow_tools.py`. A person is named as the starter by a `TaskCaller` with `human_action=True` (`gact/task_access.py`; precedents `gact/human_controls.py`, `gact/a2ui_actions/delivery.py`), and the fork's starter path is the model for the person-started spawn. `prepare_and_spawn` is split once so only caller resolution differs. | Phase 0 item 5; Part A item 1 | `grep -rn "conversations.spawn\|\.spawn(caller"` at the base | static |
| DS6 | `@server` loads through `search_tools`' `select:` path (`search_result`, `_record_loaded`), which writes `metadata["loaded_tools"]["pending"]`; `promote_loaded_tools` moves pending names to `listed` when the prompt is next rebuilt (an applied plugin change), not at a compaction. The plan's "join the tool list at the next compaction" becomes "when the prompt is next rebuilt", and N14 is amended. The catalog is the loop agent's `_clio_deferred_catalog`, built at turn start, so the summon acts once the turn's agent exists. Ownership follows `owning_server` (the longest matching prefix). | Phase 0 item 6; Part A item 2 | `gact/search_tools.py`, `gact/agents/deferred_tools.py` | static |
| DS7 | `external_mcp_servers` is live but is a REST surface only: servers installed with `POST /v1/mcp/servers` are called through `/v1/mcp/servers/{sid}/call` and listed in the catalog, never attached to an agent's tools. An agent's prompts are listed through the MCP executor's per-namespace clients (`tools/mcp_namespace_executor.py`), cached per connection era beside the tool listing. REST-installed servers contribute no prompt rows (the session's agent defines what can be named), and the old prompt routes and their separate client are deleted. | Phase 0 item 7 | `grep -rn external_mcp_servers src` (only `routes/mcp.py`, `routes/mcp_configuration.py`, `routes/catalog.py`, `tools/mcp_config.py`) | static |
| DS8 | No command file exists in the marketplace or in clio-kit's Claude Code marketplace (24 MCP-server plugins, no commands, agents or hooks), and no `{{...}}` substitution is used outside `docs/archive`. The `{{...}}` forms are deleted. The real Claude Code plugin for the scenarios is wtf-p's `vendors/claude` (36 commands, 11 agents, a `hooks.json` write guard). | Phase 0 item 4 | `find … -path '*commands/*.md'` and `grep -rn '{{'` over the marketplace and a clio-kit clone at the base | static |
| DS9 | Native plugin command text (`${CLAUDE_PLUGIN_ROOT}`, `@file`) resolves against `<plugin>/ai.iowarp.clio/` and only works for top-level files, because `gact/catalog.py` passes `md.parent.parent` to `expand_plugin_text`. The resolution command rows carry the plugin root, and expansion uses it. | Part A item 4 | | open |
| DS10 | The core workflow flow stops on a failed step: dependents are blocked, editors are offered the failure, and the run otherwise ends `step_failed`. This is wtf-p's `onFailure: stop`, so the playbooks map onto workflows without a new policy. | Phase 0 (Part D) | `plugins/builtin/core/ai.iowarp.clio/flows/workflow.py`; `tests/test_flows/test_workflow_flow.py::test_failed_step_is_a_typed_stall_in_the_run_view` | static |
| DS11 | Bare-name clash: refused as `command_ambiguous`, listing the qualified names. | Plan defaults | | open |
| DS12 | File precedence: workspace `.clio`, workspace `.claude`, user `.clio`, user `.claude`; a loser is listed as `command_shadowed`, naming the winner. | Plan defaults | | open |
| DS13 | Skills as commands: every skill the agent resolves, except `user-invocable: false`. | Plan defaults | | open |
| DS14 | After a summon the session's agent takes a turn and sees the child handles; several summons in one message all start, concurrently. | Plan defaults | | open |
| DS15 | `@server` on tools already listed loads nothing and adds only the note line. | Plan defaults | | open |
| DS16 | An assistant-role MCP prompt message becomes quoted context in the person's message. | Plan defaults | | open |
| DS17 | A command's `model`, `allowed-tools` and `disable-model-invocation` are listed in `unsupported_fields` and do not change the turn; `argument-hint` is shown. | Plan defaults; Part B item 6 | | open |
| DS18 | The gact-tui baseline at pin `57813b22`: the composer's `/` menu reads `GET /v1/commands`, the `@` popover has no subagent, server or skill group (`composer-references.tsx`), the "For agents" badge is at `settings-prompts.tsx:124`, and `message-schemas.ts` has an `agent_id` the composer never sets. | Part A item 5 | Read at the pin | static |
| DS19 | Fixtures (marketplace `testing/`): `plugins/commands-fixture` (a subfolder command with `$1`, `$2`, `${CLAUDE_PLUGIN_ROOT}` and an `@` include; a clashing command; an `agent: review`-style command naming its `fixture-reviewer` subagent; a skill; a `user-invocable: false` skill), `plugins/commands-clash`, `plugins/prompts-fixture` (an MCP server with a required-argument prompt, a prompt returning text, a resource, an image and an assistant message, three deferred tools, and `--extra-prompt` adding a prompt after 60 s), `agents/commands_fixture.py`, and `workspaces/commands` (`.clio` and `.claude` `hello.md`, a reserved `goal.md`). | Phase 0 item 8 | `clio-agent plugin check testing` 8/8 passed; the MCP server checked with an in-memory `fastmcp.Client` | static |

## Plugin compatibility

| ID | Item | Evidence | Status |
| --- | --- | --- | --- |
| PC1 | A package with a root `plugin.json` in the Clio Coder library dialect and a `.claude-plugin/` folder loads as a Claude Code package, with a typed diagnostic naming the skipped dialect (`plugins/marketplace.py::detect_format`). | | open |
| PC2 | A native package's `hooks/hooks.json` is read through the Claude Code hook mapper, and `prompt_expansion` (`UserPromptExpansion`) joins the SDK hook events. | | open |
| PC3 | Hook fidelity: wtf-p's write guard arms on `UserPromptExpansion`, denies a write outside `paper/` and `.planning/` with its reason, and disarms on `Stop`. | | open |
| PC4 | Imported agents: all 11 wtf-p agents import; an unmapped tool in `tools:` drops only that tool with a typed warning, not the agent; `skills:` preloads map. | | open |
| PC5 | Injections from a plugin's `ai.iowarp.clio/injections` load and fire for marketplace and third-party packages; an imported package's `output-styles/` is reported as unmapped. | | open |
| PC6 | Command frontmatter from Claude Code (`allowed-tools`, `argument-hint`, `model`) follows DS17. | | open |
| PC7 | Test beds: wtf-p `vendors/claude` unmodified, one clio-kit Claude Code plugin, and the DS19 fixtures. | | open |

## Qualification scenarios

| ID | Scenario | Passes when | Evidence | Status |
| --- | --- | --- | --- | --- |
| N1 | Type `/` in a new conversation of the standard agent | The menu shows the core built-ins, the agent's skills and the fixture MCP server's prompts in their groups; no `/optimize` | | open |
| N2 | `/goal`, `/loop`, `/cron` and `/clear` from the menu | Each behaves as on the base, now served from the core plugin's files | | open |
| N3 | The fixture plugin's subfolder command with two arguments | Listed with its subfolder label; `$1` and `$2` substituted; the `@` include and `${CLAUDE_PLUGIN_ROOT}` expanded in the model text | | open |
| N4 | The workspace `.clio` and `.claude` commands of the same name | The `.clio` one runs; the `.claude` one is listed as shadowed, naming the winner | | open |
| N5 | The user-level command | Listed and runs in any workspace | | open |
| N6 | The two fixture plugins' clashing command | The bare name is refused as ambiguous with both qualified names; each qualified name runs its own plugin's file | | open |
| N7 | A workspace file named `goal.md` | Listed as disabled with `command_name_reserved`; `/goal` still runs the built-in | | open |
| N8 | wtf-p's command (or the fixture plugin's) | Lists and runs as a turn; a `UserPromptExpansion` hook that denies it stops it with the hook's reason | | open |
| N9 | `/run-a-study <question>` with the science-log plugin | The skill is loaded through `load_skill` attributed to the person, the agent works from it, and a later `load_skill` of the same skill is not repeated | | open |
| N10 | A skill marked `user-invocable: false` | Absent from the menu and still loadable by the agent | | open |
| N11 | `@explore where is the spill folder written?` | An `explore` child starts, marked as started by the person, on the text without the token; the parent's turn sees the handle and reports the child's answer | | open |
| N12 | `@explore` and `@review` in one message | Both children start concurrently; both appear under the message | | open |
| N13 | A summon part naming a subagent the agent lacks (through the API) | Refused with `summon_unknown_target`; no turn starts | | open |
| N14 | `@<fixture server> …` with that server deferred | Its tools are loaded for the turn, shown as a collapsed row; the model calls one of them; no `prefix_mismatch` reset; after the next prompt rebuild they are in the tool list (DS6) | | open |
| N15 | `/mcp__<fixture>__<prompt>` without its required argument, then with it | The first is refused with the argument's name; the second runs, and the resource and image parts reach the model | | open |
| N16 | The fixture server adds a prompt mid-session | It appears in the `/` menu without a restart | | open |
| N17 | A command file with `agent:` naming a subagent | It starts that child through the same path as N11; no per-turn override in the turn's metadata | | open |
| N18 | "Email bob@example.org about @explore" typed as plain text | `@example.org` stays text; `@explore` becomes a chip on submit | | open |
| N19 | The `/` menu and `@` popover at 390 by 844 | Both fit, groups collapse, chips wrap | | open |
| N20 | Search both repositories for the deleted names (DS4) | Nothing remains outside this ledger and the plan | | open |

## wtf-p

| ID | Scenario | Passes when | Evidence | Status |
| --- | --- | --- | --- | --- |
| W1 | Install wtf-p `vendors/claude` and `plugin check` | 36 commands, 11 subagents, 7 skills and the hook load; each unmapped item has a typed diagnostic | | open |
| W2 | `/wtfp:new-paper` from the menu | Runs as a turn with its `@${CLAUDE_PLUGIN_ROOT}` includes expanded; the agent interviews and writes `.planning/` records | | open |
| W3 | `/wtfp:write-section` with a write outside `paper/` | The guard arms on `UserPromptExpansion` and denies the write with its reason; `Stop` disarms it | | open |
| W4 | `@wtfp-section-reviewer` on a section | A child starts, marked as started by the person | | open |
| W5 | `/wtfp-plan-section` as a skill command | Loaded through `load_skill`, attributed to the person | | open |
| W6 | `/wtfp:check-refs` | The bundled `node tools/wtfp-tool.js --offline bib-index` runs through the shell; network commands are refused offline | | open |
| W7 | The fork: a new paper for an IEEE venue from a plain request | Interview, outline, one section planned, drafted and reviewed, built with `latex_build` and the venue checks | | open |
| W8 | The fork: a comment on that section in the PDF | The section writer applies it | | open |
| W9 | The fork: `@wtfp-citation-expert` on the bibliography | A person-started child works on the `.bib` | | open |
| W10 | The fork: the draft-review workflow | `run_workflow` runs section-writer then section-reviewer; the reviewer runs read-only | | open |
