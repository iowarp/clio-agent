"""Built-in tools claimed by this test plugin."""

from clio_agent.sdk import native_tool

fs_read_file = native_tool("fs_read_file")
fs_glob = native_tool("fs_glob")
fs_grep = native_tool("fs_grep")
fs_propose_edit = native_tool("fs_propose_edit")
fs_apply_edit_write = native_tool("fs_apply_edit_write")
view_image = native_tool("view_image")
