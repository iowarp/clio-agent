# CLIO Agent Turn Selection

Every agent CLIO runs is resolved from plugins (see
[PLUGINS_AND_FLOWS.md](PLUGINS_AND_FLOWS.md)). An agent is addressed by its
reference: `<marketplace>/<id>` (`clio/standard`), a bare id when it is
unambiguous, or `user/<id>` for a user agent, optionally with `@<range>`.

A conversation runs one agent, the session agent:

- `POST /v1/sessions` with `agent: {"id": "<ref>"}` resolves and pins that agent
  for the session; without one, the session defaults' `agent_ref`, else
  `clio/standard`. `PATCH /v1/sessions/{sid}/agent` with
  `{"ref": ..., "add": [...], "remove": [...]}` changes the agent or its live
  plugin delta and pins the new resolution.
- There is no per-turn agent override. `POST /v1/sessions/{sid}/messages` with
  `agent` or `agent_id` is refused (HTTP 422). To bring one of the agent's
  subagents, declared workflows or flows into a message, the person summons it: a
  `{"type": "summon", "target_kind": "subagent", "target": "<name>"}` part (or
  `workflow`, `flow`) tells the session agent's turn that the person asked for it.
  The agent writes the brief and parameters from the conversation and makes the
  call (`spawn_agent_task`, `run_workflow`, `run_flow`) with every tool still
  available; that call records the person as the child's starter. A command file's
  `agent:` or `workflow:` field does the same. See
  [the summoning ledger](design/sdk/summoning-ledger.md) (DS48).
- A deep-research turn (`behavior.execution_mode: "deep_research"`) runs
  `clio/deep-researcher` for that turn only, without changing the pinned agent.

## Execution Semantics

The turn runs the resolved agent's flow (`react` unless the agent selects
another) with its composed prompt, tools, servers and skills. A skill id is NOT
an agent id (skills stopped materializing as agents in the #916 skill-semantics
change).

If the session's agent is not installed or does not resolve (a missing,
untrusted or colliding plugin), CLIO still records the user message and then
emits an assistant error turn with `error_info.error` set to
`agent_unavailable`; `details` carries the agent ref, the typed reason and every
resolution error. Creating a session with such an agent is refused with the same
error (HTTP 422).

## Provenance

A deep-research turn records `turn_agent` in the assistant message metadata:
`requested_agent_id`, `session_agent_id`, `effective_agent_id` and
`scope: "turn"`. What the person summoned is recorded on their message as
`summons` (`subagents`, `servers`, `workflows`, `flows`); the turn's answer
records `summons_outcome` (which were started and which were not, with the
reason), and a child the turn started for a summons carries
`started_by: {kind: "person", message_id}`.

The `agent.invocation.started` semantic event carries the turn's `agent_ref`,
`resolution_digest` and `flow`. This metadata is the backend hook for TUI
routing badges and context-frame provenance.
