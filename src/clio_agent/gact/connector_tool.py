"""``set_connector_value``: the agent sets a connector need marked ``agent_settable`` (CN6).

The agent uses it when the person gives a value in the conversation ("connect to
the server on node08") or another tool just produced one. It is refused for a
secret and for a sign-in (only the person types or signs in, in the setup card)
and for a need not marked ``agent_settable``. Like any built-in it passes the
session's tool permission rules (``tool_permission_layers.native_gate``). The value
is applied as the setup card applies it (CN7): the server reconnects or restarts and
lists its tools again, and the card shows the new value.

Its row reads "Set <need> of <connector>"; expansion shows the value set and the
connector's state after it, or the typed reason it was refused.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clio_agent.gact.agents.tool_instrumentation import native_tool

TOOL_NAME = "set_connector_value"


def _settable(found_all: Mapping[str, Any]) -> list[str]:
    from clio_agent.connectors import state  # noqa: PLC0415

    enabled = {cid for cid, choice in state.choices().items() if choice.enabled}
    return [
        f"{found.id}.{need.key} ({found.name}: {need.label})"
        for found in found_all.values()
        if found.id in enabled
        for need in found.needs
        if need.agent_settable
    ]


def _match(found_all: Mapping[str, Any], connector: str) -> Any:
    wanted = connector.strip().casefold()
    for found in found_all.values():
        if wanted in {found.id.casefold(), found.name.casefold()}:
            return found
    return None


async def set_connector_value(connector: str, key: str, value: str) -> dict[str, Any]:
    """Set one agent-settable need of an enabled connector."""

    from clio_agent.connectors import state  # noqa: PLC0415
    from clio_agent.gact import connectors_service as service  # noqa: PLC0415
    from clio_agent.gact import context  # noqa: PLC0415

    app = context.active_app()
    found_all = state.all_entries()
    found = _match(found_all, connector)
    settable = _settable(found_all)
    if found is None or not state.choice(found.id).enabled:
        return {
            "error": "connector_not_enabled",
            "message": f"No enabled connector {connector!r}.",
            "settable": settable,
        }
    need = found.need(key) or next(
        (n for n in found.needs if n.label.casefold() == key.strip().casefold()), None
    )
    if need is None:
        return {
            "error": "connector_need_unknown",
            "connector": found.id,
            "connector_name": found.name,
            "message": f"{found.name} has no need {key!r}.",
            "settable": settable,
        }
    try:
        updated = await service.set_values(app, found.id, {need.key: value}, by_agent=True)
    except service.ConnectorError as exc:
        return {
            "error": exc.code,
            "connector": found.id,
            "connector_name": found.name,
            "key": need.key,
            "label": need.label,
            "message": str(exc),
        }
    status = updated["state"]
    return {
        "connector": found.id,
        "connector_name": found.name,
        "key": need.key,
        "label": need.label,
        "value": value,
        "state": status,
        "tool_count": updated.get("tool_count"),
        "message": f"{need.label} of {found.name} is now {value!r}; the connector is "
        + _state_words(status),
    }


def _state_words(status: Mapping[str, Any]) -> str:
    if status.get("status") == "ready":
        return "ready."
    if status.get("status") == "needs":
        return f"waiting for: {', '.join(status.get('needs') or ())}."
    return f"not reachable: {status.get('reason') or 'no reason given'}"


def present(args: Mapping[str, Any], result: Any, structured: Any) -> dict[str, Any]:
    """One row: Set <need> of <connector>; the value, then the state or the refusal."""

    row = structured if isinstance(structured, Mapping) else result
    row = row if isinstance(row, Mapping) else {}
    connector = str(row.get("connector_name") or args.get("connector") or "connector")
    need = str(row.get("label") or args.get("key") or "value")
    blocks: list[dict[str, Any]] = []
    if row.get("error"):
        blocks.append(
            {
                "id": "outcome",
                "type": "text",
                "label": "Not set",
                "severity": "error",
                "text": str(row.get("message") or row["error"]),
                "detail": str(row["error"]),
            }
        )
        if row.get("settable"):
            blocks.append(
                {
                    "id": "settable",
                    "type": "text",
                    "label": "The agent can set",
                    "text": "\n".join(str(item) for item in row["settable"]),
                }
            )
        return {
            "action": "Set",
            "subject": f"{need} of {connector}",
            "status": "failed",
            "summary": str(row.get("message") or ""),
            "blocks": blocks,
        }
    blocks.append({"id": "value", "type": "text", "label": need, "text": str(row.get("value"))})
    status = row.get("state") if isinstance(row.get("state"), Mapping) else {}
    blocks.append(
        {
            "id": "state",
            "type": "text",
            "label": connector,
            "text": _state_words(status).rstrip(".").capitalize()
            + (f" ({row['tool_count']} tools)" if row.get("tool_count") is not None else ""),
            **({"severity": "error"} if status.get("status") == "unreachable" else {}),
        }
    )
    return {
        "action": "Set",
        "subject": f"{need} of {connector}",
        "summary": str(row.get("message") or ""),
        "blocks": blocks,
    }


def build_set_connector_value_tool() -> Any:
    return native_tool(
        set_connector_value,
        name=TOOL_NAME,
        domain="tasks",
        title="Set connector value",
        desc=(
            "Set a value one of the person's connectors needs, such as where its server "
            "runs, when the person gives it in the conversation or a tool just produced "
            "it. Only needs the connector marks as settable by the agent; never a secret "
            "or a sign-in, which the person enters in the setup card. The connector "
            "reconnects with the value and lists its tools again."
        ),
        args={
            "connector": {"type": "string", "description": "The connector's id or name."},
            "key": {"type": "string", "description": "The need's key or label."},
            "value": {"type": "string", "description": "The value, one plain line."},
        },
        presentation=present,
    )
