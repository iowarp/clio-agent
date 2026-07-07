"""Workspace store + file-listing routes for the GACT server (#714).

This concern owns the simple workspace store CRUD plus the read-only file
browsing surface the gact-tui ``@``-picker and desktop file tree consume:

* ``GET/POST /v1/workspaces`` and ``GET/PATCH/DELETE /v1/workspaces/{wid}`` --
  the workspace registry (SPEC 6.1).
* ``GET /v1/workspaces/{wid}/files`` -- capped, policy-aware file walk for the
  ``@``-picker (SPEC 6.9).
* ``GET /v1/workspaces/{wid}/repo_map`` -- the file tree reuses the same capped
  walk so it can never become an unbounded filesystem scan.
* ``GET /v1/workspaces/{wid}/files/read`` -- read one file, serving text decoded
  and binary as raw bytes with its real content type (#673, #676).

All handlers read the live ``WorkspaceStore`` via ``app.state.workspaces`` and
reach the shared direct-destructive-action permission guard through
:class:`~clio_agent.gact.routes.deps.GactDeps`. Concern-private helpers
(:data:`_TEXTUAL_WORKSPACE_MIME_TYPES`, :func:`_is_textual_workspace_file`) live
here; the module imports only leaf packages (types, file-policy, stdlib) and
never loads :mod:`clio_agent.gact.app`.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from clio_agent.gact.routes._body import json_body
from clio_agent.gact.types import (
    CreateWorkspaceRequest,
    ErrorEnvelope,
    ErrorInfo,
    ListWorkspacesResponse,
    Workspace,
)
from clio_agent.runtime import trace

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps

# Files served as decoded text/plain even though their MIME type is not under
# the ``text/`` tree (JSON/YAML/shell/TOML). Everything else unknown is sniffed.
_TEXTUAL_WORKSPACE_MIME_TYPES = frozenset(
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

# gact-tui's ``@``-trigger file picker walks the workspace root; cap the number
# of entries so a giant repo cannot lock the picker for seconds, and skip
# cost-walking dirs (VCS metadata, caches, build output, vendored deps).
_FILE_PICKER_LIMIT = 5000
_FILE_PICKER_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    ".npm",
    ".venv",
    "venv",
    ".tox",
    "build",
    "dist",
    ".egg-info",
    ".clio/agent",  # ARC's local persistence
}


def _is_textual_workspace_file(name: str, raw: bytes) -> bool:
    """Whether a workspace file should be served as decoded ``text/plain``
    (code preview) vs. raw bytes with its real content type (binary, e.g. PNG).

    Binary files (images, archives, ...) MUST be served as raw bytes with the
    correct content type -- decoding them as UTF-8 with ``errors="replace"``
    corrupts the bytes (replacement characters) and mislabels them text/plain,
    so a TUI/web preview can never recover the image
    (iowarp/clio-agent#673, #676).
    """
    import mimetypes  # noqa: PLC0415

    guessed, _ = mimetypes.guess_type(name)
    if guessed is not None:
        return guessed.startswith("text/") or guessed in _TEXTUAL_WORKSPACE_MIME_TYPES
    # Unknown extension: sniff a sample. A NUL byte or invalid UTF-8 => binary.
    sample = raw[:8192]
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def register_workspaces_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register the workspace store + file-browsing routes on ``app``.

    Handlers are defined inside this factory so they close over the ``app``
    argument FastAPI's decorators require, and reach the shared
    direct-destructive-action guard through ``deps`` rather than any
    ``build_app`` local.
    """

    # ---- /v1/workspaces (CLIO-BBBBBBBBBB-WS) -------------------------

    @app.get("/v1/workspaces", response_model=ListWorkspacesResponse)
    async def list_workspaces() -> ListWorkspacesResponse:
        """SPEC §6.1 — list workspaces."""

        rows = app.state.workspaces.list()
        return ListWorkspacesResponse(workspaces=[Workspace(**w.to_wire()) for w in rows])

    @app.post("/v1/workspaces", response_model=Workspace, status_code=201)
    async def create_workspace(req: CreateWorkspaceRequest) -> Workspace:
        """SPEC §6.1 — create a workspace pinned to ``root_path``."""

        ws = app.state.workspaces.create(
            name=req.name,
            root_path=req.root_path,
            storage_root=req.storage_root,
            metadata=req.metadata,
        )
        return Workspace(**ws.to_wire())

    @app.get("/v1/workspaces/{wid}", response_model=Workspace)
    async def get_workspace(wid: str) -> Workspace:
        ws = app.state.workspaces.get(wid)
        if ws is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"workspace not found: {wid}",
                        details={"workspace_id": wid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        return Workspace(**ws.to_wire())

    @app.patch("/v1/workspaces/{wid}", response_model=Workspace)
    async def patch_workspace(wid: str, request: Request) -> Workspace:
        """iowarp/gact-tui §audit/E-18: the desktop's Rename action
        on the Workspaces page posts PATCH /v1/workspaces/{wid} with
        {name?, metadata?, root_path?}. Without this endpoint clio
        returned 405 and the user saw 'Method Not Allowed' in a toast.
        Accept partial updates of any of those fields.
        """

        body = await json_body(request, route="PATCH /v1/workspaces/{wid}")
        name = body.get("name")
        root_path = body.get("root_path")
        metadata = body.get("metadata")
        # The desktop sends `config` as an alias for metadata.
        if metadata is None and isinstance(body.get("config"), dict):
            metadata = body.get("config")
        # Route the mutation through the store so it serialises under the
        # WorkspaceStore lock (no torn write / flush racing a concurrent
        # create) and bumps ``updated_at`` — never mutate the live object
        # returned by ``get()`` outside the lock.
        ws = app.state.workspaces.update(
            wid,
            name=name.strip() if isinstance(name, str) and name.strip() else None,
            root_path=(
                root_path.strip() if isinstance(root_path, str) and root_path.strip() else None
            ),
            metadata_patch=metadata if isinstance(metadata, dict) else None,
        )
        if ws is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"workspace not found: {wid}",
                        details={"workspace_id": wid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        return Workspace(**ws.to_wire())

    @app.delete("/v1/workspaces/{wid}")
    async def delete_workspace(wid: str) -> Response:
        """Refuses to delete ws_default — every CLIO install needs
        one workspace alive so sessions have a parent."""

        if wid == "ws_default":
            raise HTTPException(
                status_code=409,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="permission_error",
                        message="ws_default is not deletable",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        if app.state.workspaces.get(wid) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"workspace not found: {wid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        deps.guard_direct_destructive_action(
            app,
            workspace_id=wid,
            tool_name="gact.workspace.delete",
            args={"workspace_id": wid},
            summary=f"delete workspace {wid}",
            reason="user_requested_workspace_delete",
        )
        app.state.workspaces.delete(wid)
        return Response(status_code=204)

    # ---- /v1/workspaces/{wid}/files (gact-tui @-picker) -------------
    #
    # gact-tui's `@`-trigger file picker calls
    # /v1/workspaces/{wid}/files expecting a flat list of FileEntry
    # rooted at the workspace's root_path. Until this endpoint existed
    # the picker rendered as 404 ("file-picker: gact: 404"). We walk
    # the workspace root, skip cost-walking dirs (.git, __pycache__,
    # node_modules, .venv, build/), respect the file policy's
    # allow-symlinks flag, and cap at _FILE_PICKER_LIMIT entries so a
    # giant repo doesn't lock the picker for seconds while the
    # filesystem walk runs.

    @app.get("/v1/workspaces/{wid}/files")
    async def list_workspace_files(wid: str) -> dict[str, Any]:
        """SPEC §6.9 — list files under a workspace's root_path.

        Returns ``{"entries": [{"path", "type", "size", "modified"}, …]}``
        with paths relative to root_path so the TUI can show short
        labels. Type is "file" or "dir"; the picker filters dirs
        client-side. Hard-capped at _FILE_PICKER_LIMIT to keep large
        repos from blocking the modal.
        """

        ws = app.state.workspaces.get(wid)
        if ws is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"workspace not found: {wid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        root = Path(ws.root_path or os.getcwd()).expanduser()
        if not root.is_dir():
            return {"entries": []}

        # File policy decides whether symlinks are walkable; everything
        # else (size cap, allowed-roots) is enforced at read-time, not
        # listing-time.
        allow_symlinks = False
        try:
            from clio_agent.tools.file_policy import FileAccessPolicy  # noqa: PLC0415

            policy = FileAccessPolicy.from_mapping(os.environ)
            allow_symlinks = policy.allow_symlinks
        except Exception as exc:  # noqa: BLE001 - failure recorded via trace.event
            trace.event(
                "WORKSPACE",
                "file policy unavailable for %s (%s); symlinks stay excluded",
                wid,
                exc,
            )

        entries: list[dict[str, Any]] = []
        cap = _FILE_PICKER_LIMIT

        def _walk(d: Path) -> None:
            nonlocal cap
            if cap <= 0:
                return
            try:
                raw_children = list(d.iterdir())
            except (OSError, PermissionError):
                return
            # Don't stat-sort up front — a single un-statable child
            # (broken symlink, restricted unix socket in /tmp) raises
            # mid-key-eval and drops the entire list. Sort by name only;
            # we'll check is_dir per-entry behind a try.
            raw_children.sort(key=lambda p: p.name)
            for child in raw_children:
                if cap <= 0:
                    return
                name = child.name
                if name in _FILE_PICKER_SKIP_DIRS:
                    continue
                try:
                    if child.is_symlink() and not allow_symlinks:
                        continue
                    is_dir = child.is_dir()
                except OSError:
                    # Unreadable entry — skip rather than abort the whole
                    # walk. Common in /tmp where other users' sockets
                    # are 0600 and trip stat's permission check.
                    continue
                rel = str(child.relative_to(root))
                entry: dict[str, Any] = {
                    "path": rel,
                    "type": "dir" if is_dir else "file",
                }
                if not is_dir:
                    try:
                        st = child.stat()
                        entry["size"] = st.st_size
                        entry["modified"] = (
                            datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
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
        return {"entries": entries}

    @app.get("/v1/workspaces/{wid}/repo_map")
    async def workspace_repo_map(wid: str) -> dict[str, Any]:
        """SPEC §6.9 repo-map envelope for the workspace file tree.

        The map intentionally reuses the capped file picker walk so a
        large repository cannot turn the read-only contract endpoint
        into an unbounded filesystem scan.
        """

        ws = app.state.workspaces.get(wid)
        if ws is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"workspace not found: {wid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        root = Path(ws.root_path or os.getcwd()).expanduser()
        tree: dict[str, Any] = {
            "name": root.name or str(root),
            "path": "",
            "type": "dir",
            "children": [],
        }
        body = await list_workspace_files(wid)
        entries = body.get("entries", [])
        nodes_by_path: dict[str, dict[str, Any]] = {"": tree}
        token_estimate = 0
        for entry in entries:
            path = str(entry.get("path") or "")
            if not path:
                continue
            normalized = path.replace("\\", "/")
            parent_key = "/".join(normalized.split("/")[:-1])
            parent = nodes_by_path.get(parent_key, tree)
            node = {
                "name": normalized.split("/")[-1],
                "path": normalized,
                "type": entry.get("type") or "file",
            }
            if node["type"] == "dir":
                node["children"] = []
            size = entry.get("size")
            if isinstance(size, int):
                node["size"] = size
                token_estimate += max(1, size // 4)
            parent.setdefault("children", []).append(node)
            nodes_by_path[normalized] = node
        return {
            "tree": tree,
            "tokens": token_estimate,
            "truncated": len(entries) >= _FILE_PICKER_LIMIT,
        }

    @app.get("/v1/workspaces/{wid}/files/read")
    async def read_workspace_file(wid: str, path: str) -> Response:
        """SPEC §6.9 — read one file's content.

        Text files are served decoded as ``text/plain`` so the TUI's preview
        panel can render code without a base64 decode. BINARY files (images,
        archives, ...) are served as RAW bytes with their real content type
        (e.g. ``image/png``); decoding them as UTF-8 would corrupt the bytes and
        mislabel them text/plain (iowarp/clio-agent#673, #676). Refuses paths
        that escape the workspace root (``..`` segments) and paths beyond the
        file policy's max_file_size_bytes.
        """

        ws = app.state.workspaces.get(wid)
        if ws is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"workspace not found: {wid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        root = Path(ws.root_path or os.getcwd()).expanduser().resolve()
        try:
            target = (root / path).resolve()
        except Exception:  # noqa: BLE001 - path resolution failure surfaced as HTTP 400
            raise HTTPException(
                status_code=400,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="invalid_path",
                        message=f"could not resolve path: {path}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            ) from None
        # Refuse path-traversal: target must be at-or-below root.
        try:
            target.relative_to(root)
        except ValueError:
            raise HTTPException(
                status_code=403,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="path_outside_workspace",
                        message=f"path escapes workspace: {path}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            ) from None
        if not target.is_file():
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"file not found: {path}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        # Enforce file-policy size cap so a 50 GB log doesn't OOM.
        try:
            from clio_agent.tools.file_policy import FileAccessPolicy  # noqa: PLC0415

            policy = FileAccessPolicy.from_mapping(os.environ)
            max_bytes = policy.max_file_size_bytes
        except Exception as exc:  # noqa: BLE001 - failure recorded via trace.event
            trace.event(
                "WORKSPACE",
                "file policy unavailable reading %s (%s); using 1 GiB size cap",
                path,
                exc,
            )
            max_bytes = 1024 * 1024 * 1024  # 1 GiB fallback
        size = target.stat().st_size
        if size > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="file_too_large",
                        message=f"file exceeds policy cap ({size} > {max_bytes} bytes)",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        try:
            data = target.read_bytes()
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="read_failed",
                        message=f"could not read file: {exc}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            ) from exc
        if _is_textual_workspace_file(target.name, data):
            return Response(
                content=data.decode("utf-8", errors="replace"),
                media_type="text/plain; charset=utf-8",
            )
        import mimetypes  # noqa: PLC0415

        guessed, _ = mimetypes.guess_type(target.name)
        return Response(
            content=data,
            media_type=guessed or "application/octet-stream",
        )
