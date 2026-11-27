# Subagent

Work handed to a specialist with its own instructions: a reviewer, a checker, a
specialist reader. It runs as its own conversation with its own prompt and plugins, and
returns a report.

## Format

`ai.iowarp.clio/subagents/*.py` defines `Subagent(...)`:

| Field | Meaning |
| --- | --- |
| `id` | How it is started (`spawn_agent_task(agent="<id>")`, `ctx.call("<id>")`). |
| `prompt` | `PromptFile("prompt/<file>.md")` (relative to `ai.iowarp.clio/`, a prompt file without `section`) or the prompt as text. |
| `description` | When to use it and what to give it; the delegating agent reads this. |
| `plugins` | Plugins it carries besides `core` and its own plugin. |
| `tools` | Optional: the only tools it may call besides `core`'s. |
| `prefers` | `Prefers(role="...")` to run on a fleet role, or a model. |
| `effects` | `"read"` runs it read-only. |

Write its prompt as the instructions you would give a capable colleague: what to look
at, what to report, in what form, and what not to do.

## What `plugin check` enforces

- `prompt_file_missing` (error): the `PromptFile` does not exist.
- `subagent_tool_unavailable` (error): `tools` names a tool its plugins do not bring.
- A subagent is declared in Python, an executable part: trusted once, and again when
  it changes.

## Worked example

```json plugin.json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "units-review",
  "version": "0.1.0",
  "description": "A reviewer that checks units in an analysis before it is reported.",
  "author": {"name": "Clio"},
  "license": "BSD-3-Clause"
}
```

```python ai.iowarp.clio/subagents/reviewer.py
from clio_agent.sdk import PromptFile, Subagent

units_reviewer = Subagent(
    "units-reviewer",
    prompt=PromptFile("prompt/units-reviewer.md"),
    description="Checks every quantity in an analysis for its unit before it is reported. "
    "Give it the analysis text and the data's unit conventions.",
    effects="read",
)
```

```markdown ai.iowarp.clio/prompt/units-reviewer.md
You check the units in an analysis before it is reported. For every number with a
physical meaning, find its unit in the text or the data's conventions you were given.
Report each quantity whose unit is missing, inconsistent or converted wrongly, with
the line it appears on and the fix. Say plainly when everything is consistent.
```
