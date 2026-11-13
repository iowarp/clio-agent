"""Connectors as the API serves them: state in words, setup, enabling (CN1 to CN10).

A connector's state for the person is one of (CN3):

* ``needs``: a required value or secret is empty, or its sign-in has no account;
  ``state.needs`` names the missing keys. An optional empty need never holds a
  connector here.
* ``unreachable``: CLIO started or called its server and it did not answer;
  ``state.reason`` says why, in words.
* ``ready``: nothing is missing and, when enabled, its server listed its tools.

A disabled connector is not started: its state says only what is missing.

Enabling a connector installs and readies it and nothing more (CN10 *Amended*): it
reaches a conversation only when the person adds its plugin there (``plugin_ref``,
:mod:`.connector_follow`).

Changing anything (enable, disable, a value, a secret) is applied at once (CN7):
the saved connection is rewritten, configuration-dependent fleets rebuild for new
turns, the connector's namespace is rebound in the fleets running now
(:mod:`.connector_rebind`), its server is checked again, the open conversations that
have its plugin follow (:mod:`.connector_follow`), and ``connector.changed`` is
published.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from clio_agent.connectors import entries as entries_mod
from clio_agent.connectors import listing, materialize, secrets, state
from clio_agent.connectors.entries import Entry, Need
from clio_agent.gact.events import Event
from clio_agent.runtime import trace

PROBE_SECONDS = 90.0


class ConnectorError(ValueError):
    """A typed refusal the routes and the native tool return."""

    def __init__(self, code: str, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def entry(connector_id: str) -> Entry:
    found = state.all_entries().get(connector_id)
    if found is None:
        raise ConnectorError("connector_not_found", f"No connector {connector_id!r}", 404)
    return found


# ------------------------------------------------------------------ needs


def _sign_in_storage(found: Entry, values: Mapping[str, str]) -> Any:
    from clio_agent.tools.mcp_oauth_storage import DurableFileTokenStorage  # noqa: PLC0415

    url = materialize.declaration(found, values).get("url") or ""
    return DurableFileTokenStorage(str(url), connection_id=found.id)


def has_value(found: Entry, choice: state.Choice, need: Need) -> bool:
    if need.kind == "secret":
        return secrets.has(found.id, need.key)
    if need.kind == "sign-in":
        return _sign_in_storage(found, choice.values).has_tokens()
    return bool(materialize.need_value(found, choice.values, need.key))


def missing(found: Entry, choice: state.Choice) -> list[Need]:
    """Required needs with no value, secret or account (optional ones never count)."""

    return [need for need in found.needs if need.required and not has_value(found, choice, need)]


def need_row(found: Entry, choice: state.Choice, need: Need) -> dict[str, Any]:
    value = None
    if need.kind in {"address", "value"}:
        value = choice.values.get(need.key)
    return {
        "key": need.key,
        "kind": need.kind,
        "label": need.label,
        "help": need.help,
        "required": need.required,
        "agent_settable": need.agent_settable,
        "default": need.default,
        "options": list(need.options),
        "value": value,
        "has_value": has_value(found, choice, need),
    }


# -------------------------------------------------------------- connection


def saved_connection(connector_id: str) -> dict[str, Any] | None:
    from clio_agent.gact.mcp_user_configuration import get_user_mcp_server  # noqa: PLC0415

    saved = get_user_mcp_server(connector_id)
    return saved if saved and saved.get(materialize.CONNECTOR_KEY) == connector_id else None


def save_connection(found: Entry, choice: state.Choice) -> dict[str, Any]:
    """Write the connector's saved connection (a sign-in's own auth block is kept)."""

    from clio_agent.gact.mcp_user_configuration import (  # noqa: PLC0415
        get_user_mcp_server,
        set_user_mcp_server,
    )

    declared = materialize.declaration(found, choice.values)
    previous = get_user_mcp_server(found.id) or {}
    if previous and previous.get(materialize.CONNECTOR_KEY) != found.id:
        raise ConnectorError(
            "connection_name_taken",
            f"A saved connection named {found.id!r} already exists. Remove it first.",
            409,
        )
    if declared.get("auth") and previous.get("auth") and previous.get("url") == declared["url"]:
        declared["auth"] = previous["auth"]
    return set_user_mcp_server(found.id, declared, label=found.name)


# ------------------------------------------------------------------ probes


def _probes(app: Any) -> dict[str, dict[str, Any]]:
    probes = getattr(app.state, "connector_probes", None)
    if probes is None:
        probes = app.state.connector_probes = {}
    return probes


def _probe_key(found: Entry, choice: state.Choice) -> str:
    declared = materialize.declaration(found, choice.values)
    stored = secrets.values(found.id)
    material = {
        "declaration": declared,
        "secrets": hashlib.sha256(json.dumps(stored, sort_keys=True).encode()).hexdigest(),
        "signed_in": any(n.kind == "sign-in" for n in found.needs)
        and _sign_in_storage(found, choice.values).has_tokens(),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


_REFUSED = re.compile(r"connect(ion)? ?(error|refused)|all connection attempts failed", re.I)


def unreachable_reason(found: Entry, declared: Mapping[str, Any], detail: str) -> str:
    """Why a server did not answer, in words (AQ4)."""

    where = str(declared.get("url") or declared.get("command") or found.name)
    if declared.get("url"):
        parts = urlsplit(where)
        where = f"{parts.scheme}://{parts.netloc}{parts.path}"
    if _REFUSED.search(detail):
        return f"Nothing answered at {where}: the connection was refused."
    if "timed out" in detail.lower() or "timeout" in detail.lower():
        return f"{where} did not answer in time."
    if "name or service not known" in detail.lower() or "nodename" in detail.lower():
        return f"The address {where} could not be found."
    return f"{found.name} could not be started or reached: {detail[:300]}"


async def probe(app: Any, found: Entry, choice: state.Choice, *, force: bool = False) -> dict:
    """List the connector's tools once (cached until its configuration changes)."""

    from clio_agent.gact.routes.mcp_configuration import (  # noqa: PLC0415
        _probe_user_mcp_server,
    )

    key = _probe_key(found, choice)
    cached = _probes(app).get(found.id)
    if cached and cached["key"] == key and not force:
        return cached
    declared = materialize.declaration(found, choice.values)
    try:
        tools, error = await asyncio.wait_for(
            _probe_user_mcp_server(declared, details=True), PROBE_SECONDS
        )
    except TimeoutError:
        tools, error = [], f"timed out after {int(PROBE_SECONDS)} s"
    result: dict[str, Any] = {"key": key, "tool_count": None, "status": "ready", "reason": None}
    if error is None:
        result["tool_count"] = len(listing.offered(found.id, list(tools)))
        listing.remember(found.id, [tool for tool in tools if isinstance(tool, dict)])
    elif error.startswith("mcp_auth_required"):
        result["status"] = "needs_sign_in"
    else:
        result["status"] = "unreachable"
        result["reason"] = unreachable_reason(found, declared, error)
        trace.event("TOOLS", "connector_unreachable connector=%s", found.id)
    _probes(app)[found.id] = result
    return result


# --------------------------------------------------------------------- rows


def _records(app: Any) -> list[Any]:
    service = getattr(app.state, "plugin_service", None)
    if service is None:
        return []
    try:
        return list(service.store.records())
    except Exception as exc:  # noqa: BLE001 - the row still shows the entry's own plugin
        trace.event("PLUGINS", "connector_row_records_unavailable (%s)", type(exc).__name__)
        return []


def _plugin(records: list[Any], found: Entry) -> str | None:
    following = sorted(r.id for r in records if r.follows == found.id)
    return following[0] if following else (found.plugin or None)


def row(app: Any, found: Entry, choice: state.Choice | None = None) -> dict[str, Any]:
    """The connector's wire row (``mcp-apps-contracts.md``, Connectors)."""

    choice = choice or state.choice(found.id)
    absent = missing(found, choice)
    cached = _probes(app).get(found.id) if choice.enabled else None
    if cached and cached["key"] != _probe_key(found, choice):
        cached = None  # checked under another configuration (a sign-in since, a new value)
    status, needs, reason = "ready", [need.key for need in absent], None
    if absent and not any(need.kind == "sign-in" for need in absent):
        status = "needs"
    elif cached and cached["status"] == "unreachable":
        status, reason = "unreachable", cached["reason"]
    elif absent:
        status = "needs"
    elif cached and cached["status"] == "needs_sign_in":
        sign_in = [need.key for need in found.needs if need.kind == "sign-in"]
        if sign_in:
            status, needs = "needs", sign_in
        else:
            status, reason = "unreachable", "The server asks for a sign-in."
    from clio_agent.connectors.follow import plugin_refs  # noqa: PLC0415

    records = _records(app)
    return {
        "id": found.id,
        "name": found.name,
        "description": found.description,
        "logo_url": f"/v1/connectors/{found.id}/logo" if found.logo else None,
        "source": found.source,
        "enabled": choice.enabled,
        "plugin": _plugin(records, found),
        # What a conversation's plugin picker adds for this connector (CN9 *Amended*).
        "plugin_ref": plugin_refs(found.id, records)[0],
        "tool_count": cached.get("tool_count") if cached and status == "ready" else None,
        "state": {"status": status, "needs": needs if status == "needs" else [], "reason": reason},
        "needs": [need_row(found, choice, need) for need in found.needs],
    }


async def rows(app: Any, *, refresh: bool = False) -> list[dict[str, Any]]:
    found_all = state.all_entries()
    choices = state.choices()
    stale = []
    for connector_id, found in found_all.items():
        choice = choices.get(connector_id, state.Choice())
        if not choice.enabled or any(n.kind != "sign-in" for n in missing(found, choice)):
            continue
        cached = _probes(app).get(connector_id)
        if refresh or not cached or cached["key"] != _probe_key(found, choice):
            stale.append(probe(app, found, choice, force=refresh))
    if stale:
        await asyncio.gather(*stale)
    return [row(app, found, choices.get(cid, state.Choice())) for cid, found in found_all.items()]


# ------------------------------------------------------------------ changes


def publish_changed(app: Any, connector_id: str, sessions: list[str]) -> None:
    bus = getattr(app.state, "bus", None)
    if bus is not None:
        bus.publish(
            Event(
                type="connector.changed",
                session_id="",
                payload={"connector_id": connector_id, "sessions": sessions},
            )
        )


async def apply(app: Any, found: Entry) -> dict[str, Any]:
    """Apply the connector's current choices everywhere (CN7) and return its row."""

    from clio_agent.gact.connector_follow import follow_in_open_sessions  # noqa: PLC0415
    from clio_agent.gact.connector_rebind import rebind_namespace  # noqa: PLC0415
    from clio_agent.gact.routes.mcp_configuration import (  # noqa: PLC0415
        _refresh_agent_tool_gateway,
    )

    choice = state.choice(found.id)
    if choice.enabled:
        saved = await asyncio.to_thread(save_connection, found, choice)
        await _refresh_agent_tool_gateway(app)
        agent = getattr(app.state, "agent", None)
        steps = []
        if agent is not None:
            steps.append(asyncio.to_thread(rebind_namespace, agent, found.id, saved))
        if not any(need.kind != "sign-in" for need in missing(found, choice)):
            steps.append(probe(app, found, choice, force=True))
        # The running servers' rebind and the probe are separate processes: run them
        # together, so a value change costs one server start, not two in a row.
        await asyncio.gather(*steps)
    sessions = await asyncio.to_thread(follow_in_open_sessions, app)
    publish_changed(app, found.id, sessions)
    return row(app, found, state.choice(found.id))


def check_values(
    found: Entry, values: Mapping[str, Any], *, by_agent: bool
) -> tuple[dict[str, str | None], dict[str, str | None]]:
    """Split ``values`` into (plain, secret) changes, refusing what is not allowed (CN6)."""

    plain: dict[str, str | None] = {}
    secret: dict[str, str | None] = {}
    for key, raw in values.items():
        need = found.need(str(key))
        if need is None:
            raise ConnectorError("connector_need_unknown", f"{found.name} has no need {key!r}")
        if need.kind == "sign-in":
            raise ConnectorError(
                "connector_sign_in_not_a_value",
                f"{need.label} is a sign-in: the person signs in from the setup card.",
            )
        if by_agent and need.kind == "secret":
            raise ConnectorError(
                "connector_secret_not_agent_settable",
                f"{need.label} is a secret: only the person can type it, in the setup card.",
            )
        if by_agent and not need.agent_settable:
            raise ConnectorError(
                "connector_need_not_agent_settable",
                f"{need.label} of {found.name} is set by the person, not the agent.",
            )
        value = None if raw is None else str(raw).strip()
        if value and ("\n" in value or "\r" in value or "${" in value or len(value) > 4096):
            raise ConnectorError("connector_value_invalid", f"{need.label}: one plain line")
        if value and need.options and value not in need.options:
            raise ConnectorError(
                "connector_value_invalid", f"{need.label}: one of {', '.join(need.options)}"
            )
        (secret if need.kind == "secret" else plain)[need.key] = value or None
    return plain, secret


async def set_values(
    app: Any, connector_id: str, values: Mapping[str, Any], *, by_agent: bool = False
) -> dict[str, Any]:
    found = entry(connector_id)
    plain, secret = check_values(found, values, by_agent=by_agent)
    if secret:
        await asyncio.to_thread(secrets.save, found.id, secret)
    if plain:
        await asyncio.to_thread(state.update, found.id, values=plain)
    return await apply(app, found)


async def enable(app: Any, connector_id: str) -> dict[str, Any]:
    """Install and ready the connector; it joins no conversation by itself (CN10)."""

    found = entry(connector_id)
    if saved_connection(found.id) is None:
        from clio_agent.gact.mcp_user_configuration import get_user_mcp_server  # noqa: PLC0415

        if get_user_mcp_server(found.id):
            raise ConnectorError(
                "connection_name_taken",
                f"A saved connection named {found.id!r} already exists. Remove it first.",
                409,
            )
    await asyncio.to_thread(state.update, found.id, enabled=True)
    return await apply(app, found)


async def disable(app: Any, connector_id: str) -> dict[str, Any]:
    """Turn the connector off. Its saved connection stays; a conversation that has its
    plugin drops it with the typed reason ``connector_off`` (``connectors.follow``)."""

    found = entry(connector_id)
    await asyncio.to_thread(state.update, found.id, enabled=False)
    _probes(app).pop(found.id, None)
    return await apply(app, found)


# ------------------------------------------------------------------- custom


def _custom_id(text: str) -> str:
    stem = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "connector"
    taken = set(state.all_entries())
    candidate, number = stem, 2
    while candidate in taken:
        candidate, number = f"{stem}-{number}", number + 1
    return candidate


async def _asks_for_sign_in(url: str) -> bool:
    """Whether the server's own metadata asks for a sign-in (401 + resource metadata)."""

    import httpx  # noqa: PLC0415

    headers = {"Accept": "application/json, text/event-stream"}
    ping = {"jsonrpc": "2.0", "id": "clio-connector", "method": "ping"}
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.post(url, headers=headers, json=ping)
            if response.status_code != 401:
                return False
            challenge = response.headers.get("www-authenticate", "")
            if "resource_metadata" in challenge:
                return True
            parts = urlsplit(url)
            well_known = f"{parts.scheme}://{parts.netloc}/.well-known/oauth-protected-resource"
            metadata = await client.get(well_known + parts.path)
            if metadata.status_code != 200:
                metadata = await client.get(well_known)
            return metadata.status_code == 200 and bool(
                metadata.json().get("authorization_servers")
            )
    except (httpx.HTTPError, ValueError):
        return False


async def create_custom(app: Any, body: Mapping[str, Any]) -> dict[str, Any]:
    """A connector for a server with no entry (AQ11): the address is what was typed."""

    url = str(body.get("url") or "").strip()
    command = str(body.get("command") or "").strip()
    name = str(body.get("name") or "").strip()
    if bool(url) == bool(command):
        raise ConnectorError("invalid_request", "Give a URL or a command")
    if url:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username:
            raise ConnectorError("invalid_request", "Use an HTTP or HTTPS address")
        connector_id = _custom_id(name or (parts.hostname or "server"))
        needs: list[dict[str, Any]] = [
            {"key": "address", "kind": "address", "label": "Address", "required": True}
        ]
        if await _asks_for_sign_in(url):
            needs.append({"key": "account", "kind": "sign-in", "label": "Account"})
        raw = {
            "id": connector_id,
            "name": name or parts.hostname or connector_id,
            "description": f"Added by address: {parts.netloc}",
            "server": {"url": "${need.address}"},
            "needs": needs,
        }
        values = {"address": url}
    else:
        args = body.get("args") or []
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ConnectorError("invalid_request", "args must be a list of strings")
        if "${" in command or any("${" in arg for arg in args):
            raise ConnectorError("invalid_request", "A command cannot contain ${...}")
        connector_id = _custom_id(name or command.rsplit("/", 1)[-1])
        raw = {
            "id": connector_id,
            "name": name or connector_id,
            "description": f"Added by command: {command}",
            "server": {"command": command, "args": list(args)},
            "needs": [],
        }
        values = {}
    errors = entries_mod.placement_errors(raw)
    if errors:
        raise ConnectorError("invalid_request", "; ".join(errors))
    await asyncio.to_thread(state.save_custom, raw)
    if values:
        await asyncio.to_thread(state.update, connector_id, values=values)
    return await enable(app, connector_id)
