"""Grants on the record — boundary events, mid-session root grants, deny-mode egress (B5 #979).

Every effective-boundary change is a recorded DECISION by a user or the model, never a
deterministic clio choice (⚑ #974.8). This module is the owner of that record layer, built
entirely on the EXISTING permission gate + policy store — a new request KIND, not a new gate:

* :func:`emit_boundary_granted` / :func:`emit_boundary_revoked` — the ``boundary.*`` semantic
  events (SSE-listed) that make a write-root or domain grant/revoke observable live + durable.
* :func:`apply_root_grant` — a mid-session workspace root grant: register the root into the
  ONE grant registry (:mod:`clio_agent.runtime.sandbox_roots`) so the fence + advisory twin
  widen LIVE on the next spawn, persist it on the workspace record, and emit
  ``boundary.granted{kind: root}``. On a session-wide srt fence (Windows) already-spawned
  fleet children keep their compile-time territory until they respawn — reported as a typed
  ``grant_pending_respawn`` (never silence).
* deny-mode egress: :func:`workspace_deny_mode` reads the opt-in per-workspace flag;
  :func:`install_egress_gate` wires the chokepoint's CONNECT-time gate to consult the
  ``host_pattern`` policies and, on an unknown domain, open the EXISTING interactive
  permission gate via a ``network_egress`` request kind — resolution ``allow_workspace``
  derives a sticky ``host_pattern`` policy (``created_from_permission_id`` provenance) and
  emits ``boundary.granted{kind: domain}``.

Route layers (routes/workspaces.py, routes/permissions.py) call the thin emit/apply helpers
here; the god app modules never grow grant logic (no accretion).
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.gact.events import Event
from clio_agent.gact.runtime.permission_policies import (
    NETWORK_EGRESS_REQUEST_KIND,
    _host_action_for,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.runtime.net_chokepoint import EgressRecord

logger = logging.getLogger(__name__)

BOUNDARY_GRANTED_EVENT = "boundary.granted"
BOUNDARY_REVOKED_EVENT = "boundary.revoked"

#: Grant ``kind`` — a filesystem write-root boundary vs a network domain boundary.
KIND_ROOT = "root"
KIND_DOMAIN = "domain"

#: Grant ``scope`` — a single session vs the whole workspace.
SCOPE_SESSION = "session"
SCOPE_WORKSPACE = "workspace"

#: Who decided the grant (⚑ never deterministic clio): a direct user action, an interactive
#: model-driven request the user resolved, or a replay of a persisted sticky policy at boot.
GRANTOR_USER = "user"
GRANTOR_MODEL_REQUEST = "model-request"
GRANTOR_POLICY = "policy"

#: Typed root-grant application reasons (no silent fallback).
REASON_GRANT_LIVE = "grant_applied_live"  # next spawn/invocation uses the widened territory
REASON_GRANT_PENDING_RESPAWN = "grant_pending_respawn"  # session-wide fence: needs a respawn
REASON_GRANT_RECORDED_NO_FENCE = "grant_recorded_no_active_fence"  # floor: advisory-only widen

#: Deny-mode flag key on the workspace ``config`` (opt-in per workspace; config/state, not env).
DENY_MODE_CONFIG_KEY = "network_deny_mode"
#: Persisted list of granted write roots on the workspace ``config`` (replayed into the live
#: registry at boot so a recorded grant survives a restart — no new store, RULE 4).
GRANTED_ROOTS_CONFIG_KEY = "granted_write_roots"

#: How long a deny-mode egress prompt blocks the chokepoint connection thread before a typed
#: timeout denial (mirrors the tool gate's ``DEFAULT_TIMEOUT_S``).
_EGRESS_GATE_TIMEOUT_S = 600.0


# --------------------------------------------------------------------------- #
# boundary.* semantic events
# --------------------------------------------------------------------------- #


def _active_session_for_workspace(app: "FastAPI", workspace_id: str) -> str:
    """Return a representative live session id for ``workspace_id`` (recency), else ``""``.

    The ``boundary.*`` SSE stream is per-session (``GET /v1/sessions/{sid}/events``), so a
    workspace-scoped grant attributes to the workspace's most-recent session to reach the
    user's live stream. Durable trace + ARC capture the event regardless of attribution.
    """
    sessions = getattr(app.state, "sessions", None)
    if sessions is None:
        return ""
    try:
        rows = sessions.list(workspace_id=workspace_id)
    except TypeError:
        rows = [s for s in sessions.list() if getattr(s, "workspace_id", "") == workspace_id]
    return str(getattr(rows[0], "id", "") or "") if rows else ""


def _emit_boundary(
    app: "FastAPI",
    event_type: str,
    *,
    kind: str,
    scope: str,
    grantor: str,
    pattern: str,
    workspace_id: str = "",
    session_id: str = "",
    created_from_permission_id: str = "",
    reason: str = "",
) -> None:
    """Emit one ``boundary.granted``/``boundary.revoked`` semantic event (SSE-listed). Guarded."""
    sid = session_id or _active_session_for_workspace(app, workspace_id)
    payload: dict[str, Any] = {
        "kind": kind,
        "scope": scope,
        "grantor": grantor,
        "pattern": pattern,
        "workspace_id": workspace_id,
    }
    if created_from_permission_id:
        payload["created_from_permission_id"] = created_from_permission_id
    if reason:
        payload["reason"] = reason
    verb = "granted" if event_type == BOUNDARY_GRANTED_EVENT else "revoked"
    try:
        from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415

        _emit_semantic_event(
            app,
            sid,
            event_type,
            status="completed",
            summary=f"{kind} boundary {verb} ({scope}): {pattern}",
            actor={"role": "runtime", "component": "grants", "grantor": grantor},
            subject={
                "kind": kind,
                "scope": scope,
                "pattern": pattern,
                "workspace_id": workspace_id,
            },
            payload=payload,
        )
    except Exception as exc:  # noqa: BLE001 - a boundary record must never break the mutation
        logger.warning(
            "boundary event emit skipped reason=boundary_emit_failed type=%s pattern=%s error=%r",
            event_type,
            pattern,
            exc,
        )


def emit_boundary_granted(
    app: "FastAPI",
    *,
    kind: str,
    scope: str,
    grantor: str,
    pattern: str,
    workspace_id: str = "",
    session_id: str = "",
    created_from_permission_id: str = "",
    reason: str = "",
) -> None:
    """Emit ``boundary.granted`` — a new effective write-root / domain boundary (B5 #979.1)."""
    _emit_boundary(
        app,
        BOUNDARY_GRANTED_EVENT,
        kind=kind,
        scope=scope,
        grantor=grantor,
        pattern=pattern,
        workspace_id=workspace_id,
        session_id=session_id,
        created_from_permission_id=created_from_permission_id,
        reason=reason,
    )


def emit_boundary_revoked(
    app: "FastAPI",
    *,
    kind: str,
    scope: str,
    grantor: str,
    pattern: str,
    workspace_id: str = "",
    session_id: str = "",
) -> None:
    """Emit ``boundary.revoked`` — an effective boundary removed (B5 #979.1)."""
    _emit_boundary(
        app,
        BOUNDARY_REVOKED_EVENT,
        kind=kind,
        scope=scope,
        grantor=grantor,
        pattern=pattern,
        workspace_id=workspace_id,
        session_id=session_id,
    )


def emit_session_attach_boundary(app: "FastAPI", session_id: str, workspace_id: str) -> None:
    """Emit ``boundary.granted{kind: root, scope: session}`` on session→workspace attach (#979.2).

    A session inherits its workspace's write-root as its effective territory; surfacing that as
    a boundary event closes the silent session-attach mutation. Called from the (thin) session
    route so WorkspaceStore stays leaf-pure. Guarded — never break session creation.
    """
    try:
        ws = app.state.workspaces.get(workspace_id)
        root = str(getattr(ws, "root_path", "") or "") if ws is not None else ""
        if not root:
            return
        emit_boundary_granted(
            app,
            kind=KIND_ROOT,
            scope=SCOPE_SESSION,
            grantor=GRANTOR_USER,
            pattern=root,
            workspace_id=workspace_id,
            session_id=session_id,
        )
    except Exception as exc:  # noqa: BLE001 - boundary emit is observability, never fatal
        logger.debug("session-attach boundary emit skipped session=%s error=%r", session_id, exc)


def emit_boundary_for_derived_policy(
    app: "FastAPI", row: dict[str, Any], policy: Optional[dict[str, Any]]
) -> None:
    """Emit ``boundary.granted{kind: domain}`` when a resolution derived a ``host_pattern``.

    Called from the permission-resolution route after
    :func:`_append_permission_policy_from_resolution`. Only a DOMAIN boundary (a deny-mode
    ``network_egress`` grant) is a ``boundary.*`` event here — a generic tool-permission
    sticky policy is not a write-root/domain boundary, so it emits nothing (precision).
    """
    if not isinstance(policy, dict):
        return
    host = str(policy.get("host_pattern") or "")
    if not host:
        return
    emit_boundary_granted(
        app,
        kind=KIND_DOMAIN,
        scope=SCOPE_WORKSPACE if str(policy.get("scope")) == SCOPE_WORKSPACE else SCOPE_SESSION,
        grantor=GRANTOR_MODEL_REQUEST,
        pattern=host,
        workspace_id=str(policy.get("scope_id") or "")
        if policy.get("scope") == SCOPE_WORKSPACE
        else "",
        session_id=str(row.get("session_id") or ""),
        created_from_permission_id=str(policy.get("created_from_permission_id") or ""),
    )


# --------------------------------------------------------------------------- #
# Mid-session workspace root grants
# --------------------------------------------------------------------------- #


def _grant_reason_for_fence() -> str:
    """The typed root-grant application reason for THIS process's resolved fence."""
    try:
        from clio_agent.runtime import sandbox  # noqa: PLC0415

        state = sandbox.current_state()
    except Exception:  # noqa: BLE001
        state = None
    if state is None or not getattr(state, "active", False):
        return REASON_GRANT_RECORDED_NO_FENCE
    # srt Windows fs policy is SESSION-WIDE (written once per child at spawn): a live child
    # keeps its territory until it respawns, so the grant is pending-respawn, never silent.
    if getattr(state, "mechanism", "") == sandbox.MECHANISM_SRT_WINDOWS:
        return REASON_GRANT_PENDING_RESPAWN
    return REASON_GRANT_LIVE


def apply_root_grant(
    app: "FastAPI",
    workspace_id: str,
    path: str,
    *,
    grantor: str = GRANTOR_USER,
    created_from_permission_id: str = "",
    emit: bool = True,
) -> dict[str, Any]:
    """Apply a mid-session workspace root grant on the record (B5 #979.3).

    Registers ``path`` as writable territory for the workspace's root (the live fence +
    advisory both widen on the next spawn/check), persists it on the workspace record so it
    survives a restart, and emits ``boundary.granted{kind: root}``. Returns a typed result
    ``{granted, pattern, reason, pending_respawn}`` — ``grant_pending_respawn`` on a
    session-wide srt fence (Windows), ``grant_applied_live`` on a per-spawn fence,
    ``grant_recorded_no_active_fence`` on the floor. Never raises for a missing fence.
    """
    from clio_agent.runtime.sandbox_roots import register_write_root_grant  # noqa: PLC0415

    ws = app.state.workspaces.get(workspace_id)
    workspace_root = str(getattr(ws, "root_path", "") or "") if ws is not None else ""
    resolved = register_write_root_grant(workspace_root, path)
    pattern = str(resolved)
    # Persist onto the workspace record (no new store — RULE 4): the boot replay reads it back.
    # Mutate config BEFORE the store flush so ``update`` serialises the new list under the lock
    # (a post-update mutation would not reach disk until the next write).
    if ws is not None:
        existing = list(ws.config.get(GRANTED_ROOTS_CONFIG_KEY) or [])
        if pattern not in existing:
            existing.append(pattern)
        ws.config[GRANTED_ROOTS_CONFIG_KEY] = existing
        app.state.workspaces.update(workspace_id, metadata_patch=None)  # flush + bump updated_at
    reason = _grant_reason_for_fence()
    if emit:
        emit_boundary_granted(
            app,
            kind=KIND_ROOT,
            scope=SCOPE_WORKSPACE,
            grantor=grantor,
            pattern=pattern,
            workspace_id=workspace_id,
            created_from_permission_id=created_from_permission_id,
            reason=reason,
        )
    return {
        "granted": True,
        "pattern": pattern,
        "reason": reason,
        "pending_respawn": reason == REASON_GRANT_PENDING_RESPAWN,
    }


def replay_persisted_root_grants(app: "FastAPI") -> None:
    """Replay each workspace's persisted root grants into the live registry (boot, guarded).

    A recorded grant must survive a restart (RULE 4: no new store — it rides the workspace
    record). Re-registers without emitting a boundary event (no NEW decision was made).
    """
    from clio_agent.runtime.sandbox_roots import register_write_root_grant  # noqa: PLC0415

    workspaces = getattr(app.state, "workspaces", None)
    if workspaces is None:
        return
    try:
        rows = workspaces.list()
    except Exception:  # noqa: BLE001 - boot replay is best-effort
        return
    for ws in rows:
        root = str(getattr(ws, "root_path", "") or "")
        for granted in getattr(ws, "config", {}).get(GRANTED_ROOTS_CONFIG_KEY, []) or []:
            try:
                register_write_root_grant(root, str(granted))
            except Exception as exc:  # noqa: BLE001
                logger.warning("root grant replay skipped reason=replay_failed error=%r", exc)


# --------------------------------------------------------------------------- #
# Deny-mode egress gate (opt-in per workspace)
# --------------------------------------------------------------------------- #


def workspace_deny_mode(app: "FastAPI", workspace_id: str) -> bool:
    """Whether ``workspace_id`` opts into network deny mode (default ALLOW+RECORD, B4)."""
    ws = app.state.workspaces.get(workspace_id) if workspace_id else None
    if ws is None:
        return False
    for source in (getattr(ws, "config", {}) or {}, getattr(ws, "metadata", {}) or {}):
        if bool(source.get(DENY_MODE_CONFIG_KEY)):
            return True
    return False


def _workspace_id_for_root(app: "FastAPI", workspace_root: str) -> str:
    """Resolve the workspace whose ``root_path`` matches ``workspace_root`` (best-effort)."""
    if not workspace_root:
        return ""
    from pathlib import Path  # noqa: PLC0415

    try:
        target = str(Path(workspace_root).expanduser().resolve(strict=False))
    except OSError:
        target = workspace_root
    workspaces = getattr(app.state, "workspaces", None)
    if workspaces is None:
        return ""
    for ws in workspaces.list():
        root = str(getattr(ws, "root_path", "") or "")
        if not root:
            continue
        try:
            resolved = str(Path(root).expanduser().resolve(strict=False))
        except OSError:
            resolved = root
        if resolved == target or target.startswith(resolved):
            return str(getattr(ws, "id", "") or "")
    return ""


def _egress_gate_decision(app: "FastAPI", rec: "EgressRecord") -> str:
    """Decide allow/deny for one deny-mode CONNECT (B5 #979.5). Returns ``"allow"``/``"deny"``.

    Consults the workspace's ``host_pattern`` policies; an unknown domain opens the EXISTING
    interactive permission gate via a ``network_egress`` request kind and blocks the chokepoint
    connection thread until the user resolves it (or a typed timeout denial). A resolution of
    ``allow``/``allow_session``/``allow_workspace`` lets the CONNECT through and (for
    ``allow_workspace``) leaves a sticky ``host_pattern`` policy so subsequent CONNECTs need no
    gate; ``deny``/timeout blocks it with a recorded reason.
    """
    workspace_id = _workspace_id_for_root(app, rec.workspace_root)
    if not workspace_deny_mode(app, workspace_id):
        return "allow"  # default is ALLOW + RECORD (B4); deny mode is strictly opt-in
    host = (rec.host or "").strip().lower()
    action = _host_action_for(app, workspace_id=workspace_id, host=host)
    if action == "deny":
        _record_egress_denied(app, workspace_id, host, reason="policy_deny")
        return "deny"
    if action in {"allow", "allow_session", "allow_workspace"}:
        return "allow"
    return _prompt_egress(app, workspace_id, rec)


def _prompt_egress(app: "FastAPI", workspace_id: str, rec: "EgressRecord") -> str:
    """Open the interactive gate for an unknown deny-mode domain; block until resolved."""
    session_id = _active_session_for_workspace(app, workspace_id)
    host = (rec.host or "").strip().lower()
    pid = f"perm_{uuid.uuid4().hex[:12]}"
    evt = threading.Event()
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "id": pid,
        "session_id": session_id,
        "kind": NETWORK_EGRESS_REQUEST_KIND,
        "tool_call": {
            "tool_name": NETWORK_EGRESS_REQUEST_KIND,
            "input": {"host": host, "port": rec.port},
        },
        "summary": f"network egress to {host}:{rec.port} requested (deny mode)",
        "created_at": now,
        "status": "pending",
    }
    try:
        app.state.permissions[pid] = row
        app.state.permission_events[pid] = evt
    except Exception as exc:  # noqa: BLE001 - a wedged prompt store must fail SAFE (deny)
        logger.warning("egress prompt store failed reason=prompt_unwritable error=%r", exc)
        return "deny"
    _emit_egress_requested(app, session_id, row)
    if not evt.wait(timeout=_EGRESS_GATE_TIMEOUT_S):
        row["status"] = "timeout"
        _record_egress_denied(app, workspace_id, host, reason="egress_gate_timeout")
        return "deny"
    action = str(row.get("action") or "deny")
    return "allow" if action in {"allow", "allow_session", "allow_workspace"} else "deny"


def _emit_egress_requested(app: "FastAPI", session_id: str, row: dict[str, Any]) -> None:
    """Publish ``permission.requested`` (semantic + bus) for a deny-mode egress prompt."""
    try:
        from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415

        _emit_semantic_event(
            app,
            session_id,
            "permission.requested",
            status="pending",
            summary=str(row.get("summary") or "network egress requested"),
            actor={"role": "runtime", "component": "net-chokepoint"},
            subject={"permission_id": row["id"], "kind": NETWORK_EGRESS_REQUEST_KIND},
            payload=row,
        )
    except Exception as exc:  # noqa: BLE001 - the emit must never wedge the gate
        logger.debug("egress permission.requested emit skipped error=%r", exc)
    bus = getattr(app.state, "bus", None)
    if bus is not None:
        bus.publish(Event(type="permission.requested", session_id=session_id, payload=row))


def _record_egress_denied(app: "FastAPI", workspace_id: str, host: str, *, reason: str) -> None:
    """Emit a typed ``permission.resolved`` deny record for a blocked deny-mode egress."""
    try:
        from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415

        session_id = _active_session_for_workspace(app, workspace_id)
        _emit_semantic_event(
            app,
            session_id,
            "permission.resolved",
            status="completed",
            summary=f"network egress to {host} denied ({reason})",
            actor={"role": "runtime", "component": "net-chokepoint"},
            subject={"kind": NETWORK_EGRESS_REQUEST_KIND, "host": host},
            payload={
                "action": "deny",
                "reason": reason,
                "host": host,
                "workspace_id": workspace_id,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("egress deny record skipped error=%r", exc)


def install_egress_gate(app: "FastAPI") -> None:
    """Wire the chokepoint's CONNECT-time deny-mode gate to THIS app (server lifespan, guarded).

    Registers a closure over ``app`` so the runtime-side chokepoint never imports gact. The
    gate is consulted at connection OPEN, before the upstream dial; a workspace NOT in deny mode
    always returns allow (the B4 default is ALLOW + RECORD), so wiring it is inert until a
    workspace opts in.
    """
    try:
        from clio_agent.runtime.net_chokepoint import set_egress_gate  # noqa: PLC0415

        set_egress_gate(lambda rec: _egress_gate_decision(app, rec))
    except Exception as exc:  # noqa: BLE001 - gate wiring is best-effort, never blocks boot
        logger.warning("egress gate wiring skipped reason=egress_gate_wire_failed error=%r", exc)


__all__ = [
    "BOUNDARY_GRANTED_EVENT",
    "BOUNDARY_REVOKED_EVENT",
    "DENY_MODE_CONFIG_KEY",
    "GRANTED_ROOTS_CONFIG_KEY",
    "GRANTOR_MODEL_REQUEST",
    "GRANTOR_POLICY",
    "GRANTOR_USER",
    "KIND_DOMAIN",
    "KIND_ROOT",
    "REASON_GRANT_LIVE",
    "REASON_GRANT_PENDING_RESPAWN",
    "REASON_GRANT_RECORDED_NO_FENCE",
    "SCOPE_SESSION",
    "SCOPE_WORKSPACE",
    "apply_root_grant",
    "emit_boundary_for_derived_policy",
    "emit_boundary_granted",
    "emit_boundary_revoked",
    "emit_session_attach_boundary",
    "install_egress_gate",
    "replay_persisted_root_grants",
    "workspace_deny_mode",
]
