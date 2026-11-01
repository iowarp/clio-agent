"""File search and glob on ripgrep (the ``fs_search_text`` and ``fs_glob`` tools' bodies).

Both validate the requested path through the file policy (the same allowed roots,
no symlinks), then run :func:`clio_agent.runtime.ripgrep.ripgrep_executable` inside it.
ripgrep does not follow links, skips binary files, and honours ``.gitignore`` and
hidden-file rules, so dependency and Git folders stay out of results. Every result is
text for the model plus a structured value for programs, and states its truncation.

Search modes:

- ``files`` (the default): the files that match, newest first, with match counts;
- ``content``: matching lines with their numbers and ``context_lines`` around them;
- ``count``: total matches and files.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Literal

from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from mcp.types import TextContent

from clio_agent.runtime.ripgrep import RipgrepMissing, ripgrep_executable
from clio_agent.tools.file_policy import FileAccessPolicy, validate_non_empty_string
from clio_agent.tools.mcp_result_projection import (
    model_tool_result_chars,
    transcript_tool_result_chars,
)

__all__ = ["SearchMode", "glob_files", "search_files"]

SearchMode = Literal["files", "content", "count"]
_TIMEOUT_S = 30.0
_LINE_CHARS = 500


def _budget() -> int:
    return min(model_tool_result_chars(), transcript_tool_result_chars()) - 400


def _target(path: str) -> Path:
    validate_non_empty_string(path, field="path")
    policy = FileAccessPolicy.from_env()
    candidate = Path(path)
    if candidate.is_dir():
        return policy.validate_read_directory(path)
    return policy.validate_read(path, field="path")


def _run(arguments: list[str], cwd: Path) -> tuple[list[str], bool]:
    """Run ripgrep; return its output lines and whether it timed out."""
    try:
        executable = str(ripgrep_executable())
    except RipgrepMissing as exc:
        raise ToolError(str(exc)) from exc
    try:
        done = subprocess.run(
            [executable, *arguments],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        out = (
            exc.stdout
            if isinstance(exc.stdout, str)
            else (exc.stdout or b"").decode(errors="replace")
        )
        return out.splitlines(), True
    if done.returncode not in (0, 1):  # 1 is "no match"
        raise ToolError(
            f"fs_search_failed: ripgrep exited {done.returncode}: {done.stderr.strip()[:500]}"
        )
    return done.stdout.splitlines(), False


def _result(text: str, structured: dict[str, Any]) -> ToolResult:
    return ToolResult(content=[TextContent(type="text", text=text)], structured_content=structured)


def _mtime(path: str) -> float:
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return 0.0


def _query_arguments(query: str, regex: bool, case_sensitive: bool, file_glob: str) -> list[str]:
    arguments = [] if regex else ["--fixed-strings"]
    arguments.append("--case-sensitive" if case_sensitive else "--ignore-case")
    if file_glob and file_glob != "*":
        arguments += ["--glob", file_glob]
    return [*arguments, "--regexp", query]


def search_files(
    path: str,
    query: str,
    *,
    mode: SearchMode = "files",
    regex: bool = False,
    file_glob: str = "",
    context_lines: int = 0,
    case_sensitive: bool = True,
    limit: int = 100,
) -> ToolResult:
    """Search ``query`` under ``path``; see the module docstring for the modes."""
    validate_non_empty_string(query, field="query")
    target = _target(path)
    cwd = target if target.is_dir() else target.parent
    base = _query_arguments(query, regex, case_sensitive, file_glob)
    head = {"path": str(target), "query": query, "mode": mode, "regex": regex, "glob": file_glob}
    if mode in ("files", "count"):
        rows, timed_out = _run(
            ["--count-matches", "--with-filename", *base, "--", str(target)], cwd
        )
        counts = []
        for row in rows:
            name, _, count = row.rpartition(":")
            if name and count.isdigit():
                counts.append((name, int(count)))
        total = sum(count for _, count in counts)
        if mode == "count":
            text = f"{total:,} matches in {len(counts):,} files under {target}"
            if timed_out:
                text += " (search stopped at its time limit; counts are partial)"
            return _result(
                text,
                {
                    **head,
                    "total_matches": total,
                    "total_files": len(counts),
                    "truncated": timed_out,
                },
            )
        counts.sort(key=lambda item: _mtime(item[0]), reverse=True)
        shown = counts[:limit]
        truncated = timed_out or len(counts) > len(shown)
        lines = [f"{len(counts):,} files match under {target} (newest first):"]
        lines += [f"{name}  ({count})" for name, count in shown]
        if truncated:
            lines.append(_more(len(counts) - len(shown), "files", timed_out))
        files = [{"path": name, "matches": count} for name, count in shown]
        return _result(
            "\n".join(lines),
            {**head, "files": files, "total_files": len(counts), "truncated": truncated},
        )
    context = max(0, min(int(context_lines), 50))
    rows, timed_out = _run(["--json", "--context", str(context), *base, "--", str(target)], cwd)
    return _content_result(rows, head, limit, timed_out)


def _more(hidden: int, unit: str, timed_out: bool) -> str:
    if timed_out:
        return "(search stopped at its time limit; results are partial)"
    return f"(+{hidden:,} more {unit}; narrow the path or glob, or raise limit)"


def _content_result(
    rows: list[str], head: dict[str, Any], limit: int, timed_out: bool
) -> ToolResult:
    matches: list[dict[str, Any]] = []
    lines: list[str] = []
    used, budget, truncated, total = 0, _budget(), timed_out, 0
    pending_context: list[dict[str, Any]] = []
    for row in rows:
        try:
            event = json.loads(row)
        except json.JSONDecodeError:
            continue
        kind, data = event.get("type"), event.get("data") or {}
        if kind not in ("match", "context"):
            continue
        name = (data.get("path") or {}).get("text", "")
        number = data.get("line_number")
        text = ((data.get("lines") or {}).get("text") or "").rstrip("\n")[:_LINE_CHARS]
        if kind == "context":
            pending_context.append({"path": name, "line": number, "text": text})
            continue
        total += 1
        rendered = [f"{c['path']}-{c['line']}- {c['text']}" for c in pending_context]
        rendered.append(f"{name}:{number}: {text}")
        size = sum(len(r) + 1 for r in rendered)
        if len(matches) >= limit or (matches and used + size > budget):
            truncated = True
            pending_context = []
            continue
        matches.append({"path": name, "line": number, "text": text, "context": pending_context})
        lines += rendered
        used += size
        pending_context = []
    header = f"{total:,} matching lines under {head['path']}"
    if truncated:
        header += (
            f"; showing {len(matches):,}"
            if not timed_out
            else " (search stopped at its time limit)"
        )
    text = "\n".join([header, *lines])
    structured = {**head, "matches": matches, "total_matches": total, "truncated": truncated}
    return _result(text, structured)


def glob_files(pattern: str, path: str, limit: int = 200) -> ToolResult:
    """Files under ``path`` matching ``pattern`` (``**`` crosses folders), newest first."""
    validate_non_empty_string(pattern, field="pattern")
    root = FileAccessPolicy.from_env().validate_read_directory(path)
    rows, timed_out = _run(["--files", "--glob", pattern, "--", str(root)], root)
    found = sorted(rows, key=_mtime, reverse=True)
    shown = found[:limit]
    truncated = timed_out or len(found) > len(shown)
    lines = [f"{len(found):,} files match {pattern} under {root} (newest first):", *shown]
    if truncated:
        lines.append(_more(len(found) - len(shown), "files", timed_out))
    structured = {
        "path": str(root),
        "pattern": pattern,
        "files": shown,
        "total_files": len(found),
        "truncated": truncated,
    }
    return _result("\n".join(lines), structured)
