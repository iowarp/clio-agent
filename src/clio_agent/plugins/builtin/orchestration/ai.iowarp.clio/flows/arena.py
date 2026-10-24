"""The ``arena`` flow: contestants attempt the same task and a judge picks one."""

from __future__ import annotations

import json
from typing import Any

import dspy

from clio_agent.flows.outcomes import FlowStall
from clio_agent.sdk import Flow


class Verdict(dspy.Signature):
    """Judge entries that attempt the same task and pick the single best one.

    Weigh correctness, completeness and grounding in evidence; explain the choice.
    """

    task: str = dspy.InputField()
    entries: list[str] = dspy.InputField(desc="The entries, each prefixed with its [number]")
    winner: int = dspy.OutputField(desc="The number of the best entry, counting from 1")
    rationale: str = dspy.OutputField()


class Arena(Flow):
    """Several contestants attempt the task; a judge inside the flow picks the winner.

    Options: ``contestants`` (targets; ``self`` is this agent) and ``models`` (one
    per contestant: the model each runs on, empty for its own choice). The judge
    runs inside the flow, on the model this run runs on. The votes are appended to
    ``arena/votes.jsonl`` in the workspace, as a recorded step.
    """

    id = "arena"
    description = (
        "Several contestants attempt the same task independently; a judge compares the "
        "entries and picks one winner, with its rationale, and the votes are recorded. "
        "Options: contestants (targets; default three of this agent) and models (one per "
        "contestant). The judge runs on this run's model."
    )
    shape = """railroad-beta
arena = sequence(choice(terminal("entry 1"), terminal("entry 2"), terminal("entry n")),
                 terminal("judge"));"""
    contestants: tuple[str, ...] = ("self", "self", "self")
    models: tuple[str, ...] = ()

    def __init__(self, ref: str | None = None, **options: Any) -> None:
        super().__init__(ref, **options)
        self.judge = dspy.ChainOfThought(Verdict)

    def _verdict(self, task: str, entries: list[str]) -> dict[str, Any]:
        verdict = self.judge(task=task, entries=entries)
        return {"winner": int(verdict.winner), "rationale": str(verdict.rationale)}

    async def forward(self, task: Any, ctx: Any) -> Any:
        text = task if isinstance(task, str) else json.dumps(task, sort_keys=True)
        models = list(self.models)
        entries = await ctx.gather(
            [
                ctx.call(c, text, model=models[i] if i < len(models) else "")
                for i, c in enumerate(self.contestants)
            ]
        )
        numbered = [f"[{n}] {entry.answer}" for n, entry in enumerate(entries, 1)]
        verdict = await ctx.step(self._verdict, text, numbered)
        winner = verdict["winner"]
        if not 1 <= winner <= len(entries):
            raise FlowStall(
                "judge_invalid_winner",
                f"the judge picked entry {winner}, but there are {len(entries)}",
                observed=verdict,
            )
        handles = [entry.handle for entry in entries]
        vote = {"run": ctx.run_id, "winner": winner, "entries": handles, **verdict}
        if ctx.workspace.root is not None:
            await ctx.step(ctx.workspace.append, "arena/votes.jsonl", vote)
        else:
            await ctx.record_event({"event": "votes_not_recorded", "reason": "no_workspace"})
        others = "\n\n".join(
            f"[{n}] {entry.answer}" for n, entry in enumerate(entries, 1) if n != winner
        )
        # The caller reads the answer: it carries the verdict, not only the winning text.
        answer = (
            f"{entries[winner - 1].answer}\n\nJudge's choice: entry [{winner}] of {len(entries)}. "
            f"{verdict['rationale']}\n\nOther entries:\n\n{others}"
        )
        return {
            "answer": answer,
            "winner": winner,
            "rationale": verdict["rationale"],
            "entries": handles,
            # The model each entry ran on.
            "entry_models": [entry.model for entry in entries],
        }
