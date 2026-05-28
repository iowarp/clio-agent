# CLIO Agent Turn Selection

CLIO supports two levels of agent selection for GACT sessions:

- Session default: `POST /v1/sessions` or `PATCH /v1/sessions/{sid}` with
  `agent: {"id": "..."}`.
- Turn override: `POST /v1/sessions/{sid}/messages` with either
  `agent: {"id": "..."}` or legacy-friendly `agent_id: "..."`.

The turn override is intentionally scoped to one user message. It does not
mutate the session default agent. This gives the TUI a safe primitive for
"ask this expert once" and later hierarchical expert forcing without changing
the user's ongoing session configuration.

## Request Shape

```json
{
  "parts": [{"type": "text", "text": "review this plan"}],
  "agent": {"id": "reviewer"}
}
```

`agent.id` takes precedence over `agent_id` when both are present.

## Execution Semantics

If the requested id is a registered user or skill agent, CLIO runs that dynamic
agent through the same execution path used by session-pinned user/skill agents.
If the id is a built-in executable agent such as `main`, CLIO runs the built-in
session path for that turn.

If the requested id is unknown or not executable, CLIO still records the user
message and then emits an assistant error turn with `error_info.error` set to
`not_implemented`. The session default agent remains unchanged.

## Provenance

When a turn override is present, CLIO records provenance in both messages:

- User message metadata includes `agent_override.requested_agent_id`,
  `session_agent_id`, and `scope: "turn"`.
- Assistant message metadata also includes `effective_agent_id`, using the
  selected/routed expert when available and otherwise the requested id.

This metadata is the backend hook for TUI routing badges, hierarchical expert
forcing, and context-frame provenance.
