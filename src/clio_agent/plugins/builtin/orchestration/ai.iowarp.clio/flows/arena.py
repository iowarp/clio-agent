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
    entries: list[str] = dspy.InputField(desc="The entries, each prefixed with its [index]")
    winner: int = dspy.OutputField(desc="The 0-based index of the best entry")
    rationale: str = dspy.OutputField()


class Arena(Flow):
    """Several contestants attempt the task; a judge inside the flow picks the winner.

    Options: ``contestants`` (targets; ``self`` is this agent). The votes are
    appended to ``arena/votes.jsonl`` in the workspace, as a recorded step.
    """

    id = "arena"
    description = "Several attempts at one task, then a judged winner."
    contestants: tuple[str, ...] = ("self", "self", "self")

    def __init__(self, ref: str | None = None, **options: Any) -> None:
        super().__init__(ref, **options)
        self.judge = dspy.ChainOfThought(Verdict)

    def _verdict(self, task: str, entries: list[str]) -> dict[str, Any]:
        verdict = self.judge(task=task, entries=entries)
        return {"winner": int(verdict.winner), "rationale": str(verdict.rationale)}

    async def forward(self, task: Any, ctx: Any) -> Any:
        text = task if isinstance(task, str) else json.dumps(task, sort_keys=True)
        entries = await ctx.gather([ctx.call(c, text) for c in self.contestants], limit=3)
        numbered = [f"[{i}] {entry.answer}" for i, entry in enumerate(entries)]
        verdict = await ctx.step(self._verdict, text, numbered)
        winner = verdict["winner"]
        if not 0 <= winner < len(entries):
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
        return {
            "answer": entries[winner].answer,
            "winner": winner,
            "rationale": verdict["rationale"],
            "entries": handles,
        }
