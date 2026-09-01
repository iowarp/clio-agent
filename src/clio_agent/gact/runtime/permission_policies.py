"""Permission-policy data machinery for the GACT server (#714 decomposition).

SPEC §6.11.b permission policies are declarative ``allow``/``deny``/``ask`` rules
consulted at the permission boundary. This module is the single
owner of the *data* layer behind them -- validation, on-disk load/flush, and the
derivation of a sticky policy from an ``allow_session``/``allow_workspace``
permission resolution -- so both the request handlers in
:mod:`clio_agent.gact.routes.permissions` and the ``build_app`` startup path in
:mod:`clio_agent.gact.app` share one implementation instead of duplicating it.

It is a near-leaf: it imports only stdlib + the wire ``types`` it needs nothing
of, reaches state exclusively through the ``app.state`` attributes it is handed
(``permission_policies``, ``permission_policies_path``, ``sessions``), and never
imports :mod:`clio_agent.gact.app` (preserving the no-cycle invariant). The
*enforcement* side (``_policy_action_for_tool`` / ``_guard_direct_destructive_action``)
stays in ``app.py`` and reuses :func:`_permission_path_from_args` from here.

Responsibilities:

* :data:`_PERMISSION_POLICY_SCOPES` / :data:`_PERMISSION_POLICY_ACTIONS` -- the
  allowed ``scope`` / ``action`` enumerations surfaced to the client on a 422.
* :func:`_permission_path_from_args` -- extract the file path a tool call targets
  (shared with the gate-enforcement helpers in ``app.py``).
* :func:`_validate_permission_policies` -- atomically validate+normalize a
  ``PUT /v1/policies`` body (and the persisted rows on load).
* :func:`_load_permission_policies` / :func:`_flush_permission_policies` -- read
  and write the persisted policy list.
* :func:`_append_permission_policy_from_resolution` -- turn an
  ``allow_session``/``allow_workspace`` permission resolution into a sticky policy.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.gact.runtime.grant_resolver import (
    _VALID_KINDS as VALID_KINDS,  # kind enumeration owner (B4 #1057 — validate at the boundary)
)
from clio_agent.gact.runtime.grant_resolver import (
    KIND_DOMAIN,
    migrate_priorities,
    next_append_priority,
    resolve,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

_PERMISSION_POLICY_SCOPES = {"session", "workspace"}
_PERMISSION_POLICY_ACTIONS = {"allow", "allow_session", "allow_workspace", "deny", "ask"}

#: The request kind on a pending-row for a deny-mode egress prompt (B5 #979.5). It rides the
#: SAME interactive permission gate as a destructive tool call — a new request KIND, not a new
#: gate (⚑ #974.8) — so ``allow_workspace`` derives a sticky ``host_pattern`` policy below.
NETWORK_EGRESS_REQUEST_KIND = "network_egress"


def _permission_host_from_args(args: Mapping[str, Any]) -> str:
    """Return the egress host a ``network_egress`` request targets (B5 #979.5).

    The deny-mode chokepoint prompt stores the requested authority host under ``host`` (the
    ``used web:<domain>`` vocabulary), so an ``allow_workspace`` resolution derives a sticky
    ``host_pattern`` policy from it — the domain analogue of ``path_pattern``.
    """

    value = args.get("host")
    return value.strip().lower() if isinstance(value, str) and value.strip() else ""


def _permission_path_from_args(args: Mapping[str, Any]) -> str:
    """Return the first file path a tool call targets, for policy path matching."""

    for key in ("filepath", "path", "output_path", "target_path"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _validate_permission_policies(
    policies: list[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate and normalize `/v1/policies` rows.

    Invalid permission policies are a safety bug: silently dropping or storing
    a typoed deny rule can make a user believe a destructive action is blocked
    when it is not. Return every validation error so the caller can reject the
    whole update atomically.
    """

    clean: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for index, raw_policy in enumerate(policies):
        if not isinstance(raw_policy, dict):
            errors.append(
                {
                    "index": index,
                    "field": "policy",
                    "message": "policy must be an object",
                }
            )
            continue

        policy = dict(raw_policy)
        scope_raw = policy.get("scope")
        action_raw = policy.get("action")
        scope = scope_raw.strip().lower() if isinstance(scope_raw, str) else ""
        action = action_raw.strip().lower() if isinstance(action_raw, str) else ""
        policy_has_errors = False

        if scope not in _PERMISSION_POLICY_SCOPES:
            policy_has_errors = True
            errors.append(
                {
                    "index": index,
                    "field": "scope",
                    "message": "scope must be one of session, workspace",
                }
            )
        if action not in _PERMISSION_POLICY_ACTIONS:
            policy_has_errors = True
            errors.append(
                {
                    "index": index,
                    "field": "action",
                    "message": (
                        "action must be one of allow, allow_session, allow_workspace, deny, ask"
                    ),
                }
            )

        # B4 #1057: an EXPLICIT ``kind`` must be one of the resolver's valid discriminators.
        # Reject garbage (e.g. ``"Domain"``, a typo) with a typed reason rather than letting it
        # fall through :func:`grant_resolver._kind_admitted` to the legacy host-presence
        # classification, where a mis-cased kind would silently mis-route the row (⚑ no-silent-
        # fallback). Absence is legitimate — the kind is synthesized from row shape at match time.
        kind_raw = policy.get("kind")
        if kind_raw is not None and (
            not isinstance(kind_raw, str) or kind_raw.strip() not in VALID_KINDS
        ):
            policy_has_errors = True
            errors.append(
                {
                    "index": index,
                    "field": "kind",
                    "message": f"kind must be one of {', '.join(sorted(VALID_KINDS))} when present",
                }
            )

        for field in ("scope_id", "tool_name_pattern", "path_pattern", "host_pattern"):
            value = policy.get(field)
            if value is not None and not isinstance(value, str):
                policy_has_errors = True
                errors.append(
                    {
                        "index": index,
                        "field": field,
                        "message": f"{field} must be a string when present",
                    }
                )

        # The P0.2 (#1060) axis fields ``modes``/``on`` are optional list[str] narrowing filters.
        # A malformed axis (not a list, or a non-string entry) must be REJECTED with a typed reason,
        # never silently coerced or dropped (⚑ no-silent-fallback) — a typoed axis that were silently
        # ignored would widen a plan/hook grant the user believed was scoped.
        for field in ("modes", "on"):
            value = policy.get(field)
            if value is None:
                continue
            if not isinstance(value, list):
                policy_has_errors = True
                errors.append(
                    {
                        "index": index,
                        "field": field,
                        "message": f"{field} must be a list of strings when present",
                    }
                )
            elif not all(isinstance(entry, str) for entry in value):
                policy_has_errors = True
                errors.append(
                    {
                        "index": index,
                        "field": field,
                        "message": f"{field} entries must all be strings",
                    }
                )

        # A malformed priority must be REJECTED with a typed reason, never silently defaulted
        # (⚑ no-silent-fallback). Absence is legitimate — the load-time migration assigns a
        # stable descending priority. ``bool`` is an ``int`` subclass but never a valid priority.
        priority_raw = policy.get("priority")
        if priority_raw is not None and (
            not isinstance(priority_raw, int) or isinstance(priority_raw, bool)
        ):
            policy_has_errors = True
            errors.append(
                {
                    "index": index,
                    "field": "priority",
                    "message": "priority must be an integer when present",
                }
            )

        if policy_has_errors:
            continue

        policy["scope"] = scope
        policy["action"] = action
        # B4 #1057: a host-bearing row is a DOMAIN grant — stamp ``kind="domain"`` and drop any
        # stray ``tool_name_pattern`` (legacy domain rows persisted a ``"*"`` glob that let the row
        # bleed into a ``kind="tool"`` resolve). This self-heals the persisted shape on the next
        # flush; ``grant_resolver._kind_admitted`` is the belt-and-suspenders match-time guard for
        # any un-normalized in-memory row.
        if str(policy.get("host_pattern") or ""):
            policy["kind"] = KIND_DOMAIN
            policy.pop("tool_name_pattern", None)
        clean.append(policy)
    return clean, errors


def _load_permission_policies(path: Path | None) -> list[dict[str, Any]]:
    """Load persisted permission policies, ignoring invalid rows."""

    if path is None or not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - unreadable/invalid policy file yields no policies
        return []
    raw = data.get("policies", []) if isinstance(data, Mapping) else []
    if not isinstance(raw, list):
        return []
    clean, _errors = _validate_permission_policies(raw)
    # Migrate on load: legacy rows without a priority gain a unique descending priority by
    # insertion index (first row highest), so the priority-banded resolver reproduces this
    # store's historical first-match order exactly (P0.1 #1059).
    return migrate_priorities(clean)


def _flush_permission_policies(app: "FastAPI") -> None:
    """Persist the current permission policy list, if configured."""

    path = getattr(app.state, "permission_policies_path", None)
    if path is None:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps({"policies": app.state.permission_policies}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(path)


def _append_permission_policy_from_resolution(
    app: "FastAPI",
    *,
    row: Mapping[str, Any],
    action: str,
) -> dict[str, Any] | None:
    """Persist allow_session/allow_workspace decisions as policy rules."""

    if action not in {"allow_session", "allow_workspace"}:
        return None
    session_id = str(row.get("session_id") or "")
    raw_tool_call = row.get("tool_call")
    tool_call: Mapping[str, Any] = raw_tool_call if isinstance(raw_tool_call, Mapping) else {}
    tool_name = str(tool_call.get("tool_name") or "*")
    raw_args = tool_call.get("input")
    args: Mapping[str, Any] = raw_args if isinstance(raw_args, Mapping) else {}
    session = app.state.sessions.get(session_id) if session_id else None
    workspace_id = str(getattr(session, "workspace_id", "") or "")
    policy = {
        "scope": "session" if action == "allow_session" else "workspace",
        "scope_id": session_id if action == "allow_session" else workspace_id,
        "tool_name_pattern": tool_name,
        "action": "allow",
        "created_from_permission_id": str(row.get("id") or ""),
    }
    # A deny-mode egress prompt (B5 #979.5) is a ``network_egress`` request kind: an
    # ``allow_workspace`` resolution derives a sticky ``host_pattern`` policy (the domain
    # analogue of ``path_pattern``) the chokepoint consults, NOT a file path_pattern.
    if str(row.get("kind") or "") == NETWORK_EGRESS_REQUEST_KIND:
        host = _permission_host_from_args(args)
        if not host:
            # B4 #1057: a hostless egress resolution has no domain SUBJECT. Stamping
            # ``kind="domain"`` with an empty ``host_pattern`` would persist a permanently inert,
            # subject-less row (:func:`grant_resolver._policy_pattern_matches` never matches an
            # empty host pattern). Refuse to derive a policy and record the typed reason instead
            # of silently writing an unmatchable grant (⚑ no-silent-fallback).
            logger.warning(
                "egress sticky policy skipped reason=egress_grant_missing_host "
                "session=%s permission=%s",
                session_id,
                str(row.get("id") or ""),
            )
            return None
        # B4 #1057: the derived sticky row is a DOMAIN grant — stamp ``kind="domain"`` and drop the
        # tool glob copied in above, so it can never bleed into a ``kind="tool"`` resolve (the stray
        # ``"*"`` glob was the fleet-egress kind-bleed vector).
        policy["host_pattern"] = host
        policy["kind"] = KIND_DOMAIN
        policy.pop("tool_name_pattern", None)
        return _appended(app, policy)
    path = _permission_path_from_args(args)
    if path:
        policy["path_pattern"] = path
    return _appended(app, policy)


def _appended(app: "FastAPI", policy: dict[str, Any]) -> dict[str, Any]:
    """Append ``policy`` as a sticky runtime grant, in its own strictly-lowest priority band.

    A sticky grant appended at runtime must be evaluated LAST (lowest precedence) to preserve the
    historical first-match order (P0.1 #1059 follow-up). Stamping an explicit ``priority`` here
    -- strictly below every existing row's effective priority -- is required: leaving it unset lets
    :func:`resolve`'s live migration derive ``total - index``, which can collide with the current
    lowest-priority (often already-migrated legacy) row and wrongly trigger the most-restrictive
    tie-break. See :func:`~clio_agent.gact.runtime.grant_resolver.next_append_priority`.
    """

    policies = app.state.permission_policies
    policy["priority"] = next_append_priority(policies)
    policies.append(policy)
    return policy


#: Marker key stamped on a row projected from a parent session onto its spawned child, so
#: the inheritance is queryable in the persisted store rather than indistinguishable from a
#: row the user authored directly against the child.
INHERITED_FROM_SESSION = "inherited_from_session_id"

#: The only actions a spawn may project. ``allow``/``allow_session``/``allow_workspace`` would
#: WIDEN the child beyond the parent's posture, which spawn must never do.
_INHERITABLE_ACTIONS = frozenset({"ask", "deny"})


def inherit_child_session_policies(
    app: "FastAPI", parent_session_id: str, child_session_id: str
) -> list[dict[str, Any]]:
    """Project a parent's narrowing session-scoped policy rows onto a spawned child.

    A child inherits the parent's WIDENING axis (``approval_mode``) at spawn. The
    NARROWING axis has to compose the same way: a session-scoped ``ask``/``deny`` row
    is keyed to the parent's session id, and
    :func:`~clio_agent.gact.runtime.grant_resolver._scope_matches` admits a session
    row only for that exact id -- so without this projection a call the parent would
    prompt for (the explicit-``ask`` escape hatch that survives even ``bypass``) runs
    unprompted in the child.

    Each projection copies the source row verbatim except for ``scope_id``, preserving
    its ``priority`` band, subject patterns and ``modes``/``on`` axes, so the child
    resolves the call exactly as the parent does.

    Args:
        app: The GACT application whose ``state.permission_policies`` holds the rows.
        parent_session_id: The spawning parent's session id.
        child_session_id: The freshly minted child's session id.

    Returns:
        The rows appended for the child, in source order; empty when the parent carries
        no narrowing session-scoped rows.
    """

    policies = getattr(app.state, "permission_policies", None)
    if not isinstance(policies, list) or not parent_session_id or not child_session_id:
        return []
    projected: list[dict[str, Any]] = []
    for policy in list(policies):
        if not isinstance(policy, Mapping):
            continue
        if str(policy.get("scope") or "").lower() != "session":
            continue
        if str(policy.get("scope_id") or "") != parent_session_id:
            continue
        if str(policy.get("action") or "").lower() not in _INHERITABLE_ACTIONS:
            continue
        row = dict(policy)
        row["scope_id"] = child_session_id
        row[INHERITED_FROM_SESSION] = parent_session_id
        projected.append(row)
    if projected:
        policies.extend(projected)
        logger.info(
            "child session inherits parent narrowing policies "
            "reason=child_policy_inheritance parent=%s child=%s count=%d",
            parent_session_id,
            child_session_id,
            len(projected),
        )
    return projected


def _host_action_for(
    app: "FastAPI",
    *,
    workspace_id: str,
    host: str,
) -> str:
    """Return the first matching WORKSPACE-scoped ``host_pattern`` policy action (B5 #979.5).

    Consulted by the deny-mode egress chokepoint gate: a workspace-scoped ``host_pattern``
    fnmatch (the ``path_pattern`` shape, applied to the requested authority host) whose action
    is ``allow``/``allow_workspace`` lets the CONNECT through with no gate; ``deny`` blocks it.
    ``""`` means no host policy matched (the caller then opens the interactive gate).

    SESSION-scoped ``host_pattern`` policies are DELIBERATELY NOT honoured here (review
    finding 2). The egress a fleet child opens is workspace-SHARED — one persistent confined
    child serves every session in the workspace, and the ``EgressRecord`` carries no session id
    — so a connection cannot be attributed to a single session. Honouring a session-scoped host
    grant on an unattributable connection would let the MORE-restrictive ``allow_session`` choice
    LEAK to every session/workspace (broader than ``allow_workspace``). A session-scoped host
    grant that cannot be attributed must therefore NOT widen the boundary: it is skipped, so the
    connection re-prompts (fail-safe) rather than silently allowing global egress. A missing
    ``scope_id`` on a WORKSPACE row is also NOT treated as a wildcard here — an empty workspace
    scope_id would match every workspace, the same leak — so it is skipped.

    A thin shim over :func:`~clio_agent.gact.runtime.grant_resolver.resolve` (``kind="domain"``),
    which encodes this leak guard as the domain kind's per-kind scope rule. Kept as a named shim
    because :mod:`clio_agent.gact.runtime.grants` binds it (and monkeypatches it in tests).
    """

    return resolve(
        "domain",
        host,
        policies=getattr(app.state, "permission_policies", []),
        workspace_id=workspace_id,
    )
