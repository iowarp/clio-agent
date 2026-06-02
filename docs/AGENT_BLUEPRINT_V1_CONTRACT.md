# Agent Blueprint v1 Contract

Agent Blueprints are CLIO's portable Agent definition format. A Blueprint
folder is rooted at `AGENT.md`; expert definitions live under `experts/*.md`.

## Manifest

`AGENT.md` should declare:

```yaml
---
id: data-semantics
version: 1.0.0
title: Data Semantics Agent
description: Domain summary.
root_expert: main
blueprint:
  format: agent-blueprint-v1
defaults:
  prompt_profile: default
requires: {}
---
```

For `agent-blueprint-v1`, `id`, `version`, `title`, and `root_expert` are
required. Legacy aliases such as `root`, `default_expert`, and
`default_root_expert` remain accepted in compatibility mode, but validators
warn and pack authors should prefer `root_expert`.

If `blueprint.format` is omitted, CLIO treats the pack as compatibility-mode:
it can still load, but validation reports a warning so marketplace authors can
migrate intentionally.

## Experts

Each expert markdown file should declare:

```yaml
---
id: analysis
title: Analysis Expert
description: What this expert owns.
tier: 2
parent_id: main
tools:
  - parquet_analyze_schema
skills:
  - domain.review_rubric
commands:
  - /prepare-handoff
prompt_id: clio.expert.analysis
prompt_profile: heavy
provider: openai
model: gpt-5.1
---
Expert prompt body.
```

For `agent-blueprint-v1`, `title` and `tier` are required for every expert.
Experts with `tier > 1` must declare `parent_id`. Every expert must provide a
prompt body or `prompt_id`. `description` is currently recommended and reported
as a warning when missing.

Tool references are validated against CLIO built-ins, memory tools, and declared
MCP descriptor tools. MCP-backed tools remain disabled until the descriptor is
explicitly enabled and trusted. Unknown tools are validation errors.

`skills:` are runtime-loaded instruction bodies. CLIO resolves declared skill ids
in this order:

1. pack-local `skills/` inside the active Agent Blueprint;
2. workspace-local `.clio/skills`, `.claude/skills`, `.codex/skills`, and
   `.agents/skills`;
3. global `~/.config/clio-agent/skills`, `~/.claude/skills`, `~/.codex/skills`,
   and `~/.agents/skills`.

Resolved skill bodies are appended to the expert runtime prompt and recorded in
turn provenance under `skill_resolution` and `resolved_skills`. Missing declared
skills produce runtime validation warnings instead of being silently ignored.

## MCP Descriptors

MCP descriptors live under `tools/*.md`. They are disabled by default and must
declare transport-specific connection data:

```yaml
---
id: calculator
transport: stdio
command: uvx
args:
  - clio-calculator-mcp
tools:
  - calculator_add
---
```

`stdio` descriptors require `command`; `http` and `streamable-http` descriptors
require `url`. Installation, trust, verification, and container isolation
semantics are tracked by #513.

## Diagnostics

`POST /v1/agent-blueprints/validate` returns:

- `validation_errors`: release-blocking problems that disable the Blueprint or
  expert.
- `validation_warnings`: compatibility or partial-semantics notes that should
  be shown to authors but do not necessarily disable the pack.

Diagnostics include path-backed expert rows and MCP descriptor rows so UI and
benchmark reports can point authors to the broken field.
