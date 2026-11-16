"""Read the sessions to capture or optimize from, and write the person's plugins."""

from clio_agent.sdk import native_tool

session_record = native_tool("session_record")
optimize_scope = native_tool("optimize_scope")
optimize_report_shown = native_tool("optimize_report_shown", defer=True)
make_plugin = native_tool("make_plugin")
