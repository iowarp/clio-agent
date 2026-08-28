"""Grants on the record — boundary events, mid-session root grants, deny-mode egress (B5 #979).

Every effective-boundary change is a recorded DECISION by a user or the model, never a
deterministic clio choice (⚑ #974.8). This module is the owner of that record layer, built
entirely on the EXISTING permission gate + policy store — a new request KIND, not a new gate:

* :func:`apply_root_grant` — a mid-session workspace root grant: register the root into the
  ONE grant registry (:mod:`clio_agent.runtime.sandbox_roots`) so the fence + advisory twin
  widen LIVE on the next spawn, persist it on the workspace record, restart the workspace's
  resident fleet so an already-spawned, workspace-shared child actually picks up the widened
  territory (#1033 — otherwise ``grant_applied_live`` over-claims), and emit
  ``boundary.granted{kind: root}``. The restart is DRAIN-AWARE: a busy/leased fleet is never
  torn down mid-call — the grant defers to the next safe boundary and reports
  ``grant_restart_deferred_busy`` (never silence).
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

from clio_agent.gact.permission_delivery import publish_permission_event as publish_permission
from clio_agent.gact.runtime.grant_revocation import revoke_root_grant
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
#: #1044 — the one-shot AI reviewer (ai-review mode) auto-decided the grant (recorded, never silent).
GRANTOR_REVIEWER = "reviewer"

#: Typed root-grant application reasons (no silent fallback). #1033 replaced the dead
#: ``grant_pending_respawn`` with the two real drain-aware fleet-restart outcomes: an idle fleet
#: restarts NOW (``grant_restarted_live``), a busy one defers to the next safe boundary
#: (``grant_restart_deferred_busy``). ``grant_applied_live`` is the honest reason when there is no
#: resident fleet child to restart (the next spawn reads the widened territory).
REASON_GRANT_LIVE = "grant_applied_live"  # no resident child: next spawn uses the widened territory
REASON_GRANT_RESTARTED_LIVE = "grant_restarted_live"  # resident fleet restarted → live now
REASON_GRANT_DEFERRED_BUSY = "grant_restart_deferred_busy"  # busy fleet: restart deferred to drain
REASON_GRANT_RECORDED_NO_FENCE = "grant_recorded_no_active_fence"  # floor: advisory-only widen
#: A restart-WIRING failure (the request itself raised): a resident child may keep stale territory,
#: so surface it honestly instead of collapsing to ``grant_applied_live`` (#1033).
REASON_GRANT_RESTART_FAILED = "grant_restart_failed"

#: Deny-mode flag key on the workspace ``config`` (opt-in per workspace; config/state, not env).
DENY_MODE_CONFIG_KEY = "network_deny_mode"
#: Write-gate flag key on the workspace ``config`` (opt-in, DEFAULT OFF — N2). When set, a
#: WRITE-SHAPED egress to an un-granted host escalates to a human ask; reads/opaque CONNECTs stay open.
NETWORK_WRITE_GATE_CONFIG_KEY = "network_write_gate"
#: The plain-HTTP request verbs clio classifies as WRITE-SHAPED (N2). A CONNECT tunnel carries
#: ``method=""`` (opaque) so it is NEVER write-shaped — clio never over-claims a write-gate on it.
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
#: Persisted list of granted write roots on the workspace ``config`` (replayed into the live
#: registry at boot so a recorded grant survives a restart — RULE 4, no new store).
GRANTED_ROOTS_CONFIG_KEY = "granted_write_roots"

#: How long a deny-mode egress prompt blocks the chokepoint connection thread before a typed
#: timeout denial (mirrors the tool gate's ``DEFAULT_TIMEOUT_S``).
_EGRESS_GATE_TIMEOUT_S = 600.0

#: Bound on DISTINCT concurrently-pending deny-mode egress prompts (review finding 4): a flood
#: of unknown-domain CONNECTs must not spawn unbounded blocked prompts. Connects to the SAME
#: ``(workspace, host)`` COALESCE onto one prompt (below); once this many distinct prompts are
#: already open, a further distinct-host connect fails CLOSED with a typed reason (never blocks).
_MAX_CONCURRENT_EGRESS_PROMPTS = 32

#: Live deny-mode egress prompts, keyed ``(workspace_id, host)`` so concurrent connects to the
#: same domain share ONE user prompt + resolution. Each value is a small holder
#: ``{pid, event, row, waiters}``; the entry is dropped when the last waiter leaves.
_PENDING_EGRESS: dict[tuple[str, str], dict[str, Any]] = {}
_PENDING_EGRESS_LOCK = threading.Lock()

#: Typed deny reasons recorded on the trace when a deny-mode egress is blocked (no silent
#: fallback — every boundary denial reaches the trace via :func:`_record_egress_denied`).
REASON_EGRESS_POLICY_DENY = "policy_deny"
REASON_EGRESS_TIMEOUT = "egress_gate_timeout"
REASON_EGRESS_PROMPT_CAP = "egress_gate_prompt_cap_reached"
REASON_EGRESS_STORE_UNRESOLVED = "egress_gate_store_unresolved"
REASON_EGRESS_DECISION_ERROR = "egress_gate_decision_error"
REASON_EGRESS_PROMPT_UNWRITABLE = "egress_gate_prompt_unwritable"
#: N2 write-gate typed reasons (never a silent path). A write-gate decision that RAISES fails
#: CLOSED (deny) for a write-shaped egress — a wiring bug must not silently allow a write; a
#: matching ``deny`` host policy on a write is recorded distinctly from the deny-mode read deny.
REASON_EGRESS_WRITE_GATE_ERROR = "egress_write_gate_error"
REASON_EGRESS_WRITE_POLICY_DENY = "write_policy_deny"


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
    # The surviving backends (Codex, Landlock) write the write territory PER SPAWN, so a
    # mid-session root grant takes effect on the child's next spawn — an applied-live grant.
    return REASON_GRANT_LIVE


def _fold_restart_into_reason(base_reason: str, restart_outcome: str) -> str:
    """Fold the fleet-restart outcome into the grant's typed reason (#1033 — no over-claim).

    A resident workspace-shared fleet child keeps its compile-time territory, so the
    ``grant_applied_live`` base reason over-claims until it respawns. This reports what actually
    happened: restarted now, deferred to a drain, or nothing resident (base per-fence reason).
    """
    from clio_agent.tools.reaper import (  # noqa: PLC0415
        RESTART_DEFERRED_BUSY,
        RESTART_RESTARTED_LIVE,
    )

    if restart_outcome == REASON_GRANT_RESTART_FAILED:
        return REASON_GRANT_RESTART_FAILED  # never folds to applied_live
    if restart_outcome == RESTART_RESTARTED_LIVE:
        return REASON_GRANT_RESTARTED_LIVE
    if restart_outcome == RESTART_DEFERRED_BUSY:
        return REASON_GRANT_DEFERRED_BUSY
    return base_reason  # no resident child / typed skip: the base per-fence reason is honest


def _request_fleet_restart(app: "FastAPI", workspace_root: str, *, widened: bool) -> str:
    """Ask the live agent to restart ``workspace_root``'s resident fleet (#1033). Guarded, typed.

    Returns a fleet-restart outcome string. A no-op (returns
    :data:`~clio_agent.tools.reaper.RESTART_NO_RESIDENT`) when the territory did not actually
    widen (an idempotent re-grant must not recycle the shared fleet — risk #1) or when no live
    agent is mounted (the unit / pre-agent context — a typed skip, never a silent success).
    Never raises: a restart-wiring failure returns ``REASON_GRANT_RESTART_FAILED`` (honest + logged).
    """
    from clio_agent.tools.reaper import RESTART_NO_RESIDENT  # noqa: PLC0415

    if not widened or not workspace_root:
        return RESTART_NO_RESIDENT
    agent = getattr(app.state, "agent", None)
    restart = getattr(agent, "request_fleet_restart", None) if agent is not None else None
    if not callable(restart):
        logger.debug(
            "fleet restart skipped reason=no_live_agent root=%s (unit/pre-agent context)",
            workspace_root,
        )
        return RESTART_NO_RESIDENT
    try:
        return str(restart(workspace_root))
    except Exception as exc:  # noqa: BLE001 — a restart-wiring failure must never break the grant
        # Honest failure, NOT no-resident (which folds to grant_applied_live): a resident child may
        # keep stale territory — surface it on the reason field, not only the log.
        logger.warning(
            "fleet restart failed reason=fleet_restart_failed root=%s error=%r",
            workspace_root,
            exc,
        )
        return REASON_GRANT_RESTART_FAILED


def apply_root_grant(
    app: "FastAPI",
    workspace_id: str,
    path: str,
    *,
    grantor: str = GRANTOR_USER,
    created_from_permission_id: str = "",
    emit: bool = True,
) -> dict[str, Any]:
    """Apply a mid-session workspace root grant on the record (B5 #979.3, #1033).

    Registers ``path`` as writable territory, persists it on the workspace record, restarts the
    resident fleet so an already-spawned child picks up the widened territory (#1033), then emits
    ``boundary.granted{kind: root}``. Returns ``{granted, pattern, reason, restart_deferred}`` with a
    typed ``reason``: ``grant_restarted_live`` (idle fleet restarted), ``grant_restart_deferred_busy``
    (busy fleet deferred to the next drain), ``grant_restart_failed`` (restart raised — a resident
    child may keep stale territory, surfaced honestly), ``grant_applied_live`` (nothing resident), or
    ``grant_recorded_no_active_fence`` (advisory floor). Never raises for a missing fence.
    """
    from clio_agent.runtime.sandbox_roots import register_write_root_grant  # noqa: PLC0415

    ws = app.state.workspaces.get(workspace_id)
    workspace_root = str(getattr(ws, "root_path", "") or "") if ws is not None else ""
    resolved = register_write_root_grant(workspace_root, path)
    pattern = str(resolved)
    # Persist onto the workspace record (no new store — RULE 4) BEFORE the flush so ``update``
    # serialises the new list under the lock. ``widened`` tracks whether the territory ACTUALLY
    # changed (risk #1: an idempotent re-grant must not recycle the shared fleet).
    widened = True
    if ws is not None:
        existing = list(ws.config.get(GRANTED_ROOTS_CONFIG_KEY) or [])
        widened = pattern not in existing
        if widened:
            existing.append(pattern)
        ws.config[GRANTED_ROOTS_CONFIG_KEY] = existing
        app.state.workspaces.update(workspace_id, metadata_patch=None)  # flush + bump updated_at
    # Restart the resident fleet AFTER the territory flush (risk #6: the rebuilt child must read
    # the persisted, widened roots), then fold the real outcome into the reason (no over-claim).
    base_reason = _grant_reason_for_fence()
    if base_reason == REASON_GRANT_LIVE:
        # Active per-spawn fence: a resident workspace-shared child would keep its
        # compile-time territory, so restart it (drain-aware) and fold the real outcome.
        restart_outcome = _request_fleet_restart(app, workspace_root, widened=widened)
        reason = _fold_restart_into_reason(base_reason, restart_outcome)
    else:
        # Advisory floor: no fence enforces, so no resident child to restart.
        reason = base_reason
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
        "restart_deferred": reason == REASON_GRANT_DEFERRED_BUSY,
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


def workspace_write_gate(app: "FastAPI", workspace_id: str) -> bool:
    """Whether ``workspace_id`` opts into the network WRITE-gate (default OFF — N2).

    Mirrors :func:`workspace_deny_mode`: a per-workspace opt-in flag on ``config``/``metadata``,
    DEFAULT FALSE so this adds the write-gating CAPABILITY without changing the B4 default
    posture or breaking legitimate fleet POSTs. When on, a write-shaped egress to an un-granted
    host is escalated for human permission (see :func:`_write_gate_decision`).
    """
    ws = app.state.workspaces.get(workspace_id) if workspace_id else None
    if ws is None:
        return False
    for source in (getattr(ws, "config", {}) or {}, getattr(ws, "metadata", {}) or {}):
        if bool(source.get(NETWORK_WRITE_GATE_CONFIG_KEY)):
            return True
    return False


def _workspace_id_for_root(app: "FastAPI", workspace_root: str) -> str:
    """Resolve the workspace whose ``root_path`` contains ``workspace_root``.

    Path-BOUNDARY aware (review finding 3): a raw ``startswith`` would let ``/ws`` match
    ``/ws2`` — an adjacent, differently-permissioned workspace. Match only on real path
    containment (equality or ``is_relative_to``). A ``workspaces.list()`` store error is NOT
    swallowed here — it bubbles to :func:`_egress_gate_decision`, which fails CLOSED in deny
    context (never a silent allow on an unevaluable boundary).
    """
    if not workspace_root:
        return ""
    from pathlib import Path  # noqa: PLC0415

    try:
        target = Path(workspace_root).expanduser().resolve(strict=False)
    except OSError:
        target = Path(workspace_root)
    workspaces = getattr(app.state, "workspaces", None)
    if workspaces is None:
        return ""
    for ws in workspaces.list():
        root = str(getattr(ws, "root_path", "") or "")
        if not root:
            continue
        try:
            resolved = Path(root).expanduser().resolve(strict=False)
        except OSError:
            resolved = Path(root)
        if target == resolved or target.is_relative_to(resolved):
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

    FAIL-CLOSED IN DENY MODE (review finding 1). The chokepoint's own ``_gate_allows`` fails
    OPEN only for the genuinely-unwired case (no gate / deny mode off). Here, once a workspace
    is established as deny-mode — OR the store CANNOT be evaluated to prove it is NOT — any
    error fails CLOSED with a TYPED reason on the trace, never a silent allow on a boundary the
    user explicitly opted into (⚑ clio must not decide to allow).

    WRITE-GATE (N2). A WRITE-SHAPED egress (``rec.method`` in :data:`WRITE_METHODS` — so an
    opaque CONNECT tunnel, ``method=""``, is NEVER write-shaped, honouring the opacity limit)
    is consulted FIRST via :func:`_write_gate_decision`. When the workspace opts into the
    write-gate and the host is not already write-granted, it escalates to a human ask and
    returns its allow/deny; otherwise it returns ``None`` and the request falls through to the
    EXISTING read/deny-mode behaviour unchanged (reads are NEVER newly blocked by this slice).
    The write-gate fails CLOSED asymmetrically: if its decision RAISES, a write-shaped egress
    denies (a wiring bug must not silently allow a write) while a read is untouched by it and
    keeps failing OPEN (the chokepoint residual) — mirrors the B5 deny-mode fail-safe lesson.
    """
    if rec.method in WRITE_METHODS:
        try:
            write_decision = _write_gate_decision(app, rec)
        except Exception as exc:  # noqa: BLE001 — a write-gate wiring bug fails CLOSED (deny), typed
            _record_egress_denied(app, "", (rec.host or ""), reason=REASON_EGRESS_WRITE_GATE_ERROR)
            logger.warning(
                "egress write-gate decision failed reason=%s host=%s method=%s error=%r "
                "— failing closed (write)",
                REASON_EGRESS_WRITE_GATE_ERROR,
                rec.host,
                rec.method,
                exc,
            )
            return "deny"
        if write_decision is not None:
            return write_decision
        # write-gate OFF / not applicable → fall through to the existing read/deny-mode path.
    try:
        workspace_id = _workspace_id_for_root(app, rec.workspace_root)
        deny = workspace_deny_mode(app, workspace_id)
    except Exception as exc:  # noqa: BLE001 — cannot prove NOT-deny → fail closed, typed
        _record_egress_denied(app, "", (rec.host or ""), reason=REASON_EGRESS_STORE_UNRESOLVED)
        logger.warning(
            "egress deny-mode unresolved reason=%s host=%s error=%r — failing closed",
            REASON_EGRESS_STORE_UNRESOLVED,
            rec.host,
            exc,
        )
        return "deny"
    if not deny:
        return "allow"  # default is ALLOW + RECORD (B4); deny mode is strictly opt-in
    try:
        host = (rec.host or "").strip().lower()
        action = _host_action_for(app, workspace_id=workspace_id, host=host)
        if action == "deny":
            _record_egress_denied(app, workspace_id, host, reason=REASON_EGRESS_POLICY_DENY)
            return "deny"
        if action in {"allow", "allow_session", "allow_workspace"}:
            return "allow"
        return _prompt_egress(app, workspace_id, rec)
    except Exception as exc:  # noqa: BLE001 — a deny-mode decision error must fail closed, typed
        _record_egress_denied(
            app, workspace_id, (rec.host or ""), reason=REASON_EGRESS_DECISION_ERROR
        )
        logger.warning(
            "egress deny-mode decision failed reason=%s host=%s error=%r — failing closed",
            REASON_EGRESS_DECISION_ERROR,
            rec.host,
            exc,
        )
        return "deny"


def _write_gate_decision(app: "FastAPI", rec: "EgressRecord") -> Optional[str]:
    """Decide a WRITE-SHAPED egress under the opt-in write-gate (N2). ``None`` = not applicable.

    Called from :func:`_egress_gate_decision` ONLY for a write-shaped egress (``rec.method`` in
    :data:`WRITE_METHODS`). Returns:

    * ``None`` — the workspace does NOT opt into the write-gate (DEFAULT), so the request falls
      through to the existing read/deny-mode behaviour unchanged (no new gating, legitimate
      fleet POSTs keep working);
    * ``"allow"`` — the write host is already write-GRANTED (a workspace ``host_pattern`` grant
      covers it, reusing the existing host-policy check) — sticky, no re-prompt;
    * ``"deny"`` — a ``host_pattern`` deny policy matches the write host (typed record); or the
      human declined / the prompt timed out;
    * the interactive gate's allow/deny — an un-granted write host under the write-gate is
      escalated to a WRITE-labelled permission ask (:func:`_prompt_egress`, ``write=True``).

    Raising here (e.g. a store failure resolving the workspace) is caught by the caller and
    fails CLOSED for the write — this function never swallows an error into a silent allow.
    """
    workspace_id = _workspace_id_for_root(app, rec.workspace_root)
    if not workspace_write_gate(app, workspace_id):
        return None  # write-gate OFF (default) → existing behaviour, no new gating
    host = (rec.host or "").strip().lower()
    action = _host_action_for(app, workspace_id=workspace_id, host=host)
    if action in {"allow", "allow_session", "allow_workspace"}:
        return "allow"  # already write-granted (sticky) — no prompt
    if action == "deny":
        _record_egress_denied(app, workspace_id, host, reason=REASON_EGRESS_WRITE_POLICY_DENY)
        return "deny"
    return _prompt_egress(app, workspace_id, rec, write=True)


def _prompt_egress(
    app: "FastAPI", workspace_id: str, rec: "EgressRecord", *, write: bool = False
) -> str:
    """Open the interactive gate for an un-granted egress domain; block until resolved.

    Serves BOTH escalation kinds: a deny-mode unknown domain (any verb, ``write=False``) and an
    N2 WRITE-shaped egress (``write=True``) — the row is labelled distinctly so the approver sees
    whether it is a WRITE. The request KIND stays :data:`NETWORK_EGRESS_REQUEST_KIND` either way
    (a new request kind, not a new gate — ⚑ #974.8), so an ``allow_workspace`` resolution derives
    the same sticky ``host_pattern`` grant that both gates consult on the next connect.

    Concurrent connects to the SAME ``(workspace, host)`` COALESCE onto one pending prompt
    (review finding 4) — one user decision unblocks every waiter. Distinct concurrently-open
    prompts are bounded by :data:`_MAX_CONCURRENT_EGRESS_PROMPTS`; over the cap a further
    distinct-host connect fails CLOSED with a typed reason (never unbounded blocked prompts).
    """
    host = (rec.host or "").strip().lower()
    key = (workspace_id, host)
    with _PENDING_EGRESS_LOCK:
        prompt = _PENDING_EGRESS.get(key)
        is_creator = prompt is None
        if is_creator:
            if len(_PENDING_EGRESS) >= _MAX_CONCURRENT_EGRESS_PROMPTS:
                _record_egress_denied(app, workspace_id, host, reason=REASON_EGRESS_PROMPT_CAP)
                return "deny"
            pid = f"perm_{uuid.uuid4().hex[:12]}"
            summary = (
                f"network WRITE egress ({rec.method}) to {host}:{rec.port} requested (write gate)"
                if write
                else f"network egress to {host}:{rec.port} requested (deny mode)"
            )
            prompt = {
                "pid": pid,
                "event": threading.Event(),
                "row": {
                    "id": pid,
                    "session_id": _active_session_for_workspace(app, workspace_id),
                    "kind": NETWORK_EGRESS_REQUEST_KIND,
                    "egress_kind": "write" if write else "read",
                    "tool_call": {
                        "tool_name": NETWORK_EGRESS_REQUEST_KIND,
                        "input": {
                            "host": host,
                            "port": rec.port,
                            "method": rec.method,
                            "write": write,
                        },
                    },
                    "summary": summary,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "status": "pending",
                },
                "waiters": 0,
            }
            _PENDING_EGRESS[key] = prompt
        # Non-creator path took an existing (non-None) entry; creator path just built one.
        assert prompt is not None
        prompt["waiters"] += 1
    row = prompt["row"]
    evt = prompt["event"]
    if is_creator:
        try:
            app.state.permissions[row["id"]] = row
            app.state.permission_events[row["id"]] = evt
        except Exception as exc:  # noqa: BLE001 — a wedged prompt store must fail SAFE (deny)
            with _PENDING_EGRESS_LOCK:
                _PENDING_EGRESS.pop(key, None)
            _record_egress_denied(app, workspace_id, host, reason=REASON_EGRESS_PROMPT_UNWRITABLE)
            logger.warning(
                "egress prompt store failed reason=%s error=%r",
                REASON_EGRESS_PROMPT_UNWRITABLE,
                exc,
            )
            return "deny"
        _emit_egress_requested(app, str(row.get("session_id") or ""), row)
    resolved = evt.wait(timeout=_EGRESS_GATE_TIMEOUT_S)
    with _PENDING_EGRESS_LOCK:
        prompt["waiters"] -= 1
        if prompt["waiters"] <= 0:
            _PENDING_EGRESS.pop(key, None)
    if not resolved:
        row["status"] = "timeout"
        _record_egress_denied(app, workspace_id, host, reason=REASON_EGRESS_TIMEOUT)
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
    publish_permission(app, "permission.requested", owner_session_id=session_id, payload=row)


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
    "NETWORK_WRITE_GATE_CONFIG_KEY",
    "WRITE_METHODS",
    "GRANTOR_MODEL_REQUEST",
    "GRANTOR_POLICY",
    "GRANTOR_REVIEWER",
    "GRANTOR_USER",
    "KIND_DOMAIN",
    "KIND_ROOT",
    "REASON_GRANT_DEFERRED_BUSY",
    "REASON_GRANT_LIVE",
    "REASON_GRANT_RECORDED_NO_FENCE",
    "REASON_GRANT_RESTART_FAILED",
    "REASON_GRANT_RESTARTED_LIVE",
    "SCOPE_SESSION",
    "SCOPE_WORKSPACE",
    "apply_root_grant",
    "revoke_root_grant",
    "emit_boundary_for_derived_policy",
    "emit_boundary_granted",
    "emit_boundary_revoked",
    "install_egress_gate",
    "replay_persisted_root_grants",
    "workspace_deny_mode",
    "workspace_write_gate",
]
