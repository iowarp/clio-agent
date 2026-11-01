"""Presenters for the file-inspection tools: read, search, glob and list.

Each turns a tool's structured result into the shared presentation contract
(:mod:`clio_agent.gact.tool_result_presentation`): one link to the file or folder,
numbered code for read ranges and search hits (``start_line``), links that open a file
at a line, and a stated warning whenever a result is incomplete. Importing
:mod:`clio_agent.tools.tool_presentation` registers them.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import PureWindowsPath
from typing import Any

import yaml

from clio_agent.tools.tool_presentation import (
    _CODE_LANGUAGE_BY_NAME,
    _CODE_LANGUAGE_BY_SUFFIX,
    PresentationAdapter,
    _file_start,
    _structured,
    register_presentation_adapter,
    standard_mcp_presentation,
)

__all__: list[str] = []

_NUMBERED = re.compile(r"^\s*\d+\t")


def _text(result: Any) -> str:
    content = result.get("content") if isinstance(result, Mapping) else None
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, Mapping) and block.get("type") == "text"
    )


def _warning(block_id: str, text: str) -> dict[str, Any]:
    return {"id": block_id, "type": "text", "severity": "warning", "text": text}


def _language(path: PureWindowsPath) -> str:
    return _CODE_LANGUAGE_BY_NAME.get(path.name.lower()) or _CODE_LANGUAGE_BY_SUFFIX.get(
        path.suffix.lower(), "text"
    )


def _frontmatter(content: str) -> tuple[list[dict[str, Any]], str]:
    """Show a Markdown file's front matter as identity text, the rest as the document."""
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return [], content
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        return [], content
    try:
        frontmatter = yaml.safe_load("".join(lines[1:end]))
    except yaml.YAMLError:
        return [], content
    if not isinstance(frontmatter, Mapping):
        return [], content
    identity = "\n".join(
        f"{key.replace('_', ' ').capitalize()}: {value}"
        for key, value in frontmatter.items()
        if isinstance(key, str) and isinstance(value, str | int | float | bool)
    )
    rest = "".join(lines[end + 1 :]).lstrip("\r\n")
    return ([{"id": "metadata", "type": "text", "text": identity}] if identity else []), rest


def _read(args: Mapping[str, Any], result: Any, snapshot: Any) -> dict[str, Any]:
    del args, snapshot
    row = _structured(result)
    if "total_lines" not in row:
        return standard_mcp_presentation(result)
    raw_path = str(row.get("path") or "")
    path = PureWindowsPath(raw_path)
    start, end, total = row.get("start_line"), row.get("end_line"), row.get("total_lines")
    body = _text(result).split("\n")[1:]  # the first line is the header
    content = "\n".join(_NUMBERED.sub("", line, count=1) for line in body)
    link = {"id": "file-link", "type": "link", "target": "file", "uri": raw_path}
    link |= {"label": path.name, "line": start} if start else {"label": path.name}
    if start is None:
        summary = "Empty file" if total == 0 else f"Past the end ({total:,} lines)"
        return {"subject": "file-link", "summary": summary, "blocks": [link]}
    of = f"of {total:,}" if total is not None else "of a large file"
    summary = f"Lines {start:,}–{end:,} {of}"
    blocks: list[dict[str, Any]] = [link]
    suffix = path.suffix.lower()
    if suffix in {".md", ".markdown"} and start == 1:
        metadata, content = _frontmatter(content)
        blocks += [*metadata, {"id": "file", "type": "markdown", "text": content}]
    else:
        kind = "text" if suffix in {".txt", ".log"} else "code"
        block = {"id": "file", "type": kind, "text": content, "start_line": start}
        blocks.append(block | ({"language": _language(path)} if kind == "code" else {}))
    if row.get("truncated"):
        more = f"More lines follow; the next read starts at line {row.get('next_offset'):,}."
        blocks.append({"id": "continues", "type": "text", "severity": "info", "text": more})
    return {"subject": "file-link", "summary": summary, "blocks": blocks}


def _inspection_start(args: Mapping[str, Any]) -> dict[str, Any]:
    path = str(args.get("path") or "")
    return {
        "subject": "path",
        "summary": str(args.get("query") or args.get("pattern") or ""),
        "blocks": [{"id": "path", "type": "link", "target": "file", "uri": path, "label": path}],
    }


def _file_links(files: list[Any], counts: bool) -> list[dict[str, Any]]:
    blocks = []
    for i, entry in enumerate(files):
        name = str(entry.get("path") if isinstance(entry, Mapping) else entry)
        link = {"id": f"file-{i}", "type": "link", "target": "file", "uri": name}
        link["label"] = PureWindowsPath(name).name
        if counts and isinstance(entry, Mapping):
            matches = int(entry.get("matches") or 0)
            link["detail"] = f"{matches} {'match' if matches == 1 else 'matches'}"
        blocks.append(link)
    return blocks


def _search(args: Mapping[str, Any], result: Any, snapshot: Any) -> dict[str, Any]:
    del snapshot
    row = _structured(result)
    mode = row.get("mode")
    if mode not in {"files", "content", "count"}:
        return standard_mcp_presentation(result)
    view = _inspection_start({"path": row.get("path") or args.get("path")})
    query = str(row.get("query") or args.get("query") or "")
    label = "Pattern" if row.get("regex") else "Search text"
    view["blocks"].append({"id": "query", "type": "text", "label": label, "text": query})
    if mode == "count":
        total, files = int(row.get("total_matches") or 0), int(row.get("total_files") or 0)
        view["summary"] = f"{total:,} matches in {files:,} files"
    elif mode == "files":
        files = row.get("files") if isinstance(row.get("files"), list) else []
        view["summary"] = f"{int(row.get('total_files') or 0):,} files"
        view["blocks"] += _file_links(files, counts=True)
    else:
        matches = [m for m in row.get("matches") or [] if isinstance(m, Mapping)]
        view["summary"] = f"{int(row.get('total_matches') or 0):,} matching lines"
        for i, match in enumerate(matches):
            name, number = str(match.get("path") or ""), match.get("line")
            context = [c for c in match.get("context") or [] if isinstance(c, Mapping)]
            first = context[0].get("line") if context else number
            text = "\n".join([*(str(c.get("text") or "") for c in context), str(match.get("text"))])
            view["blocks"] += [
                {
                    "id": f"match-{i}",
                    "type": "link",
                    "target": "file",
                    "uri": name,
                    "label": f"{PureWindowsPath(name).name}:{number}",
                    "line": number,
                },
                {
                    "id": f"context-{i}",
                    "type": "code",
                    "language": _language(PureWindowsPath(name)),
                    "text": text,
                    "start_line": first,
                },
            ]
    if row.get("truncated"):
        view["blocks"].append(
            _warning(
                "coverage", "Results are incomplete; narrow the folder or glob to see the rest."
            )
        )
    return view


def _glob(args: Mapping[str, Any], result: Any, snapshot: Any) -> dict[str, Any]:
    del snapshot
    row = _structured(result)
    files = row.get("files")
    if not isinstance(files, list):
        return standard_mcp_presentation(result)
    view = _inspection_start({"path": row.get("path") or args.get("path")})
    view["summary"] = f"{int(row.get('total_files') or 0):,} files match {row.get('pattern', '')}"
    view["blocks"] += _file_links(files, counts=False)
    if row.get("truncated"):
        view["blocks"].append(
            _warning("coverage", "More files match than are shown; narrow the pattern or folder.")
        )
    return view


def _directory(args: Mapping[str, Any], result: Any, snapshot: Any) -> dict[str, Any]:
    del snapshot
    row = _structured(result)
    entries = row.get("entries")
    if not isinstance(entries, list):
        return standard_mcp_presentation(result)
    view = _inspection_start({"path": row.get("path") or args.get("path")})
    view["summary"] = f"{len(entries)} {'entry' if len(entries) == 1 else 'entries'}" + (
        " (partial listing)" if row.get("truncated") else ""
    )
    view["blocks"].extend(
        {
            "id": f"entry-{i}",
            "type": "link",
            "target": "file",
            "uri": str(entry.get("path") or ""),
            "label": str(entry.get("name") or ""),
            "detail": str(entry.get("type") or ""),
        }
        for i, entry in enumerate(entries)
        if isinstance(entry, Mapping)
    )
    if row.get("truncated"):
        view["blocks"].append(
            _warning("incomplete", "The entry limit was reached; this listing is incomplete.")
        )
    return view


register_presentation_adapter(
    "fs_list_directory",
    PresentationAdapter(
        _directory, start=_inspection_start, action="List directory", subject="path"
    ),
)
register_presentation_adapter(
    "fs_search_text",
    PresentationAdapter(_search, start=_inspection_start, action="Search text", subject="path"),
)
register_presentation_adapter(
    "fs_glob",
    PresentationAdapter(_glob, start=_inspection_start, action="Find files", subject="path"),
)
register_presentation_adapter(
    "fs_read_file",
    PresentationAdapter(_read, start=_file_start, action="Read", subject="file-link"),
)
