"""Local HTML files in the shared browser: ``file:`` pages inside what the session may read.

An HTML file the person opens from Files, or the agent opens by path, loads in the shared
browser as a ``file://`` page, so its scripts and CDN libraries run in a real Chromium with
nothing rewritten. The host gate allows a ``file:`` navigation only when the file lies
inside the session's readable roots: the roots CLIO's file policy gives the session's
tools (its workspace, the workspace's granted roots, its scratch folder and the configured
allowed roots). Every other local path is refused, as every other scheme is.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from clio_agent.file_urls import file_url_path

__all__ = ["file_url", "local_path", "readable_roots", "refusal", "url_for"]


def local_path(url: str) -> Path | None:
    """The local path a ``file:`` URL names (a Windows drive or UNC share included), or
    ``None`` for another scheme or a host this OS cannot open."""

    return file_url_path(url)


def file_url(path: Path) -> str:
    """The ``file://`` URL of an absolute path."""

    return path.resolve(strict=False).as_uri()


def readable_roots(app: Any, session_id: str) -> tuple[Path, ...]:
    """The roots the session's tools may read, as the file policy computes them for it."""

    from clio_agent.gact.artifacts.minting import (  # noqa: PLC0415
        _session_workspace_id,
        _workspace_root,
    )
    from clio_agent.runtime.session_scratch import session_scratch_dir  # noqa: PLC0415
    from clio_agent.tools.execution import tool_workspace_context  # noqa: PLC0415
    from clio_agent.tools.file_policy import FileAccessPolicy  # noqa: PLC0415

    workspace = _workspace_root(app, _session_workspace_id(app, session_id)) if app else None
    with tool_workspace_context(workspace):
        roots = list(FileAccessPolicy.from_env().allowed_roots)
    scratch = session_scratch_dir(session_id, create=False)
    if scratch is not None:
        roots.append(scratch.resolve(strict=False))
    return tuple(roots)


def refusal(app: Any, session_id: str, url: str) -> str:
    """Why the session may not open ``url`` as a local page, or ``""`` when it may."""

    path = local_path(url)
    if path is None:
        return "browser_file_url_invalid"
    roots = readable_roots(app, session_id)
    # Resolving a UNC path connects to its server: only one a readable root lives on.
    if path.drive.startswith(("\\\\", "//")) and not any(
        path.drive.lower() == root.drive.lower() for root in roots
    ):
        return "browser_file_outside_roots"
    resolved = path.resolve(strict=False)
    for root in roots:
        if resolved == root or root in resolved.parents:
            return ""
    return "browser_file_outside_roots"


def url_for(app: Any, session_id: str, *, path: str = "", artifact_id: str = "") -> str:
    """The ``file://`` URL of a workspace file or an artifact version's file.

    ``path`` is absolute or relative to the session's workspace. Raises ``ValueError`` with
    a typed reason when neither names a file.
    """

    from clio_agent.gact.artifacts.minting import (  # noqa: PLC0415
        _session_workspace_id,
        _workspace_root,
    )

    if artifact_id:
        from clio_agent.gact.artifacts.registry import get_registry  # noqa: PLC0415

        found = get_registry(app).get_by_artifact_id(artifact_id)
        if found is None or not found[1].path:
            raise ValueError("browser_file_artifact_unknown: no file is recorded for that artifact")
        path = found[1].path
    if not path:
        raise ValueError("browser_file_url_invalid: name a path or an artifact")
    candidate = Path(path)
    if not candidate.is_absolute():
        root = _workspace_root(app, _session_workspace_id(app, session_id))
        if root is None:
            raise ValueError("browser_file_url_invalid: the conversation has no workspace root")
        candidate = root / candidate
    if not candidate.is_file():
        raise ValueError(f"browser_file_missing: {candidate.name} is not on disk")
    return file_url(candidate)
