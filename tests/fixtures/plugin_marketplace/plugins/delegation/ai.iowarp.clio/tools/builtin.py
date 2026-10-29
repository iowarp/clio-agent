"""Built-in tools claimed by this test plugin."""

from clio_agent.sdk import native_tool

spawn_agent_task = native_tool("spawn_agent_task")
spawn_agents_parallel = native_tool("spawn_agents_parallel")
fork = native_tool("fork")
