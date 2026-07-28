"""Tool permission gating + cancellation for the GACT server (#714 decomposition).

This module is the single owner of the *enforcement* side of CLIO's tool-call
safety model: applying the declarative permission policies (SPEC §6.11.b)
consulted before the interactive gate, recording resolved permission audit rows,
and building the closures the tool-execution boundary calls
(``permission_gate`` / cancellation checker).

Whether a call needs approval is decided structurally, not by tool-name
substrings: :func:`is_read_only` (in :mod:`clio_agent.gact.runtime.grant_resolver`)
fast-allows a provably read-only call (MCP ``readOnlyHint`` annotation OR a static
catalog ``read`` tag); every other call proceeds to the policy match and
interactive prompt. There is no separate plan/architect lock step: plan/architect
enforcement is a set of built-in ``plan_acl`` policy rows (P1.1 #1063) that
:func:`~clio_agent.gact.runtime.grant_resolver.resolve` consults for every
``kind="tool"`` call whose session ``mode`` is plan-restricted — the same ONE
matcher that resolves every other policy row, dynamically prioritized above any
matching user row so it can never be outranked (adversarial-review fix, #1063).
A resulting deny is recorded with the same ``reason: "policy_deny"`` as any other
policy denial — there is no separate ``session_mode_readonly`` reason. Policy
matching delegates to the one :func:`~clio_agent.gact.runtime.grant_resolver.resolve`
matcher (#1032) — the former ``_is_destructive`` substring set and the bounded
``shell_bash`` parser are deleted (their read-only diagnostics are now covered by
:func:`is_read_only`).

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
import logging
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
    resolve_detail,
)
from clio_agent.gact.runtime.grants import GRANTOR_REVIEWER, GRANTOR_USER
from clio_agent.gact.runtime.permission_policies import (
    _append_permission_policy_from_resolution,
    _flush_permission_policies,
    _permission_path_from_args,
)
from clio_agent.gact.runtime.retention import enforce_dict_bound
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo
from clio_agent.tools.catalog import get_tool_entry

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

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


class DenyDecision(str):
    """A gate ``"deny"`` decision carrying a model-facing deny message (P1.2 #1064).

    A ``str`` subclass that IS ``"deny"``, so every existing ``decision == "deny"``
    comparison and the tool executor's ``decision != "allow"`` branch keep working
    unchanged. The extra :attr:`deny_message` is the mode-aware text the tool executor
    raises to the model in place of the generic ``"denied by permission gate"`` string
    (read via ``getattr`` at the execution boundary, so the low ``tools`` layer never
    imports gact). The typed audit reason (``policy_deny``) is decided separately and is
    NOT affected — this class only enriches the human/model-facing wording.
    """

    deny_message: str

    def __new__(cls, deny_message: str = "") -> "DenyDecision":
        obj = super().__new__(cls, "deny")
        obj.deny_message = deny_message
        return obj


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

    from clio_agent.tools.catalog import normalize_mcp_annotations  # noqa: PLC0415

    return normalize_mcp_annotations(tool)


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
    mode: str = "",
) -> str:
    """Return the winning permission policy action for a tool call.

    A thin shim over :func:`~clio_agent.gact.runtime.grant_resolver.resolve`
    (``kind="tool"``): it resolves the call's target path + workspace from the
    session and hands the active policy list to the one matcher. The
    ``/v1/policies`` endpoint is user-facing configuration, so storing policies
    without enforcing them is a silent safety bypass; matching stays small and
    predictable (scope, tool glob, optional path glob, then the raw action).
    Kept as a named shim because ``enrichment``/``proposal_effects`` bind it.

    ``mode`` (P1.1 #1063) is the session's mode: passing it makes ``resolve``
    consult the built-in plan-mode ACL (deny every non-read tool in
    plan/architect; the sole ``<plans>/*.md`` write carve-out in plan), so the
    plan/architect read-only lock is now this ONE data-driven path — no longer a
    predicate copy-pasted here + in ``enrichment`` + ``proposal_effects``. The
    three former lock sites pass ``mode``; direct route actions
    (:func:`_guard_direct_destructive_action`) leave it empty, unchanged.
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
        mode=mode,
    )


def _policy_detail_for_tool(
    app: "FastAPI",
    *,
    session_id: str,
    session: Any | None,
    tool_name: str,
    args: Mapping[str, Any],
    mode: str = "",
) -> tuple[str, str]:
    """Return ``(action, deny_message)`` for a tool call (P1.2 #1064).

    The detail-returning companion to :func:`_policy_action_for_tool`: it resolves the
    call through the SAME one matcher (:func:`~clio_agent.gact.runtime.grant_resolver.resolve_detail`)
    but also carries the plan-mode deny message. ``deny_message`` is non-empty ONLY when
    the winning deny is authored by the built-in plan_acl mode-lock, so the interactive
    gate can raise the mode-aware text (states the restriction, points at the plan file,
    says what IS allowed) while the ``policy_deny`` audit reason is unchanged.
    """

    path = _permission_path_from_args(args)
    workspace_id = getattr(session, "workspace_id", "") if session is not None else ""
    return resolve_detail(
        "tool",
        tool_name,
        policies=getattr(app.state, "permission_policies", []),
        session_id=session_id,
        workspace_id=workspace_id,
        path=path,
        mode=mode,
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


def resolve_permission(
    app: "FastAPI",
    pid: str,
    action: str,
    *,
    grantor: str = GRANTOR_USER,
    intercept: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Resolve one pending permission row and wake any blocked bridge thread.

    Lifted verbatim from ``routes/permissions.py::respond_permission`` (#1044) so the
    HTTP route (``grantor=GRANTOR_USER`` — byte-identical to the prior inline body) and
    the in-process ai-review reviewer (``grantor=GRANTOR_REVIEWER``) share ONE
    resolution path. Flips the row to ``resolved``, derives + flushes any sticky policy
    (emitting a domain ``boundary.granted`` where applicable), pops+sets the request's
    ``threading.Event`` so a waiting :class:`MCPToolBridge` thread unblocks, and emits
    ``permission.resolved`` on both the semantic highway and the bus.

    The ``grantor`` is stamped on the audit row and included in both
    ``permission.resolved`` payloads, and drives the semantic ``actor`` (was hardcoded
    ``role: "user"``), so every resolution is attributable to WHO decided it — never
    silent.

    Returns the resolved row, or ``None`` when ``pid`` is unknown or already resolved
    (idempotent — a double-resolve is a no-op, matching the route's prior behaviour).
    """

    row = app.state.permissions.get(pid)
    if row is None or row.get("status") != "pending":
        return None
    row["status"] = "resolved"
    row["action"] = action
    row["resolved_at"] = datetime.now(timezone.utc).isoformat()
    row["grantor"] = grantor
    # P2.6: an APPROVED PreToolUse defer may carry the modify/synthesize the approval
    # decides — stashed on the row BEFORE the event wake below so the parked call reads
    # it and drives the ``tool_interceptor`` (run-with-modified-args / synthesize).
    if intercept is not None and action in {"allow", "allow_session", "allow_workspace"}:
        if "result" in intercept:
            row["resolution_result"] = intercept.get("result")
        elif isinstance(intercept.get("input"), Mapping):
            row["resolution_input"] = dict(intercept["input"])
    policy = _append_permission_policy_from_resolution(app, row=row, action=action)
    if policy is not None:
        row["policy"] = policy
        # iowarp/clio-agent#759: sticky grants must survive a server restart, so flush
        # the derived policy to disk.
        _flush_permission_policies(app)
        # B5 #979.5: a derived ``host_pattern`` policy (a deny-mode ``network_egress`` grant)
        # is an effective DOMAIN boundary — emit ``boundary.granted{kind: domain}`` with its
        # provenance. A generic tool-permission sticky policy is not a boundary and emits
        # nothing (handled inside the helper).
        from clio_agent.gact.runtime.grants import (  # noqa: PLC0415
            emit_boundary_for_derived_policy,
        )

        emit_boundary_for_derived_policy(app, row, policy)
    # iowarp/clio-agent#7: wake any MCPToolBridge thread waiting on this permission.
    # A parked PreToolUse defer (P2.6) waits on this SAME event, so approving/denying
    # it out-of-band wakes the parked call, which reads ``row["action"]`` and returns
    # allow (runs the tool / applies the approval's modify/synthesize) or a typed deny.
    evt = app.state.permission_events.pop(pid, None)
    if evt is not None:
        evt.set()
    # P2.6: a TURN-ending defer (Stop / UserPromptSubmit) holds NO thread — it suspended
    # the session. Resolving its row stages the deferred-resume (approve → new turn /
    # release; deny → tighten) via the owner module. Runs exactly once (this body only
    # runs on the pending→resolved transition, which is idempotent above).
    if row.get("kind") == "turn_defer":
        from clio_agent.gact.hooks.defer import resume_turn_defer  # noqa: PLC0415

        resume_turn_defer(app, row, action)
    # B5 #979.8: surface the resolution on the highway/trace too so the request→resolution
    # lifecycle is consistently captured AND SSE-served. Guarded — a resolution must never
    # fail on an emit.
    try:
        from clio_agent.gact.runtime.globals import (  # noqa: PLC0415
            _emit_semantic_event,
        )

        _emit_semantic_event(
            app,
            str(row.get("session_id") or ""),
            "permission.resolved",
            status="completed",
            summary=f"Permission {pid} resolved: {action}.",
            actor={"role": grantor},
            subject={"permission_id": pid, "action": action},
            payload={
                "permission_id": pid,
                "action": action,
                "kind": row.get("kind") or "",
                "grantor": grantor,
            },
        )
    except Exception as exc:  # noqa: BLE001 - observability, never fatal to a resolution
        logger.warning(
            "permission.resolved semantic emit skipped reason=resolved_emit_failed "
            "permission_id=%s error=%r",
            pid,
            exc,
        )
    app.state.bus.publish(
        Event(
            type="permission.resolved",
            session_id=row.get("session_id", ""),
            payload={
                "permission_id": pid,
                "action": action,
                "session_id": row.get("session_id", ""),
                "grantor": grantor,
            },
        )
    )
    return row


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
        # P2.3 single-fire hygiene: clear any per-call intercept stash at the very
        # start so a read-only fast-allow (below) — or a prior call whose interceptor
        # never ran (a policy deny after a hook allow, a circuit break) — can never
        # let the NEXT tool's interceptor consume a stale synthesize/modify decision.
        from clio_agent.gact.hooks import stash_pre_tool_intercept  # noqa: PLC0415

        stash_pre_tool_intercept(None)
        # #1032: reads are NEVER gated. A provably read-only call (MCP
        # readOnlyHint annotation OR a static catalog ``read`` tag) fast-allows
        # here as the FIRST branch — before any hook or the plan/architect lock —
        # the structural invariant that no mode, policy, or hook can gate a read.
        # (P2.2 #1070 fixes the old ordering bug where the pre_tool hook fired
        # BEFORE this branch and could gate a read-only call.)
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
        # P2.2 #1070: PreToolUse hooks (the ported ``pre_tool`` consumer, deny-capable
        # and TIGHTEN-ONLY). A hook deny blocks the call and its reason reaches the
        # model via ``DenyDecision``; a hook allow proceeds to the policy match below
        # (a hook allow can NEVER lift a downstream policy deny). A hook infrastructure
        # failure is fail-closed per-hook inside the dispatcher and surfaces as a deny
        # whose message says it is a hook failure, NOT a user rejection.
        from clio_agent.gact.hooks import dispatch_pre_tool  # noqa: PLC0415

        hook_outcome = dispatch_pre_tool(
            name,
            dict(args),
            session_id=sid,
            turn_id=str(getattr(current, "current_turn_id", "") or ""),
            cwd=str(getattr(current, "workspace_root", "") or ""),
            context=context,
        )
        if hook_outcome.denied:
            # Clear any stale intercept so the (skipped) interceptor never fires on
            # this denied call, then block with the hook's reason.
            stash_pre_tool_intercept(None)
            _record_resolved_permission(
                app,
                session_id=sid,
                tool_name=name,
                args=args,
                status="auto_denied",
                action="deny",
                summary=f"{subject} {name!r} blocked by a PreToolUse hook",
                reason="hook_deny",
            )
            return DenyDecision(
                hook_outcome.reason or f"tool call {name!r} denied by a PreToolUse hook"
            )
        # P2.6: a PreToolUse ``defer`` PARKS this call for out-of-band approval (the
        # headline durable-defer path). deny beats defer (tighten-only): a policy deny
        # is evaluated FIRST so a matching deny-rule still blocks rather than parks;
        # otherwise the call parks on the existing gate primitive with the timeout
        # lifted and resolves from any out-of-band channel (see gact/hooks/defer.py).
        if hook_outcome.is_defer:
            mode = getattr(current, "mode", "") if current is not None else ""
            defer_policy_action, defer_plan_msg = _policy_detail_for_tool(
                app, session_id=sid, session=current, tool_name=name, args=args, mode=mode
            )
            if defer_policy_action == "deny":
                stash_pre_tool_intercept(None)
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
                return DenyDecision(defer_plan_msg) if defer_plan_msg else "deny"
            from clio_agent.gact.hooks.defer import park_pretool_defer  # noqa: PLC0415

            return park_pretool_defer(
                app, sid=sid, name=name, args=args, subject=subject, outcome=hook_outcome
            )
        # P2.3: a non-denied PreToolUse ``modify``/``synthesize`` rides forward to the
        # already-wired ``tool_interceptor`` slot. Stash it on the per-call context
        # var (single-fire: PreToolUse dispatched exactly once, here) — the interceptor
        # is a pure consumer that reads it after this gate returns "allow".
        stash_pre_tool_intercept(hook_outcome)
        # P1.1 #1063: the plan/architect read-only lock is no longer a predicate here (it was
        # copy-pasted into three modules). It is now a set of built-in plan_acl rows the ONE
        # resolver evaluates — passing the session mode makes ``resolve`` deny every non-read tool
        # in plan/architect (@40) and allow the sole ``<plans>/*.md`` write carve-out in plan (@70).
        # Read-only calls never reach here (``is_read_only`` fast-allowed above), in every mode.
        mode = getattr(current, "mode", "") if current is not None else ""
        policy_action, plan_deny_message = _policy_detail_for_tool(
            app,
            session_id=sid,
            session=current,
            tool_name=name,
            args=args,
            mode=mode,
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
            # P1.2 #1064: a plan_acl-authored deny carries a mode-aware message so the model
            # sees WHY the call is blocked (Plan Mode, read-only except the plan file) instead
            # of the generic executor string. The typed audit reason above stays ``policy_deny``;
            # a non-plan (user-policy) deny returns an empty message and the plain "deny" string.
            return DenyDecision(plan_deny_message) if plan_deny_message else "deny"
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
        # #1044: the ai-review reviewer runs ONLY when there is no explicit ``ask`` policy —
        # an explicit ``ask`` is a deliberate "always confirm THIS tool with a HUMAN" and beats
        # the mode (uniform with #1034: explicit policy > mode), so it must NOT be reviewer-
        # auto-decided. When the reviewer WILL run, stamp the typed reviewer-pending reason so
        # the ledger attributes the ask to the ai-review mode (never a silent default).
        if approval_mode == "ai-review" and policy_action != "ask":
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
        # #1044: one-shot AI reviewer (ai-review approval mode only). The pending row +
        # event are already on the ledger (auditable that a review happened, carrying the
        # typed reviewer-pending reason). Ask the in-process reviewer; on a confident
        # allow/deny, resolve the row now (grantor=reviewer, recorded + emitted) and
        # return. On escalate — OR any fail-safe (no LM / reviewer error / timeout) — stamp
        # the TYPED reason and FALL THROUGH to the existing human ``evt.wait`` below: a
        # human decides, never a silent auto-allow. The reviewer runs NO tools and spawns
        # nothing, so it cannot re-enter this (blocked) gate — no deadlock, no starvation.
        if approval_mode == "ai-review" and policy_action != "ask":
            from clio_agent.gact.runtime.ai_review import ai_review_verdict  # noqa: PLC0415

            verdict, reason = ai_review_verdict(app, sid, name, args, context, subject=subject)
            row["reason"] = reason
            if verdict in {"allow", "deny"}:
                resolve_permission(app, pid, verdict, grantor=GRANTOR_REVIEWER)
                return verdict
            # escalate/no-LM/error/timeout: the row stays pending with its typed escalation
            # reason and the call falls through to the human wait below (fail-safe).
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
