# External Editable Prompt System

Tracking issue: https://github.com/iowarp/clio-agent/issues/326
Related: [Hierarchical user-defined experts](HIERARCHICAL_EXPERTS.md)

## Purpose

CLIO needs a first-class prompt system. Prompts should not be scattered across
Python docstrings, hardcoded helper strings, dynamic user-agent records, compact
memory builders, provider adapters, and documentation examples. Users should be
able to inspect prompts, edit them, save them permanently, swap prompt profiles
for different model sizes, and understand exactly which prompt was used for a
turn.

The prompt system should support:

- External human-editable prompt files.
- Built-in default prompts that can be overridden without patching CLIO.
- TUI prompt browsing, editing, validation, and permanent save.
- Named profiles such as `heavy`, `light`, `small_model`, and `fine_tuned`.
- Profile-specific provider/model preferences.
- Prompt provenance in the TUI, Doctor, logs, and runtime metadata.
- Integration with hierarchical experts without making the expert system own
  prompt editing semantics.

This document defines the desired architecture. It does not implement it.

## Relationship To Hierarchical Experts

The hierarchical expert architecture allows each expert to reference prompt
profiles. This prompt system owns what those profiles mean.

The split should be:

- Expert definitions say which prompt id/profile an expert wants.
- The prompt registry resolves that id/profile into text, model policy,
  validation state, and provenance.
- The TUI prompt editor edits prompt files and profiles.
- The expert system consumes the resolved prompt and model policy.

This keeps expert hierarchy, prompt editing, and prompt tuning separable while
still allowing model-prompt pairings such as:

- Large cloud orchestrator with a long `heavy` routing prompt.
- Tier-2 open-source expert with a medium `light` prompt.
- Tier-3 fine-tuned local model with a narrow `fine_tuned` prompt.

## Current State

Prompt-like behavior is currently spread across several areas:

- DSPy signatures in `src/clio_agent/signatures/`.
- Built-in GACT agent prompt strings in `src/clio_agent/gact/app.py`.
- Dynamic user-agent `system_prompt` fields.
- Compact-memory summarization prompt construction.
- Provider-specific answer-synthesis instructions.
- Chat, planner, answer synthesis, data, analysis, visualization, NDP, SAC, and
  nanoagent behavior embedded in Python code.
- Benchmark/demo prompts in docs and tests.

This creates several problems:

- Users cannot see the active prompt from the TUI.
- Editing prompts requires editing source code or JSON.
- There is no consistent prompt provenance.
- Heavy prompts cannot cleanly pair with large models while light prompts pair
  with smaller local models.
- Prompt validation is ad hoc.
- Built-in and user prompts do not share one override model.

## Design Principles

1. Prompts are runtime assets.
   A prompt is a named, versioned, inspectable asset, not just a Python string.

2. Markdown is the human authoring format.
   Prompt files should be readable, editable, diffable, and easy to organize.

3. Structure lives in frontmatter.
   YAML frontmatter holds machine-readable metadata. Markdown body holds prompt
   text.

4. Built-ins are defaults, not special cases.
   Shipped prompts should load through the same registry as user prompts, with
   lower precedence.

5. Prompt-critical schemas stay protected.
   Some prompts require strict JSON or DSPy output formats. Externalization must
   preserve these guardrails.

6. Prompt profiles are first-class.
   `heavy`, `light`, `small_model`, and `fine_tuned` should be selectable,
   inspectable, and tied to model policy when needed.

7. Every runtime prompt has provenance.
   The system should be able to say: prompt id, profile, scope, file path,
   checksum/version, model policy, and fallback state.

8. TUI edits are durable and explicit.
   Editing a built-in prompt should duplicate it into a user or workspace scope
   before saving. CLIO should not mutate packaged defaults.

## Prompt File Format

Prompt definitions should be Markdown files with YAML frontmatter.

Example:

```md
---
id: planner.action
title: Planner Action Router
kind: planner
description: Chooses the next action over available experts and tools.
profile: heavy
profiles:
  heavy:
    body_path: planner.action.heavy.md
    model:
      provider: anthropic
      id: claude-opus-4.1
    constraints:
      min_context_tokens: 32000
  light:
    body_path: planner.action.light.md
    model:
      provider: openai_compatible
      id: qwen3-32b
    constraints:
      min_context_tokens: 8192
schema:
  output: agent_action_json
  strict_json: true
owner:
  component: planner
  expert_id: main
enabled: true
---

You are CLIO's action planner.

Choose exactly one next action from the available capabilities. Return only the
JSON object required by the schema.
```

### Required Fields

- `id`: stable prompt id, such as `planner.action`, `expert.data.system`, or
  `memory.compaction`.
- `title`: human-facing name.
- `kind`: prompt category. Suggested values:
  - `planner`
  - `answer`
  - `chat`
  - `expert`
  - `memory`
  - `tool`
  - `provider`
  - `nanoagent`
  - `user_agent`
- Markdown body or profile body path.

### Optional Fields

- `description`: short explanation.
- `profile`: default selected profile.
- `profiles`: named prompt profiles.
- `model`: default provider/model policy.
- `schema`: output constraints and validation hints.
- `owner`: component, expert id, or feature that consumes the prompt.
- `variables`: declared template variables.
- `enabled`: defaults to true.
- `version`: user-visible version tag.
- `metadata`: open-ended extension field.

## Prompt Profiles

A prompt may define multiple named profiles. Profiles let the same prompt role
adapt to model size, provider behavior, latency/cost constraints, or fine-tuned
models.

Recommended baseline profile names:

- `heavy`: full instructions, examples, stronger guardrails, large model.
- `light`: compact instructions, minimal examples, smaller model.
- `small_model`: highly explicit, short, low ambiguity, local/open model.
- `fine_tuned`: narrow instructions assuming the model was trained for that
  role.
- `debug`: verbose diagnostic behavior for development and benchmark runs.

Profiles can define:

- Inline body text or `body_path`.
- Provider/model preference.
- Temperature/token overrides.
- Context minimums.
- Required tool/expert availability.
- Schema strictness.
- Notes for TUI display.

Profile selection order:

1. Explicit session/profile override.
2. Expert-selected profile.
3. Workspace prompt profile default.
4. Prompt file default `profile`.
5. Model-size heuristic.
6. Built-in default profile.

If a selected profile cannot be used, CLIO should fallback clearly and record
why.

## Prompt Resolution

Prompt resolution should follow the same general shape as expert-pack
resolution:

```text
builtin -> global -> workspace -> session
```

Later scopes override earlier scopes.

The prompt registry should return an effective prompt object:

```json
{
  "id": "planner.action",
  "kind": "planner",
  "profile": "heavy",
  "text": "...",
  "scope": "workspace",
  "path": "/workspace/.clio/prompts/planner.action.md",
  "checksum": "sha256:...",
  "version": "2026-05-26",
  "model_policy": {
    "provider": "anthropic",
    "id": "claude-opus-4.1"
  },
  "schema": {
    "output": "agent_action_json",
    "strict_json": true
  },
  "validation_status": "valid",
  "validation_errors": []
}
```

Runtime code should ask the registry for prompts by stable id and requested
profile. It should not read prompt files directly.

## Prompt Variables

Prompts should support declared variables, but variable rendering must be
conservative.

Example:

```yaml
variables:
  question:
    required: true
    type: string
  capabilities:
    required: true
    type: markdown
  observations:
    required: false
    type: json
```

The registry should validate that required variables are provided before a
prompt is rendered. Prompt rendering should preserve clear boundaries for large
dynamic content such as capabilities, observations, file context, and memory.

The implementation should avoid unstructured string concatenation where prompt
variables need schema or escaping guarantees.

## Schema And Guardrails

Some prompts are structurally dangerous to edit casually because their outputs
must be parseable. The planner action prompt is the clearest example.

Prompt definitions should be able to declare output constraints:

```yaml
schema:
  output: agent_action_json
  strict_json: true
  allowed_actions:
    - tool
    - expert
    - answer
    - none
```

For these prompts:

- The TUI should warn before saving invalid edits.
- Validation should check required schema hints are retained.
- Runtime should still enforce parser validation.
- Broken overrides should disable the override and fall back to the previous
  valid prompt, with a visible warning.

Externalizing prompts must not weaken runtime safety.

## TUI Editing Model

The TUI should support prompt inspection and editing without making users know
where every file lives.

Core flows:

- Browse prompt catalog.
- Filter by kind, owner, expert, profile, scope, or validation status.
- Open prompt detail.
- See effective prompt text, source path, scope, profile, model policy, schema,
  and checksum/version.
- Edit a user/workspace/session prompt.
- Duplicate a built-in prompt into user/workspace/session scope before editing.
- Validate before save.
- Save permanently.
- Reload active prompts.
- Compare active override against built-in base.
- Revert an override.

Save semantics:

- Built-in prompts are read-only.
- Editing a built-in creates an override file.
- Workspace edits save under the workspace prompt root.
- Session edits save under session prompt overrides or a session-scoped prompt
  pack.
- Saves should be atomic.
- Failed validation should block activation unless the user explicitly saves as
  disabled/draft.

Prompt editing should use the existing TUI modal/editor patterns where possible,
but prompt browsing/editing deserves its own command surface once implemented.

## API And GACT Surface

The prompt system should expose enough API for the TUI and CLI to inspect,
edit, and reload prompts.

Suggested endpoints:

```text
GET  /v1/prompts
GET  /v1/prompts/{id}
GET  /v1/prompts/{id}/profiles
POST /v1/prompts/{id}/render
PUT  /v1/prompts/{id}
POST /v1/prompts/{id}/duplicate
POST /v1/prompts/reload
POST /v1/prompts/validate
```

Capabilities should advertise prompt editing separately from prompt inspection:

```json
{
  "prompts": true,
  "prompt_write": true,
  "prompt_profiles": true
}
```

`/v1/agents` should reference prompt ids/profiles, but should not embed the
whole prompt system.

Runtime messages and routing metadata should include prompt provenance where it
helps debugging:

- Prompt id.
- Profile.
- Source scope/path.
- Prompt checksum/version.
- Model policy requested by the prompt profile.
- Effective provider/model used.

## Built-In Prompt Inventory

The first implementation pass should inventory and assign stable prompt ids to
all prompt-bearing behavior. Suggested initial ids:

- `planner.action`
- `planner.answer`
- `chat.answer`
- `expert.data.system`
- `expert.analysis.system`
- `expert.visualization.system`
- `expert.ndp_catalog.system`
- `expert.sac_format.system`
- `memory.compaction`
- `provider.answer_synthesis`
- `nanoagent.worker`
- `user_agent.prompt_only`
- `user_agent.tool_react`

The inventory should distinguish:

- User-facing prompts.
- Internal planner prompts.
- Schema-critical prompts.
- Provider-specific prompts.
- Demo/benchmark prompts, which should not become runtime defaults unless
  explicitly promoted.

## Validation Rules

Prompt validation should catch:

- Invalid frontmatter.
- Missing `id`, `title`, `kind`, or body/profile.
- Duplicate prompt ids after scope merge.
- Profile references that do not exist.
- Missing profile body files.
- Invalid model/provider references.
- Missing required variables.
- Unknown schema ids.
- Schema-critical prompts missing strict guardrails.
- Prompt files with unsupported encodings.

Invalid prompt overrides should not silently take effect.

For built-in fallbacks:

- If a user override is invalid, disable the override and use the previous valid
  prompt.
- Surface the fallback in Doctor/TUI/logs.
- Keep the invalid file available for editing.

## Model And Prompt Pairing

Prompt profiles can carry model policy. The model resolver should merge policy
from:

1. Prompt profile.
2. Expert definition.
3. Tier default.
4. Session/global model.

The prompt system does not decide all model routing by itself, but it can
provide model preferences and constraints. The final model resolver should
record which layer supplied the effective model.

This is important for:

- Long orchestrator prompts on large cloud models.
- Smaller prompts for local open-source models.
- Fine-tuned tier-3+ expert models.
- llama.cpp/OpenAI-compatible routing setups where model ids imply local model
  semantics.

Any fallback from prompt-profile model to another model must be visible.

## Memory And Prompt Versioning

ARC and logs should record prompt provenance for completed turns:

- Prompt id.
- Profile.
- Version/checksum.
- Effective model.
- Fallback state.

This allows later analysis:

- Which prompts worked for which tasks.
- Which prompt profiles perform better on which model sizes.
- Whether a fine-tuned model actually improved a narrow expert.
- Whether prompt edits caused regressions.

This document does not define the full prompt optimizer. It only requires that
prompt provenance be available to future tuning and memory systems.

## Migration Strategy

1. Inventory all runtime prompts and assign stable ids.
2. Create built-in prompt files matching current behavior.
3. Add a prompt registry that loads built-in prompts.
4. Route existing runtime code through the registry without behavior changes.
5. Add global/workspace/session prompt roots and merge precedence.
6. Add prompt validation and fallback diagnostics.
7. Add prompt provenance to runtime metadata/logs.
8. Add prompt profile resolution.
9. Add TUI prompt catalog and read-only inspection.
10. Add duplicate/edit/save/reload flows.
11. Connect hierarchical expert prompt-profile references to the registry.

## Acceptance Criteria

- Built-in prompts load through the prompt registry.
- Existing behavior remains unchanged when no overrides exist.
- A user can override a prompt with an external Markdown file.
- A user can inspect the active prompt and provenance from the TUI.
- A user can edit and permanently save a prompt from the TUI.
- Built-in prompt edits create overrides rather than modifying package files.
- Prompt profiles such as `heavy` and `light` resolve to different prompt text.
- Prompt profiles can provide provider/model preferences.
- Invalid prompt files are rejected or disabled with clear errors.
- Schema-critical prompts retain parser/runtime guardrails.
- Runtime logs or metadata record prompt id/profile/checksum.
- Expert definitions can reference prompt profiles without duplicating prompt
  loading logic.

## Open Questions For Later

- Exact TUI layout for prompt diff/revert.
- Whether prompt files should support includes or imports.
- Whether prompt variables should use a template language or a constrained
  renderer only.
- How prompt tuning/promotions should move from ARC evidence into prompt files.
- Whether prompt packs should be distributed with the same mechanism as expert
  packs or as a separate package type.
