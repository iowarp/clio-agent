"""The agent's file writes: Write (whole file) and Edit (exact string replacement).

Both change the file on disk through :func:`clio_agent.tools.fs_write.write_text_with_policy`
and return the record of what changed: the path, whether the file was created, the
sha256 of the bytes before and after, a unified diff and its line counts. Neither tool
ever guesses: an Edit whose ``old_string`` is missing or ambiguous is refused with a
typed error, never fuzzy-matched.

The previous content that Undo needs is captured by the caller at the call boundary
(:func:`capture_previous_content`), not echoed in this result: the result is what the
model reads, and an old copy of the file would cost its context on every write.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clio_agent.tools.file_diff import unified_file_diff
from clio_agent.tools.file_policy import (
    FilePolicyError,
    validate_non_empty_string,
    validate_read_path,
    validate_write_path,
)
from clio_agent.tools.fs_write import write_text_with_policy

#: The namespaced tools that write a file through this module.
FILE_WRITE_TOOLS = frozenset({"fs_write_file", "fs_edit_file"})

#: Largest previous content kept for Undo. Over it, the change is recorded with the
#: typed reason :data:`UNDO_PREVIOUS_TOO_LARGE` instead of a copy.
UNDO_CAPTURE_MAX_BYTES = 256 * 1024

#: Typed reasons a recorded change cannot be undone.
UNDO_PREVIOUS_TOO_LARGE = "previous_content_too_large"
UNDO_PREVIOUS_NOT_TEXT = "previous_content_not_text"
UNDO_PREVIOUS_UNREADABLE = "previous_content_unreadable"
UNDO_CHANGED_BEFORE_WRITE = "file_changed_before_write"
UNDO_NOT_CAPTURED = "previous_content_not_captured"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _error(code: str, message: str, path: str, **details: Any) -> dict[str, Any]:
    """A typed refusal the model reads as the tool result (nothing was written)."""

    return {
        "ok": False,
        "path": path,
        "error": {"type": "file_edit", "code": code, "message": message, "details": details},
    }


def _diff_counts(diff: str) -> tuple[int, int]:
    lines = diff.splitlines()
    added = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
    return added, removed


def _write_change(path: Path, before: bytes | None, after: str) -> dict[str, Any]:
    """Write ``after`` and return the change record."""

    written = write_text_with_policy(str(path), after)
    before_text = before.decode("utf-8", errors="replace") if before is not None else ""
    diff = unified_file_diff(before_text, after, path.name)
    added, removed = _diff_counts(diff)
    return {
        **written,
        "created": before is None,
        "sha256_before": _sha256(before) if before is not None else "",
        "unified_diff": diff,
        "lines_added": added,
        "lines_removed": removed,
    }


def write_file(filepath: str, content: str) -> dict[str, Any]:
    """Create ``filepath`` or replace its whole content with ``content``."""

    validate_non_empty_string(filepath, field="filepath")
    # A new file's missing parent folders are created inside the allowed roots.
    path = Path(validate_write_path(filepath, field="filepath", create_parent=True))
    if path.is_dir():
        return _error("not_a_file", f"{path} is a folder, not a file", str(path))
    before = (
        Path(validate_read_path(str(path), field="filepath")).read_bytes()
        if path.exists()
        else None
    )
    body = content if isinstance(content, str) else str(content)
    return _write_change(path, before, body)


def edit_file(
    filepath: str, old_string: str, new_string: str, replace_all: bool = False
) -> dict[str, Any]:
    """Replace ``old_string`` with ``new_string`` in an existing text file, exactly."""

    validate_non_empty_string(filepath, field="filepath")
    path = Path(validate_write_path(filepath, field="filepath"))
    if not path.exists():
        return _error(
            "file_not_found",
            f"{path} does not exist; fs_edit_file changes an existing file "
            "(fs_write_file creates one)",
            str(path),
        )
    if not path.is_file():
        return _error("not_a_file", f"{path} is not a file", str(path))
    before = Path(validate_read_path(str(path), field="filepath")).read_bytes()
    try:
        text = before.decode("utf-8")
    except UnicodeDecodeError:
        return _error("file_not_text", f"{path} is not UTF-8 text", str(path))
    if not old_string:
        return _error("old_string_empty", "old_string must not be empty", str(path))
    if old_string == new_string:
        return _error("no_change", "old_string and new_string are identical", str(path))
    count = text.count(old_string)
    if count == 0:
        return _error("old_string_not_found", f"old_string was not found in {path}", str(path))
    if count > 1 and not replace_all:
        return _error(
            "old_string_not_unique",
            f"old_string occurs {count} times in {path}; include more surrounding text to "
            "make it unique, or set replace_all to replace every occurrence",
            str(path),
            occurrences=count,
        )
    after = (
        text.replace(old_string, new_string)
        if replace_all
        else text.replace(old_string, new_string, 1)
    )
    return {**_write_change(path, before, after), "replacements": count if replace_all else 1}


@dataclass(frozen=True)
class PreviousContent:
    """The target file as it was just before a write, captured for Undo.

    ``content`` is the decoded previous text, or ``None`` when the file did not exist
    or could not be kept (``reason`` then names why). ``sha256`` is the hash of the
    previous bytes (empty when the file did not exist); the recorder compares it with
    the tool's ``sha256_before`` so a file changed between capture and write is never
    restored from a stale copy.
    """

    path: str
    existed: bool
    sha256: str = ""
    content: str | None = None
    reason: str = ""


def capture_previous_content(filepath: Any) -> PreviousContent | None:
    """Read the file a write tool is about to change, bounded for Undo.

    Returns ``None`` when the path does not pass the write policy: the tool call will
    be refused for the same reason, so there is nothing to undo.
    """

    if not isinstance(filepath, str) or not filepath.strip():
        return None
    try:
        path = Path(validate_write_path(filepath, field="filepath"))
    except FilePolicyError as exc:
        # A missing parent folder: the write creates it, so no file existed there.
        # Any other refusal means the call is refused too, or a folder grant is still
        # pending; the recorder then names the change ``previous_content_not_captured``.
        return (
            PreviousContent(str(exc.path or filepath), existed=False)
            if (exc.code == "parent_not_found")
            else None
        )
    if not path.exists():
        return PreviousContent(str(path), existed=False)
    try:
        source = Path(validate_read_path(str(path), field="filepath"))
        if source.stat().st_size > UNDO_CAPTURE_MAX_BYTES:
            with source.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            return PreviousContent(str(path), True, digest, None, UNDO_PREVIOUS_TOO_LARGE)
        data = source.read_bytes()
    except (OSError, ValueError):
        return PreviousContent(str(path), True, "", None, UNDO_PREVIOUS_UNREADABLE)
    try:
        return PreviousContent(str(path), True, _sha256(data), data.decode("utf-8"))
    except UnicodeDecodeError:
        return PreviousContent(str(path), True, _sha256(data), None, UNDO_PREVIOUS_NOT_TEXT)


__all__ = [
    "FILE_WRITE_TOOLS",
    "UNDO_CAPTURE_MAX_BYTES",
    "PreviousContent",
    "capture_previous_content",
    "edit_file",
    "write_file",
]
