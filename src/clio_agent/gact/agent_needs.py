"""Ask on agent load: "These are needed and not enabled. Enable them?" (LZ-D2 to LZ-D6).

The installer prepares no tool servers any more (LZ-D1). So when a conversation
starts with an agent, or switches to one, whose plugins need something that is not
ready, the conversation shows one compact card, a ``connector_setup`` pending
interaction with intent :data:`INTENT`, before or at its first turn. Not ready is a
fact read from the conversation's resolution, never a guess:

* ``off``: a connector a plugin of the agent follows or whose server it declares is
  not enabled, so the resolution left that plugin or server out with a typed reason
  (``connector_off``: ``connectors.follow.drop_off``, ``connectors.server_gate``);
* ``needs``: such a connector has a required value, secret or sign-in missing;
* ``download``: its clio-kit server's tools are not downloaded yet
  (:mod:`clio_agent.connectors.kit_readiness`).

**Enable all** (``submit``) runs, for each item (all, or the ones ``metadata.items``
names), in the background: the clio-kit download (``tools.mcp_prepare.provision``,
the step the server's first start would run), then the existing enable and apply path
(``connectors_service.enable``), which re-pins the open conversations so the next
step has the tools. The card stays with each item's progress in words until the
person closes it (``dismiss``); a failure keeps its typed reason and can be retried.

**Not now** (``decline``) leaves the agent working without them (the resolution
already dropped them, typed) and is remembered for the agent in the conversation's
workspace (``Workspace.metadata``, :data:`NOT_NOW_KEY`), so new conversations with
that agent do not ask again unless something new is needed. The agent can still
offer them (``offer_plugin``, ``offer_connector``).

The card row lives beside the other setup cards, in memory; it is computed again
from the resolution when it is missing (after a restart). No store is added.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from clio_agent.runtime import trace

INTENT = "agent_needs"
NOT_NOW_KEY = "agent_needs_not_now"
#: An item's progress after Enable all, in order.
WAITING, PREPARING, ENABLING, READY, FAILED = "waiting", "preparing", "enabling", "ready", "failed"
#: The card's phases: the question, the work, the work done.
ASK, WORKING, DONE = "ask", "working", "done"
_OFF_REASONS = frozenset({"connector_off"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------- facts


def _plugin_brings_more(resolution: Any, plugin_id: str) -> bool:
    """Whether ``plugin_id`` brings skills, tools or prompt text besides its server."""

    if any(row.get("plugin") == plugin_id for row in resolution.skills or ()):
        return True
    if any(row.get("plugin") == plugin_id for row in resolution.tools or ()):
        return True
    return any(
        fragment.get("plugin") == plugin_id
        for section in (resolution.prompt or {}).get("sections") or ()
        for fragment in section.get("fragments") or ()
    )


def _wanted(resolution: Any) -> dict[str, set[str]]:
    """Connector id -> the plugins of this resolution that follow or declare it."""

    from clio_agent.connectors import state  # noqa: PLC0415
    from clio_agent.connectors.server_gate import bound_connector, left_out_rows  # noqa: PLC0415

    entries = state.all_entries()
    wanted: dict[str, set[str]] = {}
    for row in resolution.removed or ():
        if row.get("reason") in _OFF_REASONS and row.get("because"):
            wanted.setdefault(str(row["because"]), set()).add(str(row.get("id") or ""))
    for row in left_out_rows(resolution):
        if row.get("code") in _OFF_REASONS:
            wanted.setdefault(str(row["because"]), set()).add(str(row.get("plugin") or ""))
    for row in (resolution.mcp_servers or {}).values():
        cid = bound_connector(row.get("spec") or {}, entries)
        if cid:
            wanted.setdefault(cid, set()).add(str(row.get("plugin") or ""))
    return {cid: {p for p in plugins if p} for cid, plugins in wanted.items()}


def items_for(resolution: Any) -> list[dict[str, Any]]:
    """What this resolution needs that is not ready, one item per connector."""

    from clio_agent.connectors import kit_readiness, state  # noqa: PLC0415
    from clio_agent.gact import connectors_service as service  # noqa: PLC0415

    entries = state.all_entries()
    choices = state.choices()
    out: list[dict[str, Any]] = []
    for cid, plugins in sorted(_wanted(resolution).items()):
        found = entries.get(cid)
        if found is None:
            continue
        choice = choices.get(cid, state.Choice())
        absent = service.missing(found, choice)
        prepared = kit_readiness.prepared(found.kit) if found.kit else None
        reasons = [
            reason
            for reason, holds in (
                ("off", not choice.enabled),
                ("needs", bool(absent)),
                ("download", prepared is False),
            )
            if holds
        ]
        if not reasons:
            continue
        bundling = sorted(p for p in plugins if _plugin_brings_more(resolution, p))
        out.append(
            {
                "id": cid,
                "connector_id": cid,
                "name": found.name,
                # A plugin that brings skills is shown as the plugin (PL-D1).
                "kind": "plugin" if bundling else "connector",
                "plugins": sorted(plugins),
                "logo_url": f"/v1/connectors/{cid}/logo" if found.logo else None,
                "group": found.group or None,
                "reasons": reasons,
                "enabled": choice.enabled,
                # True: enabling downloads its tools; None: clio-kit's, not known.
                "download": prepared is False if found.kit else False,
                "download_unknown": bool(found.kit) and prepared is None,
                "asks_sign_in": any(need.kind == "sign-in" for need in found.needs),
                "missing": [
                    service.need_row(found, choice, need)
                    for need in absent
                    if need.kind != "sign-in"
                ],
                "settings": [
                    service.need_row(found, choice, need)
                    for need in found.needs
                    if need.kind != "sign-in" and need not in absent
                ],
            }
        )
    return out


# --------------------------------------------------------------------- memory


def _session(app: Any, session_id: str) -> Any:
    sessions = getattr(getattr(app, "state", None), "sessions", None)
    return sessions.get(session_id) if sessions is not None and session_id else None


def _workspace(app: Any, session_id: str) -> Any:
    session = _session(app, session_id)
    store = getattr(getattr(app, "state", None), "workspaces", None)
    wid = str(getattr(session, "workspace_id", "") or "")
    return store.get(wid) if store is not None and wid else None


def not_now(app: Any, session_id: str, agent_ref: str) -> set[str]:
    """The items the person answered Not now for this agent in this workspace."""

    workspace = _workspace(app, session_id)
    remembered = dict((getattr(workspace, "metadata", None) or {}).get(NOT_NOW_KEY) or {})
    return {str(cid) for cid in remembered.get(agent_ref) or ()}


def remember_not_now(app: Any, session_id: str, agent_ref: str, ids: Iterable[str]) -> None:
    """Remember Not now for ``agent_ref`` in the session's workspace (LZ-D5)."""

    workspace = _workspace(app, session_id)
    if workspace is None:
        trace.event("TOOLS", "agent_needs_not_now_unremembered session=%s", session_id)
        return
    remembered = dict((workspace.metadata or {}).get(NOT_NOW_KEY) or {})
    remembered[agent_ref] = sorted(set(remembered.get(agent_ref) or ()) | set(ids))
    app.state.workspaces.update(workspace.id, metadata_patch={NOT_NOW_KEY: remembered})


# ----------------------------------------------------------------------- card


def _rows(app: Any) -> dict[str, dict[str, Any]]:
    from clio_agent.gact.connector_setup import _rows as rows  # noqa: PLC0415

    return rows(app)


def _publish(app: Any, row: Mapping[str, Any], event_type: str = "connector.setup.requested"):
    from clio_agent.gact.connector_setup import _publish as publish  # noqa: PLC0415

    publish(app, event_type, row)


def _card_of(app: Any, session_id: str, agent_ref: str) -> dict[str, Any] | None:
    for row in _rows(app).values():
        if (
            row.get("intent") == INTENT
            and row["session_id"] == session_id
            and row.get("agent_ref") == agent_ref
        ):
            return row
    return None


def _askable(app: Any, session_id: str) -> bool:
    from clio_agent.gact.agents.resolution import SUBAGENT_KEY  # noqa: PLC0415

    session = _session(app, session_id)
    if session is None or getattr(session, "parent_session_id", ""):
        return False
    return not (getattr(session, "metadata", None) or {}).get(SUBAGENT_KEY)


def ensure_card(app: Any, session_id: str, resolution: Any = None) -> dict[str, Any] | None:
    """Show the card for this conversation's agent when something is not ready.

    Idempotent: one card per conversation and agent. Nothing is shown for a child or a
    defined subagent's conversation, or when every item was answered Not now.
    """

    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        SessionAgentUnavailable,
        session_resolution,
    )
    from clio_agent.gact.connector_offer import session_agent  # noqa: PLC0415

    if not _askable(app, session_id):
        return None
    try:
        resolution = resolution or session_resolution(app, session_id)
    except (SessionAgentUnavailable, RuntimeError) as exc:
        trace.event("TOOLS", "agent_needs_unresolved session=%s (%s)", session_id, exc)
        return None
    _refs, ref, name = session_agent(app, session_id)
    ref = ref or str(resolution.agent.get("ref") or "")
    existing = _card_of(app, session_id, ref)
    if existing is not None and (existing["status"] != "pending" or existing["phase"] != ASK):
        return existing
    declined = not_now(app, session_id, ref)
    items = [item for item in items_for(resolution) if item["id"] not in declined]
    if existing is not None:
        # Still asking: the items follow what was enabled elsewhere meanwhile.
        if [item["id"] for item in items] != [item["id"] for item in existing["items"]]:
            existing.update(items=items, updated_at=_now())
            if not items:
                existing.update(status="submitted", settled="ready_elsewhere")
                _publish(app, existing, "connector.setup.resolved")
            else:
                existing["connector_name"] = ", ".join(item["name"] for item in items)
                _publish(app, existing)
        return existing
    if not items:
        return None
    row = {
        "id": uuid.uuid4().hex[:12],
        "session_id": session_id,
        "intent": INTENT,
        "connector_id": "",
        "connector_name": ", ".join(item["name"] for item in items),
        "tool_name": "",
        "missing": [],
        "status": "pending",
        "phase": ASK,
        "agent_ref": ref,
        "agent_name": str(resolution.agent.get("name") or name or ref),
        "items": items,
        "created_at": _now(),
    }
    _rows(app)[row["id"]] = row
    trace.event(
        "TOOLS",
        "agent_needs_card session=%s agent=%s items=%s",
        session_id,
        ref,
        ",".join(f"{item['id']}:{'+'.join(item['reasons'])}" for item in items),
    )
    _publish(app, row)
    return row


def ensure_card_safely(app: Any, session_id: str, resolution: Any = None) -> None:
    """:func:`ensure_card` where a failure must not fail the caller (typed in the trace)."""

    try:
        ensure_card(app, session_id, resolution)
    except Exception as exc:  # noqa: BLE001 - the card is an aid; pinning goes on
        trace.event("TOOLS", "agent_needs_card_failed session=%s (%s)", session_id, exc)


def interaction(app: Any, row: Mapping[str, Any]) -> Any:
    """The card as a pending interaction (``connector_setup``, intent :data:`INTENT`)."""

    from clio_agent.gact.connector_setup import _INTERACTION_STATUS, PREFIX  # noqa: PLC0415
    from clio_agent.gact.interaction_types import (  # noqa: PLC0415
        PendingInteraction,
        PendingInteractionSource,
    )
    from clio_agent.gact.permission_delivery import attended_session_id  # noqa: PLC0415

    pending = row["status"] == "pending"
    phase = row.get("phase") or ASK
    actions = (
        ["submit", "decline"]
        if phase == ASK
        else ["dismiss", "submit"]
        if phase == DONE and any(item.get("progress") == FAILED for item in row["items"])
        else ["dismiss"]
    )
    names = [item["name"] for item in row["items"]]
    return PendingInteraction(
        id=PREFIX + row["id"],
        kind="connector_setup",
        owner_session_id=row["session_id"],
        attended_session_id=attended_session_id(app, row["session_id"]),
        status=_INTERACTION_STATUS.get(row["status"], "cancelled"),
        title=f"{row.get('agent_name') or 'This agent'} uses {', '.join(names)}",
        prompt=f"Enable {', '.join(names)} for {row.get('agent_name') or 'this agent'}?",
        requires_human_response=pending,
        source=PendingInteractionSource(protocol="native"),
        created_at=row["created_at"],
        revision=str(row.get("updated_at") or row["created_at"]),
        payload={
            "interaction_id": PREFIX + row["id"],
            "intent": INTENT,
            "connector_id": "",
            "connector_name": row["connector_name"],
            "missing": [],
            "agent_ref": row.get("agent_ref") or "",
            "agent_name": row.get("agent_name") or "",
            "phase": phase,
            "items": [dict(item) for item in row["items"]],
        },
        actions=actions if pending else [],
    )


# --------------------------------------------------------------------- answer


async def answer(
    app: Any,
    row: dict[str, Any],
    action: str,
    values: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> None:
    """Enable all (``submit``), Not now (``decline``) or close the card (``dismiss``)."""

    from clio_agent.gact.connector_setup import SetupResponseError  # noqa: PLC0415

    phase = row.get("phase") or ASK
    if action == "decline" and phase == ASK:
        ids = [item["id"] for item in row["items"]]
        await asyncio.to_thread(
            remember_not_now, app, row["session_id"], str(row.get("agent_ref") or ""), ids
        )
        row.update(status="declined", updated_at=_now())
        trace.event("TOOLS", "agent_needs_not_now session=%s items=%s", row["session_id"], ids)
        return
    if action == "dismiss" and phase != ASK:
        row.update(status="submitted", updated_at=_now())
        return
    if action != "submit" or phase == WORKING:
        raise SetupResponseError(
            422, "validation_error", f"{action or 'no action'} is not an answer to this card now"
        )
    chosen = {str(cid) for cid in (metadata or {}).get("items") or ()}
    selected = [
        item
        for item in row["items"]
        if (not chosen or item["id"] in chosen) and (phase == ASK or item.get("progress") == FAILED)
    ]
    if not selected:
        raise SetupResponseError(422, "validation_error", "choose at least one to enable")
    picked = {item["id"] for item in selected}
    for item in row["items"]:
        if item["id"] in picked:
            item.update(progress=WAITING, reason=None, detail=None)
        elif phase == ASK:
            item.update(progress="skipped")
    row.update(phase=WORKING, updated_at=_now())
    task = asyncio.create_task(_enable_all(app, row, selected, dict(values or {})))
    tasks = getattr(app.state, "agent_needs_tasks", None)
    if tasks is None:
        tasks = app.state.agent_needs_tasks = set()
    tasks.add(task)
    task.add_done_callback(tasks.discard)


def _progress(app: Any, row: dict[str, Any], item: dict[str, Any], progress: str, **fields: Any):
    item.update(progress=progress, **fields)
    row["updated_at"] = _now()
    _publish(app, row)


async def _enable_all(
    app: Any, row: dict[str, Any], selected: list[dict[str, Any]], values: Mapping[str, Any]
) -> None:
    await asyncio.gather(
        *(_enable_one(app, row, item, values.get(item["id"])) for item in selected)
    )
    row.update(phase=DONE, updated_at=_now())
    trace.event(
        "TOOLS",
        "agent_needs_enabled session=%s items=%s",
        row["session_id"],
        ",".join(f"{item['id']}:{item.get('progress')}" for item in selected),
    )
    _publish(app, row)


async def _enable_one(app: Any, row: dict[str, Any], item: dict[str, Any], values: Any) -> None:
    """Download (a clio-kit server), save values, then enable and apply (CN7)."""

    from clio_agent.gact import connectors_service as service  # noqa: PLC0415

    cid = item["id"]
    try:
        found = service.entry(cid)
        if isinstance(values, Mapping) and values:
            plain, secret = service.check_values(found, values, by_agent=False)
            from clio_agent.connectors import secrets, state  # noqa: PLC0415

            if secret:
                await asyncio.to_thread(secrets.save, cid, secret)
            if plain:
                await asyncio.to_thread(state.update, cid, values=plain)
        if found.kit:
            _progress(app, row, item, PREPARING)
            await asyncio.to_thread(_prepare_kit, cid, found.kit)
        _progress(app, row, item, ENABLING)
        result = await service.enable(app, cid)
    except service.ConnectorError as exc:
        _progress(app, row, item, FAILED, reason=exc.code, detail=str(exc)[:300])
        return
    except Exception as exc:  # noqa: BLE001 - every failure is the item's typed reason
        reason = str(getattr(exc, "reason", "") or "agent_needs_enable_failed")
        _progress(app, row, item, FAILED, reason=reason, detail=_words(reason, exc))
        return
    state_row = dict(result.get("state") or {})
    if state_row.get("status") == "unreachable":
        detail = str(state_row.get("reason") or "")[:300]
        _progress(app, row, item, FAILED, reason="connector_unreachable", detail=detail)
        return
    _progress(app, row, item, READY, tool_count=result.get("tool_count"), download=False)


def _words(reason: str, exc: BaseException) -> str:
    from clio_agent.tools.mcp_prepare import PREPARE_REASONS  # noqa: PLC0415

    return (PREPARE_REASONS.get(reason) or str(exc))[:300]


def _prepare_kit(cid: str, server: str) -> str:
    """Build the clio-kit server's environment, as its first start would (typed)."""

    from clio_agent.tools import mcp_prepare  # noqa: PLC0415
    from clio_agent.tools.mcp_config import MCPServerSpec  # noqa: PLC0415

    spec = MCPServerSpec(
        name=cid, transport="stdio", command="clio-kit", args=("mcp-server", server)
    )
    # The fleet's own preparation records are left alone: this card shows the progress.
    return mcp_prepare.provision(cid, spec)
