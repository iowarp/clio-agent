# Commands and direct summoning: implementation plan

This plan lets the person name what should handle a message from the composer: a command or skill with `/`, a subagent or an MCP server with `@`, and an MCP server's prompts as `/` commands too. It also moves the built-in commands into the core plugin and gives command files the end-to-end tests they have never had. It has six workstreams, and each builds on the ones before it:

1. **Commands are plugin content.** The built-in commands move into the core plugin's `commands/` folder. One command table is built from the session's resolved plugins plus the workspace and user command files, with plugin-qualified names, clear rules for name clashes, subfolders and positional arguments. The agent-invocable command path and the `/optimize` stub are deleted.
2. **Skills as commands.** Every skill the session's agent has is `/<skill>`, with no frontmatter opt-in. Running one loads the skill through `load_skill` and sends the person's text with it.
3. **Summoning a subagent.** `@explore …` or `@review …` starts that subagent on the message through the task layer's normal spawn, started by the person. A command file's `agent:` field uses the same path, and the per-turn agent override is deleted.
4. **Summoning an MCP server.** `@<server>` points one message at that server's tools: its deferred tools are loaded for the turn, and the model is told the person chose them.
5. **MCP prompts as commands.** Each prompt an agent's MCP server offers is `/mcp__<server>__<prompt>`, fetched over the agent's own connection to that server.
6. **The composer.** The `/` menu and the `@` popover offer all of the above, grouped and searchable, and the transcript shows what the person summoned.

The work runs to completion without stopping for review. The human reviews the finished branches.

## Contract

This plan is the contract for the six workstreams. The megachange design documents still govern everything they cover. Decisions D1 to D29 in `acceptance-ledger.md`, the F decisions of `delegation-fork-science-plan.md`, the M decisions of `long-term-memory-plan.md`, the model fleet decisions and the TD decisions of `tool-calling-plan.md` stay in force. When code and this plan disagree, change the code. If the plan turns out wrong, choose the fix that best keeps its intent, update this document in the same change, and record the decision in the ledger.

| Document | Governs |
| --- | --- |
| This plan | The command table, command files, skills as commands, summoning subagents and servers, MCP prompts as commands, and their composer and transcript presentation |
| `docs/design/sdk/summoning-ledger.md` (new) | This plan's decisions and acceptance evidence |
| `plugins-and-composition.md` | Plugins, what a plugin contributes, `plugin check` |
| `orchestration-semantics.md` | The task layer and spawning |
| `tool-calling-plan.md` | Deferred tools, `search_tools`, `loaded_tools` |
| `plugin-change-delivery.md` | Keeping the cached prefix stable |

**Base.** The head of `claude/tool-calling-long-horizon` in clio-agent, gact-tui and clio-agent-marketplace (read at clio-agent `dc77b2ce`, gact-tui pin `b5256395`, marketplace `bd8b0d58`), rebased onto `develop` once that branch merges. Summoning a server depends on its deferred-tool catalog, so this plan does not build on `develop` as it is today. The LaTeX and comments plan also changes the composer (its chat annotations move onto the document anchor model); whichever of the two lands second rebases and keeps both.

Decisions use the prefix `DS`; qualification scenarios use `N`. Neither collides with another plan.

## Rules

The megachange rules carry over unchanged: delete and replace, the task layer is the only execution authority, the model decides, no silent fallback, one mechanism per concept, no fifth store, the repository rules in `AGENTS.md`, and evidence over claims. Five more apply here:

1. **A summons is the person's decision, carried out exactly.** When the person names a command, skill, subagent or server, the runtime does that and nothing else. It never infers a summons from free text: the composer sends a structured part, and the server acts only on structured parts. An `@` in prose that the person did not pick stays prose.
2. **One path per action.** A command or skill becomes the person's message through `submit_user_command`. A summoned subagent starts through the same spawn the `spawn_agent_task` tool uses. A summoned server's tools load the way `search_tools` loads them. An MCP prompt is fetched over the connection the agent's tools already use. Nothing gets a second client, a second spawn path or a second loader.
3. **The session's agent defines what can be named.** The command table, the skills, the subagents and the servers offered to the person are exactly the ones the session's agent resolves to, plus the workspace and user command files. A name the agent does not have is refused with a typed reason. No turn starts and nothing is sent as chat.
4. **Clashes are visible, never silent.** When two sources offer the same command name, the table says so, and the person is offered the qualified names. Nothing is dropped by first-wins without a trace.
5. **The cached prefix stays stable.** Summoning changes the message, never the system text or the tool list mid-conversation, as `plugin-change-delivery.md` requires.

## Out of scope

Do not implement, stub or reserve fields for these:

- Shell commands inside command files (Claude Code's `` !`cmd` `` lines and the `shell`, `exec` and `run` fields). They stay listed as unsupported, as today.
- A command's `model` or `allowed-tools` frontmatter changing the turn's model or tools. They are listed as unsupported fields on the command (below).
- Agents invoking commands. The agent reaches skills with `load_skill` and subagents with the spawn tools; there is no third way.
- `/optimize` and any optimizer surface. That returns with the evolution and optimization item, from the plugin that implements it.
- A command editor in the UI. Commands are files.
- Summoning a peer agent over A2A, ACP or Academy. That arrives with the remote agents push, where a peer is a delegation target and will appear in the `@` popover through the same path.
- Voice summoning.

## Current state

What exists at the base, read on `claude/tool-calling-long-horizon` at `dc77b2ce` and gact-tui `b5256395`, so nothing is rebuilt and nothing is missed:

- **The built-in commands are a Python list.** `BACKEND_COMMANDS` in `gact/runtime/commands.py` holds `/clear`, `/cache-stats`, `/dump-trace`, `/cron` (alias `/schedule`), `/loop`, `/goal` and `/optimize`. Their handlers are an `if cmd_id == ...` chain in `dispatch_command` (`gact/routes/catalog.py`). `/optimize` is listed as `unavailable` and answers with the optimizer's not-implemented stub. The tools those commands drive (`set_goal`, `set_loop`, `cron_create` and the rest) are all claimed by the built-in core plugin (`plugins/builtin/core/ai.iowarp.clio/tools/`), which has no `commands/` folder.
- **Command files.** `_load_command_files_from_disk` in `gact/catalog.py` reads `*.md` from four roots in order (the workspace's `.clio` commands folder, `.claude/commands`, the user config `commands` folder, `~/.claude/commands`) and then from each resolved plugin's `commands/` folder (`resolution_command_roots`). It reads only the top level of each folder, never subfolders. The first file with a given id wins through `setdefault`, so a clash between two plugins, or between a plugin and a workspace file, silently drops one. Plugin files get `${CLAUDE_PLUGIN_ROOT}` and `@file` expansion (`plugins/command_text.py`). `_render_command_prompt` (`gact/routes/catalog_command_args.py`) substitutes `$ARGUMENTS`, `{{input}}`, `{{args}}`, `{{command}}`, `{{agent_id}}` and `{{args.<name>}}`, but not Claude Code's positional `$1`, `$2`.
- **Running a command.** A person's command becomes their message through `submit_user_command` (`gact/user_command_turn.py`), after `UserPromptExpansion` hooks, with the rendered template as the model text and `/name input` as the visible text. A command file's `agent:` field sets `agent_id` on that message, which runs that one turn as a different agent in the same session (`agent_override` in `gact/turn.py`). Nothing else sends a per-turn agent override: gact-tui's message schema has the field but the composer never sets it.
- **Agent-invocable commands.** `dispatch_command` has a second branch for `caller_type == "agent"`. It checks `agent_invocable` and the agent's allowed ids, then runs the target agent synchronously with `run_resolved_agent` in an executor, outside the task layer, and appends a synthetic assistant message. `planner_command_rows` and `agent_allowed_command_ids` serve it, and `_prompt_render_context_for_request` in `gact/app.py` fills `commands.agent_invocable`. No agent tool calls this route, no prompt template uses `commands.agent_invocable`, and no marketplace plugin declares an agent-invocable command. gact-tui shows a "For agents" badge in `settings-prompts.tsx`.
- **Skills as commands are broken in plugin sessions.** `user_command_rows` builds `SkillCatalog(cwd=cwd)` without the session's plugin skills, so only skills in filesystem skill folders are considered, never the agent's plugin skills, and only those whose frontmatter declares `command:`. `command_defs_from_skill` gives each such command `agent_id="main"`. `resolve_session_agent` (`gact/agents/resolution.py`) returns `None` for an id that is neither the session's agent nor one of its subagents, so dispatching a skill command in a plugin session fails with "command agent not found: main". `test_skill_frontmatter_command_is_listed` checks only the listing, without a session. No marketplace skill declares `command:`.
- **Tests.** `tests/test_gact/test_commands.py` covers listing, built-in dispatch, a plugin command running as a turn, a workspace command file, a `.claude` file, the shell-field refusal, a disabled plugin command and the agent-invocable branch, all through `TestClient` with a fake agent. Nothing exercises command files through the composer against a real stack, no marketplace plugin ships a `commands/` folder, and no test covers a clash, a subfolder, a user-level file or a Claude Code plugin's command end to end.
- **Skills for the agent.** `load_skill` (`gact/agents/skill_runtime.py`) reads from the session's `SkillRuntime`, which is built with the plugin skill folders, and resolves bundled files.
- **Spawning.** `prepare_and_spawn` in `gact/spawn_tools.py` calls `app.state.conversations.spawn(caller, task, target, invocation, ...)`. `conversations.spawn` is also called by `flows/dispatch.py`, `gact/agent_fork.py`, `gact/agents/skill_effects.py` and `gact/flow_tools.py` (DS5). The fork route (`gact/routes/session_fork.py`) is the existing case of the person starting work in the task tree.
- **Deferred tools.** `search_tools` returns schemas in its observation and records loaded names in `metadata["loaded_tools"]["pending"]`; they move to `listed` when the prompt is next rebuilt (an applied plugin change), not at a compaction (`gact/search_tools.py`, DS6).
- **MCP prompts.** `GET /v1/mcp/servers/{sid}/prompts` and `POST /v1/mcp/servers/{sid}/prompts/get` in `gact/routes/mcp.py` serve only servers in `app.state.external_mcp_servers`, each through a fresh `fastmcp.Client`, not the agent's plugin-declared servers or their connection. The MCP executor (`tools/mcp_namespace_executor.py`) does not list prompts. `gact/routes/prompts.py` is CLIO's own prompt registry and is unrelated.
- **The composer.** `useSessionCommands` (`web/src/hooks/use-session-commands.ts`) reads `GET /v1/commands` and dispatches through `POST /v1/sessions/{sid}/commands/{cmd}`; unknown commands are not sent as chat (`composer-service-commands.test.tsx`). The `@` popover (`composer-references.tsx`) offers reference groups for workspace files, sources, artifacts, conversations, agents and runs (past runs, `agent_run`), context records, changed files and plans. It has nothing for subagents, servers or skills.

## Design: commands are plugin content

**Built-ins move to core.** The core plugin gains `ai.iowarp.clio/commands/` with one file per built-in: `clear.md`, `cache-stats.md`, `dump-trace.md`, `cron.md` (with `aliases: [schedule]`), `loop.md` and `goal.md`. Each declares `handler: <name>`, naming a handler clio-agent registers, the way `native_tool` claims a built-in tool. The handlers move out of the `if` chain into one registry in `gact/runtime/command_handlers.py`, keyed by handler name, each in the module that owns its feature. `BACKEND_COMMANDS` is deleted. A `handler:` in a plugin that is not built in is rejected by `plugin check`. Phase 0 runs each built-in against the base; one that no longer works is deleted rather than moved.

**`/optimize` is deleted,** with its row, its dispatch branch, its test and the capability entry pointing at it (`gact/runtime/capabilities.py`).

**One table.** `command_table(app, session_id)` builds every row a session can run, from four kinds of source:

| Kind | From | Runs as |
| --- | --- | --- |
| `builtin` | Core plugin command files with `handler:` | Its handler |
| `command` | A resolved plugin's `commands/`, and the workspace and user command files | The rendered file as the person's message |
| `skill` | Every skill the session's agent resolves | `load_skill` plus the person's text (below) |
| `mcp_prompt` | Every prompt the agent's MCP servers offer | The prompt's messages (below) |

Each row carries `kind`, `id`, `qualified_id`, `plugin` (or the file root), `title`, `description`, `argument_hint`, `arguments`, `status`, a typed `disabled_reason` when it cannot run, and `unsupported_fields`. `GET /v1/commands` returns this table for a session; without a session it returns only the workspace and user files. The dispatch route looks the command up in the same table, so listing and running cannot disagree.

**Names and clashes.**

- Every plugin command and skill answers to `/<plugin>:<name>` as well as `/<name>`. MCP prompts are always `/mcp__<server>__<prompt>`.
- Core command names are reserved. `plugin check` rejects a plugin command or skill that uses one. A workspace or user file that uses one is listed as disabled with `command_name_reserved`.
- Among command files, the existing order holds (workspace `.clio`, workspace `.claude`, user `.clio`, user `.claude`). A file that loses is listed as disabled with `command_shadowed`, naming the file that wins.
- When a file command, a plugin command and a skill, or two plugins, share a bare name, the bare name is ambiguous. Running it is refused with `command_ambiguous`, listing the qualified names, and the menu shows only the qualified ones for that name.
- A command and a skill with the same name in one plugin is a `plugin check` error.

**Subfolders.** Command folders are read recursively. A file's name is its stem, and its subfolder path is shown as a label beside it (`frontend` for `commands/frontend/component.md`), as Claude Code does. Two files with the same stem in different subfolders follow the clash rules.

**Arguments.** `$1`, `$2` and so on take the space-separated words of the input, quoted words kept whole, as in Claude Code. `$ARGUMENTS` stays. Declared `arguments` with `required` are checked as today. The CLIO-only `{{...}}` forms are deleted after Phase 0 confirms no command file in the marketplace or clio-kit uses them.

**Unsupported fields.** A command file's `model`, `allowed-tools` and `disable-model-invocation` are listed in the row's `unsupported_fields` and shown on hover in the menu. They do not change the turn.

**Agent-invocable commands are deleted.** That means the `caller_type == "agent"` branch of `dispatch_command`, its audit rows and semantic events, `agent_invocable`, `user_invocable`, `invocation`, `planner_command_rows`, `agent_allowed_command_ids`, `AgentDef.commands`, the `planner` query parameter, the `commands.agent_invocable` render variable and its entry in `prompts.py`, and gact-tui's "For agents" badge and schema fields. The branch ran an agent outside the task layer, and the model already reaches the same things with `load_skill` and the spawn tools.

## Design: skills as commands

**Every skill.** The `skill` rows come from the session's `SkillRuntime`, the same set `load_skill` reads, so plugin skills are included and filesystem-only skill folders follow the same rules as for the agent. A skill whose frontmatter says `user-invocable: false` is left out of the table. The `command:`, `slash_command` and `prompt_template` opt-in keys, `_SKILL_COMMAND_KEYS`, `command_defs_from_skill`, the `SkillCatalog(cwd=cwd)` scan in `user_command_rows` and the `agent_id="main"` routing are deleted.

**Running one.** `/<skill> <text>` runs `load_skill(skill_id)` through the agent's instrumented tool, attributed to the person, and the turn's model text is the person's text followed by the loaded skill exactly as `load_skill` returned it. The skill is recorded as loaded the way a model's `load_skill` call records it, so a later call does not load it twice. The visible message is `/<skill> <text>` with a skill chip. `UserPromptExpansion` hooks see it under `<plugin>:<skill>`, as they see plugin commands.

## Design: summoning a subagent

**The part.** The composer sends `{"type": "summon", "target_kind": "subagent", "target": "<role>"}` beside the text parts. A message may carry several. The server accepts a summon part only for a subagent the session's agent resolves; otherwise the message is refused with `summon_unknown_target`, and no turn starts.

**What happens.** For each subagent summoned, the server starts a child through the same `conversations.spawn` call that `prepare_and_spawn` makes, with the session as parent, the person as the starter, and the message text (with the summon tokens removed) as the task. The children start concurrently. `prepare_and_spawn` is split so its caller resolution is the only part that differs between the tool and the person. The children appear in the Work rail under the message, marked as started by the person. The fork route is the precedent for work in the task tree that the person starts. Phase 0 records how the task layer names a person as the starter.

**The parent's turn.** The person's message is recorded in the session as usual and the session's agent takes a turn. Its model text is the person's text plus one line per child: the person started `<role>` as task `<handle>` on this message. The agent then decides whether to wait for the report, add to it or reply, as it would after spawning the child itself. Ordinary children's reports reach it the same way.

**Permissions.** The person's message is the authorization, so nothing asks. A deny rule on `spawn_agent_task` still refuses, with its reason shown on the message.

**Command files.** A command file's `agent:` field now summons that subagent with the rendered template as its task, through the same path. The per-turn agent override is deleted: `PostMessageRequest.agent_id` and `agent`, `extract_agent_id`, `turn_agent_id` and the `agent_override` metadata in `gact/turn.py` and `gact/turn_finalize.py`, and the `agent_id` field in gact-tui's message schema.

## Design: summoning an MCP server

**The part.** `{"type": "summon", "target_kind": "server", "target": "<server>"}` for a server the session's agent declares, refused with `summon_unknown_target` otherwise.

**What happens.** For a server whose tools are deferred, the server loads all of that server's deferred tools for the turn through the same function `search_tools` uses for a `select:` query. The schemas go into the turn's model text in the form `search_tools` returns them, and the names are added to `metadata["loaded_tools"]` so they join the tool list when the prompt is next rebuilt (DS6). For a server whose tools are already listed, nothing is loaded. In both cases the model text carries one line: the person pointed this message at the `<server>` tools. The model still chooses which tool to call, or none.

**What it does not do.** It does not hide other tools, change the tool list, or force a call.

## Design: MCP prompts as commands

**Listing.** The MCP executor gains a prompt listing for each connected server: `prompts/list` over the existing connection, cached per connection era beside the tool listing, and refreshed on `notifications/prompts/list_changed`. A server that does not declare the prompts capability contributes nothing. Each prompt becomes an `mcp_prompt` row with its description and declared arguments.

**Running.** `/mcp__<server>__<prompt> <args>` maps the space-separated words onto the declared arguments in order. A missing required argument is refused with the argument names. `prompts/get` runs over the agent's connection. The returned messages become the turn's model text: text content as text, an embedded resource as a resource part, an image as an image part. A prompt message with the `assistant` role becomes quoted context in the person's message, since the turn starts from the person. The visible message is the command with its arguments.

**The old routes.** `GET /v1/mcp/servers/{sid}/prompts` and `POST .../prompts/get` and their separate `fastmcp.Client` are deleted. If Phase 0 finds `external_mcp_servers` still in use, those servers are listed through the executor like any other.

## Design: the composer

**`/` menu.** Groups in this order: Commands (built-ins and command files), Skills, and one group per MCP server for its prompts. Each row shows its name, its plugin or source as a label, its argument hint, and status as icons with detail on hover (disabled, shadowed, ambiguous, unsupported fields). Typing filters across groups. An ambiguous bare name offers its qualified names. A refusal from the server keeps the draft and shows the typed reason.

**`@` popover.** Two new groups at the top: Agents (the session's subagents, with their descriptions) and Tools (the session's MCP servers, with their descriptions and tool counts). Picking one inserts a chip, which becomes a summon part. On submit, an `@name` typed in full that exactly matches a subagent or a server in the popover becomes a chip too. Anything else stays text. The existing reference groups are unchanged.

**Transcript.** The person's message shows its chips. A summoned child appears under the message as started by the person, with its task row and report like any child. A loaded skill and loaded server tools appear as collapsed rows on the message ("Loaded skill run-a-study", "Loaded 14 tools from hdf5"). An MCP prompt's command shows the prompt's name and arguments, with the fetched messages on expand.

**Settings.** The prompts settings page drops the agent-invocable badge, and lists commands by kind and source with the same status icons.

## Defaults for the open questions

Take these and record each in the ledger.

| Question | Default |
| --- | --- |
| Where built-ins live | The core plugin's `commands/`, with `handler:` |
| Built-ins kept | `/clear`, `/cache-stats`, `/dump-trace`, `/cron` (`/schedule`), `/loop`, `/goal`, if Phase 0 finds each working |
| Bare-name clash | Refused as `command_ambiguous`; qualified names offered |
| File precedence | Workspace `.clio`, workspace `.claude`, user `.clio`, user `.claude`; losers shown as shadowed |
| Which skills are commands | All the agent resolves, except `user-invocable: false` |
| Parent's turn after a summon | The agent takes a turn and sees the child handles |
| Several summons in one message | All started, concurrently |
| `@server` on listed tools | Note only, nothing loaded |
| Assistant-role MCP prompt messages | Quoted context in the person's message |
| `model`, `allowed-tools` in a command | Listed as unsupported fields |

## Phase 0: inventory and decisions

No production code changes in this phase.

1. Run each built-in command on a freshly restarted stack and record whether it works; mark any that do not for deletion.
2. List every caller of `BACKEND_COMMANDS`, the dispatch `if` chain, the agent-invocable branch and its helpers, `commands.agent_invocable`, `command_defs_from_skill`, `_SKILL_COMMAND_KEYS`, `AgentDef.commands`, the per-turn agent override and the MCP prompt routes, in both repositories, for the deletions.
3. Confirm the skill command failure on the base: a session with a plugin agent, a workspace skill declaring `command:`, and a dispatch that returns "command agent not found: main". Record it.
4. Search the marketplace and clio-kit's Claude Code marketplace for command files and for the `{{...}}` substitutions. Pick one real Claude Code plugin with a `commands/` folder for the scenarios, or record that none exists and use the fixture plugin.
5. Record how the task layer names a person as the starter of a child (the fork route's path) and the split of `prepare_and_spawn`.
6. Find the function `search_tools` uses for `select:` and how `loaded_tools` is written.
7. Record whether `external_mcp_servers` is still a live concept, and how the executor would hold a prompt listing per connection era.
8. Build the fixtures: a marketplace test plugin `commands-fixture` with two commands (one in a subfolder, one using `$1` and `${CLAUDE_PLUGIN_ROOT}` with an `@` include), a skill, and a subagent; a second test plugin whose command clashes with it by name; a workspace `.clio` command and a `.claude` command of the same name; a user-level command; and a small MCP server with two prompts (one with a required argument, one returning a resource and an image) and a deferred tool set.
9. Write `docs/design/sdk/summoning-ledger.md` with the megachange ledger's columns, decisions DS1 onward and the scenarios below. Commit, push, open draft PRs, and continue.

## Phase 1: the command table

1. The core plugin's `commands/` files, the `handler:` field, the handler registry, and `plugin check` for `handler:` and reserved names.
2. Delete `BACKEND_COMMANDS`, the `if` chain and `/optimize`.
3. `command_table` with the four kinds, qualified names, clash states, recursive folders, positional arguments and unsupported fields; `GET /v1/commands` and dispatch read it.
4. Delete the agent-invocable path and everything listed with it.
5. Tests for each clash rule, subfolders, `$1` substitution, reserved names and the deletions.

## Phase 2: skills and summoning

1. Skill rows from the `SkillRuntime`, `user-invocable: false`, running through `load_skill` attributed to the person; delete the opt-in path and `agent_id="main"`.
2. The `summon` part in clio-agent's message model and gact-tui's schema and contract (`contract/SPEC.md`), with `summon_unknown_target`.
3. Person-started spawns through the split `prepare_and_spawn`, the parent's turn line, and command files' `agent:` on the same path; delete the per-turn override.
4. Server summons through the `search_tools` loader, `loaded_tools`, and the turn line.

## Phase 3: MCP prompts

1. The executor's prompt listing, its cache and `list_changed` handling.
2. `mcp_prompt` rows, argument mapping, `prompts/get` over the agent's connection, and the message conversion.
3. Delete the old prompt routes and their client.

## Phase 4: the composer

1. The `/` menu groups, labels, status icons and qualified-name offers.
2. The Agents and Tools groups in the `@` popover, chips, and exact-match conversion on submit.
3. The transcript presentation of chips, person-started children, loaded skills and tools, and MCP prompt commands.
4. The settings page changes.

Verify each UI change as the megachange plan describes: restart the stack, run `node scripts/cloud/ui_check.mjs` with a `--steps` script at 1440 by 900 and 390 by 844, read every screenshot, and check that `consoleErrors` and `failedRequests` are empty.

## Phase 5: qualification

Run focused tests and `ruff` on touched paths while working. Then run these against a freshly restarted stack (`claude_code` provider), driving the real composer, and record each in the ledger with commands, handles and the screenshots read.

| ID | Scenario | Passes when |
| --- | --- | --- |
| N1 | Type `/` in a new conversation of the standard agent | The menu shows the core built-ins, the agent's skills and the fixture MCP server's prompts in their groups; no `/optimize` |
| N2 | `/goal`, `/loop`, `/cron` and `/clear` from the menu | Each behaves as on the base, now served from the core plugin's files |
| N3 | The fixture plugin's subfolder command with two arguments | It is listed with its subfolder label; `$1` and `$2` are substituted; the `@` include and `${CLAUDE_PLUGIN_ROOT}` are expanded in the model text |
| N4 | The workspace `.clio` and `.claude` commands of the same name | The `.clio` one runs; the `.claude` one is listed as shadowed, naming the winner |
| N5 | The user-level command | It is listed and runs in any workspace |
| N6 | The two fixture plugins' clashing command | The bare name is refused as ambiguous with both qualified names; each qualified name runs its own plugin's file |
| N7 | A workspace file named `goal.md` | It is listed as disabled with `command_name_reserved`; `/goal` still runs the built-in |
| N8 | The real Claude Code plugin's command from Phase 0 (or the fixture plugin) | It lists and runs as a turn, and a `UserPromptExpansion` hook that denies it stops it with the hook's reason |
| N9 | `/run-a-study <question>` with the science-log plugin | The skill is loaded through `load_skill` attributed to the person, the agent works from it, and a later `load_skill` of the same skill is not repeated |
| N10 | A skill marked `user-invocable: false` | It is absent from the menu and still loadable by the agent |
| N11 | `@explore where is the spill folder written?` | An `explore` child starts, marked as started by the person, on the text without the token; the parent's turn sees the handle and reports the child's answer |
| N12 | `@explore` and `@review` in one message | Both children start concurrently; both appear under the message |
| N13 | A message with a summon part naming a subagent the agent lacks (sent through the API) | Refused with `summon_unknown_target`; no turn starts |
| N14 | `@<fixture server> …` with that server deferred | Its tools are loaded for the turn, shown as a collapsed row; the model calls one of them; no `prefix_mismatch` reset; after the next prompt rebuild they are in the tool list (DS6) |
| N15 | `/mcp__<fixture>__<prompt>` without its required argument, then with it | The first is refused with the argument's name; the second runs, and the resource and image parts reach the model |
| N16 | The fixture server adds a prompt mid-session | It appears in the `/` menu without a restart |
| N17 | A command file with `agent: review` | It starts a `review` child through the same path as N11; no per-turn override appears in the turn's metadata |
| N18 | "Email bob@example.org about @explore" typed as plain text without picking from the popover, in a workspace with an `explore` subagent | `@example.org` stays text; `@explore`, an exact match, becomes a chip on submit |
| N19 | The `/` menu and `@` popover at 390 by 844 | Both fit, groups collapse, chips wrap |
| N20 | Search both repositories for `BACKEND_COMMANDS`, `agent_invocable`, `planner_command_rows`, `command_defs_from_skill`, `agent_override`, `/optimize` and the old prompt routes | Nothing remains outside the ledger and this plan |

The cloud session cannot qualify the Tauri desktop or Windows; those rows are recorded as local phase. Nothing else may be blocked. A failing scenario is fixed, not deferred.

## Done

1. Every phase is implemented, and `BACKEND_COMMANDS`, `/optimize`, the agent-invocable path, the skill command opt-in, the `main` routing, the per-turn agent override and the old MCP prompt routes are gone.
2. Focused tests pass, and `ruff check` and `ruff format --check` are clean on touched paths.
3. Every cloud-phase scenario passes against a freshly restarted stack, each shown with commands, handles and the screenshots read; local-phase rows say so.
4. The ledger holds every decision with its evidence.
5. The site's user guide has a page on commands and summoning: where command files go, the clash rules, skills as commands, `@` for agents and servers, and MCP prompts.
6. The branches are pushed in clio-agent, gact-tui and clio-agent-marketplace, the submodule pins point at the pushed commits, and the draft PRs carry a summary and the evidence.

## Working conventions

- Use the branch the cloud session is set up to push to, with the same name in clio-agent, gact-tui and clio-agent-marketplace, and write it in the ledger header. Open draft PRs against `develop` (this work: `claude/direct-summoning`, with draft PRs against `claude/tool-calling-long-horizon` until that branch merges).
- Small Conventional Commits per step, with test evidence in each PR description.
- Keep runtime state in `.cloud-run/`. Stop the stack when you finish a work block.
