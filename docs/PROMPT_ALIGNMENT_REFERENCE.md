# Prompt Alignment Reference Work

## Purpose

Create a deliberate prompt-alignment phase for CLIO after the hierarchical
expert and external prompt systems are in place.

This is separate from prompt storage/editing. The prompt system defines where
prompts live, how they are edited, how profiles resolve, and how provenance is
tracked. Prompt alignment defines what those prompts should say, how they should
behave, and how they should be evaluated against user expectations for modern
coding/scientific agents.

## Why This Is Separate

External editable prompts are infrastructure. Hierarchical experts are routing
and delegation infrastructure. Neither guarantees that the resulting prompts are
good.

CLIO needs a dedicated pass that studies existing public agent prompt patterns,
agent UX conventions, and CLIO's own scientific/tooling requirements, then tunes
the built-in prompts and prompt profiles accordingly.

## Goals

- Align CLIO's built-in prompts with behavior users expect from mature coding
  agents while preserving CLIO-specific scientific workflow semantics.
- Define prompt profiles such as `heavy`, `light`, `small_model`,
  `fine_tuned`, and provider-specific variants as behavioral profiles, not only
  shorter/longer text blobs.
- Tune hierarchy prompts so parent experts delegate clearly and child experts
  stay scoped to their responsibility.
- Tune tool-use prompts so agents prefer real evidence, cite tool outputs, and
  do not claim unsupported tool calls.
- Tune failure/recovery prompts so errors are surfaced honestly and users get
  actionable next steps.
- Tune TUI-facing behavior so prompts cooperate with visible state: permissions,
  context attachments, memory summaries, retries, model fallback, and tool
  provenance.

## Reference Sources

Use public, inspectable references only:

- public documentation for established coding agents;
- public prompt examples or prompt-engineering guides;
- open-source agent frameworks and command/skill conventions;
- CLIO's own existing docs, benchmark prompts, and real-provider evidence.

The issue and docs should describe this as reference and alignment work. They
should not rely on or mention any non-public or questionable-source material.

## Work Areas

### System Prompt Baselines

- Main CLIO agent identity and behavior.
- Planner/router prompt.
- Expert prompts for data, analysis, visualization, NDP/catalog, SAC/format, and
  future user-defined experts.
- Tool-use and tool-evidence instructions.
- Memory/context use instructions.
- Permission and destructive-action behavior.
- Error and recovery style.

### Prompt Profiles

Each profile should define behavior, not just size:

- `heavy`: more deliberate planning, stronger self-checking, richer tool
  evidence, better for larger models.
- `light`: concise, lower-latency, fewer planning tokens, suitable for smaller
  or local models.
- `small_model`: explicit schemas, narrower instructions, stronger guardrails,
  less implicit reasoning.
- `fine_tuned`: minimal prompting that assumes trained behavior.
- provider/model-specific overrides when a model has known strengths or limits.

### Hierarchical Delegation

- Parent prompts should explain available child experts and delegation criteria.
- Child prompts should avoid re-routing or broad synthesis unless explicitly
  asked.
- Delegation prompts should produce observable handoff metadata.
- Fallback prompts should disclose model/provider fallback when behavior may
  differ.

### Tool And Evidence Behavior

- Prefer structured CLIO tools over guessing.
- Distinguish observed tool evidence from inferred conclusions.
- Do not claim a tool was called unless telemetry exists.
- Preserve exact file paths, dataset names, column names, artifacts, and caveats.
- Surface unsupported tool/voice/command capabilities honestly.

### TUI Cooperation

Prompts should align with TUI affordances:

- ask-user questions;
- retries and retry notes;
- permissions;
- file mentions/context attachments;
- memory compaction summaries;
- prompt profile switching;
- expert hierarchy inspection;
- model/provider fallback warnings.

## Evaluation

Add prompt regression scenarios before changing defaults:

- routing to the correct expert;
- delegation depth and child expert scoping;
- tool evidence vs unsupported claims;
- context attachment use;
- memory summary use;
- failure recovery;
- small-model behavior;
- heavy-profile behavior;
- model fallback disclosure;
- user-defined expert prompt override behavior.

Where possible, reuse existing benchmark fixtures and real-provider evidence.
The goal is to prevent prompt changes from quietly degrading routing,
scientific grounding, or TUI-visible semantics.

## Acceptance Criteria

- A reference matrix documents which public agent patterns influenced each major
  CLIO prompt family.
- Built-in prompts are externalized through the prompt system before alignment
  changes are made.
- Prompt profiles define explicit behavioral tradeoffs.
- Hierarchical expert prompts are tuned and tested as a set, not one file at a
  time.
- Tool-use prompts require telemetry-backed claims.
- Prompt changes include regression scenarios for routing, delegation, context,
  tool evidence, failure recovery, and profile differences.
- TUI-visible behaviors such as ask-user, retry, permissions, and fallback
  warnings are reflected in the relevant prompts.

## Implementation Status

The backend alignment pass is implemented in `src/clio_agent/prompts.py` and
summarized in [PROMPT_ALIGNMENT_IMPLEMENTATION.md](PROMPT_ALIGNMENT_IMPLEMENTATION.md).

Built-in prompt families now expose `default`, `heavy`, `light`,
`small_model`, `fine_tuned`, and `debug` profiles through the same prompt
registry used for external overrides. Resolved built-in prompts carry alignment
metadata and family requirements so the GACT/TUI layer can inspect prompt
provenance without re-implementing prompt rules.

## Dependencies

- External editable prompt system.
- Hierarchical user-defined expert architecture.
- Memory/context provenance model.
- User-defined command/skill semantics.
- Ask-user and retry protocol.
