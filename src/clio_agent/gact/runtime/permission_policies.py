"""Permission-policy data machinery for the GACT server (#714 decomposition).

SPEC §6.11.b permission policies are declarative ``allow``/``deny``/``ask`` rules
consulted before the per-tool ``permission_default``. This module is the single
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

import fnmatch
import json
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

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

        if policy_has_errors:
            continue

        policy["scope"] = scope
        policy["action"] = action
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
    return clean


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
        if host:
            policy["host_pattern"] = host
        return _appended(app, policy)
    path = _permission_path_from_args(args)
    if path:
        policy["path_pattern"] = path
    return _appended(app, policy)


def _appended(app: "FastAPI", policy: dict[str, Any]) -> dict[str, Any]:
    app.state.permission_policies.append(policy)
    return policy


def _host_action_for(
    app: "FastAPI",
    *,
    workspace_id: str,
    host: str,
) -> str:
    """Return the first matching ``host_pattern`` policy action for ``host`` (B5 #979.5).

    Consulted by the deny-mode egress chokepoint gate: a workspace-scoped ``host_pattern``
    fnmatch (the ``path_pattern`` shape, applied to the requested authority host) whose action
    is ``allow``/``allow_workspace`` lets the CONNECT through with no gate; ``deny`` blocks it.
    ``""`` means no host policy matched (the caller then opens the interactive gate). Session
    scope is honoured too (a session-scoped host grant), keyed by the egress child's session
    when known; workspace scope is the default a deny-mode grant writes.
    """

    policies = getattr(app.state, "permission_policies", [])
    if not isinstance(policies, list) or not host:
        return ""
    host = host.strip().lower()
    for policy in policies:
        if not isinstance(policy, dict):
            continue
        host_pattern = str(policy.get("host_pattern") or "")
        if not host_pattern:
            continue
        scope = str(policy.get("scope") or "").lower()
        scope_id = str(policy.get("scope_id") or "")
        if scope == "workspace":
            if scope_id and scope_id != workspace_id:
                continue
        elif scope != "session":
            continue
        if not fnmatch.fnmatchcase(host, host_pattern.strip().lower()):
            continue
        action = str(policy.get("action") or "").lower()
        if action in {"allow", "allow_session", "allow_workspace", "deny", "ask"}:
            return action
    return ""
