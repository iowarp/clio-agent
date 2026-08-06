"""Artifact alias-move route (#966 S4 / #970) — the mutable-pointer mutation surface.

Split out of :mod:`clio_agent.gact.routes.artifacts` (no-accretion ground rule) so
the read/pin route file stays the owner of the query surface and the alias mutation
— a distinct write concern — lives here. Registered by
:func:`clio_agent.gact.routes.artifacts.register_artifacts_routes` via a lazy call,
so both modules load without a cycle (this module imports the shared route helpers
from ``routes.artifacts``, which is already fully loaded by registration time).

The reserved ``latest`` alias is auto-maintained to the head (S1); only user aliases
move here. The move is last-writer-wins; the emitted ``artifact.alias.moved`` event
carries ``(at, event_id)`` so a boot replay converges on the same map regardless of
order (fold determinism, S4).
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI, Request


def _workspace_default_session(app: FastAPI, workspace_id: str) -> str:
    """A session id bound to ``workspace_id`` for a workspace-scoped emit (or ``""``).

    The alias-move route is workspace-scoped, but ``_emit_semantic_event`` is keyed by
    session; the durable ``artifact.alias.moved`` fold keys on workspace/name/alias
    (not the session), so any bound session — or ``""`` — is a correct emit anchor.
    """
    store = getattr(app.state, "sessions", None) or getattr(app.state, "session_store", None)
    if store is None:
        return ""
    try:
        sessions = store.list(workspace_id=workspace_id)
    except Exception:  # noqa: BLE001 — a best-effort anchor, never load-bearing
        return ""
    return str(getattr(sessions[0], "id", "") or "") if sessions else ""


def register_artifact_alias_routes(app: FastAPI) -> None:
    """Register ``POST /v1/workspaces/{wid}/artifacts/{name}/aliases`` on ``app``."""
    from clio_agent.gact.routes.artifacts import (  # noqa: PLC0415
        _artifact_error,
        _audit,
        _available_refs,
        _record_wire,
        _registry,
        _resolve_ref,
    )

    @app.post("/v1/workspaces/{wid}/artifacts/{name}/aliases")
    async def move_artifact_alias(wid: str, name: str, request: Request) -> dict[str, Any]:
        """Move a mutable alias to a version, emitting ``artifact.alias.moved``.

        Body ``{alias, ref}`` where ``ref`` is ``latest`` | ``vN`` | an existing alias.
        The reserved ``latest`` alias is auto-maintained, so moving it by hand is
        refused ``422``.
        """
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001 — a bad body is a typed 422, not a 500
            raise _artifact_error(
                status_code=422,
                error="invalid_request",
                message="alias request body must be JSON",
                recoverable=True,
            ) from exc
        if not isinstance(body, dict):
            raise _artifact_error(
                status_code=422,
                error="invalid_request",
                message="alias request body must be an object",
                recoverable=True,
            )
        alias = str(body.get("alias") or "").strip()
        ref = str(body.get("ref") or "").strip()
        if not alias or not ref:
            raise _artifact_error(
                status_code=422,
                error="invalid_request",
                message="alias move requires 'alias' and 'ref'",
                details={"workspace_id": wid, "name": name},
                recoverable=True,
            )
        from clio_agent.gact.artifacts.records import alias_rejection_reason  # noqa: PLC0415

        alias_reason = alias_rejection_reason(alias)
        if alias_reason == "reserved_alias":
            raise _artifact_error(
                status_code=422,
                error="reserved_alias",
                message="'latest' is auto-maintained to the head and cannot be moved by hand",
                details={"workspace_id": wid, "name": name},
                recoverable=True,
            )
        if alias_reason == "invalid_alias":
            # Finding [7]: a ``vN``-shaped alias collides with the version grammar
            # (``_resolve_ref`` tests ``vN`` before the alias map), so it would be
            # silently shadowed and advertised-but-unresolvable — refuse it here AND
            # at the record layer (``move_alias``).
            raise _artifact_error(
                status_code=422,
                error="invalid_alias",
                message=(
                    f"alias {alias!r} collides with the reserved version grammar (vN); "
                    "choose a name that is not version-shaped"
                ),
                details={"workspace_id": wid, "name": name, "alias": alias},
                recoverable=True,
            )
        registry = await _registry(app)
        record = registry.get(wid, name)
        if record is None:
            raise _artifact_error(
                status_code=404,
                error="not_found",
                message=f"artifact not found: {name}",
                details={"workspace_id": wid, "name": name},
            )
        target = _resolve_ref(record, ref)
        if target is None:
            raise _artifact_error(
                status_code=404,
                error="not_found",
                message=f"alias target ref not resolvable: {ref}",
                details={
                    "workspace_id": wid,
                    "name": name,
                    "ref": ref,
                    "available": _available_refs(record),
                },
            )
        from clio_agent.gact.artifacts.registry import InvalidAliasError  # noqa: PLC0415
        from clio_agent.gact.artifacts.versions import emit_alias_moved  # noqa: PLC0415
        from clio_agent.gact.runtime.globals import _iso_from_epoch  # noqa: PLC0415
        from clio_agent.gact.semantic_events import _event_id  # noqa: PLC0415

        # Finding [5]: compute the move's ``(at, event_id)`` BEFORE the move so the
        # live apply is decided by the SAME last-writer-wins comparator the fold uses,
        # and the emitted event carries the identical key.
        at = _iso_from_epoch(time.time())
        event_id = _event_id()
        try:
            moved = registry.move_alias(
                wid, name, alias=alias, to_version=target.version, at=at, event_id=event_id
            )
        except InvalidAliasError as exc:
            raise _artifact_error(
                status_code=422,
                error=exc.reason,
                message=str(exc),
                details={"workspace_id": wid, "name": name, "alias": alias},
                recoverable=True,
            ) from exc
        if moved is None:
            raise _artifact_error(
                status_code=404,
                error="not_found",
                message="alias could not be moved (record or version missing)",
                details={"workspace_id": wid, "name": name, "alias": alias, "ref": ref},
            )
        from_version, to_version, applied = moved
        if not applied:
            # A stale live move (an older ``(at, event_id)`` than the recorded winner
            # — clock regression / a racing pipelined move). Refused IDENTICALLY to the
            # fold's ``stale_alias_move`` no-op: no state change, no event emitted.
            raise _artifact_error(
                status_code=409,
                error="stale_alias_move",
                message="alias move is stale (a newer move already won); ignored",
                details={"workspace_id": wid, "name": name, "alias": alias, "ref": ref},
                recoverable=True,
            )

        emit_alias_moved(
            app,
            _workspace_default_session(app, wid),
            workspace_id=wid,
            name=name,
            alias=alias,
            from_version=from_version,
            to_version=to_version,
            at=at,
            event_id=event_id,
        )
        _audit(app, route="move_artifact_alias", workspace_id=wid, name=name, alias=alias)
        return {
            "artifact": _record_wire(record, registry),
            "alias": alias,
            "from_version": from_version,
            "to_version": to_version,
        }


__all__ = ["register_artifact_alias_routes"]
