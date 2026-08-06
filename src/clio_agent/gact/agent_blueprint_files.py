"""Agent-blueprint file-tree listing + raw read (iowarp/clio-agent#1192).

Owner module for the read-only file-browsing surface a blueprint's window
consumes: ``GET /v1/agent-blueprints/{id}/files`` (flat recursive listing)
and ``GET /v1/agent-blueprints/{id}/files/read`` (raw content). Mirrors the
conventions of the workspace file-browsing routes
(:mod:`clio_agent.gact.routes.workspaces`) -- capped walk, skip cost-walking
dirs, text served decoded as ``text/plain``, binary served raw with its real
content type -- but scoped to an agent-blueprint root, which may be resolved
either from the installed/discovery catalog OR (session-scoped) from a
session's PATH-ACTIVATED blueprint (``metadata.active_agent_blueprint_path``)
when that active blueprint's own id matches the requested one -- the demo
case where a blueprint is activated by on-disk path rather than by installed
id (e.g. ``earthscope-flat``).

The route handlers in :mod:`clio_agent.gact.routes.blueprints` stay thin
call sites into this module (no accretion, #774/#775).
"""

from __future__ import annotations

import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.gact.agent_blueprints import (
    discover_agent_blueprints,
    parse_agent_blueprint_root,
)
from clio_agent.gact.agents.resolution import (
    _runtime_active_agent_blueprint_path,
    _runtime_workspace_catalog_cwd,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

# Files served as decoded text/plain even though their MIME type is not under
# the ``text/`` tree -- byte-identical set to
# ``routes.workspaces._TEXTUAL_WORKSPACE_MIME_TYPES``.
_TEXTUAL_BLUEPRINT_MIME_TYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "application/javascript",
        "application/x-yaml",
        "application/yaml",
        "application/x-sh",
        "application/toml",
    }
)

# A blueprint root is a curated, small tree (AGENT.md + experts/*.md + ...),
# but the cap + skip-dirs stay defensive so a workspace-scoped install that
# still carries VCS metadata (a raw marketplace clone) can never turn this
# into an unbounded walk.
_BLUEPRINT_FILE_LIMIT = 5000
_BLUEPRINT_FILE_SKIP_DIRS = frozenset(
    {
        ".git",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "node_modules",
        ".venv",
        "venv",
    }
)


class BlueprintPathEscapesRootError(Exception):
    """Raised when a requested relative path resolves outside the blueprint root."""


def resolve_agent_blueprint_root(
    app: "FastAPI",
    blueprint_id: str,
    *,
    workspace_id: str = "",
    session_id: str = "",
) -> Path | None:
    """Resolve ``blueprint_id`` to its on-disk root directory, or ``None``.

    Session-scoped resolution is tried FIRST: when ``session_id`` names a
    session whose active blueprint was PATH-activated
    (``metadata.active_agent_blueprint_path``) and that active blueprint's own
    id matches ``blueprint_id``, its root directory wins --
    :func:`~clio_agent.gact.agent_blueprints.parse_agent_blueprint_root`
    normalizes both an ``AGENT.md`` file path and a directory path to the
    same ``.root`` (the directory containing ``AGENT.md``), so this resolves
    correctly whichever shape the activation stored. Otherwise falls back to
    the installed/discovery catalog (workspace + global scopes -- the same
    resolution ``GET /v1/agent-blueprints/{id}`` uses).
    """

    if session_id:
        active_path = _runtime_active_agent_blueprint_path(app, session_id)
        if active_path is not None:
            parsed = parse_agent_blueprint_root(active_path, scope="session")
            if parsed.id == blueprint_id:
                return parsed.root
    cwd = _runtime_workspace_catalog_cwd(app, workspace_id=workspace_id, session_id=session_id)
    for blueprint in discover_agent_blueprints(cwd=cwd):
        if blueprint.id == blueprint_id:
            return blueprint.root
    return None


def list_blueprint_files(root: Path) -> list[dict[str, Any]]:
    """Flat recursive listing of ``root``: ``{"path", "type", "size", "modified"}``.

    Mirrors ``routes.workspaces.list_workspace_files``'s conventions: paths
    are relative to ``root``, ``type`` is ``"file"`` or ``"dir"``, dirs walk
    depth-first, cost-walking dirs are skipped, and the walk is hard-capped so
    a runaway tree can never block the caller.
    """

    if not root.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    cap = _BLUEPRINT_FILE_LIMIT

    def _walk(current: Path) -> None:
        nonlocal cap
        if cap <= 0:
            return
        try:
            children = sorted(current.iterdir(), key=lambda p: p.name)
        except (OSError, PermissionError):
            return
        for child in children:
            if cap <= 0:
                return
            if child.name in _BLUEPRINT_FILE_SKIP_DIRS:
                continue
            try:
                is_dir = child.is_dir()
            except OSError:
                # Unreadable entry (broken symlink, restricted socket) -- skip
                # rather than abort the whole walk.
                continue
            # Forward-slash always, regardless of host OS: this is a fresh wire
            # contract (not constrained by the workspace route's os.sep-native
            # legacy behavior) and gact-tui builds its file tree by splitting
            # on "/" -- an OS-native backslash on Windows would silently break
            # nesting there.
            entry: dict[str, Any] = {
                "path": child.relative_to(root).as_posix(),
                "type": "dir" if is_dir else "file",
            }
            if not is_dir:
                try:
                    stat = child.stat()
                    entry["size"] = stat.st_size
                    entry["modified"] = (
                        datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z")
                    )
                except OSError:
                    pass
            entries.append(entry)
            cap -= 1
            if is_dir:
                _walk(child)

    _walk(root)
    return entries


def resolve_blueprint_file_path(root: Path, relative_path: str) -> Path:
    """Resolve ``relative_path`` under ``root``, hardened to the root.

    Raises :class:`BlueprintPathEscapesRootError` for a ``..`` escape (or any
    path that fails to resolve at all) -- the route layer maps this to a
    typed HTTP 400.
    """

    resolved_root = root.resolve()
    try:
        target = (resolved_root / relative_path).resolve()
    except OSError as exc:
        raise BlueprintPathEscapesRootError(relative_path) from exc
    try:
        target.relative_to(resolved_root)
    except ValueError:
        raise BlueprintPathEscapesRootError(relative_path) from None
    return target


def is_textual_blueprint_file(name: str, raw: bytes) -> bool:
    """Whether a blueprint file should be served as decoded ``text/plain``.

    Byte-identical policy to
    ``routes.workspaces._is_textual_workspace_file`` (binary content must
    never be UTF-8-decoded, #673/#676) -- kept as its own copy so this owner
    module carries no cross-route-module coupling.
    """

    guessed, _ = mimetypes.guess_type(name)
    if guessed is not None:
        return guessed.startswith("text/") or guessed in _TEXTUAL_BLUEPRINT_MIME_TYPES
    sample = raw[:8192]
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True
