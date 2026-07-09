# Archive

Historical, one-shot, or superseded documents kept for provenance (design docs whose work shipped or was abandoned, benchmark/demo reports, announcements). Nothing in this directory describes the current system — do not treat it as ground truth. For live documentation see [`docs/README.md`](../README.md) and the roadmap at [`docs/design/roadmap.md`](../design/roadmap.md).

## Contents

### Design & capability records (archived #774)

- [CAPABILITIES_MATRIX.md](CAPABILITIES_MATRIX.md) — v0.3.1 per-capability verification matrix (superseded snapshot).
- [EXPERT_SYSTEM_DESIGN.md](EXPERT_SYSTEM_DESIGN.md) — early multi-expert orchestration design.
- [REAL_PROVIDER_SEMANTIC_REGRESSION.md](REAL_PROVIDER_SEMANTIC_REGRESSION.md) — real-provider failure-mode evidence for routing/tools/fallback/streaming.
- [arc-live-context-plane.md](arc-live-context-plane.md) — the ARC live-context-plane design record (shipped v1).
- [implementation-spec.md](implementation-spec.md) — implementation spec for the ARC live context plane.
- [per-expert-provider-lm.md](per-expert-provider-lm.md) — per-expert provider + LM off process-global state (#818 / #815).
- [unified-concurrency-model.md](unified-concurrency-model.md) — unified concurrency model (#735 / #758 / #770).

### ARC / memory

- [arc-live-context-plane.md](arc-live-context-plane.md) — ARC as the live context plane.
- [implementation-spec.md](implementation-spec.md) — implementation spec for the live context plane.
- [threads-b-a-plan.md](threads-b-a-plan.md) — ARC live plane on clio-core CTE, exposed over gact REST/TUI.
- [CROSS_SESSION_MEMORY_SEARCH.md](CROSS_SESSION_MEMORY_SEARCH.md) — cross-session memory search.
- [MEMORY_SYSTEM_REFINEMENT.md](MEMORY_SYSTEM_REFINEMENT.md) — memory system refinement.
- [MEMORY_CONTEXT_ATTACHMENT_STATS.md](MEMORY_CONTEXT_ATTACHMENT_STATS.md) — memory context attachment stats.
- [AGENT_MEMORY_TOOLS_V2.md](AGENT_MEMORY_TOOLS_V2.md) — agent memory tools (v2).

### Context files & sessions

- [CONTEXT_FILE_PERSISTENCE.md](CONTEXT_FILE_PERSISTENCE.md) — context file persistence.
- [CONTEXT_FILE_TURN_PROVENANCE.md](CONTEXT_FILE_TURN_PROVENANCE.md) — context file turn provenance.
- [FILE_MENTION_CONTEXT_ATTACHMENTS.md](FILE_MENTION_CONTEXT_ATTACHMENTS.md) — file-mention context attachments.
- [SESSION_CONTEXT_ATTACHMENT_LIFECYCLE.md](SESSION_CONTEXT_ATTACHMENT_LIFECYCLE.md) — session context attachment lifecycle.
- [SESSION_CONTEXT_POLICY.md](SESSION_CONTEXT_POLICY.md) — session context policy.

### Experts, agents & packs

- [EXPERT_SYSTEM_DESIGN.md](EXPERT_SYSTEM_DESIGN.md) — multi-expert orchestration.
- [HIERARCHICAL_EXPERTS.md](HIERARCHICAL_EXPERTS.md) — hierarchical user-defined experts.
- [EXPERT_PACK_RUNTIME_V2.md](EXPERT_PACK_RUNTIME_V2.md) — expert pack runtime (v2).
- [PROMPT_PACK_RUNTIME_V2.md](PROMPT_PACK_RUNTIME_V2.md) — prompt pack runtime (v2).
- [PACK_DEFINED_NANOAGENT_FANOUT.md](PACK_DEFINED_NANOAGENT_FANOUT.md) — pack-defined DSPy expert semantics & capability parity.
- [AGENT_BLUEPRINT_MIGRATION_PLAN.md](AGENT_BLUEPRINT_MIGRATION_PLAN.md) — agent blueprint migration plan.
- [NATIVE_EXPERT_MIGRATION_INVENTORY.md](NATIVE_EXPERT_MIGRATION_INVENTORY.md) — native expert migration inventory.
- [DYNAMIC_AGENT_RUNTIME_PROVENANCE.md](DYNAMIC_AGENT_RUNTIME_PROVENANCE.md) — dynamic agent runtime provenance.
- [per-expert-provider-lm.md](per-expert-provider-lm.md) — per-expert provider + LM (#818 / #815).

### Prompts

- [PROMPT_ALIGNMENT_IMPLEMENTATION.md](PROMPT_ALIGNMENT_IMPLEMENTATION.md) — prompt alignment implementation.
- [PROMPT_ALIGNMENT_REFERENCE.md](PROMPT_ALIGNMENT_REFERENCE.md) — prompt alignment reference work.

### Commands, permissions & capabilities

- [AGENT_INVOCABLE_COMMANDS_V2.md](AGENT_INVOCABLE_COMMANDS_V2.md) — agent-invocable commands (v2).
- [USER_DEFINED_COMMANDS_DESIGN.md](USER_DEFINED_COMMANDS_DESIGN.md) — user-defined slash-commands design.
- [COMMAND_CAPABILITY_TRUTH_DESIGN.md](COMMAND_CAPABILITY_TRUTH_DESIGN.md) — command & capability truth design.
- [CAPABILITY_GAPS.md](CAPABILITY_GAPS.md) — CLIO capability gaps.
- [CAPABILITIES_MATRIX.md](CAPABILITIES_MATRIX.md) — v0.3.1 capabilities matrix.
- [PERMISSION_POLICY_PERSISTENCE.md](PERMISSION_POLICY_PERSISTENCE.md) — permission policy persistence.
- [PERMISSION_SURFACING_DESIGN.md](PERMISSION_SURFACING_DESIGN.md) — permission surfacing design.
- [ORCHESTRATOR_ASK_USER_RETRY_V2.md](ORCHESTRATOR_ASK_USER_RETRY_V2.md) — orchestrator ask-user & retry (v2).

### TUI, workspaces & lifecycle

- [COMPOSABLE_TUI_MODULES.md](COMPOSABLE_TUI_MODULES.md) — composable TUI module layout.
- [WORKSPACE_FILE_PREVIEW.md](WORKSPACE_FILE_PREVIEW.md) — workspace file preview.
- [WORKSPACE_SCOPE_RUNTIME_V1.md](WORKSPACE_SCOPE_RUNTIME_V1.md) — workspace scope runtime (v1).
- [UNDO_REWIND_DESIGN.md](UNDO_REWIND_DESIGN.md) — undo & rewind design.
- [unified-concurrency-model.md](unified-concurrency-model.md) — unified concurrency model (#735 / #758 / #770).

### Provider evidence & vision

- [REAL_PROVIDER_SEMANTIC_REGRESSION.md](REAL_PROVIDER_SEMANTIC_REGRESSION.md) — real-provider semantic regression evidence.
- [CLIO_VISION.md](CLIO_VISION.md) — next-milestone vision document.
