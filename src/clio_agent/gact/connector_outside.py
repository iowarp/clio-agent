"""The person's one question before a connector's server may run outside CLIO's sandbox.

A connector entry that declares ``outside_sandbox`` (its server starts a desktop app or a
user-scope installer, :mod:`clio_agent.connectors.outside_sandbox`) is asked about here,
first in :func:`permission_gate._make_permission_gate`, at the first call of one of its
tools under an active fence: before that call reaches the server, so before the server
can start anything fenced. Only a person answers it, whatever the approval mode, the
person's rules or the AI reviewer would do, and only allow or deny. The answer is about
where the server runs, not about the call, which then goes through the gate's other
layers as before. An allow restarts the connector's server outside the sandbox in every
resident fleet, so this very call already reaches the unfenced server. Nothing is asked
in an unattended run, when a rule or the mode denies the call, or when the question ends
with the turn: the server stays fenced and the question comes again next time.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from clio_agent.connectors import outside_sandbox
from clio_agent.runtime import trace

if TYPE_CHECKING:
    from fastapi import FastAPI

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock(connector: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(connector, threading.Lock())


def _mounted(app: "FastAPI", connector: str) -> list[tuple[str, Any, str, Any]]:
    """``(root, executor, namespace, spec)`` for every resident fleet that runs ``connector``."""

    from clio_agent.gact.connector_rebind import _executors  # noqa: PLC0415

    agent = getattr(app.state, "agent", None)
    found: list[tuple[str, Any, str, Any]] = []
    if agent is None:
        return found
    for root, executor in _executors(agent):
        if getattr(executor, "closed", False) or getattr(executor, "_closed", False):
            continue
        specs = getattr(executor, "_clio_namespace_specs", None)
        if not isinstance(specs, dict):
            continue
        for namespace, spec in list(specs.items()):
            if (getattr(spec, "connector", "") or "") == connector:
                found.append((root, executor, namespace, spec))
    return found


def connector_of(app: "FastAPI", name: str) -> str:
    """The connector whose server serves tool ``name`` ("" for none)."""

    from clio_agent.gact.connector_rebind import _executors  # noqa: PLC0415
    from clio_agent.tools.gateway import _namespace_of  # noqa: PLC0415

    namespace = _namespace_of(name)
    agent = getattr(app.state, "agent", None)
    for _root, executor in _executors(agent) if agent is not None else ():
        specs = getattr(executor, "_clio_namespace_specs", None)
        spec = specs.get(namespace) if isinstance(specs, dict) else None
        if spec is not None:
            return str(getattr(spec, "connector", "") or "")
    return ""


def restart_servers(app: "FastAPI", connector: str, reason: str) -> int:
    """Start the connector's server again, under its current decision, in every fleet."""

    from clio_agent.gact.connector_rebind import _install  # noqa: PLC0415

    mounted = _mounted(app, connector)
    for root, executor, namespace, spec in mounted:
        _install(executor, namespace, spec, root)
    trace.event(
        "TOOLS",
        "connector_outside_sandbox_restart connector=%s reason=%s fleets=%d",
        connector,
        reason,
        len(mounted),
    )
    return len(mounted)


def card_summary(connector: str) -> str:
    """The question: what running outside the sandbox means for this connector."""

    from clio_agent.connectors import state  # noqa: PLC0415

    found = state.all_entries().get(connector)
    title = str(getattr(found, "name", "") or connector)
    return (
        f"Let {title}'s server run outside CLIO's sandbox? "
        f"{outside_sandbox.declared(connector)} "
        "Outside the sandbox it can read and write anything your account can, for every "
        "conversation, until you take this back on the connector."
    )[:500]


def ask_if_needed(
    app: "FastAPI", name: str, args: Mapping[str, Any], context: Mapping[str, Any] | None
) -> None:
    """Put the question to the person when tool ``name``'s connector needs it, once."""

    from clio_agent.runtime import sandbox  # noqa: PLC0415

    if not sandbox.fence_enforced():
        return  # nothing to leave: every server already runs unconfined
    connector = connector_of(app, name)
    if not connector or not outside_sandbox.declared(connector):
        return
    if outside_sandbox.decision(connector) is not None:
        return
    with _lock(connector):
        if outside_sandbox.decision(connector) is not None:
            return
        try:
            _ask(app, connector, name, args, context)
        except Exception as exc:  # noqa: BLE001 - the server stays fenced; the call goes on
            trace.event(
                "TOOLS",
                "connector_outside_sandbox_ask_failed connector=%s reason=%s",
                connector,
                type(exc).__name__,
            )


def _ask(
    app: "FastAPI",
    connector: str,
    name: str,
    args: Mapping[str, Any],
    context: Mapping[str, Any] | None,
) -> None:
    from clio_agent.gact import permission_gate as gate  # noqa: PLC0415 - avoid import cycle
    from clio_agent.gact import unattended  # noqa: PLC0415

    sid, current = gate._resolve_tool_session(app)
    if not sid or unattended.is_unattended(app, sid):
        return  # nobody to ask: the server stays fenced
    mode = gate.policy_mode(current)
    policy_action, _message = gate._policy_detail_for_tool(
        app, session_id=sid, session=current, tool_name=name, args=args, mode=mode
    )
    if policy_action == "deny":
        return  # the call itself is refused by the gate's other layers
    cancelled = gate.question_cancel_token(app, sid)
    card = {
        **(context or {}),
        "kind": "native",
        "summary": card_summary(connector),
        "answers": ["allow", "deny"],
        # An allow is kept for the connector, not used once: the card words it so.
        "consent": {"scope": "connector_outside_sandbox", "connector": connector, "lasting": True},
    }
    decision = gate._ask_person(
        app,
        sid,
        name,
        args,
        card,
        gate.action_subject(name, args),
        gate.script_grants.approval_mode(current),
        "ask",
        cancelled,
    )
    if cancelled is not None and cancelled.is_set():
        return  # the question ended with the turn: asked again next time
    value = "allow" if decision == "allow" else "deny"
    outside_sandbox.record(connector, value)
    trace.event(
        "TOOLS", "connector_outside_sandbox_decided connector=%s decision=%s", connector, value
    )
    if value == "allow":
        restart_servers(app, connector, "outside_sandbox_allowed")
    from clio_agent.gact.connectors_service import publish_changed  # noqa: PLC0415

    publish_changed(app, connector, [sid])
