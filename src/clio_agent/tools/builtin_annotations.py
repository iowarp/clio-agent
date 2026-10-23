"""Shared builtin tool metadata without importing or constructing MCP servers."""

from typing import Any

_READ_FILE_ANNOTATIONS: dict[str, Any] = {"readOnlyHint": True, "openWorldHint": False}
#: fs_write_file / fs_edit_file change the file on disk: a bounded, closed-world
#: destructive write that projects to the catalog ``write`` tag, so the permission
#: gate asks or allows by the session's mode and PreToolUse hooks see every write.
_FILE_WRITE_ANNOTATIONS: dict[str, Any] = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "openWorldHint": False,
}

#: Namespaced tool name → declared annotations for the fs built-ins. Exported so
#: :mod:`clio_agent.tools.catalog` projects the read/write tags from the SAME
#: mapping the decorators declare — one declaration, two consumers.
FS_TOOL_ANNOTATIONS: dict[str, dict[str, Any]] = {
    "fs_list_directory": _READ_FILE_ANNOTATIONS,
    "fs_search_text": _READ_FILE_ANNOTATIONS,
    "fs_read_file": _READ_FILE_ANNOTATIONS,
    "fs_write_file": _FILE_WRITE_ANNOTATIONS,
    "fs_edit_file": _FILE_WRITE_ANNOTATIONS,
}


_BASH_ANNOTATIONS: dict[str, Any] = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "openWorldHint": True,
}

#: Namespaced tool name → declared annotations for the shell built-ins. Exported
#: so :mod:`clio_agent.tools.catalog` projects read/write tags from the SAME
#: mapping the decorator declares.
SHELL_TOOL_ANNOTATIONS: dict[str, dict[str, Any]] = {"shell_bash": _BASH_ANNOTATIONS}
