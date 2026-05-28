# Expert Pack Runtime V2

Tracks GitHub issue #366.

## Goal

Make CLIO expert hierarchies a first-class runtime artifact. A pack should be a
shareable folder that defines agents, hierarchy, prompts, tools, skills,
commands, and model policy without requiring Python edits.

The existing expert Markdown loader is foundation only. V2 adds pack manifests,
activation, scope-aware catalog resolution, and provenance.

## Runtime Model

Expert packs are resolved in this order:

```text
builtin -> global -> workspace -> session
```

Later scopes override earlier scopes by expert id. Global packs are available in
workspaces by default. Workspace packs are visible only in that workspace.
Session overrides apply only to that session.

Each resolved expert must expose:

- `id`, `title`, `description`, `source`, `scope`, `enabled`
- `pack_id`, `pack_version`, `definition_path`
- `parent_id`, `tier`, `keywords`, `specialization`
- `prompt_id`, `prompt_profile`, or inline prompt body
- `tools`, `skills`, `commands`, `capability_refs`
- `default_provider`, `default_model`, `parameters`
- `validation_errors`, `override_chain`

## Pack Layout

Minimum pack:

```text
my-pack/
  clio-pack.yaml
  experts/
    orchestrator.md
    data.md
    data/ndp_catalog.md
  prompts/
    orchestrator.heavy.md
    data.light.md
  commands/
    summarize_dataset.md
```

`clio-pack.yaml` defines pack identity and defaults:

```yaml
id: data-semantics
version: 0.1.0
title: Data Semantics
description: Data discovery and interpretation experts.
default_root_expert: orchestrator
compatibility:
  clio_agent: ">=0.2"
defaults:
  prompt_profile: heavy
  provider: ""
  model: ""
```

Expert Markdown frontmatter may declare:

```yaml
id: ndp_catalog
parent_id: data
tier: 3
prompt_id: data.ndp_catalog
prompt_profile: heavy
tools: [ndp.search]
skills: [catalog_reasoning]
commands: [summarize-dataset]
provider: openai
model: gpt-5.1
parameters:
  temperature: 0.2
```

## APIs

Add or complete:

- `GET /v1/expert-packs`
- `GET /v1/expert-packs/{pack_id}`
- `POST /v1/expert-packs/validate`
- `POST /v1/sessions/{sid}/expert-pack`
- `GET /v1/sessions/{sid}/expert-pack`
- `GET /v1/agents` includes resolved pack/scope/provenance fields

Session activation records the selected pack id/version and any session
overrides in session metadata. It does not mutate global or workspace packs.

## Validation

Invalid files are disabled and surfaced. They are not silently skipped.

Validation must detect:

- missing ids
- duplicate ids after merge
- missing parents
- hierarchy cycles
- invalid prompt references
- invalid tools/skills/commands references
- invalid provider/model declarations
- malformed pack manifests

## Acceptance Criteria

- A pack with nested experts loads from disk and appears in `/v1/agents`.
- A session can activate one pack while another session uses a different pack.
- Global packs are visible from a workspace; workspace packs do not leak to
  other workspaces.
- An expert can declare prompt, tools, skills, commands, and model policy.
- Delegation metadata records expert id, parent id, pack id/version,
  provider/model, fallback warnings, status, and duration.
- Invalid pack files remain visible as disabled catalog rows with diagnostics.
- Tests cover builtin/global/workspace/session precedence and session pack
  activation.

