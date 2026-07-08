# CLIO Agent documentation

Index of the `docs/` tree. Start with setup, then architecture, then the
behavior contracts and integration surfaces. Historical and superseded material
lives under [archive/](archive/README.md).

## Start here

- [SETUP.md](SETUP.md) — release install, source build, and smoke test.
- [INSTALL.md](INSTALL.md) — the packaged installer and `clio` launcher.
- [CONTRIBUTOR_QUICKSTART.md](CONTRIBUTOR_QUICKSTART.md) — dev environment, quality checks, where code goes.
- [ENVIRONMENT.md](ENVIRONMENT.md) — environment variables and runtime configuration.

## Architecture & design

- [CLIO_AGENT_ARCHITECTURE.md](CLIO_AGENT_ARCHITECTURE.md) — how the whole system fits together.
- [SYSTEM_IDENTITY.md](SYSTEM_IDENTITY.md) — what CLIO is (and is not).
- [ARC_MEMORY_LAYER.md](ARC_MEMORY_LAYER.md) — the ARC memory model and storage.
- [DSPY_BLUEPRINT_EXPERT_RUNTIME.md](DSPY_BLUEPRINT_EXPERT_RUNTIME.md) — DSPy-backed expert runtime.
- [AGENT_BLUEPRINT_RUNTIME.md](AGENT_BLUEPRINT_RUNTIME.md) — agent blueprint composition and runtime.
- [AGENT_BLUEPRINT_PACKAGED_HOOKS.md](AGENT_BLUEPRINT_PACKAGED_HOOKS.md) — packaged hooks in agent blueprints.
- [PROMPT_SYSTEM.md](PROMPT_SYSTEM.md) — the external, editable prompt system.
- [PROMPT_ALIGNMENT_REFERENCE_MATRIX.md](PROMPT_ALIGNMENT_REFERENCE_MATRIX.md) — prompt families mapped to public sources.
- [SEMANTIC_EXECUTION_TRACES.md](SEMANTIC_EXECUTION_TRACES.md) — the semantic event log and traces.

## Behavior & contracts

- [PERMISSIONS.md](PERMISSIONS.md) — the tool permission system.
- [CANCELLATION_SEMANTICS.md](CANCELLATION_SEMANTICS.md) — how cancellation propagates through a turn.
- [AGENT_TURN_SELECTION.md](AGENT_TURN_SELECTION.md) — how the active agent is selected per turn.
- [GACT_BROWSER_ORIGIN_SECURITY.md](GACT_BROWSER_ORIGIN_SECURITY.md) — browser-origin security for the GACT server.
- [ASK_USER_RETRY_PROTOCOL.md](ASK_USER_RETRY_PROTOCOL.md) — the ask-user / retry protocol.
- [MCP_TOOL_INTEGRATION.md](MCP_TOOL_INTEGRATION.md) — adding tools via FastMCP.

## Providers

- [providers/](providers/README.md) — LM provider configuration and how to add a provider.

## TUI / GACT integration

- [tui/README.md](tui/README.md) — the gact-tui integration docs index.
- [tui/REAL_GAPS.md](tui/REAL_GAPS.md) — the authoritative TUI gap tracker.
- [tui/02-agent-graph.md](tui/02-agent-graph.md) — agent graph surface.
- [tui/03-experts.md](tui/03-experts.md) — experts surface.
- [tui/06-endpoints.md](tui/06-endpoints.md) — endpoint families.
- [tui/08-semantics-and-lifecycle.md](tui/08-semantics-and-lifecycle.md) — semantics and lifecycle.
- [tui/09-integration-plan.md](tui/09-integration-plan.md) — the integration plan.
- [gact/ARCHITECTURE.md](gact/ARCHITECTURE.md) — the GACT server architecture.

## Design program & roadmap

- [design/roadmap.md](design/roadmap.md) — what to build next.
- [design/system-cleanup-2026-07.md](design/system-cleanup-2026-07.md) — the active 2026-07 cleanup program (#775).
- [design/turn-transcript.md](design/turn-transcript.md) — the turn-transcript design.

## Archive

- [archive/](archive/README.md) — historical designs, migration records, and superseded specs.
