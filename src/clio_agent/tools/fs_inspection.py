"""Bounded, policy-checked repository discovery without shell execution."""

from __future__ import annotations

import os
import stat
import time
from collections import deque
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from clio_agent.tools.file_policy import (
    FileAccessPolicy,
    FilePolicyError,
    validate_non_empty_string,
    validate_positive_int,
)

_MAX_ENTRIES = 10_000
_MAX_FILES = 1_000
_MAX_FILE_BYTES = 256 * 1024
_MAX_TOTAL_BYTES = 8 * 1024 * 1024
_MAX_SECONDS = 3.0
_MAX_OUTPUT_CHARS = 32_000
_SKIP_DIRS = frozenset(
    {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache", ".ruff_cache"}
)


def _is_link(entry: os.DirEntry[str] | Path) -> bool:
    attributes = entry.stat(follow_symlinks=False)
    return stat.S_ISLNK(attributes.st_mode) or bool(
        getattr(attributes, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def list_directory(path: str, limit: int = 200) -> dict[str, Any]:
    """List a directory's immediate entries without following links or writing files."""
    validate_non_empty_string(path, field="path")
    validate_positive_int(limit, field="limit", max_value=1_000)
    directory = FileAccessPolicy.from_env().validate_read_directory(path)
    entries: list[dict[str, str]] = []
    truncated = False
    with os.scandir(directory) as iterator:
        for entry in iterator:
            if len(entries) == limit:
                truncated = True
                break
            kind = (
                "link"
                if _is_link(entry)
                else ("directory" if entry.is_dir(follow_symlinks=False) else "file")
            )
            entries.append({"name": entry.name, "path": str(Path(entry.path)), "type": kind})
    return {
        "path": str(directory),
        "entries": sorted(entries, key=lambda item: item["name"]),
        "truncated": truncated,
        "limit": limit,
    }


def search_text(
    path: str,
    query: str,
    file_glob: str = "*",
    limit: int = 100,
    context_lines: int = 2,
    case_sensitive: bool = True,
) -> dict[str, Any]:
    """Find literal text with line-numbered context in bounded approved local files."""
    validate_non_empty_string(query, field="query")
    validate_non_empty_string(path, field="path")
    validate_non_empty_string(file_glob, field="file_glob")
    validate_positive_int(limit, field="limit", max_value=200)
    if len(query) > 1_000 or not 0 <= context_lines <= 50:
        raise ValueError("query is limited to 1,000 characters and context_lines to 0–50")
    policy = FileAccessPolicy.from_env()
    single_file: Path | None = None
    try:
        root = policy.validate_read_directory(path)
    except FilePolicyError as exc:
        if exc.code != "not_a_directory":
            raise
        single_file = policy.validate_read(path)
        root = single_file.parent
    pending = deque([root])
    matches: list[dict[str, Any]] = []
    visited = scanned = total_bytes = skipped = output_chars = 0
    incomplete_files: list[str] = []
    reason = ""
    started = time.monotonic()
    needle = query if case_sensitive else query.casefold()
    while pending and not reason:
        directory = pending.popleft()
        scanner = os.scandir(directory) if single_file is None else nullcontext(iter([single_file]))
        with scanner as iterator:
            for entry in iterator:
                visited += 1
                if (
                    visited > _MAX_ENTRIES
                    or scanned >= _MAX_FILES
                    or total_bytes >= _MAX_TOTAL_BYTES
                ):
                    reason = "scan_limit"
                    break
                if time.monotonic() - started > _MAX_SECONDS:
                    reason = "time_limit"
                    break
                if _is_link(entry):
                    skipped += 1
                    continue
                candidate = Path(entry.path) if isinstance(entry, os.DirEntry) else entry
                mode = entry.stat(follow_symlinks=False).st_mode
                if stat.S_ISDIR(mode):
                    if entry.name not in _SKIP_DIRS:
                        pending.append(policy.validate_read_directory(str(candidate)))
                    continue
                if not stat.S_ISREG(mode) or not candidate.relative_to(root).match(file_glob):
                    continue
                try:
                    approved = policy.validate_read(str(candidate))
                    remaining = min(_MAX_FILE_BYTES, _MAX_TOTAL_BYTES - total_bytes)
                    with approved.open("rb") as stream:
                        raw = stream.read(remaining + 1)
                except (FilePolicyError, OSError):
                    skipped += 1
                    continue
                scanned += 1
                total_bytes += min(len(raw), remaining)
                if len(raw) > remaining:
                    incomplete_files.append(str(candidate))
                    raw = raw[:remaining]
                if b"\0" in raw:
                    skipped += 1
                    continue
                try:
                    lines = raw.decode("utf-8").splitlines()
                except UnicodeDecodeError:
                    skipped += 1
                    continue
                for index, line in enumerate(lines):
                    if needle not in (line if case_sensitive else line.casefold()):
                        continue
                    context: list[dict[str, Any]] = [
                        {"line": number + 1, "text": lines[number][:500]}
                        for number in range(
                            max(0, index - context_lines),
                            min(len(lines), index + context_lines + 1),
                        )
                    ]
                    size = (
                        len(str(candidate))
                        + len(line[:500])
                        + sum(len(item["text"]) for item in context)
                    )
                    if output_chars + size > _MAX_OUTPUT_CHARS:
                        reason = "output_limit"
                        break
                    matches.append(
                        {
                            "path": str(candidate),
                            "line": index + 1,
                            "text": line[:500],
                            "context": context,
                        }
                    )
                    output_chars += size
                    if len(matches) == limit:
                        reason = "result_limit"
                        break
                if reason:
                    break
    return {
        "path": str(single_file or root),
        "query": query,
        "matches": matches,
        "files_scanned": scanned,
        "entries_visited": visited,
        "bytes_scanned": total_bytes,
        "skipped": skipped,
        "truncated": bool(reason or incomplete_files),
        "stop_reason": reason,
        "incomplete_files": incomplete_files,
    }
