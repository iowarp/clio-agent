# Tool

The same computation the agent wrote in `run_code` more than once. A tool makes it one
call with typed inputs, the same every time. Write a Python `@tool`; never an MCP
server for something a function does.

## Format

`ai.iowarp.clio/tools/*.py`:

```text
@tool(name="", description="", read_only=False)
```

- The function's arguments, types and defaults become the tool's parameters; its
  docstring is its description (what it does and when, in plain words).
- `read_only=True` when it only reads: it then runs in read-only conversations too.
- Return plain data (a dict, a list, a number, text). Raise an exception with a clear
  message on bad input; the agent reads it.
- Keep it to the computation. Reading files, the network or running commands is
  allowed but shown to the person when they trust the plugin.

When scripts call it (inside `run_code`), a **script binding** may fit better: plain
Python bound into every script under a name (`kernel_bindings` in `plugin.json`, see
the Plugin reference). Use a binding when the computation is a step inside larger
scripts; a tool when the agent calls it on its own.

## What `plugin check` enforces

- `tool_collision` (error): another plugin in the agent has a tool of the same name.
  Name it for the field (`beam_profile`, not `plot`).
- `native_tool_unknown` (error): a `native_tool("name")` claim of a built-in that does
  not exist.
- Python that does not parse or import fails. A tool is an executable part: trusted
  once, and again when it changes.

## Worked example

```json plugin.json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "beam-profile",
  "version": "0.1.0",
  "description": "Summarizes a beam profile the same way every time.",
  "author": {"name": "Clio"},
  "license": "BSD-3-Clause"
}
```

```python ai.iowarp.clio/tools/profile.py
from clio_agent.sdk import tool


@tool(read_only=True)
def beam_profile_fwhm(positions: list[float], intensities: list[float]) -> dict:
    """Full width at half maximum of a beam profile, from positions and intensities."""
    if len(positions) != len(intensities) or len(positions) < 3:
        raise ValueError("positions and intensities must be the same length, at least 3")
    peak = max(intensities)
    half = peak / 2
    above = [x for x, y in zip(positions, intensities) if y >= half]
    return {"peak": peak, "fwhm": max(above) - min(above), "center": positions[intensities.index(peak)]}
```
