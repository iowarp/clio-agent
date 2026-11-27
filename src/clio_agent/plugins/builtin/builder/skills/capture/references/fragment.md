# Prompt fragment

Something the agent could not infer and needed in every request: a data layout, a
naming rule, a unit convention. A fragment is part of every request the agent makes,
so it must be short and always relevant. Longer material is a skill or a skill
reference instead.

## Format

A Markdown file in `ai.iowarp.clio/prompt/` with frontmatter:

| Field | Rule |
| --- | --- |
| `id` | Starts with `<plugin>.`, such as `beamline-units.keV`. |
| `section` | `capabilities` (what a tool can do) or `domain` (facts about the work). |
| `when` | Optional. `{tools: [name]}` applies it only when that tool is resolved; `{plugins: [id]}` only when those plugins are. |

Write facts, not orders: "Energies in this lab's files are in keV" rather than "ALWAYS
convert to keV". Never claim the plugin is the only way to do something.

## What `plugin check` enforces

- `fragment_budget` (warning): above about 150 tokens. Cut it, or move the detail into
  a skill reference.
- `fragment_invalid` (error): an `id` that does not start with `<plugin>.`, a `section`
  other than `capabilities` or `domain` (`guidelines` and `rendering` belong to
  `core`), or a `when` with keys other than `tools`, `plugins` and `session`.
- A Markdown file in `prompt/` without `section` is a plain prompt file (for a
  subagent), not a fragment.

## Worked example

```json plugin.json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "beamline-units",
  "version": "0.1.0",
  "description": "This lab's unit conventions for beamline data.",
  "author": {"name": "Clio"},
  "license": "BSD-3-Clause"
}
```

```markdown ai.iowarp.clio/prompt/units.md
---
id: beamline-units.keV
section: domain
---
Energies in this lab's beamline files are in keV, even where an axis has no `units`
attribute. Temperatures are in kelvin. Report results in the same units.
```
