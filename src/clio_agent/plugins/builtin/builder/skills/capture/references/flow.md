# Flow

A fixed sequence with fan-out, checks or loops: a pattern that decides its next call
from earlier results (a check-and-retry loop, a debate, a tournament). When the steps
can be listed as data (step, target, inputs, dependencies), a **workflow**
(`run_workflow`) is simpler and needs no code; write a flow when the pattern needs its
own logic.

## Format

`ai.iowarp.clio/flows/<name>.py` holds a `Flow` class:

- `id`: how it is started (`run_flow(flow="<id>", input=..., options=...)`).
- `description`: what it does, in one sentence a non-specialist understands, then
  `Options:` naming every option.
- `shape`: the steps drawn as a railroad (`sequence`, `choice` for parallel parts,
  `oneOrMore` for a repeat, `optional`, `terminal`). Keep it short and faithful to
  `forward`: it is what the person reads before trusting the plugin.
- class attributes are its options (sizes, which agent plays each part: `self`, an
  installed agent, a subagent, another flow); the caller overrides them with `options`.
- `async def forward(self, task, ctx)` receives the caller's input. Return text, or a
  mapping whose `answer` is what the caller reads.

Each `ctx` call is a task the person can see, stop and cancel: `ctx.call(target, input)`
and `ctx.start(...)` run a subagent, `self`, an installed agent, another flow or a tool;
`ctx.gather([...])` and `ctx.as_completed([...])` run calls together; `ctx.ask(question)`
asks the person; `ctx.step(fn, *args)` records any other side effect; `ctx.workspace` is
the session's folder. Reach nothing else directly (files, network, commands): the person
is shown whatever the code reaches. A failed or cancelled call raises a typed outcome;
catch it to decide what to do.

Decide what happens next from results (a judge's verdict), not a fixed count, when the
pattern calls for it. Leave sizes and bounds open by default: Clio's own limits (subagent
depth and concurrency in the person's settings) and the person's Stop bound every run.
Keep `forward` deterministic given its results: no clock, randomness or I/O outside
`ctx`. After a restart it runs again from the top and re-attaches to the calls it made.

## What `plugin check` enforces

- `flow_missing` (error): an agent selects a flow no resolved plugin provides.
- Python that does not parse or import fails. A flow is an executable part: the person
  trusts it once (with its shape and code shown), and again when it changes.

## Worked example

```json plugin.json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "debate",
  "version": "0.1.0",
  "description": "Two sides argue a question in rounds; a judge decides.",
  "author": {"name": "Clio"},
  "license": "BSD-3-Clause"
}
```

```python ai.iowarp.clio/flows/debate.py
import itertools

from clio_agent.sdk import Flow


class Debate(Flow):
    id = "debate"
    description = (
        "Sides argue a question in rounds; a judge decides or asks for another round. "
        "Options: sides (labels, or {label, agent}), judge (agent), max_rounds."
    )
    shape = """railroad-beta
debate = sequence(oneOrMore(choice(terminal("side 1"), terminal("side 2"))), terminal("judge"));"""
    sides = ("for", "against")
    judge = "self"
    max_rounds = None  # open: the judge decides when it is done

    async def forward(self, task, ctx):
        sides = [s if isinstance(s, dict) else {"label": s, "agent": "self"} for s in self.sides]
        said = {s["label"]: [] for s in sides}
        verdict = None
        rounds = range(1, self.max_rounds + 1) if self.max_rounds else itertools.count(1)
        for round_ in rounds:
            heard = "\n\n".join(f"[{k}] {v[-1]}" for k, v in said.items() if v)
            calls = [
                ctx.call(s["agent"], f"{task}\n\nArgue: {s['label']}. Round {round_}.\n\n{heard}")
                for s in sides
            ]
            for side, result in zip(sides, await ctx.gather(calls)):
                said[side["label"]].append(result.answer)
            transcript = "\n\n".join(
                f"[{k}, round {i + 1}] {a}" for k, v in said.items() for i, a in enumerate(v)
            )
            verdict = await ctx.call(
                self.judge,
                f"{task}\n\nJudge this debate. Start your answer with DECIDED or UNDECIDED."
                f"\n\n{transcript}",
            )
            if not verdict.answer.lstrip().upper().startswith("UNDECIDED"):
                break
        return {"answer": verdict.answer}
```
