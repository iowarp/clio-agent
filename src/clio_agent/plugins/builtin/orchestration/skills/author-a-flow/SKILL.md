---
name: author-a-flow
title: Write and deploy a new flow
description: Write a reusable flow (a new orchestration pattern such as a debate, a tournament or a check-and-retry loop) as a plugin with the CLIO SDK, check it and install it so the person can trust it and add it. Use when a plan or the existing flows cannot express the pattern.
---

# Write and deploy a new flow

A plan (`run_plan`) covers work whose steps you can list as data. A shipped flow
(`run_flow`: council, arena) covers its own pattern. Write a new flow when the
pattern is reusable and needs its own logic: deciding the next call from earlier
results, looping until a check passes, or a structure the existing flows lack.

## Layout

A flow ships in a plugin folder:

```text
<name>/
  plugin.json
  ai.iowarp.clio/flows/<name>.py
```

`plugin.json` (Agent Plugins 1.0.0):

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "<name>",
  "version": "0.1.0",
  "description": "<what the flow does, one sentence>",
  "author": {"name": "<the person's name or team>"},
  "license": "BSD-3-Clause",
  "extensions": {"ai.iowarp.clio": {"requires": ["delegation"]}}
}
```

## The flow

```python
from clio_agent.sdk import Flow


class Debate(Flow):
    id = "debate"
    description = "Two sides argue a question; a moderator decides. Options: sides."
    sides = ("for", "against")  # an option: a class attribute a caller can override

    async def forward(self, task, ctx):
        args = await ctx.gather([ctx.call("self", f"{task}\n\nArgue {s}.") for s in self.sides])
        transcript = "\n\n".join(f"[{s}] {a.answer}" for s, a in zip(self.sides, args))
        verdict = await ctx.call("self", f"{task}\n\nWeigh these and decide:\n\n{transcript}")
        return {"answer": verdict.answer}
```

- `forward(task, ctx)` receives the caller's input. Return text, or a mapping whose
  `answer` is what the caller reads; put anything the caller needs in the answer.
- Each `ctx` call is a task the person can see, stop and cancel:
  `ctx.call(target, input)` and `ctx.start(...)` run a subagent, `self`, an
  installed agent, another flow or a tool; `ctx.gather([...])` and
  `ctx.as_completed([...])` run calls together; `ctx.ask(question)` asks the
  person; `ctx.step(fn, *args)` records any other side effect; `ctx.workspace`
  is the session's folder.
- A failed or cancelled call raises a typed outcome; catch it to decide what to do.
- Keep `forward` deterministic given its results: no clock, randomness or I/O
  outside `ctx`. After a restart it runs again from the top and re-attaches to
  the calls it already made.
- Name the options in `description`, so a caller knows what it can set.

## Check, install, hand over

1. Write the folder in the workspace, then run `clio-agent plugin check <folder>`
   and fix every error it reports.
2. Install it: `clio-agent plugin install <folder>`.
3. Tell the person it is installed and that it needs their trust step before it
   can run: **Settings, Plugins & marketplaces, Trust** (or
   `clio-agent plugin trust <name>`). Trusting code is the person's decision.
4. Once trusted, the person adds it to this conversation from the + menu. Then
   start it with `run_flow(flow="<id>", input=..., options=...)`; its run appears
   in the flow view like any other flow.
