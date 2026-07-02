# User-Defined Slash Commands Design

Tracking issue: https://github.com/iowarp/clio-agent/issues/330

Related docs:

- [Hierarchical user-defined experts](HIERARCHICAL_EXPERTS.md)
- [External editable prompt system](PROMPT_SYSTEM.md)
- [Command and capability truth](COMMAND_CAPABILITY_TRUTH_DESIGN.md)
- [Permission surfacing](PERMISSION_SURFACING_DESIGN.md)

## Purpose

Add user-defined `/commands` that trigger CLIO agent actions.

This is a feature, not command cleanup. The command/capability truth issue
should make visible commands honest; this design adds a new user-extension
surface where commands are real invocation recipes.

The design should align with current Codex and Claude-style capabilities:

- Slash commands are discoverable palette/session actions.
- Custom commands can come from files.
- Richer command definitions behave like skills or recipes, not just text
  snippets.
- Commands may be user-invocable, agent-invocable, or both.

CLIO should use its existing skill/user-agent runtime rather than creating a
parallel command-only runtime.

## Current State

Relevant existing pieces:

- GACT defines `/v1/commands` and `POST /v1/sessions/{sid}/commands/{cmd}`.
- The TUI already renders a slash-command palette from backend commands,
  local commands, and plugin commands.
- gact plugins already support local binary execution from slash commands.
- CLIO already exposes `AgentDef` through `/v1/agents`.
- CLIO already has user agents persisted in `~/.config/clio-agent/agents.json`.
- CLIO already discovers skill files from:
  - `~/.claude/skills`
  - `~/.codex/skills`
  - `~/.agents/skills`
  - `.claude/skills`
  - `.codex/skills`
  - `.agents/skills`
- Prompt-only and tool-backed dynamic agents already execute through DSPy.

Implemented backend pieces:

- Skill files can surface slash-command metadata.
- `/v1/commands` returns backend built-ins, user-agent commands, skill
  commands, workspace/global command files, compatible Claude command files,
  and active Agent Blueprint packaged commands.
- `POST /v1/sessions/{sid}/commands/{cmd}` dispatches prompt-backed
  user-defined commands through the dynamic-agent path.
- Command argument validation and template substitution are implemented for
  declared arguments and `$ARGUMENTS`/`{{args.*}}` placeholders.
- Agent-invocable commands are gated by command metadata and per-agent/expert
  command allowlists.
- CLIO-native command roots exist at workspace/global scope, and Agent
  Blueprints may package session-scoped commands under `commands/*.md`.

Remaining pieces:

- MCP prompt exposure as slash commands is still future work.
- TUI affordances should keep improving around provenance, argument entry,
  disabled command rendering, and agent-auto-use settings.
- Real-provider marketplace benchmark evidence should include at least one
  packaged command invocation from an Agent Blueprint session.

## Core Model

User-defined slash commands are invocable skills/recipes.

They should compile into one internal model with two outward views:

1. `AgentDef` view for `/v1/agents`.
2. `Command` view for `/v1/commands`.

This avoids separate definitions for "skill" and "slash command" when they
represent the same reusable agent action.

The command is the invocation handle. The skill/agent is the runtime behavior.

Examples:

- `/review-pr 123` invokes a code-review skill with argument `123`.
- `/summarize-log errors.log` invokes a prompt-only skill over a user argument.
- `/hdf5-check /data/run.h5` invokes a tool-scoped CLIO skill with allowed HDF5
  tools.
- `/ndp-search climate reanalysis` targets an NDP/domain expert when that
  integration is enabled.

## Discovery Roots

Support both CLIO-native and compatibility roots.

Global native roots:

- `~/.config/clio-agent/skills/**/SKILL.md`
- `~/.config/clio-agent/commands/*.md`

Workspace native roots:

- `.clio/skills/**/SKILL.md`
- `.clio/commands/*.md`

Compatibility roots:

- `~/.claude/skills/**/SKILL.md`
- `~/.claude/commands/*.md`
- `~/.codex/skills/**/SKILL.md`
- `.claude/skills/**/SKILL.md`
- `.claude/commands/*.md`
- `.codex/skills/**/SKILL.md`
- existing `.agents/skills/**/SKILL.md`

Precedence:

1. Workspace native.
2. Workspace compatibility.
3. User native.
4. User compatibility.
5. Built-in package definitions.

Within the same precedence level, duplicate command IDs are validation errors.
Across levels, higher precedence overrides lower precedence and provenance must
show the shadowed source.

Reserved command IDs:

- Built-in TUI commands such as `/clear`, `/help`, `/mcp`, `/tools`,
  `/permissions`, `/diff`, `/compact`, `/doctor`.
- Backend built-ins such as `/cache-stats`, `/dump-trace`, `/optimize`.

Reserved IDs cannot be shadowed unless a future override policy explicitly
allows it.

## File Format

Use Markdown with YAML-style frontmatter. The parser can start with the existing
bounded frontmatter parser and grow only as needed.

Example:

```markdown
---
name: review-pr
title: Review PR
description: Review a GitHub pull request and summarize risks.
argument-hint: "<number-or-url>"
user-invocable: true
agent-invocable: true
context: inline
agent: code_reviewer
prompt-profile: heavy
allowed-tools:
  - github_fetch_pr
  - github_fetch_pr_patch
status: available
---

Review this pull request:

$ARGUMENTS

Focus on correctness, missing tests, regressions, and risky assumptions.
```

Command ID is derived as:

- `slash_id` if present,
- otherwise `name`,
- otherwise filename/stem.

The exposed slash ID must start with `/`. If the file says `name: review-pr`,
the runtime exposes `/review-pr`.

## Metadata

Recommended metadata fields:

| Field | Meaning | Default |
|---|---|---|
| `name` | Stable command/skill id without leading slash. | File or directory name. |
| `slash_id` | Explicit slash command id. | `"/" + name`. |
| `title` | Palette display title. | `name`. |
| `description` | Palette/help description. | First non-empty body line. |
| `argument-hint` | Human hint shown in palette/detail. | Empty. |
| `arguments` | Structured argument definitions. | Empty positional args. |
| `user-invocable` | User can run from palette/slash. | `true`. |
| `agent-invocable` | Agent may invoke if global setting allows. | `true`. |
| `context` | `inline` or `fork`. | `inline`. |
| `agent` | Target agent/expert id. | This skill's own agent id. |
| `agents` | Agents/experts that may see or invoke this command. | Empty = user palette only unless targeted. |
| `skill-pack` | Optional package/group id for attaching commands to agents. | Empty. |
| `prompt-profile` | Prompt profile to request. | Session/default profile. |
| `allowed-tools` | Tool allowlist for tool-backed execution. | Empty/prompt-only. |
| `model` | Preferred model id. | Agent/session default. |
| `provider` | Preferred provider id. | Agent/session default. |
| `status` | `available`, `todo`, `unsupported`, `unavailable`. | `available`. |
| `disabled_reason` | User-facing reason for disabled command. | Derived from status. |

Claude-compatible field spellings such as `allowed_tools`, `allowed-tools`,
`disable-model-invocation`, and `user-invocable` should be accepted where they
map cleanly to CLIO semantics.

## Per-Agent Skills And Commands

Commands and skills should not only be global palette entries. They should also
be attachable to agents/experts.

An agent definition may declare:

- explicit command ids,
- explicit skill ids,
- skill-pack ids,
- capability/tag selectors.

The command registry should expose the effective command/skill surface per
agent, after applying scope precedence, validation, disabled states, and policy.

This gives CLIO a clean way to say:

- the code-review agent can invoke review/refactor commands;
- the NDP expert can invoke NDP dataset review recipes;
- the visualization expert can invoke plotting recipes;
- the root orchestrator can see broad delegation handles but not every
  low-level tool recipe unless explicitly configured.

Agent-scoped commands should still respect global user settings for
agent-invocable commands. Manual user invocation can remain available even when
agent auto-use is disabled.

## Argument Handling

Support raw and structured arguments.

Raw substitutions:

- `$ARGUMENTS`: entire user argument string after command id.
- `$ARGUMENTS[N]`: zero-based tokenized argument.
- `$0`, `$1`, `$2`: positional shorthand.

Named arguments:

- If `arguments` is declared, parse positional tokens into named values.
- Required missing arguments produce a structured command error.
- Extra arguments are preserved in `$ARGUMENTS` unless the command declares
  `strict_arguments: true`.

Environment-like substitutions:

- `${CLIO_SESSION_ID}`
- `${CLIO_WORKSPACE_ID}`
- `${CLIO_COMMAND_ID}`
- `${CLIO_COMMAND_DIR}`

No shell expansion is performed.

## Invocation Modes

### User Invocation

Users invoke commands through:

- TUI slash palette.
- Typed slash command.
- Future CLI command dispatch.

If `user-invocable=false`, the command may still be visible in diagnostics but
must not appear as a runnable user command.

### Agent Invocation

Agents may invoke commands only when all conditions are true:

1. command metadata has `agent-invocable=true`;
2. the global/TUI setting `allow_agent_user_commands=true`;
3. the command is `status=available`;
4. required tools and target agent are available;
5. permission policy allows any side effects.

The TUI must expose a setting to disable agent auto-use of user commands while
leaving manual user invocation available.

Default setting:

- `allow_agent_user_commands=true` for parity with agentic skill systems, but it
  must be visible and easy to disable.

If this default feels too permissive during implementation, the safer fallback
is `false` with a visible opt-in. The design requirement is that the setting
exists and gates agent auto-use.

## Execution Context

Default context is `inline`.

`inline`:

- Runs in the current session.
- Appends normal user/assistant messages and command-result metadata.
- Uses the current session's model/provider unless the command requests a
  prompt profile or model override.

`fork`:

- Runs in a child/subsession when session branching/subagents are available.
- Parent session receives a visible handoff/result summary.
- If the backend cannot fork, command is disabled or falls back only when the
  command declares `fallback_context: inline`.

## Runtime Dispatch

`GET /v1/commands` should return:

- built-in backend commands,
- MCP prompts when wired,
- user/skill command views,
- TODO/disabled command rows with status metadata.

`POST /v1/sessions/{sid}/commands/{cmd}` should:

1. Resolve the command by slash id.
2. Reject disabled/TODO/unavailable commands without running.
3. Validate session and command invocation permission.
4. Parse arguments.
5. Render the command body with safe substitutions.
6. Resolve target agent/skill/prompt profile.
7. Execute through the existing dynamic-agent path.
8. Materialize visible transcript output and publish SSE events.

Command output should not be only an HTTP response because the TUI currently
relies on transcript/SSE refresh for visible command results.

## Safety Model

No direct shell in v1.

User-defined commands may steer CLIO agents/tools, but they must not run local
shell snippets or arbitrary local binaries. Existing gact plugins remain the
local-binary extension point.

Tool use from commands must go through:

- tool allowlists,
- existing MCP/tool execution path,
- permission policy,
- audit rows for destructive operations.

Commands must not bypass plan/architect read-only mode.

## TUI Requirements

The TUI should:

- show user commands in the palette with source/provenance;
- show argument hints;
- allow typing arguments after the selected command;
- render disabled/TODO commands without dispatching;
- expose the global setting for agent auto-use;
- show command provenance/details in a detail modal or command row subtitle;
- display command errors as recoverable hints or structured error messages, not
  fatal UI state.

Command examples:

- `/review-pr 123`
- `/summarize-log logs/error.txt`
- `/hdf5-check /data/run.h5`

## Relationship To Other Designs

Hierarchical experts:

- Commands may target expert ids.
- Expert hierarchy owns routing/delegation semantics.
- This design owns explicit slash invocation.

Prompt system:

- Commands may request prompt profiles.
- Prompt registry owns profile resolution and provenance.
- This design should pass profile ids through without duplicating prompt logic.

Command/capability truth:

- TODO commands must remain visible but disabled.
- Command status metadata should be shared.
- Drift tests should include user-defined commands.

Permissions:

- Destructive command tool actions must be permission-gated and auditable.

Plugins:

- Plugins remain separate because they execute local binaries.
- User commands steer CLIO's agent runtime.

## Implementation Steps

1. Define an internal command recipe model derived from `AgentDef` plus command
   metadata.
2. Extend skill discovery roots and add native `.clio` roots.
3. Parse command metadata and validate reserved IDs.
4. Expose command recipe views through `/v1/commands`.
5. Dispatch command recipes through the existing dynamic-agent execution path.
6. Add argument substitution and validation.
7. Add agent auto-use gating setting to backend/TUI configuration.
8. Update TUI palette rendering for user command provenance, args, and disabled
   rows.
9. Add tests for discovery, list, dispatch, arguments, disabled commands,
   permissions, and TUI behavior.

## Acceptance Criteria

- A workspace `.clio/commands/review.md` file appears as `/review` in
  `/v1/commands` and the TUI palette.
- A compatible `.claude/commands/foo.md` file appears as `/foo`.
- A `SKILL.md` command can be shown both as an agent and as a slash command
  when metadata allows it.
- User invocation runs inline by default and creates visible transcript output.
- Commands can target a user/built-in expert.
- Required arguments are validated before execution.
- `status=todo` commands remain visible but disabled.
- Agent auto-use is gated by command metadata and the global/TUI setting.
- Tool-backed commands use permission/audit policy.
- No command file can execute local shell directly in v1.
