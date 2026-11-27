# Hook

A correction that must never be violated, such as "never write to raw/". A hook runs
on every matching event and can refuse; its reason is what the agent reads, so write it
as the explanation the person gave.

## Format

`ai.iowarp.clio/hooks/*.py`, with the `hook` decorator:

```text
@hook(event, *, id="", before=None, after=None, tools="", fail_closed=False)
```

- `event`: `tool_call` (before a tool runs; can refuse), `tool_result`, `prompt_submit`
  (can refuse), `answer` (adds a note or check under the answer), `session_start` and
  others (see the Hooks page).
- `tools`: a regular expression matching a tool's whole name, for tool events.
- Return `None` to allow, or `{"decision": "deny", "reason": "..."}` to refuse. Raising
  `PermissionError` also refuses. A hook has 30 seconds.
- Read the call from `ev.tool_name` and `ev.tool_input`.

Make the pattern exactly as wide as the rule: a hook that blocks more than the person
meant gets in their way every day.

## What `plugin check` enforces

- `hook_cycle` (error): `before` and `after` form a cycle.
- Python that does not parse (`python_syntax`) or does not import fails.
- A hook is an executable part: the person trusts it once, and again when it changes.

## Worked example

```json plugin.json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "raw-data-guard",
  "version": "0.1.0",
  "description": "Keeps the instrument's original files untouched.",
  "author": {"name": "Clio"},
  "license": "BSD-3-Clause"
}
```

```python ai.iowarp.clio/hooks/raw.py
from clio_agent.sdk import hook


@hook("tool_call", tools=r"fs_write_file|fs_edit_file")
def protect_raw_data(ev):
    """Refuse any write under a raw/ folder."""
    path = "/" + str((ev.tool_input or {}).get("filepath", "")).replace("\\", "/")
    if "/raw/" in path:
        return {
            "decision": "deny",
            "reason": "raw/ holds the instrument's original files; write results to processed/.",
        }
    return None
```
