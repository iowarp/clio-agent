# Hierarchical User-Defined Experts

Tracking issue: https://github.com/iowarp/clio-agent/issues/325

## Purpose

CLIO should treat its expert hierarchy as a configurable intelligence graph, not
as a hardcoded list of built-in science modules. The default CLIO science
hierarchy should still ship out of the box, but users should be able to point
CLIO at a domain-specific expert pack and get a different, deeply nested,
tool-scoped, model-aware assistant without changing Python code.

The target system supports:

- Human-editable expert definitions.
- User-defined tier-2, tier-3, tier-4, and deeper experts.
- Recursive expert delegation.
- Per-expert tool and MCP access.
- Per-expert provider/model selection.
- Tier-aware provider/model fallback with visible warnings.
- Prompt profile references such as `heavy`, `light`, and `fine_tuned`.
- Clear TUI/Doctor/log diagnostics when definitions are invalid or fallbacks
  are used.

This document is intentionally architectural. It defines the end state and the
interfaces that future implementation work should converge on. It does not
implement the feature.

## Current State

The existing system already has several important pieces:

- GACT exposes `/v1/agents` as the canonical agent catalog.
- `AgentDef` already includes `id`, `source`, `title`, `description`,
  `system_prompt`, `default_provider`, `default_model`, `parameters`, `tools`,
  `tier`, `specialization`, and `keywords`.
- CLIO has built-in experts such as `main`, `data`, `analysis`,
  `visualization`, `ndp_catalog`, and `sac_format`.
- CLIO has a registry with `AgentCapability.parent_id`, which already points
  toward nested experts.
- User agents can be persisted today, but the current primary store is JSON,
  which is not a good human authoring format.
- Dynamic prompt-only and tool-backed user agents already exist in partial form.
- Tool ownership and visibility are currently driven by a static catalog.
- Expert handoffs and routing decisions already have visible GACT parts and
  benchmark metadata.

The missing pieces are:

- A first-class human-editable expert-pack format.
- A recursive loader and validator.
- A generic delegation mechanism that every expert can use.
- Full provider/model policy per expert and per tier.
- Runtime merge semantics across global, workspace, and session scope.
- Clear TUI/Doctor surfacing of effective hierarchy, validation errors, and
  model fallback warnings.

## Design Principles

1. Human-editable first.
   Expert definitions should be easy to read, diff, review, and copy between
   projects. JSON should not be the primary user-authored format.

2. Existing GACT surfaces remain canonical.
   `/v1/agents` remains the catalog. Routing decisions, expert handoffs, tool
   results, and session metadata remain the observable runtime surface.

3. The built-in hierarchy is just the default pack.
   CLIO's current science behavior should be expressible in the same model used
   by user-defined packs.

4. Hierarchy is semantic, not positional.
   Folder structure is for human organization. Parent-child relationships come
   from `parent_id`.

5. Delegation must be observable.
   If an expert delegates to another expert, the user and the TUI should see
   what happened, which model ran, which tools were available, and whether any
   fallback occurred.

6. Strict validation beats silent partial loading.
   Broken expert files should be disabled individually and shown clearly. They
   should never load halfway and affect routing unpredictably.

7. Tool access is explicit.
   Experts get only their declared tools, declared MCP servers, and the internal
   delegation capability for their allowed children.

8. Model fallback is allowed, but never quiet.
   Falling back from an expert-specific model to a tier default or global model
   is acceptable only when the TUI/logs make it obvious.

## Expert Pack Format

Expert definitions should be Markdown files with YAML frontmatter. The
frontmatter is the machine-readable definition. The Markdown body is the default
system prompt.

Example:

```md
---
id: ndp_catalog
title: NDP Catalog Expert
description: Finds, ranks, and stages bounded datasets from the National Data Platform.
parent_id: data
tier: 3
specialization: ndp_catalog
keywords:
  - ndp
  - dataset catalog
  - osdf
  - public data
tools:
  - ndp_list_organizations
  - ndp_search_datasets
  - ndp_get_dataset_details
  - ndp_stage_resource
mcp_servers:
  - clio-kit-ndp
model:
  provider: openai_compatible
  id: ndp-catalog-finetune
  api_base_env: NDP_LLAMACPP_BASE_URL
prompt_profile: heavy
prompt_profiles:
  heavy:
    prompt_path: prompts/ndp_catalog.heavy.md
    model:
      provider: openai_compatible
      id: ndp-catalog-finetune
  light:
    prompt_path: prompts/ndp_catalog.light.md
enabled: true
order: 20
---

You are the NDP catalog expert.

Find candidate datasets, prefer bounded downloadable resources, explain why a
resource was selected, and do not stage very large resources unless the user
explicitly asks for them.
```

### Required Fields

- `id`: stable expert id. Lowercase snake_case is recommended.
- `title`: human-facing display name.
- `description`: one or two sentence capability summary.
- `tier`: integer. `1` is the root orchestrator, `2` is a primary specialist,
  `3+` are deeper specialists or workers.

### Optional Fields

- `parent_id`: parent expert id. Missing means root-level.
- `specialization`: short domain tag for color, grouping, and route hints.
- `keywords`: routing and search hints.
- `tools`: explicit tool allowlist.
- `mcp_servers`: optional MCP/server connections this expert may use.
- `skills`: explicit skill ids or skill-pack references available to this
  expert.
- `commands`: explicit slash/action command ids this expert may invoke or expose
  when targeted.
- `model`: expert-preferred provider/model policy.
- `prompt_profile`: selected default profile key.
- `prompt_profiles`: named prompt/model profiles.
- `enabled`: defaults to true.
- `order`: stable display ordering within a parent.
- `metadata`: open-ended backend-specific metadata.

### Folder Semantics

Expert pack roots are scanned recursively for `*.md` files. Folder names do not
define hierarchy. This means users can organize packs however they want:

```text
biology-clio/
  sequencing/
    genome_qc.md
    variant_annotation.md
  imaging/
    microscopy_qc.md
  prompts/
    genome_qc.heavy.md
```

The only hierarchy CLIO uses is `parent_id`.

## Pack Activation And Precedence

Expert packs can be active at three scopes:

1. Global: default user-wide CLIO expert packs.
2. Workspace: project/domain-specific packs.
3. Session: rare per-session overrides or experiments.

Merge order is:

```text
global -> workspace -> session
```

Later scopes win. If the same `id` exists in multiple scopes, the later
definition replaces the earlier definition. A later definition can also disable
an inherited expert:

```md
---
id: visualization
enabled: false
---
```

The loader should preserve provenance for each final expert:

- `pack_id`
- `pack_scope`: `builtin`, `global`, `workspace`, or `session`
- `definition_path`
- `overrides`: previous definitions replaced by this one

The default built-in CLIO hierarchy is treated as an implicit `builtin` pack at
the lowest precedence.

## Validation Rules

Validation is strict per file. Invalid files are disabled individually and
reported through Doctor/TUI/logs. One invalid file should not disable the whole
pack unless the user explicitly asks for fail-fast pack validation.

Validation should detect:

- Invalid frontmatter syntax.
- Missing required fields.
- Duplicate ids after merge resolution.
- Unknown parent ids.
- Cycles in `parent_id`.
- Tier/parent mismatches, such as a tier-4 expert with a tier-1 parent unless
  explicitly allowed by policy.
- Invalid provider/model definitions.
- Missing prompt profile references.
- Missing prompt files.
- Unknown tool ids.
- Tools not available because a required MCP server is not mounted.
- Invalid MCP server references.
- Unsafe tool declarations without permission metadata.

Validation output should include:

- Expert id, if parseable.
- File path.
- Line and column when available.
- Error code.
- Human-readable message.
- Whether the expert was disabled.

Invalid definitions must never partly load into routing.

## Runtime Hierarchy

The runtime hierarchy is a tree built from effective expert definitions.

Example:

```text
main
  data
    ndp_catalog
    hdf5_format
    adios_format
  analysis
    parquet_statistics
    sac_format
  visualization
    charts
    seismic_plots
```

The architecture should allow any depth:

```text
main -> biology -> genomics -> variant_annotation -> clinvar_lookup
```

The system should not hardcode "tier 3 is the deepest level." `tier` is display
and policy metadata. `parent_id` defines the actual hierarchy.

## Delegation Semantics

Recursive delegation is the core behavior.

Each expert receives a prompt/context section listing its children and what each
child can do. The expert can answer directly, call one of its allowed tools, or
delegate to one of its children.

Delegation should be represented internally as a tool-like action:

```text
delegate_to_expert(
  expert_id: "ndp_catalog",
  task: "Find a bounded seismic waveform dataset and stage the selected resource",
  constraints: {...}
)
```

The delegation action should:

- Check that `expert_id` is a child or otherwise allowed delegate.
- Create a child execution trace.
- Use the target expert's prompt, tools, model policy, and children.
- Return a structured result to the parent.
- Emit GACT-visible handoff metadata.

The TUI and logs should be able to render:

```text
main -> data -> ndp_catalog -> sac_format -> visualization
```

Delegation should produce `expert_handoff` parts or equivalent structured
metadata with:

- `agent_id`
- `parent_id`
- `dispatch_target`
- `stage`
- `status`
- `input_summary`
- `output_summary`
- `duration_ms`
- `model_effective`
- `model_requested`
- `model_fallback_reason`
- `tools_allowed`
- `tools_called`

## Routing Modes

The existing session routing modes should continue to apply:

- `auto`: normal routing/delegation.
- `chat`: force conversational answer path.
- `experts`: require expert/tool/delegation work before final answer.
- `reasoning_only`: prefer planner reasoning over deterministic shortcuts.

For user-defined experts, the system should support both:

- Automatic routing by keywords, hierarchy, and planner judgment.
- Explicit targeting through TUI picker or slash/command flow.

Explicit targeting should bypass top-level route ambiguity but should not bypass
tool permissions or model policy.

## Tool And MCP Access

Tool access is allowlist-based.

An expert may only use:

- Tools listed in its `tools`.
- Tools exposed by declared `mcp_servers`, after server capability resolution.
- Skills listed in its `skills` or inherited through an explicit skill pack.
- Commands listed in its `commands` when command auto-use is enabled.
- Internal safe tools CLIO grants automatically, such as `delegate_to_expert`
  for allowed children.

Children do not automatically inherit parent tools. If inheritance is needed
later, it should be an explicit policy field, not the default.

The same rule should apply to skills and commands. An agent should not inherit a
global command/skill surface just because it exists in the workspace. Per-agent
skills make the hierarchy more predictable: the NDP expert can know NDP review
recipes, the code expert can know review/refactor recipes, and the root
orchestrator can decide when to delegate instead of exposing every recipe to
every model call.

Tool visibility should be included in the planner context for each expert. The
planner should not see one global scientific tool pool; it should see scoped
capabilities per expert.

## Provider And Model Policy

Each expert can declare a preferred provider/model. Tiers can also declare
defaults. The session/global provider remains the final fallback.

Fallback order:

1. Prompt profile model, if selected.
2. Expert model.
3. Tier default model.
4. Session/global model.

Fallbacks are allowed, but must be visible.

Examples:

- Root orchestrator uses a large cloud model.
- Tier-2 science experts use strong open-source models.
- Tier-3 narrow experts use fine-tuned local or llama.cpp-served models.
- If a fine-tuned model is unavailable, CLIO falls back to the tier default and
  emits a warning.

Fallback metadata should include:

- Requested provider/model.
- Effective provider/model.
- Fallback step used.
- Fallback reason.
- Whether user action is needed.

TUI expectations:

- Red or warning-styled marker when fallback occurs.
- Doctor view lists missing per-expert models.
- Route/handoff detail shows requested vs effective model.
- Logs contain enough detail to debug provider-specific behavior.

## Prompt Profiles

Experts may declare named prompt profiles. This allows prompt/model pairings
without putting the entire prompt system into this document.

Example:

```yaml
prompt_profile: heavy
prompt_profiles:
  heavy:
    prompt_path: prompts/analysis.heavy.md
    model:
      provider: anthropic
      id: claude-opus-4.1
  light:
    prompt_path: prompts/analysis.light.md
    model:
      provider: openai_compatible
      id: qwen3-32b
  fine_tuned:
    prompt_path: prompts/analysis.ft.md
    model:
      provider: llama_cpp
      id: analysis-ft-q4
```

This architecture only defines how experts refer to prompt profiles and how a
selected profile contributes to model selection. The full prompt-system design
should separately define:

- Prompt file editing.
- TUI prompt editor.
- Permanent save semantics.
- Prompt validation.
- Profile selection UX.
- Prompt tuning and benchmarking.
- Heavy/light defaults by model size.

## GACT And API Surface

`/v1/agents` remains canonical.

`AgentDef` should be extended with optional fields rather than replaced:

```json
{
  "id": "ndp_catalog",
  "source": "user",
  "title": "NDP Catalog Expert",
  "description": "...",
  "system_prompt": "...",
  "default_provider": "openai_compatible",
  "default_model": "ndp-catalog-finetune",
  "parameters": {},
  "tools": ["ndp_search_datasets"],
  "skills": ["ndp_dataset_review"],
  "commands": ["ndp-check"],
  "tier": 3,
  "specialization": "ndp_catalog",
  "keywords": ["ndp", "dataset"],

  "parent_id": "data",
  "prompt_path": "/path/to/biology-clio/ndp_catalog.md",
  "pack_id": "biology-clio",
  "pack_scope": "workspace",
  "enabled": true,
  "model_policy": {},
  "prompt_profiles": {},
  "validation_status": "valid",
  "validation_errors": []
}
```

Suggested endpoint behavior:

- `GET /v1/agents`: returns enabled effective agents.
- `GET /v1/agents?include_disabled=true`: includes disabled/invalid agents for
  admin/TUI diagnostics.
- `GET /v1/agents/{id}`: returns effective definition and provenance.
- `POST /v1/agents`: can create a new user expert file in the active pack root,
  once the editing workflow is implemented.
- `PUT /v1/agents/{id}`: can update the user-owned definition, never built-ins.
- `DELETE /v1/agents/{id}`: disables or removes a user-owned definition, never
  built-ins.

The write endpoints should eventually write Markdown, not JSON.

## TUI Requirements

The first TUI work should focus on inspect, edit, and diagnose:

- Browse hierarchy as a tree.
- Inspect effective prompt, prompt path, tools, MCP servers, provider/model,
  prompt profile, parent, children, and pack provenance.
- Show disabled/invalid experts with validation errors.
- Show routing/delegation paths in transcript detail.
- Show model fallback warnings prominently.
- Support reload of expert definitions.
- Support add/remove/edit eventually by writing the Markdown definition file.

Configurable sidebar/module layout is not part of this design. It should be a
separate document. Expert definitions may carry simple display metadata like
`order`, but they should not define the whole TUI layout system.

## Migration Strategy

1. Document the built-in CLIO hierarchy as an equivalent built-in expert pack.
2. Add loader support for Markdown expert definitions.
3. Extend `AgentDef` with optional hierarchy/provenance/validation/model fields.
4. Merge built-in, global, workspace, and session definitions.
5. Expose effective hierarchy through `/v1/agents`.
6. Add validation diagnostics to Doctor/TUI/logs.
7. Add internal `delegate_to_expert` action.
8. Convert hardcoded nested expert paths, such as NDP and SAC, to use the same
   delegation path.
9. Add per-expert/tier model policy and visible fallback metadata.
10. Add TUI browse/edit/reload flows.

## Acceptance Criteria

- A user can create a Markdown expert file and see it in `/v1/agents`.
- A user-defined tier-2 expert can be selected automatically by routing.
- A user can explicitly target a user-defined expert.
- A parent expert can delegate to a child expert through `delegate_to_expert`.
- Delegation works beyond tier 2.
- The transcript records and displays the delegation path.
- Tool allowlists are enforced for user-defined experts.
- Skill and command allowlists are enforced for user-defined experts.
- MCP/server references resolve into available tools or validation errors.
- Per-expert model/provider selection works.
- Tier fallback works and is visibly reported.
- Invalid expert files are disabled and surfaced, not silently partially loaded.
- The existing built-in CLIO behavior remains available as the default pack.

## Open Questions For Later Design Docs

- Exact prompt editing/versioning UX belongs to the prompt-system document.
- Optional NDP integration should decide whether NDP ships as a default disabled
  expert pack, an installable pack, or an integration provider.
- Memory refinement should decide what expert traces become durable ARC memory.
- User-defined slash commands should decide whether commands can target experts,
  prompt profiles, sessions, or arbitrary agent actions.
- User-defined slash commands and skills should also decide how command/skill
  definitions attach to specific agents, whether by explicit ids, skill packs,
  inheritance, or capability tags.
- Configurable TUI module/sidebar layout deserves a separate architecture.
