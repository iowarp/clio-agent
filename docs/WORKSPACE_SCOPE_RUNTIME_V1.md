# Workspace Scope Runtime V1

## Goal

Define workspace/local/global boundaries so the major runtime features have a
shared scope model. This is a guardrail issue, not the main product center.

## Defaults

- Local workspace storage defaults to `<workspace>/.clio`.
- The workspace storage root is configurable.
- User-home config remains valid for globally installed packs, prompts,
  commands, agents, provider settings, and global sessions.
- Global sessions are real sessions in a global/user scope, not invisible memory
  automatically mixed into every workspace.

## Runtime Boundary

By default, an agent running in a workspace sees:

- current session memory
- same-workspace sessions when user intent/policy permits cross-session memory
- global agents/prompts/commands/packs
- workspace agents/prompts/commands/packs
- session overrides

By default, it does not see:

- other workspace sessions
- other workspace ARC memory
- other workspace-local packs/commands/prompts

## Catalog Resolution

Reusable definitions resolve in this order:

```text
builtin -> global -> workspace -> session
```

Global definitions are available by default. Workspace definitions override
global only inside that workspace. Session definitions override only inside that
session.

## Storage

Workspace-local stores should eventually include:

- sessions
- messages
- context files
- context frames
- memory indexes / ARC data
- session permission policy overrides
- session pack/prompt activation metadata

Global stores should include:

- provider/user settings
- globally installed packs
- globally installed prompts
- globally installed commands
- global sessions and user-level memory

## Acceptance Criteria

- New workspace sessions persist under the workspace storage root by default.
- A config override can relocate workspace storage.
- Two workspaces do not list/read each other's sessions by default.
- Global artifacts appear in workspace catalogs.
- Workspace artifacts do not leak to other workspaces.
- Memory tools can distinguish current session, current workspace, global, and
  denied other-workspace scopes.
- Tests cover default `.clio`, storage override, catalog precedence, and memory
  isolation.

## Pre-Benchmark Proof

`tests/test_gact/test_workspace_scope_prebenchmark.py` exercises the combined
scope contract that the final benchmark depends on:

- two distinct workspace roots,
- overlapping local Agent Blueprint IDs,
- global Agent Blueprint visibility alongside workspace-local overrides,
- overlapping local command IDs,
- global command visibility alongside workspace-local command files,
- session-local Agent Blueprint activation,
- per-workspace command file resolution,
- same-workspace cross-session memory search with explicit intent,
- denied other-workspace memory search,
- explicit global memory search through the agent-facing memory tool.

This is backend/API proof. The final real-provider benchmark still needs to show
the same policy decisions in session logs and runtime provenance when an agent
uses the memory and workspace surfaces during a real task.
