"""V2EX Testing Agent: a live-verification testing agent."""

from clio_agent.sdk import Agent


class V2exTesting(Agent):
    """Live-verification agent for the synthetic v2 MCP exerciser (tests/test_tools/mcp_exerciser.py)."""

    id = "v2ex-testing"
    version = "0.1.0"
    name = "V2EX Testing Agent"
    role = "a synthetic-exerciser verification agent that calls exactly the named tool"
    plugins = Agent.plugins + ["v2ex-testing"]
