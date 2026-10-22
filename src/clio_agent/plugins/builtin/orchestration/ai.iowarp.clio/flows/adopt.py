"""The ``adopt-plugin`` flow: ask the person to use a plugin Clio wrote, then use it."""

from __future__ import annotations

from typing import Any

from clio_agent.gact.plugin_authoring import adopt_here, request_review
from clio_agent.sdk import Flow


class AdoptPlugin(Flow):
    """Asks in the conversation whether to trust and add a plugin Clio wrote here.

    The Question carries what the person reviews: what the plugin does, each
    flow's shape, what its code reaches and the code itself. On Yes the plugin
    is trusted and added to the conversation that asked. A plugin Clio did not
    write, or one changed since, is refused: its trust step is in Settings.
    """

    id = "adopt-plugin"
    description = (
        "Asks the person whether to trust and add a plugin you wrote with make_plugin, "
        "and does both on Yes. make_plugin starts it; start it again if the person "
        "changes their mind. Input: {name}."
    )

    async def forward(self, task: Any, ctx: Any) -> Any:
        name = str(task.get("name") if isinstance(task, dict) else task or "").strip()
        review = await ctx.step(request_review, name)
        if not review.get("authored"):
            return {
                "answer": (
                    f"{name!r} is not a plugin written in a conversation here, or its files "
                    "changed since, so it is not asked about here. Trust it in Settings, "
                    "Plugins & marketplaces."
                ),
                "trusted": False,
            }
        flows = ", ".join(row["id"] for row in review["flows"]) or "no flows"
        yes = await ctx.ask(
            f"Use the {name} plugin Clio wrote ({flows})?",
            schema=bool,
            reason=review["description"],
            key=f"adopt:{name}:{review['version']}",
            details={"plugin_review": review},
        )
        if not yes:
            return {
                "answer": f"The person chose not to use {name}. It stays installed and untrusted.",
                "trusted": False,
            }
        adopted = await ctx.step(adopt_here, name)
        if not adopted.get("trusted"):
            return {"answer": f"{name} was not trusted: {adopted.get('message')}", **adopted}
        return {
            "answer": (
                f"{name} is trusted and added to this conversation ({', '.join(adopted['added'])}). "
                f"Start its flows with run_flow: {flows}."
            ),
            **adopted,
        }
