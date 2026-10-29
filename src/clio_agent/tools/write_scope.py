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
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_ACTIVE_TOOL_WRITE_ROOT: contextvars.ContextVar[str] = contextvars.ContextVar(
    "clio_active_tool_write_root", default=""
)


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

    The scratch is the platform temp dir and the workspace's own generated state, cache
    and document scratch (``<workspace>/.tmp``), which the shell's ``TMPDIR`` points at.
    None of it is another node's output.
    """

    roots = [node, Path(tempfile.gettempdir()).resolve()]
    if workspace is not None:
        from clio_agent import paths  # noqa: PLC0415 - avoid import cycle at module load

        roots += [
            paths.workspace_state_dir(workspace).resolve(),
            paths.workspace_cache_dir(workspace).resolve(),
            (workspace / ".tmp").resolve(),
        ]
    return tuple(dict.fromkeys(roots))


__all__ = ["active_tool_write_root", "narrowed_write_roots", "tool_write_root_context"]
