"""Start flows and plans as owned work, and write flow plugins the person can adopt."""

from clio_agent.sdk import native_tool

run_flow = native_tool("run_flow")
run_plan = native_tool("run_plan")
make_plugin = native_tool("make_plugin")
