"""Built-in tools claimed by this test plugin."""

from clio_agent.sdk import native_tool

fs_read_file = native_tool("fs_read_file")
fs_glob = native_tool("fs_glob")
fs_grep = native_tool("fs_grep")
fs_write_file = native_tool("fs_write_file")
fs_edit_file = native_tool("fs_edit_file")
view_image = native_tool("view_image")
