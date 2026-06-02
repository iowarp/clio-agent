# Agent Blueprint Packaged Hooks

## Goal

Allow marketplace Agent Blueprints to package runtime hook files without making
installation or activation execute arbitrary code silently.

Packaged hooks are for domain-specific audit, policy, artifact registration, and
benchmark instrumentation. They extend the existing CLIO runtime hook system;
they do not create a second hook runtime.

## Pack Layout

The supported v1 layout is:

```text
AGENT.md
experts/
hooks/
  pre_message.py
  post_message.py
  pre_tool.py
  post_tool.py
  semantic_event.py
  on_error.py
```

Each file must be named after one supported runtime hook event. Unknown event
names are validation errors.

## Discovery And Validation

`validate_agent_blueprint_path()` and `/v1/agent-blueprints/{id}` return
`hook_descriptors` for packaged hook files. Descriptors include:

- hook id and event,
- source Blueprint id and definition path,
- checksum,
- disabled status,
- explicit trust metadata,
- validation errors and warnings.

Packaged hooks are always reported as disabled until explicitly enabled. Merely
installing or activating a Blueprint does not run packaged hooks.

## Enablement

`POST /v1/agent-blueprints/{blueprint_id}/hooks/{hook_id}/enable` enables one
packaged hook descriptor.

The endpoint:

1. Resolves the Blueprint in the requested workspace scope.
2. Requires a valid packaged hook descriptor.
3. Requires trust, either by explicit request (`{"trust": true}`), descriptor
   policy, or local development config.
4. Verifies the hook file is inside the Blueprint root.
5. Copies the file into the configured local Python runtime hook directory at
   `blueprints/<blueprint_id>/<hook_id>.py`.
6. Writes a sidecar metadata file next to the installed hook with source
   Blueprint id, original definition path, checksum, event, trust, and scope.
7. Rebuilds and installs the process runtime hook registry.

The installed path uses the existing runtime hook scope layout, so the hook fires
only when dispatch scope includes the same `blueprint_id`.

## Trust

Packaged hooks are Python code and must not be enabled silently.

The strict path is:

```json
{"workspace_id": "ws_...", "trust": true}
```

For local development, `CLIO_GACT_HOOK_TRUST_ALWAYS=true` allows explicit
enablement without a request trust flag. Setting it to `false` requires
`trust: true`.

This mirrors the current MCP descriptor trust shape while keeping the execution
surface separate.

## Runtime Scope

Runtime hook dispatch already carries:

- `session_id`,
- `workspace_id`,
- `blueprint_id` for active Agent Blueprint turns.

Packaged Blueprint hooks install into `hooks/blueprints/<blueprint_id>/`, so the
existing runtime hook matcher enforces Blueprint scope. A packaged hook enabled
for one Blueprint does not fire for default sessions or other Blueprints.

## Semantic Trace Provenance

Runtime hook invocation events include handler provenance when hooks match the
current dispatch scope. For packaged Blueprint hooks, `hook.invocation.started`,
`hook.invocation.completed`, and blocked pre-message events include:

- source: `agent_blueprint`,
- `agent_blueprint_id`,
- original `definition_path`,
- runtime `installed_path`,
- hook checksum,
- trust metadata,
- runtime scope,
- invocation status and error when blocked or failed.

This lets benchmark/session trace review distinguish "a hook event happened"
from "this exact packaged hook from this Blueprint ran and produced this
result."

## Current Limits

- Only the `local_python` runtime hook backend supports packaged hook enablement.
  Factory and disabled hook backends return a structured unsupported-backend
  error.
- Enablement is process/runtime state plus copied hook file state. There is no
  separate packaged-hook registry database yet.
- Session-specific packaged hook enablement is not implemented; the current
  scope is Blueprint id.
- The current packaged-hook provenance proof covers message-hook dispatch. Tool
  hook provenance should be reviewed when a benchmark pack uses packaged
  `pre_tool` or `post_tool` hooks.
- Real-provider benchmark proof is still required to show packaged hook events
  inside a marketplace task before claiming final 1.0 readiness.
