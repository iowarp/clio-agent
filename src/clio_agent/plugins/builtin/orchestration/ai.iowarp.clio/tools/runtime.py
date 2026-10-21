"""Start flows and plans as owned work, returning a handle at acceptance."""

from clio_agent.sdk import native_tool

run_flow = native_tool("run_flow")
run_plan = native_tool("run_plan")
