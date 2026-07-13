# Case 12: Workspace Marketplace Swap

Status: not passed.

## Prompt Intent

Run two different marketplace Agent Blueprints in different workspaces or
sessions, then ask follow-up questions that should only see the intended
workspace memory and active pack.

## Semantics To Prove

- Workspace-local pack activation and isolation.
- Same-workspace memory access only with explicit intent.
- Pack swap changes available experts, tools, and prompts.
- Trace shows which pack and workspace were active for each turn.

## Required Expert Decomposition

- `workspace_a_main`: runs a scientific workflow under one marketplace pack.
- `workspace_b_main`: runs a different workflow under another marketplace pack.
- `memory_scope`: retrieves only explicitly requested same-workspace evidence.
- `pack_scope`: proves available tools/experts changed after the pack swap.
- `audit`: checks no cross-workspace or inactive-pack evidence entered the
  final answer.

The case must include real scientific work in both workspaces. It is not enough
to toggle pack activation and inspect metadata.

## Current Core Problem

Existing memory and pack-scope proofs are isolated infrastructure checks. The
benchmark needs a realistic cross-session scientific workflow.
