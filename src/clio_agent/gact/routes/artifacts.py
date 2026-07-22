"""Artifact HTTP routes (#966 S2 / #968) — read + user-pin over the registry.

The outbound query surface for the artifact registry projection (owner decision
#966.8): list per session / per workspace, resolve one version by relay
``artifact_id`` or by name+``ref``, serve bytes hash-verified, and the
user-pinned designation channel (``POST .../pin``). Follows the ``memory.py``
route pattern — ``ErrorEnvelope`` typed errors, a clamped ``limit`` + ``before``
cursor, a bounded audit ledger — and is advertised via the ``x_clio_artifacts``
vendor capability flag.

The registry rebuilds LAZILY off the durable event log (RULE 4 / #737). Its boot
fold does unbounded synchronous I/O, so a first access is offloaded to a worker
thread (:func:`_registry`) — a route handler runs on the event loop, where a
first access would raise :class:`RegistryFoldOnLoopError`. Byte-serving,
re-hashing and the pin mint are likewise offloaded (blocking file I/O).

Custody note (S2): every artifact minted this campaign is
``workspace-referenced`` — CAS ingestion is S6. The ``/bytes`` route therefore
re-hashes the referenced workspace file and, on a valid hash, points the client
at the path-based workspace file route (``custody_not_cas``); a hash mismatch is
a typed ``integrity_violation``. Detection is the universal guarantee (design §7)
— it covers workspace-referenced bytes too, not only CAS.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from clio_agent.gact.artifacts.minting import (
    artifact_name_for_path,
    compute_identity,
    mint_artifact,
)
from clio_agent.gact.artifacts.records import (
    ArtifactKind,
    ArtifactRecord,
    ArtifactVersion,
    Custody,
    Mechanism,
)
from clio_agent.gact.artifacts.registry import ArtifactRegistry, get_registry
from clio_agent.gact.artifacts.wire import artifact_uri, fetch_url_for, mime_for, ui_payload_uri
from clio_agent.gact.runtime.retention import enforce_list_bound
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps

# Clamp on ``limit`` so a listing can never turn into an unbounded dump.
_LIST_LIMIT_DEFAULT = 50
_LIST_LIMIT_MAX = 200
_HASH_CHUNK_BYTES = 1024 * 1024


def _artifact_error(
    *,
    status_code: int,
    error: str,
    message: str,
    details: Optional[dict[str, Any]] = None,
    recoverable: bool = False,
) -> HTTPException:
    """Build an ``ErrorEnvelope``-wrapped :class:`HTTPException` (SPEC §6.0)."""
    return HTTPException(
        status_code=status_code,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error=error,
                message=message,
                details=details or {},
                recoverable=recoverable,
            )
        ).model_dump(exclude_none=True),
    )


def _audit(app: FastAPI, *, route: str, **fields: Any) -> None:
    """Append one bounded provenance row to the artifact-route audit ledger."""
    if not hasattr(app.state, "artifact_route_audit"):
        app.state.artifact_route_audit = []
    row = {"route": route, "at": time.time(), **fields}
    app.state.artifact_route_audit.append(row)
    enforce_list_bound(app, app.state.artifact_route_audit, "artifact_route_audit")


def _clamp_limit(limit: Optional[int]) -> int:
    """Clamp a caller ``limit`` into ``[1, _LIST_LIMIT_MAX]`` (default when unset)."""
    if limit is None:
        return _LIST_LIMIT_DEFAULT
    if limit < 1:
        return 1
    return min(limit, _LIST_LIMIT_MAX)


async def _registry(app: FastAPI) -> ArtifactRegistry:
    """Return the app's artifact registry, offloading a first-access boot fold.

    A built registry returns immediately; an unbuilt one is folded on a worker
    thread (the boot fold's synchronous I/O must never run on the event loop —
    ``get_registry`` raises :class:`RegistryFoldOnLoopError` if it would).
    """
    existing = getattr(app.state, "artifact_registry", None)
    if existing is not None:
        return existing
    return await asyncio.to_thread(get_registry, app)


def _workspace_root(app: FastAPI, workspace_id: str) -> Optional[Path]:
    """Resolve the bound workspace's root path, or ``None`` when unresolvable."""
    store = getattr(app.state, "workspaces", None)
    if store is None or not workspace_id:
        return None
    try:
        ws = store.get(workspace_id)
    except Exception:  # noqa: BLE001 — an unresolvable workspace is a typed skip
        return None
    root = str(getattr(ws, "root_path", "") or "") if ws is not None else ""
    if not root:
        return None
    return Path(root).expanduser().resolve(strict=False)


def _session_workspace_id(app: FastAPI, sid: str) -> Optional[str]:
    """The workspace id bound to a session, or ``None`` when the session is unknown."""
    store = getattr(app.state, "sessions", None) or getattr(app.state, "session_store", None)
    if store is None:
        return None
    session = store.get(sid)
    if session is None:
        return None
    return str(getattr(session, "workspace_id", "") or "")


def _version_uri(workspace_id: str, name: str, version: ArtifactVersion) -> str:
    """The logical URI for a version (``ui://`` for a ``ui_payload``, else ``artifact://``)."""
    if version.kind == ArtifactKind.UI_PAYLOAD:
        return ui_payload_uri(workspace_id, name, version.version)
    return artifact_uri(workspace_id, name, version.version)


def _version_wire(workspace_id: str, name: str, version: ArtifactVersion) -> dict[str, Any]:
    """Project one immutable version to its route wire dict."""
    return {
        "artifact_id": version.artifact_id,
        "workspace_id": workspace_id,
        "name": name,
        "version": version.version,
        "kind": version.kind.value,
        "custody": version.custody.value,
        "mechanism": version.mechanism.value,
        "evidence_class": version.evidence.evidence_class.value,
        "sha256": version.sha256,
        "size_bytes": version.size_bytes,
        "authority": version.evidence.authority,
        "path": version.path,
        "created_at": version.created_at,
        "annotation": version.annotation,
        "producer": dict(version.producer),
        # S4 (#970): the ``wasRevisionOf`` edge + honest custody/kind markers.
        "prior_version": version.prior_version,
        "prior_sha256": version.prior_sha256,
        "kind_warning": version.kind_warning,
        "custody_gap": version.custody_gap,
        "uri": _version_uri(workspace_id, name, version),
        "fetch_url": fetch_url_for(version.artifact_id),
    }


def _record_wire(record: ArtifactRecord) -> dict[str, Any]:
    """Project a logical record (its version chain + aliases) to its wire dict."""
    head = record.head
    return {
        "workspace_id": record.workspace_id,
        "name": record.name,
        "kind": record.kind.value,
        "latest_version": head.version if head is not None else 0,
        "head_artifact_id": head.artifact_id if head is not None else "",
        "aliases": dict(record.aliases),
        "versions": [_version_wire(record.workspace_id, record.name, v) for v in record.versions],
    }


def _paginate_records(
    records: list[ArtifactRecord], *, limit: int, before: Optional[str]
) -> tuple[list[ArtifactRecord], Optional[str]]:
    """Order records newest-first and apply the ``limit`` + ``before`` cursor.

    Order key: the head version's ``created_at`` descending, tie-broken by name
    (stable, deterministic). ``before`` is an ``artifact_id`` cursor that anchors on
    the RECORD OWNING that version id — ANY of the record's versions, head or
    superseded, resolves it. This is what keeps pagination stable when message ids
    cannot: a handed-out ``next_cursor`` is a head id at page time, but if that
    boundary record is re-versioned before the next page fetch its head id rotates;
    the stale id is still one of the record's versions, so it still resolves to the
    record's position and the page continues (rather than a hard 404). The page
    returns records strictly AFTER that record in the ordering (older). The returned
    ``next_cursor`` is the head ``artifact_id`` of the last record on this page when
    the limit truncated more, else ``None``. An ``artifact_id`` matching no known
    version is a 404.
    """

    def _key(r: ArtifactRecord) -> tuple[str, str]:
        head = r.head
        return (head.created_at if head is not None else "", r.name)

    ordered = sorted(records, key=_key, reverse=True)
    start = 0
    if before is not None:
        # Map EVERY version id (not just heads) to its owning record's position, so a
        # cursor whose boundary record was re-versioned in the inter-page window still
        # anchors (finding [3] — the head id it names is now a superseded version id).
        pos_by_version_id: dict[str, int] = {}
        for idx, rec in enumerate(ordered):
            for ver in rec.versions:
                pos_by_version_id[ver.artifact_id] = idx
        if before not in pos_by_version_id:
            raise _artifact_error(
                status_code=404,
                error="not_found",
                message=f"cursor artifact not found: {before}",
                details={"before": before},
            )
        start = pos_by_version_id[before] + 1
    page = ordered[start : start + limit]
    next_cursor = None
    if start + limit < len(ordered) and page:
        tail = page[-1].head
        next_cursor = tail.artifact_id if tail is not None else None
    return page, next_cursor


def _sha256_file(path: Path) -> str:
    """Stream a file's sha256 (bounded memory). Raises ``OSError`` on read failure."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_handle(handle: Any) -> str:
    """Stream the sha256 of an ALREADY-OPEN handle from its current position.

    Chunked so a multi-GB blob never lands in memory. Leaves the handle open (the
    caller ``seek(0)``s and streams the SAME handle to the client) so the bytes we
    hash are the bytes we serve — no re-open TOCTOU between verify and send.
    """
    digest = hashlib.sha256()
    while True:
        chunk = handle.read(_HASH_CHUNK_BYTES)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _stream_handle(handle: Any) -> Iterator[bytes]:
    """Yield an open handle's bytes in bounded chunks, closing it when exhausted.

    Starlette iterates this sync generator in a threadpool, so the file read never
    blocks the event loop and never buffers the whole file (never ``read_bytes``).
    """
    try:
        while True:
            chunk = handle.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            yield chunk
    finally:
        handle.close()


def register_artifacts_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register the artifact read + user-pin routes on ``app`` (#968).

    Uniform ``register_<concern>_routes(app, deps)`` factory signature; the read
    routes need no cross-concern seam from ``deps`` (they reach the registry +
    workspace/session stores through ``app.state``).
    """

    # ---- listing ----------------------------------------------------------

    @app.get("/v1/sessions/{sid}/artifacts")
    async def list_session_artifacts(
        sid: str, limit: int | None = None, before: str | None = None
    ) -> dict[str, Any]:
        """List the artifacts of a session's workspace (newest-first, paginated)."""
        workspace_id = _session_workspace_id(app, sid)
        if workspace_id is None:
            raise _artifact_error(
                status_code=404,
                error="not_found",
                message=f"session not found: {sid}",
                details={"session_id": sid},
            )
        registry = await _registry(app)
        records = registry.list_for_workspace(workspace_id)
        page, next_cursor = _paginate_records(records, limit=_clamp_limit(limit), before=before)
        _audit(
            app,
            route="list_session_artifacts",
            session_id=sid,
            workspace_id=workspace_id,
            returned=len(page),
        )
        return {
            "artifacts": [_record_wire(r) for r in page],
            "count": len(records),
            "next_cursor": next_cursor,
        }

    @app.get("/v1/workspaces/{wid}/artifacts")
    async def list_workspace_artifacts(
        wid: str, limit: int | None = None, before: str | None = None
    ) -> dict[str, Any]:
        """List the artifacts of a workspace directly (newest-first, paginated)."""
        registry = await _registry(app)
        records = registry.list_for_workspace(wid)
        page, next_cursor = _paginate_records(records, limit=_clamp_limit(limit), before=before)
        _audit(app, route="list_workspace_artifacts", workspace_id=wid, returned=len(page))
        return {
            "artifacts": [_record_wire(r) for r in page],
            "count": len(records),
            "next_cursor": next_cursor,
        }

    @app.get("/v1/workspaces/{wid}/artifacts/{name}")
    async def resolve_artifact_by_name(wid: str, name: str, ref: str = "latest") -> dict[str, Any]:
        """Resolve one version by name + ``ref`` (``latest`` | ``vN`` | alias).

        Full alias resolution is LIVE (S4 #970): ``latest`` (the auto-maintained head
        alias), ``vN``, and ANY tracked alias resolve. The S2 placeholder ``409
        alias_resolution_not_available`` is gone — resolution is complete, so an
        unknown ref is now an honest ``404 not_found`` carrying the full set of
        resolvable refs in ``details.available`` (``latest``, every ``v1..vN``, and
        every named alias). A ``vN`` naming a version that does not exist is the same
        honest ``404``.
        """
        registry = await _registry(app)
        record = registry.get(wid, name)
        if record is None:
            raise _artifact_error(
                status_code=404,
                error="not_found",
                message=f"artifact not found: {name}",
                details={"workspace_id": wid, "name": name},
            )
        version = _resolve_ref(record, ref)
        if version is None:
            raise _artifact_error(
                status_code=404,
                error="not_found",
                message=f"artifact ref not resolvable: {ref}",
                details={
                    "workspace_id": wid,
                    "name": name,
                    "ref": ref,
                    "available": _available_refs(record),
                },
            )
        _audit(app, route="resolve_artifact_by_name", workspace_id=wid, name=name, ref=ref)
        return {
            "artifact": _record_wire(record),
            "resolved": _version_wire(wid, name, version),
            "ref": ref,
        }

    # The mutable alias-move route lives in its own owner module (no-accretion, S4);
    # register it here so the ``x_clio_artifacts`` surface is assembled in one place.
    from clio_agent.gact.routes.artifact_aliases import (  # noqa: PLC0415
        register_artifact_alias_routes,
    )
    from clio_agent.gact.routes.artifact_lineage import (  # noqa: PLC0415
        register_artifact_lineage_routes,
    )

    register_artifact_alias_routes(app)
    # S5 (#971): lineage + transform read routes ride the same ``x_clio_artifacts``
    # surface (assembled in one place, no-accretion).
    register_artifact_lineage_routes(app)

    @app.get("/v1/artifacts/{artifact_id}")
    async def get_artifact(artifact_id: str) -> dict[str, Any]:
        """Resolve one version by its relay ``artifact_id`` + its logical record."""
        registry = await _registry(app)
        found = registry.get_by_artifact_id(artifact_id)
        if found is None:
            raise _artifact_error(
                status_code=404,
                error="not_found",
                message=f"artifact not found: {artifact_id}",
                details={"artifact_id": artifact_id},
            )
        record, version = found
        _audit(app, route="get_artifact", artifact_id=artifact_id)
        return {
            "artifact": _record_wire(record),
            "resolved": _version_wire(record.workspace_id, record.name, version),
        }

    # ---- bytes (re-hash on read) -----------------------------------------

    @app.get("/v1/artifacts/{artifact_id}/bytes")
    async def get_artifact_bytes(artifact_id: str) -> Response:
        """Serve a version's bytes hash-verified, or a typed 409.

        Order (detection is the universal guarantee, design §7): resolve the version
        → re-hash the referenced bytes → a recorded-hash MISMATCH is a 409
        ``integrity_violation`` (the file changed since mint), for ANY custody →
        then the custody gate: a non-CAS version is a 409 ``custody_not_cas`` that
        points the client at the path-based workspace file route (the bytes live in
        the workspace, not the app store; S6 adds CAS ingestion). Only a CAS version
        with a valid hash is served here. A stat-pinned version (no recorded sha)
        skips the integrity check — identity was never hashed.
        """
        registry = await _registry(app)
        found = registry.get_by_artifact_id(artifact_id)
        if found is None:
            raise _artifact_error(
                status_code=404,
                error="not_found",
                message=f"artifact not found: {artifact_id}",
                details={"artifact_id": artifact_id},
            )
        record, version = found
        return await asyncio.to_thread(_serve_bytes, app, record, version)

    # ---- pin (user-designation channel) ----------------------------------

    @app.post("/v1/sessions/{sid}/artifacts/pin")
    async def pin_artifact(sid: str, request: Request) -> dict[str, Any]:
        """User-pinned designation: register a workspace file as an artifact.

        The user-pinned channel (owner decision #966.1). The harness hashes the file
        in-hand (mechanism ``harness``, ``hashed-at-use`` evidence — the model is
        never load-bearing in the chain of custody); a ``designation=user-pinned``
        producer note makes the channel visible in the record. Containment is
        enforced BEFORE any stat/hash (owner decision 10): a path outside the
        workspace root is refused with a typed error, no read. The whole mint is
        offloaded to a worker thread (blocking hash + the registry boot fold must
        not run on the event loop).
        """
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001 — a bad body is a typed 422, not a 500
            raise _artifact_error(
                status_code=422,
                error="invalid_request",
                message="pin request body must be JSON",
                recoverable=True,
            ) from exc
        if not isinstance(body, dict):
            raise _artifact_error(
                status_code=422,
                error="invalid_request",
                message="pin request body must be an object",
                recoverable=True,
            )
        raw_path = str(body.get("path") or "").strip()
        if not raw_path:
            raise _artifact_error(
                status_code=422,
                error="invalid_request",
                message="pin requires a non-empty 'path'",
                details={"session_id": sid},
                recoverable=True,
            )
        workspace_id = _session_workspace_id(app, sid)
        if workspace_id is None:
            raise _artifact_error(
                status_code=404,
                error="not_found",
                message=f"session not found: {sid}",
                details={"session_id": sid},
            )
        result = await asyncio.to_thread(
            _pin_mint,
            app,
            sid,
            workspace_id=workspace_id,
            raw_path=raw_path,
            name=str(body.get("name") or "").strip(),
            kind_override=str(body.get("kind") or "").strip(),
            annotation=str(body.get("annotation") or ""),
        )
        # ``result`` is either an HTTPException-raising tuple or the version wire.
        if isinstance(result, HTTPException):
            raise result
        _audit(
            app,
            route="pin_artifact",
            session_id=sid,
            workspace_id=workspace_id,
            artifact_id=result.get("artifact_id", ""),
        )
        return {"pinned": result}


def _resolve_ref(record: ArtifactRecord, ref: str) -> Optional[ArtifactVersion]:
    """Resolve a ``latest`` | ``vN`` | alias ``ref`` to a version (or ``None``).

    ``latest`` → the head; ``vN`` → the version numbered N; any other alias present
    in ``record.aliases`` → its target (full alias resolution, live in S4). An
    unknown ref returns ``None`` and the caller surfaces an honest ``404`` with the
    resolvable set.
    """
    ref = (ref or "latest").strip()
    if ref == "latest":
        return record.head
    if ref.startswith("v") and ref[1:].isdigit():
        target = int(ref[1:])
        return next((v for v in record.versions if v.version == target), None)
    alias_target = record.aliases.get(ref)
    if alias_target is not None:
        return next((v for v in record.versions if v.version == alias_target), None)
    return None


def _available_refs(record: ArtifactRecord) -> list[str]:
    """The full set of resolvable refs for a record — the honest 404 ``available``.

    ``latest`` + every ``v1..vN`` + every tracked alias name (``latest`` de-duped),
    sorted stably so the wire is deterministic. This is what makes an unknown-ref
    ``404`` honest now that resolution is complete (S4): the client is told exactly
    what WOULD resolve, not merely that its guess failed.
    """
    refs = {"latest"}
    refs.update(f"v{v.version}" for v in record.versions)
    refs.update(record.aliases.keys())
    return sorted(refs)


def _serve_bytes(app: FastAPI, record: ArtifactRecord, version: ArtifactVersion) -> Response:
    """Re-hash + custody-gate a version's bytes (runs on a worker thread).

    Raising path is via :func:`_artifact_error`; the caller awaited us on a thread,
    so the HTTPException propagates to FastAPI unchanged.
    """
    source = Path(version.path) if version.path else None
    if source is None or not source.is_file():
        raise _artifact_error(
            status_code=404,
            error="not_found",
            message="artifact bytes are not retrievable (source missing)",
            details={"artifact_id": version.artifact_id, "path": version.path},
        )
    # Defence in depth: never read outside the workspace root (owner decision 10).
    # An UNRESOLVABLE root REFUSES the serve (typed ``containment_unresolved``) —
    # containment cannot be verified, so precision over recall: we do not read the
    # path at all (finding [5]), rather than silently skipping the check.
    root = _workspace_root(app, record.workspace_id)
    if root is None:
        raise _artifact_error(
            status_code=409,
            error="containment_unresolved",
            message="workspace root is unresolvable; cannot serve artifact bytes",
            details={
                "artifact_id": version.artifact_id,
                "workspace_id": record.workspace_id,
            },
        )
    from clio_agent.tools.file_policy import _is_relative_to  # noqa: PLC0415

    if not _is_relative_to(source.expanduser().resolve(strict=False), root):
        raise _artifact_error(
            status_code=403,
            error="path_outside_workspace",
            message="artifact path escapes its workspace root",
            details={"artifact_id": version.artifact_id},
        )
    recorded_sha = version.sha256
    if version.custody != Custody.CAS:
        # Non-served custody (workspace-referenced/external). Detection is still the
        # universal guarantee: re-hash the referenced file and 409 on a mismatch,
        # then point the client at the workspace file route (the bytes live there).
        if recorded_sha:
            try:
                actual = _sha256_file(source)
            except OSError as exc:
                raise _artifact_error(
                    status_code=404,
                    error="not_found",
                    message="artifact bytes are not readable",
                    details={"artifact_id": version.artifact_id},
                ) from exc
            if actual != recorded_sha:
                raise _artifact_error(
                    status_code=409,
                    error="integrity_violation",
                    message="artifact content no longer matches its recorded hash",
                    details={
                        "artifact_id": version.artifact_id,
                        "recorded_sha256": recorded_sha,
                        "actual_sha256": actual,
                    },
                )
        try:
            rel = str(source.expanduser().resolve(strict=False).relative_to(root))
        except ValueError:
            rel = ""
        raise _artifact_error(
            status_code=409,
            error="custody_not_cas",
            message=(
                "artifact bytes are workspace-referenced, not app-served; fetch via "
                "the workspace file route"
            ),
            details={
                "artifact_id": version.artifact_id,
                "custody": version.custody.value,
                "workspace_id": record.workspace_id,
                "fetch_via": f"/v1/workspaces/{record.workspace_id}/files/read?path={rel}",
            },
        )
    # CAS (app-served). Hash + serve from ONE open handle so the served bytes ARE the
    # verified bytes — no read-then-send TOCTOU (the old ``read_bytes`` re-opened the
    # path after a separate hash) — and stream chunked so a large blob never buffers
    # whole in RAM. Residual TOCTOU: a same-fd truncation/rewrite between the hash
    # and the stream is served as-is; CAS is the app-private store, documented limit.
    try:
        handle = open(source, "rb")
    except OSError as exc:
        raise _artifact_error(
            status_code=404,
            error="not_found",
            message="artifact bytes are not readable",
            details={"artifact_id": version.artifact_id},
        ) from exc
    if recorded_sha:
        try:
            actual = _sha256_handle(handle)
        except OSError as exc:
            handle.close()
            raise _artifact_error(
                status_code=404,
                error="not_found",
                message="artifact bytes are not readable",
                details={"artifact_id": version.artifact_id},
            ) from exc
        if actual != recorded_sha:
            handle.close()
            raise _artifact_error(
                status_code=409,
                error="integrity_violation",
                message="artifact content no longer matches its recorded hash",
                details={
                    "artifact_id": version.artifact_id,
                    "recorded_sha256": recorded_sha,
                    "actual_sha256": actual,
                },
            )
        handle.seek(0)
    return StreamingResponse(_stream_handle(handle), media_type=mime_for(version, record.name))


def _pin_mint(
    app: FastAPI,
    sid: str,
    *,
    workspace_id: str,
    raw_path: str,
    name: str,
    kind_override: str,
    annotation: str,
) -> Any:
    """Contain + hash + mint a user-pinned artifact (runs on a worker thread).

    Returns the version wire dict on success, or an :class:`HTTPException` the async
    caller re-raises (raising HTTP here would surface as a bare 500 through
    ``to_thread``).
    """
    from clio_agent.gact.artifacts.designation import kind_for_path  # noqa: PLC0415
    from clio_agent.tools.file_policy import _is_relative_to  # noqa: PLC0415

    root = _workspace_root(app, workspace_id)
    if root is None:
        return _artifact_error(
            status_code=409,
            error="containment_unresolved",
            message="workspace root is unresolvable; cannot pin",
            details={"workspace_id": workspace_id},
        )
    path = Path(raw_path)
    if not path.is_absolute():
        path = root / path
    resolved = path.expanduser().resolve(strict=False)
    if not _is_relative_to(resolved, root):
        return _artifact_error(
            status_code=403,
            error="path_outside_workspace",
            message="pin path escapes the workspace root",
            details={"workspace_id": workspace_id, "path": raw_path},
        )
    if not resolved.is_file():
        return _artifact_error(
            status_code=404,
            error="not_found",
            message=f"pin path is not a file: {raw_path}",
            details={"workspace_id": workspace_id, "path": raw_path},
        )
    try:
        evidence = compute_identity(resolved)
    except OSError:
        return _artifact_error(
            status_code=409,
            error="stat_hash_failed",
            message="could not stat/hash the pin path",
            details={"workspace_id": workspace_id, "path": raw_path},
        )
    kind = kind_for_path(resolved)
    if kind_override:
        try:
            kind = ArtifactKind(kind_override)
        except ValueError:
            return _artifact_error(
                status_code=422,
                error="invalid_request",
                message=f"unknown artifact kind: {kind_override}",
                details={"kind": kind_override},
                recoverable=True,
            )
    artifact_name = name or artifact_name_for_path(resolved)
    try:
        version = mint_artifact(
            app,
            sid,
            name=artifact_name,
            workspace_id=workspace_id,
            evidence=evidence,
            kind=kind,
            mechanism=Mechanism.HARNESS,
            producer={
                "designation": "user-pinned",
                "session_id": sid,
            },
            custody=Custody.WORKSPACE_REFERENCED,
            path=str(resolved),
            annotation=annotation,
        )
    except ValueError as exc:  # reserved kind (e.g. plan) — typed, not a 500
        return _artifact_error(
            status_code=422,
            error="reserved_kind",
            message=str(exc),
            details={"kind": kind.value},
            recoverable=True,
        )
    if version is None:
        return _artifact_error(
            status_code=500,
            error="mint_failed",
            message="artifact pin mint returned no version",
            details={"workspace_id": workspace_id, "name": artifact_name},
        )
    return _version_wire(workspace_id, artifact_name, version)


__all__ = ["register_artifacts_routes"]
