"""Presenter for ``fs_git_commit``: what was recorded, where, and as which commit.

The row reads ``Commit`` with the folder as its subject and a summary such as
``3 files, a1b2c3d on main``. Expansion shows the commit message, a note when the
repository was created, and each committed file as a link with its change
(grouped as added, modified, deleted); a list cut at the tool's limit says so. A call that
committed nothing says why, and a refusal keeps git's own words and the next step.
Importing :mod:`clio_agent.tools.tool_presentation` registers it.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path, PureWindowsPath
from typing import Any

from clio_agent.tools.tool_presentation import (
    PresentationAdapter,
    _structured,
    register_presentation_adapter,
    standard_mcp_presentation,
)

__all__: list[str] = []

#: Why nothing was committed, in words (the tool's ``reason`` codes).
_NOTHING = {"nothing_to_commit": "No changes to commit"}


def _folder(path: str) -> dict[str, Any]:
    return {
        "id": "folder",
        "type": "link",
        "target": "file",
        "uri": path,
        "label": PureWindowsPath(path).name or path,
    }


def _start(args: Mapping[str, Any]) -> dict[str, Any] | None:
    """Name the folder and the message before git runs, without claiming a result."""

    path = str(args.get("path") or "")
    if not path:
        return None
    blocks = [_folder(path)]
    if args.get("message"):
        blocks.append(
            {"id": "message", "type": "text", "label": "Message", "text": str(args["message"])}
        )
    return {"subject": "folder", "summary": "", "blocks": blocks}


#: The order changes are listed in; any other status follows, as git named it.
_STATUS_ORDER = ("added", "modified", "deleted", "type changed")


def _files(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The committed files as links, under one line per kind of change ("2 added")."""

    repository = Path(str(row.get("repository") or ""))
    groups: dict[str, list[str]] = {}
    for entry in row.get("files") or []:
        if isinstance(entry, Mapping) and entry.get("path"):
            groups.setdefault(str(entry.get("status") or "changed"), []).append(str(entry["path"]))
    order = [s for s in _STATUS_ORDER if s in groups] + [
        s for s in groups if s not in _STATUS_ORDER
    ]
    blocks: list[dict[str, Any]] = []
    index = 0
    for status in order:
        names = groups[status]
        blocks.append({"id": f"group-{status}", "type": "text", "text": f"{len(names):,} {status}"})
        for name in names:
            blocks.append(
                {
                    "id": f"file-{index}",
                    "type": "link",
                    "target": "file",
                    "uri": str(repository / name),
                    "label": name,
                    "detail": status,
                }
            )
            index += 1
    return blocks


def _commit(args: Mapping[str, Any], result: Any, snapshot: Any) -> dict[str, Any]:
    del snapshot
    row = _structured(result)
    if row.get("ok") is not True:
        view = standard_mcp_presentation(result)
        error = row.get("error") if isinstance(row.get("error"), Mapping) else {}
        if error.get("next_action"):
            view["blocks"].append(
                {"id": "next", "type": "text", "label": "Next", "text": str(error["next_action"])}
            )
        path = str(row.get("path") or args.get("path") or "")
        if path:
            view["blocks"].insert(0, _folder(path))
            view["subject"] = "folder"
        return view
    path = str(row.get("path") or args.get("path") or "")
    view: dict[str, Any] = {"subject": "folder", "summary": "", "blocks": [_folder(path)]}
    if row.get("initialized"):
        view["blocks"].append(
            {
                "id": "initialized",
                "type": "text",
                "text": f"Created a new git repository in {row.get('repository') or path}.",
            }
        )
    if row.get("committed") is not True:
        view["summary"] = _NOTHING.get(str(row.get("reason")), "Nothing committed")
        return view
    total = int(row.get("files_total") or 0)
    short = str(row.get("commit") or "")[:7]
    branch = str(row.get("branch") or "")
    view["summary"] = f"{total:,} {'file' if total == 1 else 'files'}, {short}" + (
        f" on {branch}" if branch else ""
    )
    view["blocks"].append(
        {"id": "message", "type": "text", "label": "Message", "text": str(row.get("message"))}
    )
    if row.get("author"):
        view["blocks"].append(
            {"id": "author", "type": "text", "label": "Author", "text": str(row["author"])}
        )
    view["blocks"] += _files(row)
    if row.get("files_truncated"):
        view["blocks"].append(
            {
                "id": "coverage",
                "type": "text",
                "severity": "warning",
                "text": f"Showing {len(row.get('files') or [])} of {total:,} committed files.",
            }
        )
    return view


register_presentation_adapter(
    "fs_git_commit",
    PresentationAdapter(_commit, start=_start, action="Commit", subject="folder"),
)
