"""V2 Stress Testing Agent: a live-verification testing agent."""

from clio_agent.sdk import Agent


class V2Stress(Agent):
    """Live-verification stress agent: the web MCP server and the synthetic v2 exerciser in one turn."""

    id = "v2-stress"
    version = "0.1.0"
    name = "V2 Stress Testing Agent"
    role = "a plumbing-stress verification agent that runs every numbered step in order"
    plugins = Agent.plugins + ["web", "v2-stress"]
