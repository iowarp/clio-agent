# Agent-Invocable Commands V2

Tracks GitHub issue #368.

## Goal

Promote eligible user-defined slash commands from user-only shortcuts into
model-visible capabilities that agents can call when policy permits.

The existing custom command loader and user dispatch path stay valid. V2 adds
planner visibility, caller policy, allowlists, and audit provenance.

## Command Model

Command files remain Markdown/frontmatter. Required fields for agent-invocable
commands:

```yaml
id: summarize-dataset
title: Summarize dataset
user_invocable: true
agent_invocable: true
target_agent: data
args:
  path:
    type: string
    required: true
permission:
  mode: ask
```

Scope resolution:

```text
builtin -> global -> workspace -> session
```

Global commands are available by default. Workspace commands do not leak to
other workspaces. Session commands apply only to that session.

## Planner Visibility

The orchestrator sees only commands that are:

- enabled and valid
- `agent_invocable: true`
- allowed by active agent/expert command allowlist
- allowed by session/workspace policy
- not shell/process commands

User-only commands stay visible in `/v1/commands` for the TUI but are not model
capabilities.

## Invocation Path

Use one shared command execution path for user-triggered and agent-triggered
commands where possible. The caller differs:

- user caller: explicit slash command
- agent caller: planner/tool action

Every invocation records:

- command id and scope
- caller type, agent id, expert id if present
- target agent/expert
- args after validation
- permission decision
- status, result, error
- source file/provenance

## APIs

Add or complete:

- `/v1/commands` exposes user/agent invocability separately
- planner capability construction includes allowed commands
- `POST /v1/sessions/{sid}/commands/{command_id}` supports caller provenance
- internal orchestrator action/tool for command invocation

## Acceptance Criteria

- `agent_invocable: true` command appears in planner capabilities when allowed.
- User-only command never appears in planner capabilities.
- Expert pack command allowlists restrict what an expert may call.
- Workspace command is not visible in another workspace.
- Invalid or disabled commands are visible with a reason and cannot run.
- Agent-triggered invocation produces audit/provenance metadata.
- Tests cover allowed call, denied call, invalid args, allowlists, and audit.

