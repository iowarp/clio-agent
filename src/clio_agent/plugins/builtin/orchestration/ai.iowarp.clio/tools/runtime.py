"""Start flows and workflows as owned work, and write flow plugins the person can adopt."""

from clio_agent.sdk import native_tool

run_flow = native_tool("run_flow")
run_workflow = native_tool("run_workflow")
make_plugin = native_tool("make_plugin")
