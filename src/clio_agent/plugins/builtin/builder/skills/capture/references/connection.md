# Named connection

An external service the session reached through a saved MCP connection (an instrument's
control service, a lab database, a home server). The plugin names the connection; the
credentials stay on the CLIO host and never enter the plugin.

## Format

In `plugin.json`, under `extensions["ai.iowarp.clio"]`:

```text
"connections": {"<tool namespace>": "<saved connection name>"}
```

- The namespace is the prefix the connection's tools get in this plugin (`lab_` for
  `"lab"`). Use the namespace the session's tool calls already used.
- The connection name is the one under **Extensions > MCP connections** on this host.
  A host without it must add it before the plugin's tools work.
- Never write an MCP server (`mcp.json`) for a service that already has a saved
  connection; name the connection instead.
- Pair it with a skill that says how to use the service, and a fragment only for what
  is needed every time.

## What `plugin check` enforces

- `connections` must be an object of names; another field under `ai.iowarp.clio` fails
  the load.
- The plugin loads without the connection present; its tools appear once the host has
  it.

## Worked example

```json plugin.json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "beamline-control",
  "version": "0.1.0",
  "description": "Reads the beamline's status through the lab's saved connection.",
  "author": {"name": "Clio"},
  "license": "BSD-3-Clause",
  "extensions": {"ai.iowarp.clio": {"connections": {"beamline": "beamline-status"}}}
}
```

```markdown skills/beamline-status/SKILL.md
---
name: beamline-status
description: Read the beamline's current status (shutter, energy, ring current) before planning a run. Use when the person asks whether the beamline is ready or what it is doing now.
---

# Beamline status

1. Call the `beamline_` tools to read the shutter state, the energy and the ring current.
2. Say whether the beamline is ready for the run the person plans, and what is not.
```
