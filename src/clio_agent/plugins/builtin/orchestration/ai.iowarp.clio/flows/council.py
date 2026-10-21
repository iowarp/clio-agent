"""The ``council`` flow: independent views of one question, then one synthesis."""

from __future__ import annotations

import json
from itertools import cycle
from typing import Any

import dspy

from clio_agent.sdk import Flow


class Synthesis(dspy.Signature):
    """Reconcile independent views of the same question into one answer.

    Say where the views agree, resolve each disagreement with its reasons, and
    keep any uncertainty that remains material.
    """

    question: str = dspy.InputField()
    views: list[str] = dspy.InputField(desc="Each member's answer, labelled with its perspective")
    answer: str = dspy.OutputField()


class Council(Flow):
    """Ask several members the same question independently, then synthesize one answer.

    Options: ``members`` (targets; ``self`` is this agent), ``perspectives`` (one
    per member), ``synthesizer`` (a target; empty synthesizes inside the flow).
    """

    id = "council"
    description = "Independent views from several members, then one synthesized answer."
    members: tuple[str, ...] = ("self", "self", "self")
    perspectives: tuple[str, ...] = (
        "a careful domain expert",
        "a skeptical reviewer looking for errors, gaps and unstated assumptions",
        "a pragmatist focused on what should be done next",
    )
    synthesizer = ""

    def __init__(self, ref: str | None = None, **options: Any) -> None:
        super().__init__(ref, **options)
        self.synthesize = dspy.ChainOfThought(Synthesis)

    def _synthesis(self, question: str, views: list[str]) -> dict[str, Any]:
        return {"answer": str(self.synthesize(question=question, views=views).answer)}

    async def forward(self, question: Any, ctx: Any) -> Any:
        text = question if isinstance(question, str) else json.dumps(question, sort_keys=True)
        members = list(self.members)
        perspectives = list(self.perspectives) or [""]
        pairs = list(zip(members, cycle(perspectives)))
        calls = [
            ctx.call(member, f"{text}\n\nAnswer independently, as {view}." if view else text)
            for member, view in pairs
        ]
        views = await ctx.gather(calls, limit=len(calls))
        labelled = [
            f"{view or member}: {result.answer}"
            for (member, view), result in zip(pairs, views, strict=True)
        ]
        handles = [result.handle for result in views]
        if self.synthesizer:
            final = await ctx.call(self.synthesizer, {"question": text, "views": labelled})
            return {"answer": final.answer, "members": handles, "synthesizer": final.handle}
        synthesis = await ctx.step(self._synthesis, text, labelled)
        return {"answer": synthesis["answer"], "members": handles}
