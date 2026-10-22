---
name: author-a-flow
title: Write and deploy a new flow
description: Write a reusable flow (a new orchestration pattern such as a debate, a tournament or a check-and-retry loop) as a plugin with the CLIO SDK and hand it to the person with make_plugin. Use when a plan or the existing flows cannot express the pattern.
---

# Write and deploy a new flow

A workflow (`run_workflow`) covers work whose steps you can list as data. A shipped flow
(`run_flow`: council, arena) covers its own pattern. Write a new flow when the
pattern is reusable and needs its own logic: deciding the next call from earlier
results, looping until a check passes, or a structure the existing flows lack.

## The files

A flow plugin is two files:

- `plugin.json` (Agent Plugins 1.0.0):

  ```json
  {
    "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
    "name": "<name>",
    "version": "0.1.0",
    "description": "<what the flow does, one sentence a non-specialist understands>",
    "author": {"name": "Clio"},
    "license": "BSD-3-Clause",
    "extensions": {"ai.iowarp.clio": {"requires": ["delegation"]}}
  }
  ```

- `ai.iowarp.clio/flows/<name>.py`:

  ```python
  import itertools

  from clio_agent.sdk import Flow


  class Debate(Flow):
      id = "debate"
      description = (
          "Sides argue a question in rounds; a judge decides or asks for another round. "
          "Options: sides (labels, or {label, agent}), judge (agent), max_rounds."
      )
      shape = """railroad-beta
  debate = sequence(oneOrMore(choice(terminal("side 1"), terminal("side 2"), terminal("side n"))),
                    terminal("judge"));"""
      sides = ("for", "against")
      judge = "self"
      max_rounds = None  # open: the judge decides when it is done

      async def forward(self, task, ctx):
          sides = [s if isinstance(s, dict) else {"label": s, "agent": "self"} for s in self.sides]
          said: dict[str, list[str]] = {s["label"]: [] for s in sides}
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
              transcript = "\n\n".join(f"[{k}, round {i + 1}] {a}" for k, v in said.items()
                                       for i, a in enumerate(v))
              verdict = await ctx.call(
                  self.judge,
                  f"{task}\n\nJudge this debate. Start your answer with DECIDED or UNDECIDED.\n\n"
                  f"{transcript}",
              )
              if not verdict.answer.lstrip().upper().startswith("UNDECIDED"):
                  break
          return {"answer": verdict.answer}
  ```

- `forward(task, ctx)` receives the caller's input. Return text, or a mapping whose
  `answer` is what the caller reads; put anything the caller needs in the answer.
- Make the pattern's sizes options (class attributes the caller can override with
  `options`): how many sides, members or rounds, and which agent plays each part
  (`self`, an installed agent, another flow). Name every option in `description`.
- Decide what happens next from results (the judge's verdict above), not from a fixed
  count, when the pattern calls for it. Leave sizes and bounds open by default: the
  caller may set them, and Clio's own limits (subagent depth and concurrency in the
  person's settings) and the person's Stop and cancel bound every run.
- `shape` draws the steps for the person as a railroad (`sequence`, `choice` for
  parallel parts, `oneOrMore` for a repeat, `optional`, `terminal`). Keep it
  short and faithful to `forward`: it is what the person reads before trusting it.
- Each `ctx` call is a task the person can see, stop and cancel:
  `ctx.call(target, input)` and `ctx.start(...)` run a subagent, `self`, an
  installed agent, another flow or a tool; `ctx.gather([...])` and
  `ctx.as_completed([...])` run calls together; `ctx.ask(question)` asks the
  person; `ctx.step(fn, *args)` records any other side effect; `ctx.workspace`
  is the session's folder. Reach nothing else directly (files, network,
  commands): the person is shown whatever the code reaches.
- A failed or cancelled call raises a typed outcome; catch it to decide what to do.
- Keep `forward` deterministic given its results: no clock, randomness or I/O
  outside `ctx`. After a restart it runs again from the top and re-attaches to
  the calls it already made.

## Hand it over

Call `make_plugin(name, files)` with both files. It checks the plugin as
`clio-agent plugin check` does (fix what it reports and call it again) and
installs it. Under bypass it is trusted and added to this conversation at once;
otherwise the person is asked here, with what it does, its shape and its code,
and the answer arrives as a task result. Once it is added, start it with
`run_flow(flow="<id>", input=..., options=...)`. To change it, call
`make_plugin` again with the new files and a higher version; the person is
asked again.
