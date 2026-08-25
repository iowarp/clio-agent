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
from fastapi.responses import JSONResponse, Response

from clio_agent.gact.protocol_v3 import requests_gact_v3, workspace_to_v3
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


_GRANTOR_USER = "user"


def _emit_boundary_root(
    app: FastAPI, workspace_id: str, root_path: str, *, grantor: str, revoked: bool = False
) -> None:
    """Emit a ``boundary.granted``/``boundary.revoked`` for a workspace write-root (B5 #979.2).

    Thin route-layer shim over the grants owner module so WorkspaceStore stays leaf-pure (no
    bus). Guarded — a boundary record must never break the workspace mutation.
    """
    try:
        from clio_agent.gact.runtime import grants  # noqa: PLC0415

        (grants.emit_boundary_revoked if revoked else grants.emit_boundary_granted)(
            app,
            kind=grants.KIND_ROOT,
            scope=grants.SCOPE_WORKSPACE,
            grantor=grantor,
            pattern=root_path,
            workspace_id=workspace_id,
        )
    except Exception as exc:  # noqa: BLE001 - boundary emit is observability, never fatal
        trace.event("WORKSPACE", "boundary emit skipped wid=%s reason=%r", workspace_id, exc)


def _grant_workspace_domain(app: FastAPI, workspace_id: str, host: str) -> dict[str, Any]:
    """Grant a network domain to a workspace: sticky ``host_pattern`` policy + boundary event."""
    from clio_agent.gact.runtime import grants  # noqa: PLC0415

    # B4 #1057: a domain grant declares ``kind="domain"`` and carries NO ``tool_name_pattern`` —
    # the legacy stray ``"*"`` glob let this fleet-egress row bleed into every ``kind="tool"``
    # resolve (grant_resolver._kind_admitted now refuses it at match time; not persisting the glob
    # keeps the stored shape honest so a self-heal on load has nothing to strip).
    policy: dict[str, Any] = {
        "kind": grants.KIND_DOMAIN,
        "scope": grants.SCOPE_WORKSPACE,
        "scope_id": workspace_id,
        "host_pattern": host,
        "action": "allow",
    }
    policies = getattr(app.state, "permission_policies", None)
    if isinstance(policies, list):
        from clio_agent.gact.runtime.grant_resolver import (  # noqa: PLC0415
            next_append_priority,
        )
        from clio_agent.gact.runtime.permission_policies import (  # noqa: PLC0415
            _flush_permission_policies,
        )

        # A sticky runtime append must be its own strictly-lowest priority band, or it can
        # collide with a migrated legacy row's priority and wrongly trigger the most-restrictive
        # tie-break (P0.1 #1059 follow-up) -- see next_append_priority's docstring.
        policy["priority"] = next_append_priority(policies)
        policies.append(policy)
        _flush_permission_policies(app)
    grants.emit_boundary_granted(
        app,
        kind=grants.KIND_DOMAIN,
        scope=grants.SCOPE_WORKSPACE,
        grantor=_GRANTOR_USER,
        pattern=host,
        workspace_id=workspace_id,
    )
    return {"granted": True, "host_pattern": host}


def _grant_workspace_tool(
    app: FastAPI, workspace_id: str, pattern: str, body: dict[str, Any]
) -> dict[str, Any]:
    """Append a sticky tool permission policy for a workspace (#1034 kind-dispatch).

    Builds the policy row through :class:`~clio_agent.gact.runtime.grant_resolver.GrantRecord`
    so the ``kind``/scope/action encoding matches exactly what :func:`resolve` enforces, then
    flushes the store. ``decision`` defaults to ``allow`` and validates to the coarse
    ``allow``/``deny``/``ask`` vocabulary; scope defaults to the workspace.

    The appended row is stamped with an explicit, strictly-lowest ``priority`` (P0.1 #1059
    follow-up) so it never collides with a pre-existing (often already-migrated legacy) row's
    priority: an unprioritized append would otherwise be assigned ``total - index`` by
    :func:`resolve`'s live migration, which can equal the current lowest legacy row's priority
    and wrongly trigger the most-restrictive tie-break instead of preserving appended-last
    (lowest) precedence.
    """
    from clio_agent.gact.runtime.grant_resolver import (  # noqa: PLC0415
        KIND_TOOL,
        GrantRecord,
        next_append_priority,
    )
    from clio_agent.gact.runtime.grants import SCOPE_WORKSPACE  # noqa: PLC0415

    decision = str(body.get("decision") or body.get("action") or "allow").lower()
    if decision not in {"allow", "deny", "ask"}:
        decision = "allow"
    scope = str(body.get("scope") or SCOPE_WORKSPACE)
    scope_id = str(body.get("scope_id") or (workspace_id if scope == SCOPE_WORKSPACE else ""))
    policies = getattr(app.state, "permission_policies", None)
    priority = next_append_priority(policies) if isinstance(policies, list) else None
    rec = GrantRecord(
        kind=KIND_TOOL,
        pattern=pattern,
        decision=decision,
        scope=scope,
        scope_id=scope_id,
        grantor=_GRANTOR_USER,
        priority=priority,
    )
    if isinstance(policies, list):
        policies.append(rec.to_policy_row())
        from clio_agent.gact.runtime.permission_policies import (  # noqa: PLC0415
            _flush_permission_policies,
        )

        _flush_permission_policies(app)
    return {"granted": True, "kind": KIND_TOOL, "pattern": pattern, "decision": decision}


def _apply_kind_grant(
    app: FastAPI, workspace_id: str, kind: str, body: dict[str, Any]
) -> dict[str, Any]:
    """Apply a ``kind``-dispatched workspace grant (#1034, ADDITIVE to the subset-probe body).

    ``kind`` is a :mod:`grant_resolver` discriminator: ``fs_root`` (alias ``root``) grants a
    writable root via :func:`~clio_agent.gact.runtime.grants.apply_root_grant`, ``domain`` grants
    a network host via :func:`_grant_workspace_domain`, and ``tool`` appends a sticky tool policy
    via :func:`_grant_workspace_tool`. It routes to the SAME apply helpers as the legacy subset
    body so the boundary event + policy row are identical either way. Raises 400 on an unknown
    kind or a missing ``pattern`` so a malformed grant is surfaced, never silently dropped.
    """
    from clio_agent.gact.runtime.grant_resolver import (  # noqa: PLC0415
        KIND_DOMAIN,
        KIND_ROOT,
        KIND_TOOL,
    )

    normalized = KIND_ROOT if kind in {"root", KIND_ROOT} else kind
    pattern = str(
        body.get("pattern")
        or body.get("root")
        or body.get("path")
        or body.get("domain")
        or body.get("host")
        or body.get("tool")
        or ""
    ).strip()
    if not pattern:
        raise HTTPException(
            status_code=400,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="invalid_request",
                    message=f"kind grant ({kind}) requires a non-empty 'pattern'",
                    recoverable=True,
                )
            ).model_dump(exclude_none=True),
        )
    if normalized == KIND_ROOT:
        return _apply_root_kind_grant(app, workspace_id, pattern)
    if normalized == KIND_DOMAIN:
        return _grant_workspace_domain(app, workspace_id, pattern.lower())
    if normalized == KIND_TOOL:
        return _grant_workspace_tool(app, workspace_id, pattern, body)
    raise HTTPException(
        status_code=400,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error="invalid_request",
                message=f"unknown grant kind: {kind!r} (expected tool, domain, or fs_root)",
                recoverable=True,
            )
        ).model_dump(exclude_none=True),
    )


def _apply_root_kind_grant(app: FastAPI, workspace_id: str, pattern: str) -> dict[str, Any]:
    """Thin adapter so the kind-dispatch reuses the exact ``apply_root_grant`` semantics."""
    from clio_agent.gact.runtime import grants  # noqa: PLC0415

    return grants.apply_root_grant(app, workspace_id, pattern)


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

    # ---- /v1/workspaces -------------------------

    @app.get("/v1/workspaces", response_model=ListWorkspacesResponse)
    async def list_workspaces(request: Request) -> ListWorkspacesResponse | JSONResponse:
        """SPEC §6.1 — list workspaces."""

        rows = app.state.workspaces.list()
        if requests_gact_v3(request):
            return JSONResponse(content={"workspaces": [workspace_to_v3(row) for row in rows]})
        return ListWorkspacesResponse(workspaces=[Workspace(**w.to_wire()) for w in rows])

    @app.post("/v1/workspaces", response_model=Workspace, status_code=201)
    async def create_workspace(
        req: CreateWorkspaceRequest, request: Request
    ) -> Workspace | JSONResponse:
        """SPEC §6.1 — create a workspace pinned to ``root_path``."""

        ws = app.state.workspaces.create(
            name=req.name,
            root_path=req.root_path,
            storage_root=req.storage_root,
            metadata=req.metadata,
        )
        # B5 #979.2: a new workspace pins a write-root boundary — previously a silent mutation
        # (WorkspaceStore has no bus). Emit at the ROUTE layer (which has ``app``), keeping the
        # store leaf-pure. ``grantor=user`` (a direct user action, never a clio decision, ⚑).
        if ws.root_path:
            _emit_boundary_root(app, ws.id, ws.root_path, grantor=_GRANTOR_USER)
        if requests_gact_v3(request):
            return JSONResponse(content=workspace_to_v3(ws), status_code=201)
        return Workspace(**ws.to_wire())

    @app.get("/v1/workspaces/{wid}", response_model=Workspace)
    async def get_workspace(wid: str, request: Request) -> Workspace | JSONResponse:
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
        if requests_gact_v3(request):
            return JSONResponse(content=workspace_to_v3(ws))
        return Workspace(**ws.to_wire())

    @app.patch("/v1/workspaces/{wid}", response_model=Workspace)
    async def patch_workspace(wid: str, request: Request) -> Workspace | JSONResponse:
        """iowarp/gact-tui §audit/E-18: the desktop's Rename action
        on the Workspaces page posts PATCH /v1/workspaces/{wid} with
        {name?, display_name?, metadata?, root_path?}. Without this endpoint clio
        returned 405 and the user saw 'Method Not Allowed' in a toast.
        Accept partial updates of any of those fields.
        """

        body = await json_body(request, route="PATCH /v1/workspaces/{wid}")
        name = body.get("name")
        display_name = body.get("display_name")
        root_path = body.get("root_path")
        metadata = body.get("metadata")
        # The desktop sends `config` as an alias for metadata.
        if metadata is None and isinstance(body.get("config"), dict):
            metadata = body.get("config")
        # Capture the prior root so a root_path change emits an honest revoked→granted pair.
        prior = app.state.workspaces.get(wid)
        prior_root = str(getattr(prior, "root_path", "") or "") if prior is not None else ""
        new_root = root_path.strip() if isinstance(root_path, str) and root_path.strip() else None
        # Route the mutation through the store so it serialises under the
        # WorkspaceStore lock (no torn write / flush racing a concurrent
        # create) and bumps ``updated_at`` — never mutate the live object
        # returned by ``get()`` outside the lock.
        ws = app.state.workspaces.update(
            wid,
            name=name.strip() if isinstance(name, str) and name.strip() else None,
            display_name=(
                display_name.strip()
                if isinstance(display_name, str) and display_name.strip()
                else None
            ),
            root_path=new_root,
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
        # B5 #979.2: a root_path change is an effective write-territory boundary change —
        # emit revoked(old)→granted(new) so the mutation is on the record, not silent.
        if new_root is not None and new_root != prior_root:
            if prior_root:
                _emit_boundary_root(app, wid, prior_root, grantor=_GRANTOR_USER, revoked=True)
            _emit_boundary_root(app, wid, new_root, grantor=_GRANTOR_USER)
        if requests_gact_v3(request):
            return JSONResponse(content=workspace_to_v3(ws))
        return Workspace(**ws.to_wire())

    # ---- /v1/workspaces/{wid}/grants (B5 #979.3 — mid-session grants) ----

    @app.post("/v1/workspaces/{wid}/grants")
    async def create_workspace_grant(wid: str, request: Request) -> dict[str, Any]:
        """Grant new effective boundary to a workspace mid-session (B5 #979.3).

        Body (any subset): ``{"root": "<path>"}`` grants a writable root — the fence + advisory
        widen LIVE and the workspace's resident fleet is restarted so an already-spawned child
        picks up the new territory (a busy fleet defers, reported ``grant_restart_deferred_busy``
        — #1033); ``{"domain": "<host>"}`` grants a network domain (a sticky
        workspace ``host_pattern`` policy the deny-mode chokepoint honours); ``{"deny_mode":
        true|false}`` toggles the workspace's opt-in network deny mode;
        ``{"network_write_gate": true|false}`` toggles the N2 write-gate (opt-in, default OFF).
        A newer client may instead post a ``kind``-dispatched grant (#1034, ADDITIVE — the old
        subset shape keeps working): ``{"kind": "fs_root"|"domain"|"tool", "pattern": "...", ...}``
        routes to the SAME apply helpers. Every grant is a recorded USER decision emitting
        ``boundary.granted`` (⚑ the model requests, the user grants).
        """
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
        body = await json_body(request, route="POST /v1/workspaces/{wid}/grants")
        from clio_agent.gact.runtime import grants  # noqa: PLC0415

        result: dict[str, Any] = {"workspace_id": wid}
        # A ``kind``-dispatched grant (#1034) is checked FIRST but COEXISTS with the legacy
        # subset-probe keys below — an old client keeps posting {root|domain|deny_mode}, a new
        # client may instead post {kind, pattern, ...}. Both routes reach the same apply helpers.
        kind = body.get("kind")
        root = body.get("root") or body.get("path")
        domain = body.get("domain") or body.get("host")
        deny_mode = body.get("deny_mode")
        write_gate = body.get("network_write_gate")
        if isinstance(kind, str) and kind.strip():
            result["grant"] = _apply_kind_grant(app, wid, kind.strip(), body)
        if isinstance(deny_mode, bool):
            ws.config[grants.DENY_MODE_CONFIG_KEY] = deny_mode
            app.state.workspaces.update(wid, metadata_patch=None)
            result["deny_mode"] = deny_mode
        if isinstance(write_gate, bool):
            # N2 write-gate toggle, beside deny_mode — opt-in per-workspace network flag,
            # DEFAULT OFF; distinct from the approval axis (a session field), it gates
            # write-shaped egress to un-granted hosts.
            ws.config[grants.NETWORK_WRITE_GATE_CONFIG_KEY] = write_gate
            app.state.workspaces.update(wid, metadata_patch=None)
            result["network_write_gate"] = write_gate
        # When a kind-dispatch already consumed ``root``/``domain`` via ``kind``, do NOT re-apply
        # the same subject through the subset probe (the kind branch is authoritative for its shape).
        if "grant" not in result and isinstance(root, str) and root.strip():
            result["root"] = grants.apply_root_grant(app, wid, root.strip())
        if "grant" not in result and isinstance(domain, str) and domain.strip():
            result["domain"] = _grant_workspace_domain(app, wid, domain.strip().lower())
        if not any(
            key in result for key in ("grant", "root", "domain", "deny_mode", "network_write_gate")
        ):
            raise HTTPException(
                status_code=400,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="invalid_request",
                        message=(
                            "grant body must include one of: kind, root, domain, "
                            "deny_mode, network_write_gate"
                        ),
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        return result

    @app.delete("/v1/workspaces/{wid}/grants")
    async def delete_workspace_grant(wid: str, request: Request) -> dict[str, Any]:
        """Remove one user-granted additional workspace folder.

        The primary workspace root remains owned by ``PATCH /v1/workspaces``;
        this endpoint only revokes an additive ``fs_root`` grant.
        """

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
        kind = str(request.query_params.get("kind") or "fs_root")
        pattern = str(request.query_params.get("pattern") or "").strip()
        if kind not in {"root", "fs_root"} or not pattern:
            raise HTTPException(
                status_code=400,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="invalid_request",
                        message="grant removal requires kind=fs_root and a non-empty pattern",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        from clio_agent.gact.runtime import grants  # noqa: PLC0415

        return {
            "workspace_id": wid,
            "grant": grants.revoke_root_grant(app, wid, pattern),
        }

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
