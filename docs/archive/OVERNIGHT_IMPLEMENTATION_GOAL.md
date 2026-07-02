# Overnight Implementation Goal

## Objective

Implement the CLIO backend semantics behind issues #366-#370 plus the new
workspace-scope guardrail issue. Do not implement TUI UI work. Keep each issue
on its own branch/PR unless two issues require the same core contract; in that
case, land the shared contract first and rebase dependent branches immediately.

## Priority Order

1. Workspace scope guardrail: storage roots, scope resolution helpers, and tests.
2. Prompt pack runtime: packaged prompt files and safe dynamic rendering.
3. Expert pack runtime: manifests, activation, catalog provenance, skills and
   commands fields.
4. Agent-invocable commands: planner visibility, policy, allowlists, audit.
5. Agent memory tools: orchestrator-callable same-workspace/global scoped reads.
6. Ask-user/retry: planner action and honest retry override behavior.

## Branching Rule

Use one branch per issue, but stack shared-contract branches when necessary:

- `fix/workspace-scope-runtime`
- `fix/prompt-pack-runtime`
- `fix/expert-pack-runtime`
- `fix/agent-invocable-commands`
- `fix/agent-memory-tools`
- `fix/orchestrator-ask-user-retry`

After each merge to `develop`, rebase or merge `develop` into remaining branches
before continuing. This prevents the shared GACT app/types conflicts from
piling up.

## Done Criteria

- Each PR references and closes its issue.
- Each PR has focused tests for its acceptance criteria.
- Full CLIO test suite passes on final `develop`.
- Issues are closed only after backend semantics are complete, not merely after
  scaffolding lands.
- Any remaining TUI-only work is moved to `gact-tui` issues with exact CLIO API
  references.

