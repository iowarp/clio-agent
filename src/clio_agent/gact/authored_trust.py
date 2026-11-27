"""Trust follows authorship, for every executable part (B7).

A plugin Clio wrote (:mod:`clio_agent.plugins.authored`) whose installed files are still
exactly what it wrote is asked about once, when it first reaches a conversation or a
child: added with ``offer_plugin``, given to a child with ``spawn_agent_task``'s
``plugins``, or when one of its flows is run. The person sees its review (what each flow,
tool, hook and script binding does and what its code reaches) and the executable files
changed since a version they trusted, and allows it once; only that plugin is trusted
(:meth:`~clio_agent.plugins.store.PluginStore.trust` with ``plugins``). In a conversation
that runs in bypass, a plugin Clio wrote in that same conversation is allowed on the
record, without a question.

A plugin with only text parts needs no trust step. Any plugin Clio did not write keeps
the ordinary trust step (Settings, or ``run_flow``'s ask for an installed flow) in every
mode: bypass never trusts someone else's code, and ``plugin_not_trusted`` says so.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from clio_agent.plugins import authored
from clio_agent.plugins.store import BUILTIN_MARKETPLACE, StoreError

#: The permission input marks an add-time trust ask (the UI's trust card reads it).
TRUST_KIND = "add_plugin"


def clio_session(store: Any, record: Any) -> str:
    """The conversation in which Clio wrote ``record``'s installed files, or empty."""

    if record.marketplace != authored.PRIVATE_MARKETPLACE:
        return ""
    found = authored.authorship(store.root, record.id, Path(record.root))
    return str(found.get("session_id") or "") if found else ""


def trust_row(store: Any, record: Any) -> dict[str, Any]:
    """What a trust ask names: the plugin, who wrote it, what changed, where to review it."""

    session = clio_session(store, record)
    by_clio = bool(session)
    market = store.marketplaces().get(record.marketplace) or {}
    trusted = (market.get("trusted_digests") or {}).get(record.id, "")
    changed = authored.changed_since(store.root, record.id, trusted) if by_clio else None
    return {
        "plugin": record.id,
        "marketplace": record.marketplace,
        "ref": f"{record.marketplace}/{record.id}",
        "version": record.version,
        "root": record.root,
        "snapshot": str(market.get("snapshot") or ""),
        "authored_by_clio": by_clio,
        "authored_in_session": session,
        "author": "Clio" if by_clio else record.marketplace,
        "review_url": f"/v1/plugins/{record.id}/review"
        if record.marketplace == authored.PRIVATE_MARKETPLACE
        else f"/v1/plugins/{record.marketplace}/review",
        # Executable files changed since the version the person trusted (None: never).
        "changed_since_trusted": changed,
    }


def needs_trust(store: Any, record: Any) -> bool:
    return (
        record.marketplace != BUILTIN_MARKETPLACE
        and record.has_executable_parts
        and not store.trusted(record)
    )


def _ask(
    app: Any,
    session_id: str,
    tool: str,
    row: dict[str, Any],
    call: dict[str, Any],
    summary: str,
) -> str:
    """Put the trust ask to the conversation's permission gate (blocking)."""

    session = app.state.sessions.get(session_id)
    bypass = getattr(session, "approval_mode", "") == "bypass"
    here = row["authored_by_clio"] and row["authored_in_session"] == session_id
    gate = app.state.make_permission_gate()
    shown = {k: v for k, v in row.items() if k not in ("snapshot", "root", "authored_in_session")}
    return gate(
        tool,
        {**shown, **call},
        {
            "kind": "native",
            "summary": summary,
            # Only a plugin Clio wrote in THIS conversation follows its approval mode.
            "always_ask": not (here and bypass),
        },
    )


def trust_plugin(store: Any, row: dict[str, Any], *, reason: str) -> None:
    store.trust(
        row["marketplace"],
        reason=reason,
        snapshot=row["snapshot"],
        plugins=frozenset({row["plugin"]}),
    )


async def permit_plugin(
    app: Any, session_id: str, record: Any, *, tool: str, call: dict[str, Any], where: str
) -> dict[str, Any]:
    """Ask once to trust ``record`` before it reaches ``where`` (this conversation, a child).

    Returns ``{"trusted": True, ...}`` when it may be used, or a typed refusal:
    ``plugin_not_trusted`` (someone else's plugin, or the person said no) or
    ``plugin_trust_failed`` (the store refused, such as content changed while asked).
    """

    from clio_agent.gact.agents.resolution import plugin_service  # noqa: PLC0415
    from clio_agent.gact.plugin_authoring import installed_changed  # noqa: PLC0415

    store = plugin_service(app).store
    if not needs_trust(store, record):
        return {"trusted": True, "plugin": record.id, "asked": False}
    row = await asyncio.to_thread(trust_row, store, record)
    if not row["authored_by_clio"]:
        return {
            "accepted": False,
            "reason": "plugin_not_trusted",
            "plugin": record.id,
            "marketplace": record.marketplace,
            "error": (
                f"{record.id} from {record.marketplace} has executable parts and is not "
                "trusted. Clio did not write it, so only the person can trust it, in "
                "Settings > Plugins; it is never trusted from a conversation."
            ),
        }
    changed = row["changed_since_trusted"]
    detail = f" ({len(changed)} changed file{'s' * (len(changed) != 1)})" if changed else ""
    decision = await asyncio.to_thread(
        _ask,
        app,
        session_id,
        tool,
        {**row, "trust_kind": TRUST_KIND, "where": where},
        call,
        f"Use {record.id}, written by Clio, in {where}{detail}: not trusted yet",
    )
    if str(decision) != "allow":
        return {
            "accepted": False,
            "reason": "plugin_not_trusted",
            "plugin": record.id,
            "error": f"using {record.id!r} was not allowed: {decision}",
        }
    session = app.state.sessions.get(session_id)
    reason = (
        "authored_bypass"
        if row["authored_in_session"] == session_id
        and getattr(session, "approval_mode", "") == "bypass"
        else "allowed_to_use"
    )
    try:
        await asyncio.to_thread(trust_plugin, store, row, reason=reason)
    except StoreError as exc:
        return {"accepted": False, "reason": "plugin_trust_failed", "error": str(exc)}
    installed_changed(app)
    return {"trusted": True, "plugin": record.id, "asked": True, "trust_reason": reason}


def find_record(app: Any, ref: str) -> Any | None:
    """The installed record ``ref`` (``market/id`` or a bare id) names, newest first."""

    from clio_agent.gact.agents.resolution import plugin_service  # noqa: PLC0415

    store = plugin_service(app).store
    market, _, plugin = ref.rpartition("/")
    plugin = (plugin or ref).split("@", 1)[0]
    pending = {(r.marketplace, r.id): r for r in store.pending()}
    rows = [
        pending.get((r.marketplace, r.id), r)
        for r in store.records()
        if r.id == plugin and (not market or r.marketplace == market)
    ]
    return rows[-1] if rows else None


async def permit_child_plugins(
    app: Any, session_id: str, change: dict[str, list[str]]
) -> dict[str, Any] | None:
    """Before a child starts with ``change["add"]``: every added plugin installed and
    trusted, a plugin Clio wrote asked about once. ``None`` when the child may start,
    else the typed refusal the spawn returns (nothing is started)."""

    for ref in change.get("add") or []:
        record = await asyncio.to_thread(find_record, app, ref)
        if record is None:
            return {
                "accepted": False,
                "reason": "plugin_not_installed",
                "plugin": ref,
                "error": f"no installed plugin is {ref!r}",
            }
        permit = await permit_plugin(
            app,
            session_id,
            record,
            tool="spawn_agent_task",
            call={"plugins": change},
            where="a child",
        )
        if not permit.get("trusted"):
            return permit
    return None
