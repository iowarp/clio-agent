"""The workspace facts the egress gate reads: its opt-in flags, reason codes and lookups.

:mod:`clio_agent.gact.runtime.grants` owns the gate itself (policy, prompts, holds); this
module holds what it consults about a workspace — whether it opts into deny mode or the write
gate, which workspace contains a child's root, and which session a workspace-scoped record is
attributed to — plus the typed reasons a blocked egress is recorded with.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

#: Deny-mode flag key on the workspace ``config`` (opt-in per workspace; config/state, not env).
DENY_MODE_CONFIG_KEY = "network_deny_mode"
#: Write-gate flag key on the workspace ``config`` (opt-in, DEFAULT OFF — N2). When set, a
#: WRITE-SHAPED egress to an un-granted host escalates to a human ask; reads/opaque CONNECTs stay open.
NETWORK_WRITE_GATE_CONFIG_KEY = "network_write_gate"
#: The plain-HTTP request verbs clio classifies as WRITE-SHAPED (N2). A CONNECT tunnel carries
#: ``method=""`` (opaque) so it is NEVER write-shaped — clio never over-claims a write-gate on it.
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Typed deny reasons recorded on the trace when a deny-mode egress is blocked (no silent
#: fallback — every boundary denial reaches the trace via ``grants._record_egress_denied``).
REASON_EGRESS_POLICY_DENY = "policy_deny"
REASON_EGRESS_TIMEOUT = "egress_gate_timeout"
REASON_EGRESS_PROMPT_CAP = "egress_gate_prompt_cap_reached"
REASON_EGRESS_STORE_UNRESOLVED = "egress_gate_store_unresolved"
REASON_EGRESS_DECISION_ERROR = "egress_gate_decision_error"
REASON_EGRESS_PROMPT_UNWRITABLE = "egress_gate_prompt_unwritable"
REASON_EGRESS_DENIED_THIS_TURN = "egress_denied_this_turn"
#: N2 write-gate typed reasons (never a silent path). A write-gate decision that RAISES fails
#: CLOSED (deny) for a write-shaped egress — a wiring bug must not silently allow a write; a
#: matching ``deny`` host policy on a write is recorded distinctly from the deny-mode read deny.
REASON_EGRESS_WRITE_GATE_ERROR = "egress_write_gate_error"
REASON_EGRESS_WRITE_POLICY_DENY = "write_policy_deny"


def _active_session_for_workspace(app: "FastAPI", workspace_id: str) -> str:
    """Return a representative live session id for ``workspace_id`` (recency), else ``""``.

    The ``boundary.*`` SSE stream is per-session (``GET /v1/sessions/{sid}/events``), so a
    workspace-scoped grant attributes to the workspace's most-recent session to reach the
    user's live stream. Durable trace + ARC capture the event regardless of attribution.
    """
    sessions = getattr(app.state, "sessions", None)
    if sessions is None:
        return ""
    from clio_agent.gact.session_records import PageQuery  # noqa: PLC0415

    # The newest created session of the workspace, from the index: no row is read.
    ids = sessions.ids(PageQuery(workspace_id=workspace_id, archived=None))
    return ids[0] if ids else ""


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
    host is escalated for human permission (see ``grants._write_gate_decision``).
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
    swallowed here — it bubbles to ``grants._egress_gate_decision``, which fails CLOSED in deny
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
