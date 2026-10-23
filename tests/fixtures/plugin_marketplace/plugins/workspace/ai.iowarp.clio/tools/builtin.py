"""Built-in tools claimed by this test plugin."""

from clio_agent.sdk import native_tool

fs_read_file = native_tool("fs_read_file")
fs_list_directory = native_tool("fs_list_directory")
fs_search_text = native_tool("fs_search_text")
fs_write_file = native_tool("fs_write_file")
fs_edit_file = native_tool("fs_edit_file")
view_image = native_tool("view_image")
