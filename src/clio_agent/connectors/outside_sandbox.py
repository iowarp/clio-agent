"""A connector's server running outside CLIO's sandbox, after the person allows it.

Every stdio connector server runs in the fleet fence (:mod:`clio_agent.tools.mcp_confine`).
Some servers start desktop apps or user-scope installers, and those keep the person's own
settings where the fence blocks them: the Windows registry and Documents, ``~/Library`` on
macOS, ``~/.config`` and the display socket on Linux. A connector entry that needs that
says so in ``outside_sandbox`` (what its server starts, in words). The first time an agent
calls one of its tools under an active fence, the permission gate asks the person once,
allow or deny (:mod:`clio_agent.gact.connector_outside`); no rule, mode or AI reviewer
decides it. The decision is kept in ``connectors.yaml`` for that connector and the server
declaration it was given for, so a changed server (another pinned release, another
command) is asked again. An allow makes the connector's server spawn unconfined from then
on; the person takes it back on the connector (``DELETE
/v1/connectors/{id}/outside-sandbox``), and the server runs fenced again.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from clio_agent.connectors import state

#: The connector entry field, and the stored choice's key.
FIELD = "outside_sandbox"
#: The confinement reason a server spawned this way carries.
REASON_ALLOWED = "outside_sandbox_allowed_for_connector"

Decision = Literal["allow", "deny"]


def _entry(connector_id: str) -> Any:
    return state.all_entries().get(connector_id) if connector_id else None


def declared(connector_id: str) -> str:
    """What the connector's server starts, as its entry says ("" when it asks for nothing)."""

    found = _entry(connector_id)
    return str(getattr(found, "outside_sandbox", "") or "") if found is not None else ""


def server_key(connector_id: str) -> str:
    """The fingerprint of the server declaration a decision is given for."""

    found = _entry(connector_id)
    server = dict(getattr(found, "server", None) or {}) if found is not None else {}
    shape = {key: server.get(key) for key in ("command", "args", "url")}
    return hashlib.sha256(json.dumps(shape, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def decision(connector_id: str) -> Decision | None:
    """The person's decision for the connector's current server, or ``None`` (not asked)."""

    if not declared(connector_id):
        return None
    stored = state.field_value(connector_id, FIELD)
    if not isinstance(stored, dict) or stored.get("server") != server_key(connector_id):
        return None
    value = stored.get("decision")
    return value if value in ("allow", "deny") else None


def allowed(connector_id: str) -> bool:
    """Whether the connector's server spawns outside the sandbox."""

    return decision(connector_id) == "allow"


def record(connector_id: str, value: Decision) -> None:
    """Keep the person's decision for the connector's current server."""

    state.set_field(connector_id, FIELD, {"decision": value, "server": server_key(connector_id)})


def forget(connector_id: str) -> None:
    """Take the decision back: the server runs fenced, and the person is asked again."""

    state.set_field(connector_id, FIELD, None)


def wire(connector_id: str) -> dict[str, Any] | None:
    """The connector row's ``outside_sandbox``: what it starts and the person's decision."""

    reason = declared(connector_id)
    return {"reason": reason, "decision": decision(connector_id)} if reason else None
