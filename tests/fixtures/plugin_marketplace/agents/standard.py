"""The test stand-in for CLIO's standard agent."""

from clio_agent.sdk import Agent


class StandardAgent(Agent):
    id = "standard"
    version = "1.0.0"
    name = "CLIO"
    role = "an autonomous scientific coding and data assistant working in the user's workspace"
    description = "Test standard agent."
    plugins = Agent.plugins + ["workspace", "shell", "visuals", "delegation", "document-runtime"]
