"""Web Testing Agent: a live-verification testing agent."""

from clio_agent.sdk import Agent


class WebTesting(Agent):
    """Live-verification agent for the web MCP server (web_fetch, web_search, web_fetch_events)."""

    id = "web-testing"
    version = "0.1.0"
    name = "Web Testing Agent"
    role = "a web verification agent that fetches or searches exactly as instructed"
    plugins = Agent.plugins + ["web", "web-testing"]
