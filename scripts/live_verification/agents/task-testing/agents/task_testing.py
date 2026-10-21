"""Asynchronous Task Qualification: a live-verification testing agent."""

from clio_agent.sdk import Agent


class TaskTesting(Agent):
    """Isolated live model qualification of production task owners and controls."""

    id = "task-testing"
    version = "0.1.0"
    name = "Asynchronous Task Qualification"
    role = "a task qualification agent that follows the supplied mission with real tools"
    plugins = Agent.plugins + ["workspace", "shell", "task-testing"]
