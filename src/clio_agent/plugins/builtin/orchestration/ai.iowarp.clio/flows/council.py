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
    per member), ``models`` (one per member: the model each runs on, empty for the
    member's own choice), ``synthesizer`` (a target; empty synthesizes inside the
    flow, on the model this run runs on) and ``synthesizer_model``.
    """

    id = "council"
    description = (
        "Several members answer the same question independently, then one synthesized "
        'answer. Options: members (a list of targets, "self" for this agent; default '
        '["self", "self", "self"]), perspectives '
        "(one per member), models (one per member, e.g. a small model for each view), "
        "synthesizer (a target; empty synthesizes inside the flow, on this run's model) "
        "and synthesizer_model."
    )
    shape = """railroad-beta
council = sequence(choice(terminal("member 1"), terminal("member 2"), terminal("member n")),
                   terminal("synthesis"));"""
    members: tuple[str, ...] = ("self", "self", "self")
    perspectives: tuple[str, ...] = (
        "a careful domain expert",
        "a skeptical reviewer looking for errors, gaps and unstated assumptions",
        "a pragmatist focused on what should be done next",
    )
    synthesizer = ""
    models: tuple[str, ...] = ()
    synthesizer_model = ""

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
        models = list(self.models)
        calls = [
            ctx.call(
                member,
                f"{text}\n\nAnswer independently, as {view}." if view else text,
                model=models[index] if index < len(models) else "",
            )
            for index, (member, view) in enumerate(pairs)
        ]
        views = await ctx.gather(calls, limit=len(calls))
        labelled = [
            f"{view or member}: {result.response_text}"
            for (member, view), result in zip(pairs, views, strict=True)
        ]
        handles = [result.handle for result in views]
        # The model each member ran on, so the caller can see the mix it asked for.
        asked = {"member_models": [result.model for result in views]}
        # The caller reads the answer, so it also says who answered on which model.
        who = [
            _on(view or member, result.model)
            for (member, view), result in zip(pairs, views, strict=True)
        ]
        if self.synthesizer or self.synthesizer_model:
            final = await ctx.call(
                self.synthesizer or "self",
                {"question": text, "views": labelled},
                model=self.synthesizer_model,
            )
            who.append(_on("synthesis", final.model))
            return {
                "answer": f"{final.response_text}\n\nMembers:\n" + "\n".join(who),
                "members": handles,
                "synthesizer": final.handle,
                **asked,
            }
        synthesis = await ctx.step(self._synthesis, text, labelled)
        who.append(_on("synthesis", ""))
        return {
            "answer": f"{synthesis['answer']}\n\nMembers:\n" + "\n".join(who),
            "members": handles,
            **asked,
        }


def _on(name: str, model: str) -> str:
    return f"- {name}, on {model}" if model else f"- {name}"
