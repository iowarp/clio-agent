# Agent (Save as agent)

The set of plugins the person works with: their usual agent plus the plugin just made.
When they will start future conversations from that set, offer **Save as agent**
instead of writing anything: it stores their own agent as its parent plus the changes,
and fixes to the parent keep reaching it.

## How it is saved

- In the app: the agent menu of the conversation (**+**), then **Save local variation**
  (or **Save as a setup**). The person names it; it is addressed as `user/<id>`.
- What is stored:

```text
{"id": "beamline", "name": "Beamline", "extends": "clio/standard@^1",
 "add": ["private-local/beamline-checks"], "remove": []}
```

- Tell the person what it will carry, in one line, and where to pick it for a new
  conversation (the agent picker). Do not write an agent class for them.

## When an agent class is right

A marketplace of their own with Python agent classes (`agents/*.py`, `class Lab(Agent)`
with `plugins = Agent.plugins + [...]`) is for someone publishing agents to others, not
for one person's setup. The builder does not write one.

## What `plugin check` enforces

`plugin check` resolves every agent of a marketplace: `plugin_not_installed`,
`plugin_version_mismatch` and `plugin_ambiguous` when a plugin reference does not
resolve to exactly one installed version, and `plugin_untrusted` when the agent would
carry an untrusted plugin with executable parts.

## Worked example

A plugin that brings nothing but the requirement on the person's plugins is a **pack**
when it only groups members; saving an agent is usually simpler. This pack groups the
lab's two plugins so a saved agent adds one name:

```json plugin.json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "beamline-lab",
  "version": "0.1.0",
  "description": "The beamline lab's plugins, added together.",
  "author": {"name": "Clio"},
  "license": "BSD-3-Clause",
  "extensions": {"ai.iowarp.clio": {"kind": "pack", "members": ["guide", "builder"]}}
}
```
