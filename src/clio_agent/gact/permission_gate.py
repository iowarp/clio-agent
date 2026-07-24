"""Tool permission gating + cancellation for the GACT server (#714 decomposition).

This module is the single owner of the *enforcement* side of CLIO's tool-call
safety model: applying the declarative permission policies (SPEC §6.11.b)
consulted before the interactive gate, recording resolved permission audit rows,
and building the closures the tool-execution boundary calls
(``permission_gate`` / cancellation checker).

Whether a call needs approval is decided structurally, not by tool-name
substrings: :func:`is_read_only` (in :mod:`clio_agent.gact.runtime.grant_resolver`)
fast-allows a provably read-only call (MCP ``readOnlyHint`` annotation OR a static
catalog ``read`` tag); every other call proceeds to the plan/architect lock,
policy match, and interactive prompt. Policy matching itself delegates to the one
:func:`~clio_agent.gact.runtime.grant_resolver.resolve` matcher (#1032) — the
former ``_is_destructive`` substring set and the bounded ``shell_bash`` parser are
deleted (their read-only diagnostics are now covered by :func:`is_read_only`).

It pairs with :mod:`clio_agent.gact.runtime.permission_policies`, which owns the
*data* layer (validation, on-disk load/flush, resolution-derived policies) and
exports :func:`_permission_path_from_args`, reused here for policy path matching.

Boundaries (preserving the no-cycle invariant): this module imports only stdlib,
FastAPI/Starlette wire types, the gact ``events``/``types`` wire models, and the
``runtime`` leaves it needs (``globals._resolve_tool_session`` for the
turn-driving session, ``permission_policies._permission_path_from_args``,
``grant_resolver.resolve``/``is_read_only``). It NEVER imports
:mod:`clio_agent.gact.app`. ``build_app`` and ``GactDeps`` import
:func:`_make_permission_gate`, :func:`_guard_direct_destructive_action`, etc.
from here so the ``app.state.make_permission_gate`` seam and the route/turn
dependents keep working.

Responsibilities:

* :func:`_policy_action_for_tool` -- match the active permission policies (a thin
  shim over :func:`~clio_agent.gact.runtime.grant_resolver.resolve`).
* :func:`_record_resolved_permission` -- emit a resolved permission audit row +
  ``permission.resolved`` event.
* :func:`_direct_permission_denied` / :func:`_guard_direct_destructive_action`
  -- policy/audit semantics for explicit direct (route-initiated) destructive
  actions.
* :func:`_make_permission_gate` -- build the ``MCPToolBridge.permission_gate``
  closure (the interactive blocking gate).
* :func:`_make_cancellation_checker` -- build the tool-executor cancellation
  checker for the active session.
"""

from __future__ import annotations

import inspect
import threading
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from clio_agent.gact.events import Event
from clio_agent.gact.runtime.globals import _resolve_tool_session
from clio_agent.gact.runtime.grant_resolver import (
    EXTERNAL_MCP_CONTEXT_KIND,
    is_read_only,
    resolve,
)
from clio_agent.gact.runtime.permission_policies import _permission_path_from_args
from clio_agent.gact.runtime.retention import enforce_dict_bound
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo
from clio_agent.tools.catalog import get_tool_entry

if TYPE_CHECKING:
    from fastapi import FastAPI

#: The gate ``context`` kind marking an external MCP tool call. Single-sourced in
#: :mod:`grant_resolver`; re-exported here under its historical private name for the
#: routes/builders/tests that bind ``permission_gate._EXTERNAL_MCP_PERMISSION_CONTEXT_KIND``.
_EXTERNAL_MCP_PERMISSION_CONTEXT_KIND = EXTERNAL_MCP_CONTEXT_KIND

#: #1034 typed approval-mode reasons stamped on the resolved / pending permission row so the
#: trace always shows WHY a non-read call was auto-approved or still prompted (never silent).
REASON_APPROVAL_BYPASS = "approval_mode_bypass"
REASON_APPROVAL_AUTO_EDITS = "approval_mode_auto_edits"
#: ai-review still PROMPTS today (the reviewer agent is the split follow-up slice); the pending
#: row carries this typed reason so the ask is attributable to the reviewer-pending state.
REASON_AI_REVIEW_REVIEWER_PENDING = "ai_review_reviewer_pending"


def default_decision(
    approval_mode: str,
    kind: str,
    name: str,
    args: Mapping[str, Any],
) -> str:
    """Decide a non-read tool call at the gate's PROMPT boundary from the approval mode (#1034).

    Consulted ONLY after :func:`is_read_only` fast-allow, the plan/architect read-only lock, and
    the explicit-policy resolve have all declined to decide the call — so an explicit ``allow`` /
    ``deny`` policy always WINS over a mode default. Reads never reach here. The approval axis is
    orthogonal to ``session.mode`` and only ever relaxes toward auto-approve or falls through to
    the existing interactive prompt; it never manufactures a denial (that stays with the lock and
    explicit deny policies above). Returns:

    * ``allow`` — ``bypass`` (any non-read call) or ``auto-edits`` for an fs WRITE (a call whose
      static catalog entry carries the ``write`` tag);
    * ``ask``   — ``ask`` (default), ``auto-edits`` for a non-write (e.g. ``shell_bash``, whose
      writes live behind the OS fence, not a catalog ``write`` tag), or ``ai-review`` (the caller
      stamps the typed ``ai_review_reviewer_pending`` reason on the pending row).

    ``kind``/``args`` are accepted for a stable signature (a future kind may inspect them) but the
    two current signals (mode + the catalog ``write`` tag) do not consult them.
    """

    _ = (kind, args)
    if approval_mode == "bypass":
        return "allow"
    if approval_mode == "auto-edits":
        entry = get_tool_entry(name)
        if entry is not None and "write" in entry.tags:
            return "allow"
        return "ask"
    # ask (default) and ai-review both route to the existing interactive prompt.
    return "ask"


def _normalize_mcp_tool_annotations(tool: Any) -> dict[str, Any] | None:
    """Return a JSON-compatible MCP annotation mapping from a listed tool.

    FastMCP currently exposes ``annotations`` as an MCP Pydantic model, while
    tests and persisted descriptor rows commonly use plain mappings. Unknown
    shapes normalize to ``None``; external-MCP classification treats that as
    missing evidence and therefore requires permission.
    """

    raw = getattr(tool, "annotations", None)
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        return dict(raw)
    model_dump = getattr(raw, "model_dump", None)
    if not callable(model_dump):
        return None
    try:
        dumped = model_dump(mode="json", by_alias=True)
    except TypeError:
        dumped = model_dump()
    if not isinstance(dumped, Mapping):
        return None
    return dict(dumped)


def _external_mcp_permission_context(annotations: Any) -> dict[str, Any]:
    """Build the explicit gate context for one external MCP tool call."""

    return {
        "kind": _EXTERNAL_MCP_PERMISSION_CONTEXT_KIND,
        "annotations": annotations,
    }


def _invoke_permission_gate(
    gate: Any,
    name: str,
    args: Mapping[str, Any],
    context: Mapping[str, Any],
) -> str:
    """Invoke a gate with context while retaining legacy two-argument hooks.

    The built-in gate accepts the structured third argument. Existing custom
    hooks are a documented two-argument seam; signature binding selects that
    form before invocation, avoiding a catch-and-retry that could accidentally
    run a gate twice when its own body raises ``TypeError``.
    """

    try:
        gate_signature = inspect.signature(gate)
    except (TypeError, ValueError):
        return gate(name, args, context)
    try:
        gate_signature.bind(name, args, context)
    except TypeError:
        return gate(name, args)
    return gate(name, args, context)


def _is_external_mcp_permission_context(context: Mapping[str, Any] | None) -> bool:
    return bool(
        isinstance(context, Mapping)
        and context.get("kind") == _EXTERNAL_MCP_PERMISSION_CONTEXT_KIND
    )


def _policy_action_for_tool(
    app: "FastAPI",
    *,
    session_id: str,
    session: Any | None,
    tool_name: str,
    args: Mapping[str, Any],
) -> str:
    """Return the first matching permission policy action for a tool call.

    A thin shim over :func:`~clio_agent.gact.runtime.grant_resolver.resolve`
    (``kind="tool"``): it resolves the call's target path + workspace from the
    session and hands the active policy list to the one matcher. The
    ``/v1/policies`` endpoint is user-facing configuration, so storing policies
    without enforcing them is a silent safety bypass; matching stays small and
    predictable (scope, tool glob, optional path glob, then the raw action).
    Kept as a named shim because ``enrichment``/``proposal_effects`` bind it.
    """

    path = _permission_path_from_args(args)
    workspace_id = getattr(session, "workspace_id", "") if session is not None else ""
    return resolve(
        "tool",
        tool_name,
        policies=getattr(app.state, "permission_policies", []),
        session_id=session_id,
        workspace_id=workspace_id,
        path=path,
    )


def _record_resolved_permission(
    app: "FastAPI",
    *,
    session_id: str,
    tool_name: str,
    args: Mapping[str, Any],
    status: str,
    action: str,
    summary: str,
    reason: str,
) -> str:
    pid = f"perm_{uuid.uuid4().hex[:12]}"
    now_iso = datetime.now(timezone.utc).isoformat()
    row = {
        "id": pid,
        "session_id": session_id,
        "tool_call": {
            "tool_name": tool_name,
            "input": dict(args),
        },
        "summary": summary,
        "created_at": now_iso,
        "status": status,
        "action": action,
        "resolved_at": now_iso,
        "reason": reason,
    }
    if hasattr(app.state, "permissions"):
        app.state.permissions[pid] = row
        enforce_dict_bound(app, app.state.permissions, "permissions", session_id=session_id)
    if hasattr(app.state, "bus"):
        app.state.bus.publish(
            Event(
                type="permission.resolved",
                session_id=session_id,
                payload={
                    "permission_id": pid,
                    "action": action,
                    "session_id": session_id,
                    "reason": reason,
                },
            )
        )
    return pid


def _direct_permission_denied(
    *,
    tool_name: str,
    args: Mapping[str, Any],
    summary: str,
) -> HTTPException:
    return HTTPException(
        status_code=403,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error="permission_error",
                message=f"{summary} blocked by permission policy",
                details={
                    "tool_name": tool_name,
                    "input": dict(args),
                    "reason": "policy_deny",
                    "recovery_actions": ["change_policy", "retry", "exit"],
                },
                recoverable=True,
            )
        ).model_dump(exclude_none=True),
    )


def _guard_direct_destructive_action(
    app: "FastAPI",
    *,
    session_id: str = "",
    workspace_id: str = "",
    tool_name: str,
    args: Mapping[str, Any],
    summary: str,
    reason: str,
) -> None:
    """Apply permission policy/audit semantics to direct GACT DELETE actions.

    These routes are already explicit user actions, so there is no extra
    interactive prompt. Policies can still deny before mutation, and all
    allowed direct destructive actions land in `/v1/permissions` as resolved
    audit rows.
    """

    session = app.state.sessions.get(session_id) if session_id else None
    if session is None and workspace_id:
        session = SimpleNamespace(workspace_id=workspace_id)
    policy_action = _policy_action_for_tool(
        app,
        session_id=session_id,
        session=session,
        tool_name=tool_name,
        args=args,
    )
    if policy_action == "deny":
        _record_resolved_permission(
            app,
            session_id=session_id,
            tool_name=tool_name,
            args=args,
            status="auto_denied",
            action="deny",
            summary=f"{summary} blocked by permission policy",
            reason="policy_deny",
        )
        raise _direct_permission_denied(tool_name=tool_name, args=args, summary=summary)
    _record_resolved_permission(
        app,
        session_id=session_id,
        tool_name=tool_name,
        args=args,
        status="auto_approved",
        action="allow",
        summary=summary,
        reason="policy_allow"
        if policy_action in {"allow", "allow_session", "allow_workspace"}
        else reason,
    )


def _make_permission_gate(app: "FastAPI"):
    """Build a callable suitable for MCPToolBridge.permission_gate.

    Provably read-only calls (:func:`is_read_only`) fast-allow as the FIRST
    branch — before the plan/architect lock — so no mode ever gates a read
    (structural invariant, #1032). Every other call proceeds to the plan/architect
    lock, the policy match, and, absent a policy, registers a permission row,
    publishes ``permission.requested`` into the EventBus, and blocks on a
    ``threading.Event`` with a generous timeout, returning "allow"/"deny" from the
    user's resolution. A missing session fails closed immediately; timeouts default
    to deny — fail-safe.
    """

    DEFAULT_TIMEOUT_S = 600.0

    def gate(
        name: str,
        args: Mapping[str, Any],
        context: Mapping[str, Any] | None = None,
    ) -> str:
        # iowarp/clio-agent#20: user-defined pre_tool hook can veto
        # the call by raising PermissionError. Returns ignored;
        # only the raise/no-raise distinction matters.
        try:
            from clio_agent.runtime.hooks import fire as _fire_hook

            _fire_hook("pre_tool", name, dict(args))
        except PermissionError:
            return "deny"
        # #1032: reads are NEVER gated. A provably read-only call (MCP
        # readOnlyHint annotation OR a static catalog ``read`` tag) fast-allows
        # here, BEFORE the plan/architect lock — the structural invariant that no
        # mode or policy can gate a read. Everything else proceeds to approval.
        if is_read_only("tool", name, args, context):
            return "allow"
        subject = (
            "external MCP tool"
            if _is_external_mcp_permission_context(context)
            else "destructive tool"
        )
        # Prefer the session currently driving the turn. Recency is
        # only a fallback for truly out-of-band tool calls.
        sid, current = _resolve_tool_session(app)
        if current is not None:
            # iowarp/clio-agent — plan_mode + architect mode reject
            # destructive tool calls without prompting. Read-only
            # contract is hard, not advisory.
            if current.mode in {"plan", "architect"}:
                row = {
                    "id": f"perm_{uuid.uuid4().hex[:12]}",
                    "session_id": sid,
                    "tool_call": {
                        "tool_name": name,
                        "input": dict(args),
                    },
                    "summary": (f"{subject} {name!r} blocked by session.mode={current.mode!r}"),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "status": "auto_denied",
                    "action": "deny",
                    "resolved_at": datetime.now(timezone.utc).isoformat(),
                }
                app.state.permissions[row["id"]] = row
                enforce_dict_bound(app, app.state.permissions, "permissions", session_id=sid)
                app.state.bus.publish(
                    Event(
                        type="permission.resolved",
                        session_id=sid,
                        payload={
                            "permission_id": row["id"],
                            "action": "deny",
                            "session_id": sid,
                            "reason": "session_mode_readonly",
                        },
                    )
                )
                return "deny"
        policy_action = _policy_action_for_tool(
            app,
            session_id=sid,
            session=current,
            tool_name=name,
            args=args,
        )
        if policy_action == "deny":
            _record_resolved_permission(
                app,
                session_id=sid,
                tool_name=name,
                args=args,
                status="auto_denied",
                action="deny",
                summary=f"{subject} {name!r} blocked by permission policy",
                reason="policy_deny",
            )
            return "deny"
        if policy_action in {"allow", "allow_session", "allow_workspace"}:
            _record_resolved_permission(
                app,
                session_id=sid,
                tool_name=name,
                args=args,
                status="auto_approved",
                action="allow",
                summary=f"{subject} {name!r} allowed by permission policy",
                reason=f"policy_{policy_action}",
            )
            return "allow"
        # #1034: consult the session's approval_mode at the PROMPT boundary. Precedence is uniform
        # — an explicit policy (deny/allow already returned above, AND an explicit ``ask``) beats the
        # mode. Here policy_action is only "" (no policy) or "ask" (explicit "always confirm this
        # tool"): the mode may auto-approve ONLY the unpolicied case, so an explicit ``ask`` survives
        # even bypass. Reads never reach here (is_read_only returned at the top).
        approval_mode = getattr(current, "approval_mode", "ask") if current is not None else "ask"
        if (
            policy_action != "ask"
            and default_decision(approval_mode, "tool", name, args) == "allow"
        ):
            # bypass (any non-read) or auto-edits (fs write): auto-approve but STILL record a
            # resolved audit row + permission.resolved boundary event — the OS fence is
            # untouched (⚑ never a confinement disable), so the approval is on the record.
            _record_resolved_permission(
                app,
                session_id=sid,
                tool_name=name,
                args=args,
                status="auto_approved",
                action="allow",
                summary=f"{subject} {name!r} allowed by approval_mode={approval_mode!r}",
                reason=(
                    REASON_APPROVAL_BYPASS
                    if approval_mode == "bypass"
                    else REASON_APPROVAL_AUTO_EDITS
                ),
            )
            return "allow"
        if not sid:
            return "deny"
        pid = f"perm_{uuid.uuid4().hex[:12]}"
        evt = threading.Event()
        row = {
            "id": pid,
            "session_id": sid,
            "tool_call": {
                "tool_name": name,
                "input": dict(args),
            },
            "summary": f"{subject} call: {name}",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "pending",
        }
        # ai-review still prompts today (the reviewer agent is the split follow-up); stamp the
        # typed reviewer-pending reason on the recorded/published row so the ask is never a
        # silent default — the trace attributes it to the ai-review approval mode.
        if approval_mode == "ai-review":
            row["reason"] = REASON_AI_REVIEW_REVIEWER_PENDING
        app.state.permissions[pid] = row
        app.state.permission_events[pid] = evt
        enforce_dict_bound(app, app.state.permissions, "permissions", session_id=sid)
        app.state.bus.publish(
            Event(
                type="permission.requested",
                session_id=sid,
                payload=row,
            )
        )
        # Block the bridge thread until POST /v1/permissions/{pid}
        # sets the event (or we time out).
        if not evt.wait(timeout=DEFAULT_TIMEOUT_S):
            row["status"] = "timeout"
            return "deny"
        action = row.get("action", "deny")
        if action in {"allow", "allow_session", "allow_workspace"}:
            return "allow"
        return "deny"

    return gate


def _make_cancellation_checker(app: "FastAPI"):
    """Build a tool-executor cancellation checker for the active GACT session."""

    def check() -> bool:
        sid, _current = _resolve_tool_session(app)
        if not sid:
            return False
        event = app.state.cancel_events.get(sid)
        if event is not None and event.is_set():
            return True
        return sid in app.state.cancel_flags

    return check
