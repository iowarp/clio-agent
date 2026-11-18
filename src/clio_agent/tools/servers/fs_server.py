"""Filesystem MCP server for CLIO.

Seven curated tools. Four read, without executing anything; three write:

    - read_file: read a range of a text file's lines, numbered, sized to the result bound.
    - list_directory: discover an approved folder's immediate entries (bounded).
    - search_text: find text with ripgrep: matching files, matching lines, or counts.
    - glob: find files by path pattern, newest first.
    - write_file: create a file or replace its whole content (Claude Code's Write).
    - edit_file: replace an exact string in an existing file (Claude Code's Edit).
    - git_commit: record the folder's changes in git, run by CLIO with hooks disabled.

Every tool honors file_policy + workspace boundaries. Reads inside the policy are
always allowed. The two writes change the file on disk; their declared annotations
make them writes, so the permission gate asks or allows by the session's mode and
PreToolUse hooks run on them. GACT records each successful write as an applied
``file_diff`` the person can undo (``POST /v1/sessions/{sid}/diffs/revert``).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from pydantic import Field

from clio_agent.runtime.check_scope import checked
from clio_agent.tools import fs_edit
from clio_agent.tools import git_commit as _git_commit
from clio_agent.tools.builtin_annotations import (
    _FILE_WRITE_ANNOTATIONS,
    _GIT_COMMIT_ANNOTATIONS,
    _READ_FILE_ANNOTATIONS,
)
from clio_agent.tools.builtin_annotations import (
    FS_TOOL_ANNOTATIONS as FS_TOOL_ANNOTATIONS,
)
from clio_agent.tools.fs_inspection import list_directory as _list_directory
from clio_agent.tools.fs_read import read_lines, read_text_range
from clio_agent.tools.fs_search_rg import SearchMode, glob_files, search_files

fs_server = FastMCP("fs")

# Declared MCP ToolAnnotations — the SINGLE source of truth for each built-in
# tool's effect class (#1061). The catalog read/write tags are now a PROJECTION
# of these hints (``clio_agent.tools.catalog.classification_tags``); no
# hand-authored read/write tag competes with the declaration below.
#
#   - read_file / list_directory / search_text: provably read-only, closed-world
#     (local disk read only).
#   - write_file / edit_file: NOT read-only; a bounded, closed-world destructive
#     write (a single known path), so they project to the catalog ``write`` tag
#     the auto-edits approval mode keys on.


@fs_server.tool(annotations=_READ_FILE_ANNOTATIONS)
@checked
def list_directory(path: str, limit: Annotated[int, Field(ge=1, le=1_000)] = 200) -> dict[str, Any]:
    """List immediate entries in an absolute approved directory; does not follow links.

    Use this to discover repository paths without guessing filenames or running shell.
    Results explicitly report truncation at the requested entry limit (maximum 1,000).
    """
    return _list_directory(path, limit)


@fs_server.tool(annotations=_READ_FILE_ANNOTATIONS)
@checked
def search_text(
    path: str,
    query: Annotated[str, Field(min_length=1, max_length=1_000)],
    mode: Annotated[
        SearchMode,
        Field(
            description="files: matching files with counts (default); content: matching lines "
            "with numbers; count: totals."
        ),
    ] = "files",
    regex: bool = False,
    file_glob: Annotated[
        str, Field(description="Only search files matching this glob, e.g. *.py or src/**/*.ts.")
    ] = "",
    context_lines: Annotated[int, Field(ge=0, le=50)] = 0,
    case_sensitive: bool = True,
    limit: Annotated[int, Field(ge=1, le=500)] = 100,
    offset: Annotated[
        int,
        Field(ge=0, description="Files or matching lines to skip: the offset a result names."),
    ] = 0,
) -> ToolResult:
    """Search text in an approved file or folder (ripgrep; skips binary, Git and ignored files).

    Start with mode="files" to find where something is, then read the ranges you need, or
    use mode="content" for the matching lines themselves. query is literal unless regex is
    true. Truncation is stated in the result; no match is not proof of absence when it is.
    When files or matches remain, the result names the offset to continue from.
    """
    return search_files(
        path,
        query,
        mode=mode,
        regex=regex,
        file_glob=file_glob,
        context_lines=context_lines,
        case_sensitive=case_sensitive,
        limit=limit,
        offset=offset,
    )


@fs_server.tool(annotations=_READ_FILE_ANNOTATIONS)
@checked
def glob(
    pattern: Annotated[
        str, Field(min_length=1, description="A file pattern such as **/*.py or docs/*.md.")
    ],
    path: str,
    limit: Annotated[int, Field(ge=1, le=1_000)] = 200,
) -> ToolResult:
    """Find files under an approved folder whose path matches pattern, newest first.

    ** crosses folders. Skips Git and ignored files. Truncation is stated in the result.
    """
    return glob_files(pattern, path, limit)


#: ``fs.read_lines``, read when the server starts: the schema's default and maximum.
_READ_LINES = read_lines()


@fs_server.tool(annotations=_READ_FILE_ANNOTATIONS)
@checked
def read_file(
    path: str,
    offset: Annotated[int, Field(ge=1, description="1-based line to start from.")] = 1,
    limit: Annotated[
        int, Field(ge=1, le=_READ_LINES, description="Most lines to return.")
    ] = _READ_LINES,
    access_reason: Annotated[
        str,
        Field(
            description="Explain why this file is needed when requesting access outside permitted folders."
        ),
    ] = "",
) -> ToolResult:
    """Read a text file's lines, each prefixed with its line number.

    Returns whole lines from ``offset`` until ``limit`` or the room one result has in your
    context window, after a header with the range and the file's line count; when lines
    remain, the header ends with the offset to continue from. Read the ranges you need rather than whole large
    files. Binary files are refused with the tool that can read them. Only files inside
    the allowed roots are readable.
    """

    return read_text_range(path, offset, limit)


@fs_server.tool(annotations=_FILE_WRITE_ANNOTATIONS)
@checked
def write_file(
    path: str,
    content: str,
    access_reason: Annotated[
        str, Field(description="Explain why this write needs access outside permitted folders.")
    ] = "",
) -> dict[str, Any]:
    """Create a file, or replace its whole content, with ``content``.

    Missing parent folders are created. The bytes on disk are exactly ``content``.
    The result says whether the file was ``created``, its line counts and the sha256 of
    the file before and after; replacing a file also carries the ``unified_diff`` of
    the change (a new file's content is what was written).
    The change is listed in the person's diff panel, where they can undo it.
    """

    return fs_edit.write_file(path, content)


@fs_server.tool(annotations=_FILE_WRITE_ANNOTATIONS)
@checked
def edit_file(
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    access_reason: Annotated[
        str, Field(description="Explain why this edit needs access outside permitted folders.")
    ] = "",
) -> dict[str, Any]:
    """Replace ``old_string`` with ``new_string`` in an existing text file.

    The match is exact, including whitespace and indentation. ``old_string`` must
    occur exactly once unless ``replace_all`` is true, which replaces every
    occurrence. A missing file, a missing ``old_string``, or one that occurs more
    than once returns a typed error and changes nothing. The result carries the
    ``unified_diff`` of the change, its line counts, and the sha256 before and after.
    """

    return fs_edit.edit_file(path, old_string, new_string, replace_all)


@fs_server.tool(annotations=_GIT_COMMIT_ANNOTATIONS)
@checked
def git_commit(
    path: Annotated[
        str, Field(description="The folder whose changes to record, inside the workspace.")
    ],
    message: Annotated[str, Field(min_length=1, description="The commit message.")],
    files: Annotated[
        list[str] | None,
        Field(
            description="Exact paths, relative to path, to commit; omit to commit every "
            "change under path."
        ),
    ] = None,
    init: Annotated[
        bool,
        Field(description="Create the repository at path when there is none."),
    ] = False,
    author: Annotated[
        str,
        Field(
            description="'Name <email>' to commit as, only when the person gave it; "
            "otherwise their git identity is used."
        ),
    ] = "",
) -> dict[str, Any]:
    """Record changes in git: stage and commit them, creating the repository with init.

    Use this for git changes: your commands and file tools cannot write a repository's
    .git. CLIO runs git itself, with repository hooks disabled, after the person approves.
    Untracked files are included, so a new repository's first commit takes everything
    under path that .gitignore does not exclude. The result names the commit, its branch
    and the files it changed, or why nothing was committed. Reading history (status, log,
    diff) works with ordinary git commands.
    """

    return _git_commit.git_commit(path, message, files, init, author)


__all__ = ["FS_TOOL_ANNOTATIONS", "fs_server"]
