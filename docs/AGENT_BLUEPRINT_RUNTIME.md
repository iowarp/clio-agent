# Agent Blueprint Runtime

Tracks GitHub issues #382, #383, #384, and #385.

## Goal

Make CLIO agents shareable, editable, and swappable as file-backed Agent
Blueprints. An Agent Blueprint is a Markdown-defined source package that
instantiates a complete Agent in a session: expert hierarchy, prompts, tools,
skills, commands, model/provider defaults, and runtime metadata.

This supersedes the narrower "expert pack" framing. The old loader remains a
compatibility path, but the canonical model is:

- **Expert**: one agentic loop instance tuned for a task through prompts,
  tools, skills, commands, and optional model/provider defaults.
- **Agent**: a hierarchy of experts combined into one unified intelligence for
  a domain or goal.
- **Agent Blueprint**: the file-backed definition of an Agent.
- **Session**: an instantiated Agent from an Agent Blueprint, plus session-local
  overlays and memory.

The current built-in Data Exploration/Search Agent is one Agent composed of
Data, Analysis, Visualization, NDP, SAC/format, chat, and utility experts. It
should be represented as a built-in Agent Blueprint.

## Blueprint Layout

Marketplace repositories contain one or more top-level Blueprint folders:

```text
clio-agent-marketplace/
  data-exploration/
    AGENT.md
    experts/
      root.md
      data.md
      analysis.md
      visualization.md
      ndp.md
    prompts/
      root.heavy.md
      data.default.md
    profiles/
      heavy.md
      light.md
      small-model.md
    skills/
      dataset-review/SKILL.md
    commands/
      summarize-dataset.md
    tools/
      earthscope-mcp.md
  genomics/
    AGENT.md
    experts/
    prompts/
```

`AGENT.md` is the canonical root definition. It is Markdown with frontmatter;
the body is human-facing documentation.

```md
---
id: data-exploration
version: 0.3.0
title: Data Exploration/Search Agent
description: Scientific data discovery, inspection, analysis, and visualization.
root_expert: root
compatibility:
  clio_agent: ">=0.3"
defaults:
  prompt_profile: heavy
  provider: ""
  model: ""
requires:
  tools:
    - hdf5_analyze_file
    - parquet_analyze_schema
  memory_tools:
    - memory_search_sessions
blueprint:
  format: agent-blueprint-v1
---

Human-readable overview of the agent, expected data domains, and intended use.
```

The existing `clio-pack.yaml` format remains readable as legacy Expert Pack V2
input, but new marketplace content should use `AGENT.md`.

## Expert Files

Each Expert is a Markdown file under `experts/`. The frontmatter defines the
runtime contract; the body may provide the Expert's system prompt unless
`prompt_id` is used.

```md
---
id: data
title: Data Expert
parent_id: root
tier: 2
loop: react
prompt_id: clio.expert.data
prompt_profile: heavy
tools:
  - hdf5_analyze_file
  - hdf5_list_datasets
  - memory_search_sessions
skills:
  - dataset-review
commands:
  - summarize-dataset
provider: ""
model: ""
parameters:
  max_iters: 5
---

Optional prompt body for this Expert.
```

Required validation:

- every enabled Blueprint has one root Expert matching `root_expert`;
- every non-root Expert has a valid `parent_id`;
- hierarchy cycles are rejected;
- duplicate Expert ids are rejected within the active Agent;
- prompt, tool, skill, command, model, and provider references are validated;
- invalid Experts stay visible as disabled rows with diagnostics.

## Agent Instantiation

A session has exactly one active Agent Blueprint. Activating a Blueprint
instantiates the whole Agent for that session. It is not an overlay on the
default CLIO Agent; it replaces the active session Agent.

The default CLIO Data Exploration/Search Agent remains available by being
packaged as the built-in Agent Blueprint.

Session activation records:

- Blueprint id, version, title, source scope, and installed path;
- source Git/local origin, ref, commit, and checksum when available;
- activation time and selected root Expert;
- any session overlay id/checksum.

Runtime catalog resolution for an active session:

```text
installed Agent Blueprint snapshot -> session overlay
```

Global and workspace installations control which Blueprints are available.
They do not merge into the active Agent unless the user activates one.

## Expert Communication

Experts communicate through explicit callable boundaries, not informal prompt
convention. A parent Expert can call declared child Experts through generated
internal tools. Those generated tools enforce the hierarchy edge and record
provenance.

Example internal tool shape:

```text
expert.data(question, context_refs) -> ExpertResult
expert.analysis(question, context_refs) -> ExpertResult
```

The runtime may implement this using the existing expert delegation machinery,
but the callable edge must come from the active Agent Blueprint graph.

Memory access is also tool-based. If an Expert should search or read other
sessions, it must declare the memory tools it may use:

- `memory_search_sessions`
- `memory_read_session_summary`
- `memory_read_context_frame`

CLIO continues to enforce memory policy. A Blueprint can grant an Expert access
to the memory tool, but it cannot bypass session/workspace/global policy.

## Prompts And Profiles

Behavior-bearing runtime/system prompts must live in Markdown files, not Python
strings. DSPy signatures/classes, adapters, validators, and field descriptions
may remain in Python.

Move these into prompt/profile files:

- built-in CLIO default Agent prompts;
- profile policy text such as `heavy`, `light`, `small_model`, `fine_tuned`,
  and `debug`;
- dynamic-agent wrapper instructions such as prompt-only and tool-using agent
  behavior;
- alignment requirements currently appended in Python.

Prompt files keep dynamic placeholders for session catalogs:

- `{{ agents.available_tree }}`
- `{{ agents.available_flat }}`
- `{{ tools.available }}`
- `{{ commands.agent_invocable }}`
- `{{ memory.policy_summary }}`
- `{{ permissions.policy_summary }}`
- `{{ provider.current }}`
- `{{ session.active_pack }}`

`session.active_pack` should be retained for compatibility and mirrored by a
new Agent Blueprint naming key in the render context.

## Tools And MCP Descriptors

Blueprint Experts may reference:

- built-in CLIO tools;
- memory tools;
- generated child-Expert tools;
- installed MCP tools;
- agent-invocable slash commands;
- skills.

Blueprints may package MCP descriptors under `tools/` as Markdown/frontmatter.
Installing a Blueprint records descriptors but does not enable them
automatically. User or workspace policy must explicitly enable MCP servers
before their tools become callable.

Enabling a descriptor through
`POST /v1/agent-blueprints/{blueprint_id}/mcp/{descriptor_id}/enable` probes the
declared MCP server by default. A successful probe stores the server under the
stable id `agent_blueprint_mcp_{blueprint_id}_{descriptor_id}`, records the live
tool schemas, marks tools `enabled=true`, and makes them callable through
`POST /v1/mcp/servers/{server_id}/call` subject to the normal permission policy.
Clients may pass `{"probe": false}` to stage a descriptor as
`enabled_pending_probe` without making its tools callable.

Validation distinguishes:

- missing tool;
- declared but disabled MCP descriptor;
- unsupported descriptor shape;
- permission-blocked tool;
- tool available but not visible to the Expert.

Blueprints do not ship arbitrary executable tool code as automatically runnable
content in this phase.

## Installation And Updates

Supported install sources:

- local path;
- Git URL/ref.

Install behavior:

- copy a pinned snapshot into global or workspace CLIO storage;
- record source URL/path, ref, resolved commit when available, install time,
  checksum, and CLIO compatibility;
- do not auto-update installed Blueprints;
- explicit update creates a new snapshot and preserves provenance.

Scopes:

- global Blueprints are available to all workspaces;
- workspace Blueprints are available only in that workspace;
- session overlays apply only to one session.

## Session Overlays

TUI edits to prompts, profiles, model/provider defaults, hierarchy metadata, or
capability references create a session overlay by default.

Session overlays must not mutate installed global/workspace Blueprints. The
user can explicitly save or fork a session overlay into a workspace/global
Blueprint revision later.

APIs should expose both effective values and provenance:

- value from installed Blueprint;
- value from session overlay;
- validation diagnostics for the effective value.

## APIs

Add first-class Agent Blueprint APIs:

- `GET /v1/agent-blueprints`
- `GET /v1/agent-blueprints/{blueprint_id}`
- `POST /v1/agent-blueprints/validate`
- `POST /v1/agent-blueprints/install`
- `POST /v1/agent-blueprints/{blueprint_id}/update`
- `DELETE /v1/agent-blueprints/{blueprint_id}`
- `GET /v1/sessions/{sid}/agent-blueprint`
- `POST /v1/sessions/{sid}/agent-blueprint`
- `GET /v1/sessions/{sid}/agent-overlay`
- `PUT /v1/sessions/{sid}/agent-overlay`
- `POST /v1/sessions/{sid}/agent-overlay/export`

Keep existing `/v1/expert-packs` routes as compatibility aliases during
migration.

Session overlays are drafts over the active Agent Blueprint. `PUT` validates
the edited graph, prompt references, provider ids, and declared tools before it
persists the overlay. `POST /agent-overlay/export` is the explicit save/fork
operation: it materializes the effective session overlay as a new workspace or
global Agent Blueprint without mutating the installed source Blueprint.

Catalog responses should make the active Agent explicit:

- `/v1/agents` returns only the active session Agent graph when `session_id` is
  supplied;
- `/v1/prompts` includes active Blueprint and overlay prompt sources;
- `/v1/tools` includes MCP descriptor status and Expert visibility;
- turn metadata records active Blueprint, overlay, Expert, prompt, profile,
  provider/model, and generated child-Expert tool provenance.

## Acceptance Criteria

- A marketplace repo with multiple top-level `AGENT.md` folders can be
  discovered, validated, and installed.
- Installed Blueprints are pinned and reproducible.
- A session can swap from the built-in Data Exploration/Search Agent to a
  different Agent Blueprint.
- Built-in CLIO default behavior is represented as an Agent Blueprint and still
  works.
- Child Experts are callable only through declared graph edges.
- Memory tools work for Experts that declare them and continue to enforce
  CLIO's memory policy.
- MCP descriptors install disabled by default; explicit enablement probes the
  server and makes successfully discovered tools visible/callable.
- Session overlay edits do not affect other sessions or installed Blueprints.
- Prompt provenance points to Markdown files/checksums.
- Tests prevent behavior-bearing runtime/system prompt strings from returning
  to Python outside allowed schema/field-description surfaces.
