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

#: fs_git_commit adds commits (and, on request, the repository): it never rewrites or
#: drops history, so it is not destructive, but it is a closed-world write.
_GIT_COMMIT_ANNOTATIONS: dict[str, Any] = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "openWorldHint": False,
}

#: Namespaced tool name → declared annotations for the fs built-ins. Exported so
#: :mod:`clio_agent.tools.catalog` projects the read/write tags from the SAME
#: mapping the decorators declare — one declaration, two consumers.
FS_TOOL_ANNOTATIONS: dict[str, dict[str, Any]] = {
    "fs_list_directory": _READ_FILE_ANNOTATIONS,
    "fs_search_text": _READ_FILE_ANNOTATIONS,
    "fs_glob": _READ_FILE_ANNOTATIONS,
    "fs_read_file": _READ_FILE_ANNOTATIONS,
    "fs_write_file": _FILE_WRITE_ANNOTATIONS,
    "fs_edit_file": _FILE_WRITE_ANNOTATIONS,
    # Records the folder's changes in its repository: a bounded write inside the
    # approved folder (its ``.git``), never the network, so it asks like an edit.
    "fs_git_commit": _GIT_COMMIT_ANNOTATIONS,
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

#: web_* / literature_* change nothing on this computer but send queries and URLs to
#: hosted services (open world), so they are actions the permission gate and a playbook
#: may narrow, not fast-allowed reads; plan mode allows them as plan-safe (plan_acl).
_WEB_ANNOTATIONS: dict[str, Any] = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "openWorldHint": True,
}

#: Namespaced tool name → declared annotations for the web and literature built-ins.
WEB_TOOL_ANNOTATIONS: dict[str, dict[str, Any]] = {
    "web_search": _WEB_ANNOTATIONS,
    "web_fetch": _WEB_ANNOTATIONS,
    "literature_search": _WEB_ANNOTATIONS,
    "literature_lookup": _WEB_ANNOTATIONS,
}

#: Namespaced tool name → the action a person reads for a call to a built-in tool: the
#: title of its permission card ("Run command", not "shell command call: shell_bash").
BUILTIN_TOOL_TITLES: dict[str, str] = {
    "fs_list_directory": "List folder",
    "fs_search_text": "Search text",
    "fs_glob": "Find files",
    "fs_read_file": "Read file",
    "fs_write_file": "Write file",
    "fs_edit_file": "Edit file",
    "fs_git_commit": "Commit changes",
    "shell_bash": "Run command",
    "web_search": "Search the web",
    "web_fetch": "Fetch a web page",
    "literature_search": "Search the literature",
    "literature_lookup": "Look up a paper",
}
