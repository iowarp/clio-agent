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
install:
  method: uvx
  package: clio-calculator-mcp
runtime:
  args:
    - serve
tools:
  - calculator_add
trust:
  policy: explicit
env_policy:
  secrets: none
verification:
  probe: list_tools
---
```

`stdio` descriptors require `command`; `http` and `streamable-http` descriptors
require `url`. For self-contained marketplace descriptors, `stdio` launch data
may also be derived from:

- `install.method: uvx` plus `install.package`;
- `install.method: npx` plus `install.package`;
- `install.method: binary` plus `install.package` or `install.binary`;
- `install.method: pack-local` plus `install.path`, launched with `python`.

Descriptor enablement records `install`, `runtime`, `env_policy`,
`verification`, and `trust` metadata in the MCP server registry and returned
wire payload. `trust.policy: explicit` is the default. For local development,
`CLIO_GACT_MCP_TRUST_ALWAYS=true` permits enablement without a per-request trust
flag; setting it to `false` requires the caller to pass explicit trust during
enablement. Non-trusting/containerized MCP execution remains future hardening,
but untrusted descriptors no longer silently enable.

## Diagnostics

`POST /v1/agent-blueprints/validate` returns:

- `validation_errors`: release-blocking problems that disable the Blueprint or
  expert.
- `validation_warnings`: compatibility or partial-semantics notes that should
  be shown to authors but do not necessarily disable the pack.

Diagnostics include path-backed expert rows and MCP descriptor rows so UI and
benchmark reports can point authors to the broken field.
