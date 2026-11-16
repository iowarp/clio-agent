"""``make_plugin``, and running a flow whose plugin is not trusted yet.

``make_plugin`` (claimed by the ``builder`` plugin) writes a plugin into the person's
``private-local`` marketplace (:mod:`clio_agent.plugins.authored`), checks it untrusted
as ``clio-agent plugin check`` does and installs the marketplace. Writing the name of a
plugin Clio wrote makes its next version, with the diff recorded; a name the person
wrote is ``name_taken_by_person``, and a plugin of another marketplace is changed only
as the person's variation under a new name (``variation_of``).

``run_flow`` on a flow no trusted plugin of the conversation provides looks for an
installed, untrusted plugin that declares it, read from its files without importing
them, and asks (:mod:`clio_agent.gact.authored_trust`): allowed, that one plugin is
trusted, added to the conversation, and the flow starts.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from clio_agent.flows.outcomes import FlowNotTrusted
from clio_agent.gact import authored_trust, session_warmup
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
from clio_agent.plugins.store import BUILTIN_MARKETPLACE, StoreError
from clio_agent.tools.tool_settings import self_gated


def _untrusted_records(store: Any) -> list[Any]:
    """Installed plugins whose installed version may not run yet: never trusted, or an
    update that waits for trust (the waiting version, not the one still in use)."""

    pending = {(r.marketplace, r.id): r for r in store.pending()}
    rows = [pending.pop((r.marketplace, r.id), r) for r in store.installed_records()]
    return [r for r in [*rows, *pending.values()] if authored_trust.needs_trust(store, r)]


def untrusted_flows(store: Any) -> list[dict[str, Any]]:
    """Every flow an untrusted installed plugin declares, read from its files."""

    rows: list[dict[str, Any]] = []
    for record in sorted(_untrusted_records(store), key=lambda r: (r.marketplace, r.id)):
        root = Path(record.root)
        if not (root / "plugin.json").is_file():
            # Flows come from an Agent Plugins plugin; a Claude Code plugin declares none.
            continue
        review = authored.review(root)
        if not review["flows"]:
            continue
        row = authored_trust.trust_row(store, record)
        rows.extend(
            {
                **row,
                "flow": flow["id"],
                "description": flow["description"],
                "file": flow["file"],
            }
            for flow in review["flows"]
        )
    return rows


def untrusted_flow(store: Any, flow_id: str) -> dict[str, Any] | None:
    """The first untrusted installed plugin that declares ``flow_id``, as the ask names it."""

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
    """The review of ``name`` (a marketplace, or a plugin of the private marketplace) as
    installed, whether it is exactly what Clio wrote, and what changed since trusted."""

    store = plugin_service(app).store
    entry = store.marketplaces().get(name)
    if entry is not None:
        snapshot = Path(entry["snapshot"])
        return {**authored.review(snapshot), "authored_by_clio": False}
    record = authored_trust.find_record(app, f"{authored.PRIVATE_MARKETPLACE}/{name}")
    if record is None:
        raise LookupError(f"{name!r} is not installed")
    row = authored_trust.trust_row(store, record)
    return {
        **authored.review(Path(record.root)),
        "authored_by_clio": row["authored_by_clio"],
        "changed_since_trusted": row["changed_since_trusted"],
    }


def add_to_conversation(app: Any, session_id: str, refs: list[str]) -> list[str]:
    """Add the plugins ``refs`` to ``session_id``'s agent; returns the refs it added."""

    delta = session_delta(app, session_id)
    added = [ref for ref in refs if ref not in delta.add]
    pin_session_agent(
        app,
        session_id,
        delta=Delta((*delta.add, *added), tuple(r for r in delta.remove if r not in refs)),
    )
    prompt_basis.settle_basis(app, session_id)
    session_warmup.start_session_warmup(app, session_id, trigger="agent_changed")
    return added


def outside_flows(store: Any, resolved: set[str]) -> list[dict[str, Any]]:
    """Flows that trusted installed plugins declare and this agent does not resolve: running
    one adds its plugin (``ref``) to the conversation, with no question, as it is trusted."""

    rows: list[dict[str, Any]] = []
    for record in store.records():
        if not record.flows or not store.trusted(record):
            continue
        builtin = record.marketplace == BUILTIN_MARKETPLACE
        ref = record.id if builtin else f"{record.marketplace}/{record.id}"
        rows.extend(
            {"flow": flow.id, "description": flow.description, "plugin": record.id, "ref": ref}
            for flow in record.flows
            if flow.id not in resolved
        )
    return rows


async def permit_untrusted_flow(
    app: Any, session_id: str, flow_id: str, call: dict[str, Any]
) -> dict[str, Any] | None:
    """Ask to run ``flow_id`` from an untrusted plugin; trust and add it when allowed.

    Returns ``None`` when no untrusted plugin declares it, ``{"trusted": True, ...}``
    once it can start, or a typed refusal (``flow_not_trusted``, ``flow_not_added``).
    """

    store = plugin_service(app).store
    found = await asyncio.to_thread(untrusted_flow, store, flow_id)
    if found is None:
        return None
    who = "Clio" if found["authored_by_clio"] else f"the {found['marketplace']} marketplace"
    decision = await asyncio.to_thread(
        authored_trust._ask,  # noqa: SLF001 - the one gate wording for trust asks
        app,
        session_id,
        "run_flow",
        found,
        {"flow": flow_id, **call},
        f"Run the {flow_id} flow, written by {who}, which is not trusted yet",
    )
    if str(decision) != "allow":
        return {
            "accepted": False,
            "reason": "flow_not_trusted",
            "error": f"running {flow_id!r} was not allowed: {decision}",
            "plugin": found["plugin"],
        }
    session = app.state.sessions.get(session_id)
    here = found["authored_by_clio"] and found["authored_in_session"] == session_id
    bypass = getattr(session, "approval_mode", "") == "bypass"
    reason = "authored_bypass" if here and bypass else "allowed_to_run"
    try:
        await asyncio.to_thread(authored_trust.trust_plugin, store, found, reason=reason)
        installed_changed(app)
        added = await asyncio.to_thread(add_to_conversation, app, session_id, [found["ref"]])
    except (StoreError, SessionAgentUnavailable) as exc:
        return {"accepted": False, "reason": "flow_not_added", "error": str(exc)}
    return {"trusted": True, "plugin": found["plugin"], "added": added}


# ------------------------------------------------------------------ make_plugin


def _taken_elsewhere(store: Any, name: str) -> str:
    """The marketplace that already provides a plugin (or is itself) named ``name``."""

    if name in store.marketplaces() and name != authored.PRIVATE_MARKETPLACE:
        return name
    for record in store.records():
        if record.id == name and record.marketplace != authored.PRIVATE_MARKETPLACE:
            return record.marketplace
    return ""


def _write(
    store: Any, name: str, files: dict[str, str], session_id: str, variation_of: str
) -> tuple[Path, dict[str, Any]]:
    with store.lock:
        folder, version = authored.write(
            store.root,
            name,
            files,
            session_id,
            variation_of=variation_of,
            taken_elsewhere=_taken_elsewhere(store, name),
        )
    return folder, version


def _installed(store: Any, name: str) -> Any | None:
    rows = {(r.marketplace, r.id): r for r in store.installed_records()}
    rows.update({(r.marketplace, r.id): r for r in store.pending()})
    return rows.get((authored.PRIVATE_MARKETPLACE, name))


def _version_view(version: dict[str, Any]) -> dict[str, Any]:
    diff = version.get("diff") or {}
    return {
        "version": version["version"],
        "variation_of": version.get("variation_of", ""),
        "added": diff.get("added", []),
        "changed": diff.get("changed", []),
        "removed": diff.get("removed", []),
        "executable_changed": diff.get("executable_changed", []),
        "diff": diff.get("unified", ""),
    }


async def make_plugin(name: str, files: dict[str, str], variation_of: str = "") -> dict[str, Any]:
    from clio_agent.gact.flow_tools import _runtime  # noqa: PLC0415

    app, caller = _runtime()
    service = plugin_service(app)
    store = service.store
    try:
        folder, version = await asyncio.to_thread(
            _write, store, name, files, caller.session_id, variation_of.strip()
        )
    except authored.AuthoredError as exc:
        return {"installed": False, "reason": exc.reason, "error": str(exc), **exc.details}
    # Not trusted yet: checked without importing the Python Clio just wrote.
    report = await asyncio.to_thread(check, folder, service=service, trusted=False)
    if not report.ok:
        return {
            "installed": False,
            "reason": "check_failed",
            "check": report.to_dict(),
            "message": "Fix the files and call make_plugin again with the same name.",
            **_version_view(version),
        }
    try:
        await asyncio.to_thread(
            store.install,
            str(authored.private_root(store.root)),
            name=authored.PRIVATE_MARKETPLACE,
        )
    except StoreError as exc:
        return {"installed": False, "reason": exc.code, "error": str(exc)}
    installed_changed(app)
    record = await asyncio.to_thread(_installed, store, name)
    if record is not None:
        await asyncio.to_thread(authored.note_installed, store.root, name, record.executable_digest)
    review = await asyncio.to_thread(authored.review, folder)
    executable = bool(record is not None and record.has_executable_parts)
    trusted = bool(record is not None and store.trusted(record))
    return {
        "installed": True,
        "check": "passed",
        "plugin": name,
        "ref": f"{authored.PRIVATE_MARKETPLACE}/{name}",
        "marketplace": authored.PRIVATE_MARKETPLACE,
        **_version_view(version),
        "flows": [row["id"] for row in review["flows"]],
        "tools": [row["name"] for row in review["tools"]],
        "hooks": [row["id"] for row in review["hooks"]],
        "executable": executable,
        "trusted": trusted or not executable,
        "warnings": [
            row
            for row in report.to_dict().get("diagnostics", [])
            if row.get("severity") == "warning"
        ],
        "message": (
            "Installed in the person's private marketplace. Add it to this conversation with "
            "offer_plugin, or to a child with spawn_agent_task's plugins; "
            + (
                "its executable parts are asked about once when it is first used."
                if executable and not trusted
                else "it needs no trust step."
            )
        ),
    }


def build_make_plugin_tool() -> Any:
    from clio_agent.gact.builder.presentation import make_plugin_presentation  # noqa: PLC0415

    return self_gated(
        native_tool(
            make_plugin,
            name="make_plugin",
            domain="agents",
            title="Make plugin",
            desc=(
                "Write a plugin from its files (plugin.json and its parts, as the capture "
                "skill's references describe) into the person's private marketplace, check "
                "it and install it. Calling it again with the name of a plugin you wrote "
                "makes its next version (the diff is recorded). A name the person wrote is "
                "name_taken_by_person; a plugin of another marketplace is changed only as a "
                "variation under a new name with variation_of set to its ref. A failed check "
                "returns its diagnostics: fix the files and call again."
            ),
            args={
                "name": {"type": "string", "description": "The plugin's name, as in plugin.json."},
                "files": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "Each file's path inside the plugin, mapped to its text.",
                },
                "variation_of": {
                    "type": "string",
                    "default": "",
                    "description": "marketplace/plugin this is a variation of, if any.",
                },
            },
            presentation=make_plugin_presentation,
        )
    )
