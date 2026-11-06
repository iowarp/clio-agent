"""A subagent result too big to hand back whole is saved to a file (#1844).

``get_task_result`` returns a result whole when its JSON fits
:func:`task_result_inline_chars` (``limits.agent_task_output_digest_chars``, the
same bound that decides when a child's answer is inlined into its parent's wait).
A larger result is written once, exactly, to the reading session's spill folder,
the one oversize shell output uses (:mod:`clio_agent.tools.servers.shell_spill_store`),
so deleting the session deletes it. The caller gets the file's path and size and a
head excerpt instead of the content: the parent's context is not flooded, the model
can read the file with its file tools, and the user gets an "Open full result" link
(:func:`saved_result_block`).

A string result is saved as text; any other result as indented JSON whose parse
equals the result. The file name carries the handle and a digest of the content,
so reading the same result again reuses the file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from clio_agent.gact.agents.agent_task_output_digest import agent_task_output_digest_chars
from clio_agent.runtime import trace
from clio_agent.tools.servers.shell_spill_store import spill_directory, spill_root
from clio_agent.tools.shell_spill_presentation import format_size

#: Typed reason on a result written to a file instead of returned whole.
TASK_RESULT_SAVED_REASON = "task_result_saved_to_file"
#: Typed reason when the file could not be written; only the excerpt is returned.
TASK_RESULT_SAVE_FAILED_REASON = "task_result_save_failed"
#: Characters of the result's head returned beside the file.
RESULT_EXCERPT_CHARS = 2000

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def task_result_inline_chars() -> int:
    """The largest result, in JSON characters, returned whole (``<= 0``: no limit)."""

    return agent_task_output_digest_chars()


def _workspace_root(app: Any, session_id: str) -> Path:
    """The reading session's workspace root, else the folder shell spills fall back to."""

    session = app.state.sessions.get(session_id) if session_id else None
    workspace_id = str(getattr(session, "workspace_id", "") or "")
    workspace = app.state.workspaces.get(workspace_id) if workspace_id else None
    root = str(getattr(workspace, "root_path", "") or "")
    return Path(root) if root else spill_root(Path.cwd())


def _write_once(path: Path, text: str) -> None:
    if path.is_file():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".partial-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def save_task_result(
    app: Any, session_id: str, handle: str, value: Any, encoded: str
) -> dict[str, Any]:
    """Save ``value`` for ``session_id`` and return the fields that replace the result.

    Args:
        app: The GACT app (resolves the session's workspace).
        session_id: The session reading the result; it owns the file.
        handle: The task handle, used in the file name.
        value: The (selected) result.
        encoded: ``value`` as the compact JSON the size bound was measured on.

    Returns:
        ``result`` (``None``), ``result_file`` (path, size, reason) and
        ``result_excerpt`` (the head of ``encoded``), with no ``next_cursor``.
    """

    text, suffix = (
        (value, ".txt")
        if isinstance(value, str)
        else (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), ".json")
    )
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    name = f"task-result-{_UNSAFE.sub('_', handle)[:64]}-{digest}{suffix}"
    path = spill_directory(_workspace_root(app, session_id), session_id=session_id) / name
    size = len(text.encode("utf-8"))
    saved: dict[str, Any] = {
        "path": str(path),
        "bytes": size,
        "chars": len(encoded),
        "media_type": "text/plain" if suffix == ".txt" else "application/json",
        "reason": TASK_RESULT_SAVED_REASON,
    }
    try:
        _write_once(path, text)
    except OSError as exc:
        saved = {**saved, "path": "", "reason": TASK_RESULT_SAVE_FAILED_REASON, "error": str(exc)}
    trace.event(
        "TOOLS",
        "task result saved reason=%s session=%s handle=%s chars=%d path=%s",
        saved["reason"],
        session_id,
        handle,
        len(encoded),
        saved["path"],
    )
    return {
        "result": None,
        "result_file": saved,
        "result_excerpt": encoded[:RESULT_EXCERPT_CHARS],
        "next_cursor": None,
    }


def saved_result_block(row: Mapping[str, Any], index: int) -> dict[str, Any] | None:
    """The "Full result saved" link for a row whose result went to a file."""

    saved = row.get("result_file")
    if not isinstance(saved, Mapping):
        return None
    ok = saved.get("reason") == TASK_RESULT_SAVED_REASON and bool(saved.get("path"))
    size = format_size(int(saved.get("bytes") or 0))
    return {
        "id": f"task-{index}-saved-result",
        "type": "link",
        "target": "file",
        "uri": str(saved.get("path") or "") if ok else "",
        "label": f"Full result {'saved' if ok else 'could not be saved'} ({size})",
        "action_label": "Open full result" if ok else "",
        "text": str(row.get("result_excerpt") or ""),
        "detail": "Beginning of the result",
    }


__all__ = [
    "RESULT_EXCERPT_CHARS",
    "TASK_RESULT_SAVED_REASON",
    "TASK_RESULT_SAVE_FAILED_REASON",
    "save_task_result",
    "saved_result_block",
    "task_result_inline_chars",
]
