"""Task and Subagent Qualification: a live-verification testing agent."""

from clio_agent.sdk import Agent


class TaskSubagentTesting(Agent):
    """Actual model qualification over owned task inputs, with a defined worker subagent."""

    id = "task-subagent-testing"
    version = "0.1.0"
    name = "Task and Subagent Qualification"
    role = "a task qualification coordinator that spawns only the requested worker assignment"
    plugins = Agent.plugins + ["workspace", "shell", "delegation", "task-subagent-testing"]
