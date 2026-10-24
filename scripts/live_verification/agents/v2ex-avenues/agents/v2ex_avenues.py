"""V2EX Avenues Testing Agent: a live-verification testing agent."""

from clio_agent.sdk import Agent


class V2exAvenues(Agent):
    """Live-verification agent exposing the full synthetic v2 exerciser tool matrix for leg C2."""

    id = "v2ex-avenues"
    version = "0.1.0"
    name = "V2EX Avenues Testing Agent"
    role = "a synthetic-exerciser verification agent that calls exactly the named tools in order"
    plugins = Agent.plugins + ["v2ex-avenues"]
