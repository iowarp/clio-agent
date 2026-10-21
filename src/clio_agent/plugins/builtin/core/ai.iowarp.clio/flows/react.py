"""The default flow: one ReAct loop over the agent's resolved tools."""

from clio_agent.sdk import Flow


class React(Flow):
    """Run the agent's ClioReAct loop on the task and return its prediction."""

    id = "react"

    async def forward(self, task, ctx):
        return await ctx.react(task)
