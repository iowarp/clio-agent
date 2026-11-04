"""A session's narrowed write territory: the node folder of an isolated workflow run.

A workflow started with ``isolate: true`` gives every node session its own folder
(:mod:`clio_agent.gact.node_folders`). While such a session's tool call runs, the folder
is bound here, and the one territory source both boundaries read narrows with it: the
advisory file policy (:mod:`clio_agent.tools.file_policy`) refuses a write outside it,
and the shell's OS fence (:func:`clio_agent.runtime.sandbox_roots.effective_write_roots`)
grants only it. Reads are not narrowed: a node reads the whole workspace.

Kept free of heavy imports so the file policy can read it on every call.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_ACTIVE_TOOL_WRITE_ROOT: contextvars.ContextVar[str] = contextvars.ContextVar(
    "clio_active_tool_write_root", default=""
)
#: A read-only session's tool call (an explore child, a side conversation): its shell
#: may write only its own scratch folder and temp (``read_only_write_roots``).
_ACTIVE_TOOL_READ_ONLY: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "clio_active_tool_read_only", default=False
)


@contextmanager
def tool_read_only_context(read_only: bool) -> Iterator[None]:
    """Bind whether the calling session is read-only."""

    token = _ACTIVE_TOOL_READ_ONLY.set(bool(read_only))
    try:
        yield
    finally:
        _ACTIVE_TOOL_READ_ONLY.reset(token)


def active_tool_read_only() -> bool:
    """Whether the calling session is read-only (its shell writes only scratch and temp)."""

    return _ACTIVE_TOOL_READ_ONLY.get()


@contextmanager
def tool_write_root_context(root: str | Path | None) -> Iterator[None]:
    """Bind the calling session's node folder (``""`` leaves writes un-narrowed)."""

    token = _ACTIVE_TOOL_WRITE_ROOT.set(str(root or ""))
    try:
        yield
    finally:
        _ACTIVE_TOOL_WRITE_ROOT.reset(token)


def active_tool_write_root() -> Path | None:
    """The bound node folder, resolved, or ``None`` when writes are not narrowed."""

    raw = _ACTIVE_TOOL_WRITE_ROOT.get().strip()
    if not raw:
        return None
    try:
        return Path(raw).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return Path(raw).expanduser().absolute()


def narrowed_write_roots(node: Path, workspace: Path | None) -> tuple[Path, ...]:
    """Where a node session may write: its folder, plus the scratch every process needs.

    The scratch is the session's own scratch folder (its ``TMPDIR``), the platform temp
    dir unless ``paths.allow_system_temp`` is off, and the workspace's own generated
    state, cache and document scratch (``<workspace>/.tmp``). None of it is another
    node's output.
    """

    from clio_agent.runtime.session_scratch import (  # noqa: PLC0415 - avoid import cycle
        active_scratch_dir,
        system_temp_roots,
    )

    roots = [node, *(root.resolve() for root in system_temp_roots())]
    # The session's own scratch folder (TD56). This tuple is the file policy's narrowed
    # ``write_roots``, which the shell fence's narrowed branch
    # (:func:`clio_agent.runtime.sandbox_roots.effective_write_roots`) starts from.
    if (scratch := active_scratch_dir()) is not None:
        roots.append(scratch)
    if workspace is not None:
        from clio_agent import paths  # noqa: PLC0415 - avoid import cycle at module load

        roots += [
            paths.workspace_state_dir(workspace).resolve(),
            paths.workspace_cache_dir(workspace).resolve(),
            (workspace / ".tmp").resolve(),
        ]
    return tuple(dict.fromkeys(roots))


__all__ = [
    "active_tool_read_only",
    "active_tool_write_root",
    "narrowed_write_roots",
    "tool_read_only_context",
    "tool_write_root_context",
]
