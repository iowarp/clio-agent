"""Built-in tools claimed by this test plugin (the marketplace visuals plugin's declarations)."""

from clio_agent.sdk import native_tool

create_a2ui_surface = native_tool("create_a2ui_surface", defer=False)
update_a2ui_components = native_tool("update_a2ui_components", defer=False)
update_a2ui_data_model = native_tool("update_a2ui_data_model", defer=False)
delete_a2ui_surface = native_tool("delete_a2ui_surface", defer=True)
inspect_a2ui_surface = native_tool("inspect_a2ui_surface", defer=True)
capture_a2ui_surface = native_tool("capture_a2ui_surface", defer=True)
publish_dashboard_report = native_tool("publish_dashboard_report", defer=True)
