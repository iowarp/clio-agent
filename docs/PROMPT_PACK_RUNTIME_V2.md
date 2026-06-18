# Prompt Pack Runtime V2

Tracks GitHub issue #367.

## Goal

Move CLIO's built-in prompts into external Markdown/frontmatter files and make
prompt rendering dynamic enough for runtime catalogs. The orchestrator prompt
must be editable as a file while still receiving the current accessible experts,
tools, commands, memory policy, permission policy, and provider/model context.

## Prompt Sources

Prompt resolution order:

```text
packaged builtin -> global -> workspace -> session
```

Later scopes override earlier scopes by `id` + `profile`.

Built-in prompts should live as packaged resources, not DSPy docstrings. User
and workspace prompts use the same parser and registry path as built-ins.

## Prompt File Shape

```md
---
id: clio.main.planner
title: Main planner
profile: heavy
provider: ""
model: ""
requires:
  - agents.catalog
  - tools.catalog
  - commands.agent_invocable
  - memory.policy
  - permissions.policy
schema: planner_action
---
You are the CLIO orchestrator.

Available experts:
{{ agents.available_tree }}

Available tools:
{{ tools.available }}

Agent-invocable commands:
{{ commands.agent_invocable }}

Memory policy:
{{ memory.policy_summary }}
```

## Dynamic Rendering

Prompt files may use only approved placeholders or named renderers. They do not
execute arbitrary Python, Jinja, shell, or user code.

Supported v1 placeholders:

- `{{ agents.available_tree }}`
- `{{ agents.available_flat }}`
- `{{ tools.available }}`
- `{{ commands.agent_invocable }}`
- `{{ memory.policy_summary }}`
- `{{ permissions.policy_summary }}`
- `{{ provider.current }}`
- `{{ session.active_pack }}`

Unknown placeholders fail validation or produce an explicit diagnostic. They
must not render as empty text silently.

The prompt registry does not discover experts itself. It receives a normalized
runtime catalog from the agent/pack registry for the active session.

## APIs

Add or complete:

- `GET /v1/prompts`
- `GET /v1/prompts/{prompt_id}`
- `PUT /v1/prompts/{prompt_id}`
- `POST /v1/prompts/{prompt_id}/validate`
- `POST /v1/prompts/{prompt_id}/render`
- `POST /v1/prompts/reload`

Rendered prompt responses include:

- prompt id/profile
- source scope/path/checksum
- selected fallback profile if any
- render placeholders used
- catalog/context versions used
- validation diagnostics

## Acceptance Criteria

- Primary built-in prompt bodies are loaded from packaged Markdown files.
- A workspace or session override replaces a built-in prompt profile.
- Orchestrator prompt rendering includes the current accessible expert tree.
- Rendered prompts can include tools, agent-invocable commands, memory policy,
  permission policy, active pack, and provider/model context.
- Unknown placeholders fail validation or surface explicit diagnostics.
- Runtime execution records prompt provenance and render-context provenance.
- Tests cover built-in loading, override precedence, dynamic placeholders, and
  expert-pack prompt references.

