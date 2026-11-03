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

``files`` and ``content`` results continue: ``offset`` skips the files or matching lines
already seen. A content result holds up to ``limit`` matches that fit the room one result
has in the model's context window (:mod:`clio_agent.tools.result_room`); when matches
remain, its header names the offset to continue from, so no match is dropped unseen.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Literal

from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from mcp.types import TextContent

from clio_agent import conf
from clio_agent.runtime.ripgrep import RipgrepMissing, ripgrep_executable
from clio_agent.tools.file_policy import FileAccessPolicy, validate_non_empty_string
from clio_agent.tools.result_room import ResultRoom, result_room

__all__ = ["SearchMode", "glob_files", "search_files"]

SearchMode = Literal["files", "content", "count"]


def search_timeout_s() -> float:
    """Seconds one ripgrep run may take; a slower search returns what it found so far.

    Raises:
        ValueError: The configured timeout is not positive.
    """
    value = conf.resolve(
        "fs.search_timeout_s", env="CLIO_FS_SEARCH_TIMEOUT_S", default=30.0, cast=conf.as_float
    )
    if not value > 0:
        raise ValueError(f"fs.search_timeout_s must be positive, not {value}")
    return value


def search_line_chars() -> int:
    """Characters of one matching line a content search returns.

    Raises:
        ValueError: The configured length is below 1.
    """
    value = conf.resolve(
        "fs.search_line_chars", env="CLIO_FS_SEARCH_LINE_CHARS", default=500, cast=conf.as_int
    )
    if value < 1:
        raise ValueError(f"fs.search_line_chars must be at least 1, not {value}")
    return value


#: Characters kept for a content result's header.
_HEADER_RESERVE = 400


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
            timeout=search_timeout_s(),
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
    offset: int = 0,
) -> ToolResult:
    """Search ``query`` under ``path``; see the module docstring for the modes."""
    validate_non_empty_string(query, field="query")
    offset = max(0, int(offset))
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
            if not total:
                text += f". {SKIPPED_NOTE}"
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
        shown = counts[offset : offset + limit]
        following = offset + len(shown)
        truncated = timed_out or len(counts) > following
        lines = [f"{len(counts):,} files match under {target} (newest first):"]
        if not counts:
            lines = [f"0 files match under {target}. {SKIPPED_NOTE}"]
        elif offset:
            lines[0] = lines[0][:-1] + f"; from file {offset + 1:,}:"
        lines += [f"{name}  ({count})" for name, count in shown]
        if truncated:
            lines.append(_more(len(counts) - following, "files", timed_out, following))
        files = [{"path": name, "matches": count} for name, count in shown]
        structured = {**head, "files": files, "total_files": len(counts), "truncated": truncated}
        return _result("\n".join(lines), {**structured, **_next(truncated, following)})
    context = max(0, min(int(context_lines), 50))
    rows, timed_out = _run(["--json", "--context", str(context), *base, "--", str(target)], cwd)
    return _content_result(rows, head, limit, timed_out, offset)


#: What an empty result cannot show: the files a search or glob skipped on purpose.
SKIPPED_NOTE = (
    "Ignored (.gitignore) and hidden files and folders are skipped; if what you want may be "
    "in one, search that folder directly, or use fs_list_directory."
)


def _more(hidden: int, unit: str, timed_out: bool, following: int | None = None) -> str:
    if timed_out:
        return "(search stopped at its time limit; results are partial)"
    if following is None:
        return f"(+{hidden:,} more {unit}; narrow the path or glob, or raise limit)"
    return f"(+{hidden:,} more {unit}; continue with offset={following}, or narrow the search)"


def _next(truncated: bool, following: int) -> dict[str, int | None]:
    return {"next_offset": following if truncated else None}


def _content_result(
    rows: list[str], head: dict[str, Any], limit: int, timed_out: bool, offset: int = 0
) -> ToolResult:
    room = result_room()
    found: list[tuple[dict[str, Any], list[str]]] = []
    pending_context: list[dict[str, Any]] = []
    line_chars, total = search_line_chars(), 0
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
        text = ((data.get("lines") or {}).get("text") or "").rstrip("\n")
        if len(text) > line_chars:
            text = f"{text[:line_chars]}… [line cut: {len(text):,} characters]"
        if kind == "context":
            pending_context.append({"path": name, "line": number, "text": text})
            continue
        total += 1
        if total > offset and len(found) < limit:
            rendered = [f"{c['path']}-{c['line']}- {c['text']}" for c in pending_context]
            rendered.append(f"{name}:{number}: {text}")
            match = {"path": name, "line": number, "text": text, "context": pending_context}
            found.append((match, rendered))
        pending_context = []
    shown, used, budget = 0, 0, room.chars - _HEADER_RESERVE
    for _, rendered in found:
        size = sum(len(r) + 1 for r in rendered)
        if shown and used + size > budget:
            break
        shown, used = shown + 1, used + size
    by_room = shown < len(found)
    text = _content_text(found, shown, head, total, offset, timed_out, room, by_room)
    # The model's tokenizer can count more than four characters a token: keep what fits.
    while shown > 1 and not room.fits(text):
        keep = shown * room.tokens // max(1, room.measure(text))
        shown, by_room = max(1, min(shown - 1, keep)), True
        text = _content_text(found, shown, head, total, offset, timed_out, room, by_room)
    matches = [match for match, _ in found[:shown]]
    truncated = timed_out or total > offset + shown
    structured = {**head, "matches": matches, "total_matches": total, "truncated": truncated}
    return _result(text, {**structured, **_next(total > offset + shown, offset + shown)})


def _content_text(
    found: list[tuple[dict[str, Any], list[str]]],
    shown: int,
    head: dict[str, Any],
    total: int,
    offset: int,
    timed_out: bool,
    room: ResultRoom,
    by_room: bool,
) -> str:
    header = f"{total:,} matching lines under {head['path']}"
    if not total:
        header += f". {SKIPPED_NOTE}"
    following = offset + shown
    if offset or following < total:
        header += f"; showing {offset + 1:,}-{following:,}" if shown else "; showing none"
    if following < total:
        header += f"; continue with offset={following}"
        if by_room:
            header += f" (one result holds about {room.tokens:,} tokens)"
    if timed_out:
        header += " (search stopped at its time limit; matches are partial)"
    return "\n".join([header, *(line for _, rendered in found[:shown] for line in rendered)])


def glob_files(pattern: str, path: str, limit: int = 200) -> ToolResult:
    """Files under ``path`` matching ``pattern`` (``**`` crosses folders), newest first."""
    validate_non_empty_string(pattern, field="pattern")
    root = FileAccessPolicy.from_env().validate_read_directory(path)
    rows, timed_out = _run(["--files", "--glob", pattern, "--", str(root)], root)
    found = sorted(rows, key=_mtime, reverse=True)
    shown = found[:limit]
    truncated = timed_out or len(found) > len(shown)
    lines = [f"{len(found):,} files match {pattern} under {root} (newest first):", *shown]
    if not found:
        lines = [f"0 files match {pattern} under {root}. {SKIPPED_NOTE}"]
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
