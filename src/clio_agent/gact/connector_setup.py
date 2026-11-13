"""The setup card in the conversation (CN5).

When the agent calls a tool of an enabled connector that is not ready (a required
value or secret is empty, its sign-in has no account), the call does not fail. It
waits on its tool thread, after the permission gate allowed it, exactly as a call
waits for a permission (``permission_gate._ask_and_wait``): a pending interaction of
kind ``connector_setup`` is published with the missing rows, and the person answers
it at ``POST /v1/sessions/{sid}/interactions/connector:<id>/respond``:

* ``submit`` with ``values`` (secrets included; a sign-in row completes through the
  sign-in flow first) applies them as Extensions does (CN7, which also rebinds the
  connector's namespace in the fleet this call runs on), and the same call
  continues and returns its result. A submit that leaves a required row empty is
  refused (422) and the card stays.
* ``decline`` returns the typed refusal ``connector_declined`` naming the connector.

There is no timeout (plan rule 6): stopping the turn ends the wait, and the call is
recorded as cancelled. The interaction lives in memory beside the permissions: the
values are never stored in it, nor in a question record or a transcript.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from clio_agent.gact.events import Event
from clio_agent.runtime import trace

DECLINED = "connector_declined"
PREFIX = "connector:"


def _rows(app: Any) -> dict[str, dict[str, Any]]:
    rows = getattr(app.state, "connector_setups", None)
    if rows is None:
        rows = app.state.connector_setups = {}
    return rows


def _events(app: Any) -> dict[str, threading.Event]:
    events = getattr(app.state, "connector_setup_events", None)
    if events is None:
        events = app.state.connector_setup_events = {}
    return events


def _publish(app: Any, event_type: str, row: Mapping[str, Any]) -> None:
    from clio_agent.gact.permission_delivery import attended_session_id  # noqa: PLC0415

    bus = getattr(app.state, "bus", None)
    if bus is None:
        return
    owner = str(row["session_id"])
    payload = {
        "session_id": owner,
        "interaction_id": PREFIX + str(row["id"]),
        "connector_id": row["connector_id"],
    }
    for target in {owner, attended_session_id(app, owner)}:
        bus.publish(Event(type=event_type, session_id=target, payload=dict(payload)))


def _denial(message: str, outcome: str = "") -> Any:
    from clio_agent.gact.permission_gate import DenyDecision  # noqa: PLC0415

    decision = DenyDecision(message)
    if outcome:
        decision.outcome = outcome  # type: ignore[attr-defined]
    return decision


def wait_for_card(app: Any, row: dict[str, Any]) -> bool:
    """Show ``row`` as a card and wait for the person; ``True`` when the turn was stopped.

    No timeout (plan rule 6): only an answer or stopping the turn ends the wait. The
    row's ``status`` says how it was answered.
    """

    from clio_agent.gact.code_task_context import question_cancel_token  # noqa: PLC0415
    from clio_agent.runtime.commitment_activity import track  # noqa: PLC0415

    event = threading.Event()
    _events(app)[row["id"]] = event
    _rows(app)[row["id"]] = row
    _publish(app, "connector.setup.requested", row)
    session_id = str(row["session_id"])
    cancelled = question_cancel_token(app, session_id)
    try:
        with track(True, session_id=session_id):
            while not event.wait(0.1):
                if cancelled is not None and cancelled.is_set():
                    row.update(status="cancelled", updated_at=_now())
                    _publish(app, "connector.setup.resolved", row)
                    return True
    finally:
        _events(app).pop(row["id"], None)
    return False


def await_setup(app: Any, session_id: str, connector_id: str, tool_name: str) -> Any:
    """Wait until the connector is ready; ``None`` to continue, else a refusal."""

    from clio_agent.connectors import state  # noqa: PLC0415
    from clio_agent.gact import connectors_service as service  # noqa: PLC0415
    from clio_agent.tools.tool_observation import peek_call_identity  # noqa: PLC0415

    found = state.all_entries().get(connector_id)
    choice = state.choice(connector_id)
    if found is None or not choice.enabled:
        return None
    absent = service.missing(found, choice)
    if not absent:
        return None
    if not session_id:
        return _denial(f"{DECLINED}: {found.name} needs setup and no conversation can show it")
    row = {
        "id": uuid.uuid4().hex[:12],
        "session_id": session_id,
        "connector_id": found.id,
        "connector_name": found.name,
        "tool_name": tool_name,
        # The waiting call's own id: the conversation shows the card's line under its row.
        "invocation_id": peek_call_identity(),
        "missing": [service.need_row(found, choice, need) for need in absent],
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if wait_for_card(app, row):
        return _denial(
            f"The call to {tool_name!r} was cancelled while {found.name} waited for setup.",
            outcome="cancelled",
        )
    if row["status"] == "declined":
        return _denial(
            f"{DECLINED}: the person declined to set up the {found.name} connector "
            f"({found.id}), so {tool_name} was not called."
        )
    return None


async def retry_after_setup(
    app: Any, context: Any, spec: Any, tool_name: str, error: BaseException
) -> bool:
    """A connector's call the server refused for want of a sign-in waits for its card.

    The server can reject what CLIO holds after the call was allowed (it revoked the
    sign-in on its side; the refresh is refused). The call then waits for the same
    setup card as a call made while not ready, on a worker thread in the call's own
    context. ``True``: set up again, run the call once more. A decline raises the
    typed ``connector_declined`` refusal.
    """

    from clio_agent.connectors.listing import held_for_setup  # noqa: PLC0415

    connector = str(getattr(spec, "connector", "") or "")
    # A call routed through the proxy loses the error's type; the refused sign-in was
    # forgotten (``DurableOAuthProvider``), so CLIO's own record then says what is missing.
    if not connector or not await asyncio.to_thread(held_for_setup, connector, error):
        return False

    def wait() -> Any:
        from clio_agent.gact.runtime.globals import _resolve_tool_session  # noqa: PLC0415

        session_id, _session = _resolve_tool_session(app)
        return await_setup(app, str(session_id or ""), connector, tool_name)

    trace.event("TOOLS", "connector_sign_in_refused connector=%s tool=%s", connector, tool_name)
    refusal = await asyncio.to_thread(context.run, wait)
    if refusal is not None:
        raise PermissionError(getattr(refusal, "deny_message", "") or str(refusal)) from error
    return True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def with_connector_setup(app: Any, gate: Callable[..., Any] | None) -> Callable[..., Any] | None:
    """``gate`` followed, for an allowed call of a connector's tool, by :func:`await_setup`."""

    if gate is None:
        return None

    def gated(name: str, args: Mapping[str, Any], context: Mapping[str, Any] | None = None) -> Any:
        from clio_agent.tools.execution import _invoke_permission_gate  # noqa: PLC0415

        decision = _invoke_permission_gate(gate, name, args, context)
        if decision != "allow" or not context or context.get("kind") != "external_mcp":
            return decision
        from clio_agent.gact.runtime.globals import _resolve_tool_session  # noqa: PLC0415

        session_id, _session = _resolve_tool_session(app)
        refusal = await_setup(app, str(session_id or ""), name.partition("_")[0], name)
        return refusal if refusal is not None else decision

    return gated


# ----------------------------------------------------------- the interaction


def projected(app: Any, owners: set[str]) -> list[Any]:
    """The connector setup rows of ``owners`` as pending interactions."""

    from clio_agent.gact.interaction_types import (  # noqa: PLC0415
        PendingInteraction,
        PendingInteractionSource,
    )
    from clio_agent.gact.permission_delivery import attended_session_id  # noqa: PLC0415

    out = []
    for row in list(_rows(app).values()):
        if row["session_id"] not in owners:
            continue
        pending = row["status"] == "pending"
        offer = row.get("intent") == "enable"
        out.append(
            PendingInteraction(
                id=PREFIX + row["id"],
                kind="connector_setup",
                owner_session_id=row["session_id"],
                attended_session_id=attended_session_id(app, row["session_id"]),
                status={"pending": "pending", "submitted": "answered", "declined": "denied"}.get(
                    row["status"], "cancelled"
                ),
                title=f"Turn on {row['connector_name']}"
                if offer
                else f"Set up {row['connector_name']}",
                prompt=f"The agent offers {row['connector_name']}: {row.get('reason') or ''}".strip()
                if offer
                else f"{row['tool_name']} waits for {row['connector_name']} to be set up.",
                requires_human_response=pending,
                source=PendingInteractionSource(
                    protocol="mcp",
                    tool_name=row["tool_name"],
                    invocation_id=str(row.get("invocation_id") or ""),
                ),
                created_at=row["created_at"],
                revision=str(row.get("updated_at") or row["created_at"]),
                payload={
                    "interaction_id": PREFIX + row["id"],
                    "connector_id": row["connector_id"],
                    "connector_name": row["connector_name"],
                    "logo_url": _logo_url(row["connector_id"]),
                    "missing": row["missing"],
                    "tool_name": row["tool_name"],
                    # Connector discovery (CD-1 to CD-6): the agent offers a connector
                    # that is off; the card turns it on (``connector_offer``).
                    **(
                        {
                            "intent": "enable",
                            "description": row.get("description") or "",
                            "reason": row.get("reason") or "",
                            "agent_ref": row.get("agent_ref") or "",
                            "agent_name": row.get("agent_name") or "",
                            "asks_sign_in": bool(row.get("asks_sign_in")),
                        }
                        if offer
                        else {}
                    ),
                },
                actions=["submit", "decline"] if pending else [],
            )
        )
    return out


def _logo_url(connector_id: str) -> str | None:
    from clio_agent.connectors import state  # noqa: PLC0415

    found = state.all_entries().get(connector_id)
    return f"/v1/connectors/{connector_id}/logo" if found is not None and found.logo else None


class SetupResponseError(ValueError):
    def __init__(self, status: int, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.status, self.code, self.details = status, code, details


async def respond(
    app: Any,
    scope: set[str],
    interaction_id: str,
    action: str,
    values: Mapping[str, Any],
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Answer a setup card; returns the updated row's interaction.

    An offer card (``intent: enable``) is answered by :func:`.connector_offer.answer`:
    ``submit`` turns the connector on with ``metadata.scope`` (``all`` or ``agent``).
    """

    from clio_agent.connectors import state  # noqa: PLC0415
    from clio_agent.gact import connectors_service as service  # noqa: PLC0415

    row = _rows(app).get(interaction_id.removeprefix(PREFIX))
    if row is None or row["session_id"] not in scope:
        raise SetupResponseError(404, "not_found", f"interaction not found: {interaction_id}")
    if row["status"] != "pending":
        raise SetupResponseError(409, "interaction_resolved", "interaction is already resolved")
    if row.get("intent") == "enable":
        from clio_agent.gact.connector_offer import answer  # noqa: PLC0415

        await answer(app, row, action, values, metadata or {})
    elif action == "decline":
        row.update(status="declined", updated_at=_now())
    elif action == "submit":
        found = service.entry(row["connector_id"])
        absent = await asyncio.to_thread(service.missing, found, state.choice(found.id))
        if values:
            try:
                await service.set_values(app, row["connector_id"], values)
            except service.ConnectorError as exc:
                raise SetupResponseError(exc.status, exc.code, str(exc)) from None
            absent = await asyncio.to_thread(service.missing, found, state.choice(found.id))
        elif not absent:
            # Signed in through the sign-in flow: the waiting call's fleet reconnects to it
            # (a connection that failed for want of a sign-in is not reused).
            await service.apply(app, found)
        if absent:
            raise SetupResponseError(
                422,
                "connector_setup_incomplete",
                f"{found.name} still needs: {', '.join(need.label for need in absent)}",
                missing=[need.key for need in absent],
            )
        row.update(status="submitted", updated_at=_now())
    else:
        raise SetupResponseError(422, "validation_error", "action must be submit or decline")
    _publish(app, "connector.setup.resolved", row)
    event = _events(app).get(row["id"])
    if event is not None:
        event.set()
    return next(
        item for item in projected(app, {row["session_id"]}) if item.id == PREFIX + row["id"]
    )
