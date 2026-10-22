"""Running a flow whose plugin is not trusted asks the person, where they are.

``make_plugin`` writes a plugin Clio authored (:mod:`clio_agent.plugins.authored`),
checks it as ``clio-agent plugin check`` does and installs it, untrusted.

``run_flow`` on a flow no trusted plugin of the conversation provides looks for an
installed, untrusted plugin that declares it, read from its installed files without
importing them. Running it is then a permission request through the conversation's
permission gate: the flow, who wrote it, its file, and where to read it. Allowed, the
plugin is trusted, added to the conversation, and the flow starts.

Who decides follows who wrote it. A flow Clio wrote, whose installed files are still
exactly what Clio wrote, takes the conversation's approval mode: under bypass it runs
at once, on the record. Any other plugin is asked about in every mode: bypass does not
trust someone else's code.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from clio_agent.flows.outcomes import FlowNotTrusted
from clio_agent.gact import session_warmup
from clio_agent.gact.agents import prompt_basis
from clio_agent.gact.agents.resolution import (
    SessionAgentUnavailable,
    pin_session_agent,
    plugin_service,
    session_delta,
)
from clio_agent.gact.agents.tool_instrumentation import native_tool
from clio_agent.plugins import authored
from clio_agent.plugins.check import check
from clio_agent.plugins.resolve import Delta
from clio_agent.plugins.store import StoreError


def _authored_by_clio(store: Any, market: str, snapshot: Path) -> bool:
    """Whether the installed files are exactly the ones Clio wrote as ``market``."""

    record = authored.authorship(store.root, market)
    return record is not None and record["digest"] == authored.folder_digest(snapshot)


def untrusted_flows(store: Any) -> list[dict[str, Any]]:
    """Every flow an installed plugin declares that is not trusted in its installed version
    (never trusted, or an update waits for trust), read from its files."""

    waiting = {row.marketplace for row in store.pending()}
    rows: list[dict[str, Any]] = []
    for market, entry in sorted(store.marketplaces().items()):
        if entry.get("trusted") and market not in waiting:
            continue
        snapshot = Path(entry["snapshot"])
        review = authored.review(snapshot)
        by_clio = _authored_by_clio(store, market, snapshot) if review["flows"] else False
        rows.extend(
            {
                "flow": flow["id"],
                "description": flow["description"],
                "plugin": market,
                "version": review["version"],
                "authored_by_clio": by_clio,
                "author": "Clio" if by_clio else market,
                "file": flow["file"],
                "review_url": f"/v1/plugins/{market}/review",
            }
            for flow in review["flows"]
        )
    return rows


def untrusted_flow(store: Any, flow_id: str) -> dict[str, Any] | None:
    """The first untrusted installed plugin that declares ``flow_id``: who wrote it and
    where to read it, as the run permission presents it."""

    found = next((row for row in untrusted_flows(store) if row["flow"] == flow_id), None)
    if found is None:
        return None
    return {k: v for k, v in found.items() if k not in {"flow", "description"}}


def refuse_untrusted_flow(app: Any, flow_id: str) -> None:
    """Raise :class:`FlowNotTrusted` when ``flow_id`` is declared by an installed plugin
    that is not trusted: a running flow cannot ask (its agent is fixed), so it is asked
    about when a flow or workflow is started."""

    found = untrusted_flow(plugin_service(app).store, flow_id)
    if found is not None:
        raise FlowNotTrusted(
            f"flow {flow_id!r} is from the {found['plugin']} plugin, not trusted yet; "
            "run_flow, or starting the workflow again, asks the person",
            target=flow_id,
            plugin=found["plugin"],
        )


def installed_changed(app: Any) -> None:
    """Installed plugins ship A2UI catalogs: the next lookup re-reads the set."""

    registry = getattr(app.state, "a2ui_catalogs", None)
    if registry is not None:
        registry.invalidate()


def installed_review(app: Any, name: str) -> dict[str, Any]:
    """The review of ``name``'s installed files, and whether they are exactly what Clio wrote."""

    store = plugin_service(app).store
    snapshot = Path(store.marketplaces()[name]["snapshot"])
    return {
        **authored.review(snapshot),
        "authored_by_clio": _authored_by_clio(store, name, snapshot),
    }


def trust_and_add(app: Any, market: str, session_id: str, *, reason: str) -> list[str]:
    """Trust ``market`` and add its plugins to ``session_id``'s agent; returns their refs."""

    store = plugin_service(app).store
    store.trust(market, reason=reason)
    installed_changed(app)
    refs = sorted(
        f"{row.marketplace}/{row.id}" for row in store.records() if row.marketplace == market
    )
    delta = session_delta(app, session_id)
    added = [ref for ref in refs if ref not in delta.add]
    pin_session_agent(
        app,
        session_id,
        delta=Delta((*delta.add, *added), tuple(r for r in delta.remove if r not in refs)),
    )
    prompt_basis.settle_basis(app, session_id)
    session_warmup.start_session_warmup(app, session_id, trigger="agent_changed")
    return refs


def _ask_to_run(
    app: Any, session_id: str, flow_id: str, found: dict[str, Any], call: dict[str, Any]
) -> str:
    """Put running the untrusted flow to the conversation's permission gate (blocking)."""

    session = app.state.sessions.get(session_id)
    bypass = getattr(session, "approval_mode", "") == "bypass"
    who = "Clio" if found["authored_by_clio"] else f"the {found['plugin']} plugin"
    gate = app.state.make_permission_gate()
    return gate(
        "run_flow",
        {"flow": flow_id, **found, **call},
        {
            "kind": "native",
            "summary": f"Run the {flow_id} flow, written by {who}, which is not trusted yet",
            # Only a flow Clio wrote may follow the approval mode (bypass runs it).
            "always_ask": not (found["authored_by_clio"] and bypass),
        },
    )


async def permit_untrusted_flow(
    app: Any, session_id: str, flow_id: str, call: dict[str, Any]
) -> dict[str, Any] | None:
    """Ask to run ``flow_id`` from an untrusted plugin; trust and add it when allowed.

    Returns ``None`` when no untrusted plugin declares it, ``{"trusted": True, ...}``
    once it can start, or a typed refusal.
    """

    store = plugin_service(app).store
    found = await asyncio.to_thread(untrusted_flow, store, flow_id)
    if found is None:
        return None
    decision = await asyncio.to_thread(_ask_to_run, app, session_id, flow_id, found, call)
    if str(decision) != "allow":
        return {
            "accepted": False,
            "reason": "flow_not_trusted",
            "error": f"running {flow_id!r} was not allowed: {decision}",
            "plugin": found["plugin"],
        }
    session = app.state.sessions.get(session_id)
    reason = (
        "authored_bypass"
        if found["authored_by_clio"] and getattr(session, "approval_mode", "") == "bypass"
        else "allowed_to_run"
    )
    try:
        added = await asyncio.to_thread(
            trust_and_add, app, found["plugin"], session_id, reason=reason
        )
    except (StoreError, SessionAgentUnavailable) as exc:
        return {"accepted": False, "reason": "flow_not_added", "error": str(exc)}
    return {"trusted": True, "plugin": found["plugin"], "added": added}


async def make_plugin(name: str, files: dict[str, str]) -> dict[str, Any]:
    from clio_agent.gact.flow_tools import _runtime  # noqa: PLC0415

    app, caller = _runtime()
    store = plugin_service(app).store
    try:
        folder = await asyncio.to_thread(authored.write, store.root, name, files, caller.session_id)
    except authored.AuthoredError as exc:
        return {"installed": False, "reason": exc.reason, "error": str(exc)}
    report = await asyncio.to_thread(check, folder, service=plugin_service(app))
    if not report.ok:
        return {"installed": False, "reason": "check_failed", "check": report.to_dict()}
    try:
        await asyncio.to_thread(store.install, str(folder), name=name)
    except StoreError as exc:
        return {"installed": False, "reason": exc.code, "error": str(exc)}
    installed_changed(app)
    flows = [row["id"] for row in (await asyncio.to_thread(authored.review, folder))["flows"]]
    return {
        "installed": True,
        "check": "passed",
        "flows": flows,
        "message": (
            "Start it with run_flow and these flow ids. The first run asks the person to "
            "trust it, unless the conversation runs in bypass."
        ),
    }


def build_make_plugin_tool() -> Any:
    return native_tool(
        make_plugin,
        name="make_plugin",
        domain="tasks",
        title="Make plugin",
        desc=(
            "Write a plugin from its files (plugin.json and its code, as the author-a-flow "
            "skill describes), check it and install it. Returns its flow ids; run_flow "
            "starts them. The first run asks the person to trust it (not under bypass). "
            "Calling it again with changed files makes a new version, asked about again."
        ),
        args={
            "name": {"type": "string", "description": "The plugin's name, as in plugin.json."},
            "files": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "Each file's path inside the plugin, mapped to its text.",
            },
        },
        presentation="tasks",
    )
