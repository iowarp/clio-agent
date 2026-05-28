# Agent Blueprint Migration Plan

Tracks GitHub issues #382, #383, #384, and #385.

## Purpose

CLIO already has foundations for prompt packs, expert packs, memory tools,
skills, commands, and session-scoped runtime state. The next step is to align
those pieces under one stronger abstraction: Agent Blueprints.

This document maps the current implementation to the intended runtime so the
implementation does not drift into another partial pack system.

## Current State

Existing foundations:

- `docs/EXPERT_PACK_RUNTIME_V2.md` defines shareable expert-pack folders.
- `docs/PROMPT_PACK_RUNTIME_V2.md` defines Markdown prompt files and dynamic
  render placeholders.
- `docs/AGENT_MEMORY_TOOLS_V2.md` defines agent-callable memory tools.
- `/v1/expert-packs` can discover and activate partial expert packs.
- `/v1/prompts` can list, render, validate, reload, and save prompts.
- `/v1/agents` exposes tools, skills, commands, prompt ids, model/provider
  defaults, hierarchy fields, and validation errors.

Remaining gaps:

- the pack is not yet the complete active Agent for a session;
- the canonical root is still `clio-pack.yaml`, not Markdown `AGENT.md`;
- the default CLIO Data Exploration/Search Agent is still mostly code-backed;
- some runtime prompt/profile policy text remains in Python;
- child Expert call boundaries are hardwired to current CLIO registry behavior;
- MCP descriptors are not yet packaged by Blueprints with safe enablement;
- TUI edits need a session overlay rather than mutating installed files.

## Migration Phases

### Phase 1: Add Agent Blueprint Loader

Implement `AGENT.md` parsing and validation while keeping `clio-pack.yaml`
support.

Required behavior:

- discover top-level Blueprint folders under global/workspace install roots;
- parse Markdown frontmatter and body;
- load `experts/`, `prompts/`, `profiles/`, `commands/`, `skills/`, and
  `tools/` subtrees;
- expose validation diagnostics without dropping invalid files;
- map old Expert Pack definitions into Agent Blueprint-compatible internal
  structures.

Compatibility:

- `/v1/expert-packs` continues to work;
- `/v1/agent-blueprints` becomes the preferred API;
- existing tests for Expert Pack V2 remain valid until callers migrate.

### Phase 2: Session Agent Instantiation

Change session activation so a Blueprint is the whole active Agent for that
session.

Required behavior:

- the active session Agent graph comes from one Blueprint snapshot;
- the default CLIO Agent is selected when no explicit Blueprint is active;
- `/v1/agents?session_id=...` reports the active Agent graph, not a global
  overlay catalog;
- turn metadata records active Blueprint id/version/source/checksum;
- generated child-Expert tools are derived from the active graph.

### Phase 3: Built-In Data Exploration/Search Blueprint

Move the current default Agent into packaged files.

Required files:

- built-in `AGENT.md`;
- Expert files for root, chat, data, analysis, visualization, NDP, SAC/format,
  and utility/shell/edit surfaces where supported;
- prompt/profile files for current planner, answer, chat, and expert behavior;
- MCP/tool requirement descriptors where useful.

Python may retain:

- DSPy signatures/classes;
- tool executors and native expert implementations;
- validators and parsers;
- provider adapters;
- compatibility shims.

Python should not retain behavior-bearing system/runtime prompt text.

### Phase 4: Prompt/Profile Extraction Cleanup

Finish prompt externalization beyond the existing built-in prompt files.

Move to files:

- profile policy text;
- prompt alignment requirements;
- prompt-only dynamic agent wrapper instructions;
- tool-using dynamic agent wrapper instructions;
- any fallback runtime prompt used when an Expert has no prompt body.

Add an audit test that allows DSPy schema and field descriptions in Python but
fails on new behavior-bearing runtime/system prompt strings.

### Phase 5: MCP Descriptor Safety

Allow Blueprints to package MCP descriptors without enabling them by default.

Required behavior:

- descriptor install is separate from descriptor enablement;
- disabled descriptors produce structured diagnostics for dependent Experts;
- explicit enablement is persisted by user/workspace policy;
- tool catalog shows descriptor source, enabled state, and visibility.

### Phase 6: Session Overlays

Add a session-local edit layer.

Required behavior:

- TUI edits write to a session overlay by default;
- overlay values win over installed Blueprint values for that session only;
- overlay provenance appears in agent, prompt, and turn metadata;
- explicit save/fork can materialize an overlay as a workspace/global Blueprint
  revision.

## API Migration

Preferred new routes:

- `/v1/agent-blueprints`
- `/v1/sessions/{sid}/agent-blueprint`
- `/v1/sessions/{sid}/agent-overlay`

Compatibility routes:

- `/v1/expert-packs`
- `/v1/sessions/{sid}/expert-pack`

Compatibility route responses should include deprecation metadata after the new
routes are stable, but should not be removed until the TUI and tests migrate.

## Testing Strategy

Add fixture Blueprints for:

- built-in Data Exploration/Search;
- minimal prompt-only Agent;
- multi-Expert Agent with child Expert calls;
- Agent using memory tools;
- Agent with disabled MCP descriptor;
- invalid Agent with missing parent/cycle/missing prompt.

Required test classes:

- parser and validation tests for `AGENT.md` and Expert files;
- install/update tests for local path and Git snapshot metadata;
- session activation and swap tests;
- session overlay isolation tests;
- prompt provenance and no-hardcoded-runtime-prompt audit tests;
- MCP descriptor disabled/enablement tests;
- memory policy tests proving Blueprint tool declarations do not bypass CLIO
  workspace/session restrictions.

## Implementation Guardrails

- Do not add another parallel "pack" abstraction; adapt Expert Pack V2 into
  Agent Blueprint compatibility.
- Do not let global/workspace Blueprints merge into every session by default.
  A session has one active Agent.
- Do not auto-enable MCP servers from installed Blueprint content.
- Do not make TUI edits mutate installed Blueprints by default.
- Do not hide invalid Blueprint content; surface disabled rows with diagnostics.
- Do not move DSPy schemas/classes into Markdown. Only runtime behavior text
  and Agent/Expert definitions must become file-backed.
