# CLIO Agent Turn Selection

Every agent CLIO runs is resolved from plugins (see
[PLUGINS_AND_FLOWS.md](PLUGINS_AND_FLOWS.md)). An agent is addressed by its
reference: `<marketplace>/<id>` (`clio/standard`), a bare id when it is
unambiguous, or `user/<id>` for a user agent, optionally with `@<range>`.

CLIO supports two levels of agent selection for GACT sessions:

- Session agent: `POST /v1/sessions` with `agent: {"id": "<ref>"}` resolves and
  pins that agent for the session; without one, the session defaults'
  `agent_ref`, else `clio/standard`. `PATCH /v1/sessions/{sid}/agent` with
  `{"ref": ..., "add": [...], "remove": [...]}` changes the agent or its live
  plugin delta and pins the new resolution.
- Turn override: `POST /v1/sessions/{sid}/messages` with either
  `agent: {"id": "<ref>"}` or legacy-friendly `agent_id: "<ref>"`.

The turn override is intentionally scoped to one user message. It resolves the
named agent for that turn (with no delta) and does not change the session's
pinned agent. A deep-research turn with no override runs `clio/deep-researcher`
the same way.

## Request Shape

```json
{
  "parts": [{"type": "text", "text": "review this plan"}],
  "agent": {"id": "clio/deep-researcher"}
}
```

`agent.id` takes precedence over `agent_id` when both are present.

## Execution Semantics

The turn runs the resolved agent's flow (`react` unless the agent selects
another) with its composed prompt, tools, servers and skills. A skill id is NOT
an agent id (skills stopped materializing as agents in the #916 skill-semantics
change).

If the requested agent is not installed or does not resolve (a missing,
untrusted or colliding plugin), CLIO still records the user message and then
emits an assistant error turn with `error_info.error` set to
`agent_unavailable`; `details` carries the agent ref, the typed reason and every
resolution error. Creating a session with such an agent is refused with the same
error (HTTP 422). The session's pinned agent remains unchanged.

## Provenance

When a turn override is present, CLIO records provenance in both messages:

- User message metadata includes `agent_override.requested_agent_id`,
  `session_agent_id`, and `scope: "turn"`.
- Assistant message metadata also includes `effective_agent_id`, using the
  agent that ran when available and otherwise the requested id.

The `agent.invocation.started` semantic event carries the turn's `agent_ref`,
`resolution_digest` and `flow`. This metadata is the backend hook for TUI
routing badges and context-frame provenance.
