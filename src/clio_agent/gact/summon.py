"""The person names what handles a message: ``summon`` parts (owner module).

The composer sends ``{"type": "summon", "target_kind": ..., "target": ...}`` beside
the text. The server acts only on these parts, never on an ``@`` typed in prose, and
only for a name the session's agent has. Nothing starts before the turn: each summons
is a CLIO addition ahead of the person's message, recorded when the turn's agent loop
starts, and the agent keeps every tool.

* ``subagent``, ``workflow``, ``flow``: the addition (a core injection) says the person
  asked for that target and which call starts it. The agent writes the brief and sets
  the call's parameters from the whole conversation. When a call of this turn names a
  summoned target, the person is recorded as its starter; a summoned target no call
  started is recorded on the turn's answer as not started.
* ``server``: that server's deferred tools are loaded through ``search_tools``' own
  ``select:`` path, with a line saying the person pointed the message at them.

A name the agent lacks is refused with ``summon_unknown_target``; a deny rule on the
call a summons asks for refuses it with ``summon_denied``. Either way no turn starts.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from clio_agent.gact.types import ErrorEnvelope, ErrorInfo
from clio_agent.runtime import trace

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.parts import Part

SUMMON = "summon"
SUBAGENT = "subagent"
SERVER = "server"
WORKFLOW = "workflow"
FLOW = "flow"
#: The kinds a call of the turn starts, and the call (tool) each asks for.
DELEGATED = {SUBAGENT: "spawn_agent_task", WORKFLOW: "run_workflow", FLOW: "run_flow"}
#: Message metadata naming what the person summoned (the transcript reads it).
SUMMONS_KEY = "summons"
#: Assistant metadata: which summoned targets the turn's calls started.
OUTCOME_KEY = "summons_outcome"
#: The injection source of a server summons, recorded ahead of the person's message.
SERVER_SUMMON_SOURCE = "summon_server"
#: Session metadata on a child the person started.
STARTED_BY_PERSON = {"kind": "person"}
#: Flows that are not offered on their own (the agent loop, and the workflow runner).
_INTERNAL_FLOWS = frozenset({"react", "workflow"})


def _refuse(status: int, error: str, message: str, **details: Any) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail=ErrorEnvelope(
            error=ErrorInfo(error=error, message=message, details=details, recoverable=True)
        ).model_dump(exclude_none=True),
    )


@dataclass
class Summons:
    """What one message summons, checked before it is accepted."""

    targets: dict[str, list[str]] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return any(self.targets.values())

    def metadata(self) -> dict[str, Any]:
        return {
            "subagents": [{"role": name} for name in self.targets.get(SUBAGENT, [])],
            "servers": list(self.targets.get(SERVER, [])),
            "workflows": list(self.targets.get(WORKFLOW, [])),
            "flows": list(self.targets.get(FLOW, [])),
        }


def summon_parts(parts: Iterable["Part"]) -> list["Part"]:
    return [part for part in parts if part.type == SUMMON]


def _known(app: "FastAPI", sid: str) -> tuple[Any, dict[str, list[dict[str, Any]]]]:
    """The session's resolution and, per kind, the rows a person can summon."""

    from clio_agent.gact.agents.resolution import delegable_subagents, session_resolution
    from clio_agent.plugins.workflows import resolution_workflows  # noqa: PLC0415

    resolution = session_resolution(app, sid)
    servers = dict(getattr(resolution, "mcp_servers", {}) or {})
    return resolution, {
        SUBAGENT: [
            {"id": row["id"], "plugin": row["plugin"], "description": row["description"]}
            for row in delegable_subagents(resolution)
        ],
        SERVER: [{"name": name, **row} for name, row in sorted(servers.items())],
        WORKFLOW: [
            {
                "id": record.id,
                "plugin": plugin,
                "description": record.description,
                "inputs": [dict(row) for row in record.inputs],
            }
            for plugin, record in resolution_workflows(resolution)
        ],
        FLOW: [
            {
                "id": str(row["id"]),
                "plugin": str(row.get("plugin") or ""),
                "description": str(row.get("description") or ""),
            }
            for row in getattr(resolution, "flows", None) or []
            if row.get("id") not in _INTERNAL_FLOWS
        ],
    }


def summon_targets(app: "FastAPI", sid: str) -> dict[str, Any]:
    """What the person can ``@``-summon here: the agent's subagents, servers, workflows, flows.

    A server's tool count is known once the agent's connection has listed it (``None``
    before that); ``deferred`` says its tools load on demand.
    """

    from clio_agent.gact.agents.resolution import SessionAgentUnavailable  # noqa: PLC0415
    from clio_agent.gact.code_mode.stubs import owning_server  # noqa: PLC0415
    from clio_agent.gact.mcp_prompt_commands import session_executor  # noqa: PLC0415

    try:
        resolution, known = _known(app, sid)
    except SessionAgentUnavailable:
        return {"subagents": [], "servers": [], "workflows": [], "flows": []}
    servers = dict(resolution.mcp_servers or {})
    executor = session_executor(app, sid)
    prepared = getattr(executor, "is_namespace_prepared", None)
    names = getattr(executor, "get_tool_names", None)
    counts: dict[str, int] = {}
    if callable(names):
        for tool in names():
            owner = owning_server(tool, servers)
            if owner:
                counts[owner] = counts.get(owner, 0) + 1
    return {
        "subagents": known[SUBAGENT],
        "servers": [
            {
                "name": row["name"],
                "plugin": str(row.get("plugin") or ""),
                "description": str(row.get("description") or ""),
                "deferred": bool(row.get("defer")),
                "tools": counts.get(row["name"])
                if counts.get(row["name"]) or (callable(prepared) and prepared(row["name"]))
                else None,
            }
            for row in known[SERVER]
        ],
        "workflows": known[WORKFLOW],
        "flows": known[FLOW],
    }


def _target_names(rows: list[dict[str, Any]]) -> list[str]:
    """One kind's summonable names, sorted: a row's ``id``, a server's ``name``."""

    names: list[str] = []
    for row in rows:
        name = row.get("id") or row.get("name")
        if not isinstance(name, str):
            raise TypeError(f"a summon target has no name: {row!r}")
        names.append(name)
    return sorted(names)


def plan_summons(app: "FastAPI", sid: str, parts: Sequence["Part"]) -> Summons:
    """Check ``parts``' summonses against the session's agent; raise a typed refusal."""

    from clio_agent.gact.agents.resolution import SessionAgentUnavailable  # noqa: PLC0415

    wanted = summon_parts(parts)
    if not wanted:
        return Summons()
    try:
        _resolution, known = _known(app, sid)
    except SessionAgentUnavailable as exc:
        raise _refuse(
            409, "summon_unknown_target", f"this conversation has no agent: {exc}"
        ) from exc
    names = {kind: _target_names(rows) for kind, rows in known.items()}
    unknown = [
        {"target_kind": p.target_kind, "target": p.target}
        for p in wanted
        if p.target not in names.get(p.target_kind, [])
    ]
    if unknown:
        raise _refuse(
            422,
            "summon_unknown_target",
            "this agent has no "
            + ", ".join(f"{row['target_kind']} {row['target']!r}" for row in unknown),
            unknown=unknown,
            subagents=names[SUBAGENT],
            servers=names[SERVER],
            workflows=names[WORKFLOW],
            flows=names[FLOW],
        )
    targets = {
        kind: list(dict.fromkeys(p.target for p in wanted if p.target_kind == kind))
        for kind in (SUBAGENT, SERVER, WORKFLOW, FLOW)
    }
    for kind, tool in DELEGATED.items():
        if targets[kind]:
            _refuse_denied(app, sid, tool)
    return Summons(targets=targets)


async def plan_for_request(app: "FastAPI", sid: str, req: Any) -> Summons:
    """What ``req`` summons; raises a typed refusal."""

    from clio_agent.gact.off_loop import run_off_loop  # noqa: PLC0415

    if not summon_parts(req.parts):
        return Summons()
    return await run_off_loop(plan_summons, app, sid, req.parts)


def _refuse_denied(app: "FastAPI", sid: str, tool: str) -> None:
    """A deny rule on the call a summons asks for holds for the person's summons too."""

    from clio_agent.gact.permission_gate import _policy_detail_for_tool  # noqa: PLC0415

    session = app.state.sessions.get(sid)
    action, message = _policy_detail_for_tool(
        app, session_id=sid, session=session, tool_name=tool, args={}
    )
    if action == "deny":
        raise _refuse(
            403,
            "summon_denied",
            message or f"a permission rule denies {tool} in this conversation",
            tool_name=tool,
        )


def summon_injections(app: "FastAPI", sid: str, summons_metadata: Any) -> list[tuple[str, str]]:
    """The turn's additions for what the person summoned, ahead of their message.

    A delegated target's text is its core injection (``summon_subagent``,
    ``summon_workflow``, ``summon_flow``); a server's is a marker the agent loop
    expands (:func:`expand_server_summons`).
    """

    from clio_agent.gact.agents.resolution import SessionAgentUnavailable  # noqa: PLC0415
    from clio_agent.injections.resolution import render  # noqa: PLC0415

    record = summons_metadata if isinstance(summons_metadata, dict) else {}
    if not any(record.get(key) for key in ("subagents", "workflows", "flows", "servers")):
        return []
    try:
        _resolution, known = _known(app, sid)
    except (SessionAgentUnavailable, OSError, ValueError) as exc:
        # The agent cannot resolve, or its pinned resolution cannot be read; the
        # turn's own resolution reports it.
        trace.event("SUMMON", "summon_injections_unavailable reason=%s", exc)
        known = {}
    rows = {kind: {row.get("id"): row for row in known.get(kind, [])} for kind in DELEGATED}
    out: list[tuple[str, str]] = []
    wanted = [
        *((SUBAGENT, row.get("role")) for row in record.get("subagents") or ()),
        *((WORKFLOW, name) for name in record.get("workflows") or ()),
        *((FLOW, name) for name in record.get("flows") or ()),
    ]
    for kind, name in wanted:
        row = rows.get(kind, {}).get(name) or {}
        inputs = ", ".join(
            item["name"] + ("" if item.get("required") else " (optional)")
            for item in row.get("inputs") or ()
        )
        text = render(
            f"summon_{kind}",
            {"name": name, "description": row.get("description") or "", "inputs": inputs},
            app=app,
            session_id=sid,
        )
        if text:
            out.append((f"summon_{kind}", text))
    out.extend(
        (SERVER_SUMMON_SOURCE, json.dumps({"server": name})) for name in record.get("servers") or ()
    )
    return out


_CLAIMS_LOCK = threading.Lock()


def open_turn(app: "FastAPI", turn_id: str, message_id: str, summons_metadata: Any) -> None:
    """Remember what this turn's person message summoned, for :func:`claim`."""

    record = summons_metadata if isinstance(summons_metadata, dict) else {}
    rows = [
        *({"kind": SUBAGENT, "target": row.get("role")} for row in record.get("subagents") or ()),
        *({"kind": WORKFLOW, "target": name} for name in record.get("workflows") or ()),
        *({"kind": FLOW, "target": name} for name in record.get("flows") or ()),
    ]
    if rows and turn_id:
        with _CLAIMS_LOCK:
            _turns(app)[turn_id] = {"message_id": message_id, "rows": rows}


def _turns(app: Any) -> dict[str, dict[str, Any]]:
    state = app.state
    if not hasattr(state, "summon_turns"):
        state.summon_turns = {}
    return state.summon_turns


def claim(kind: str, target: str) -> dict[str, Any] | None:
    """Claim a target the person summoned when this turn's call starts it.

    Each summoned target is claimed once: by the first call naming it. Returns
    ``{"started_by": ..., "row": ...}``, the child's ``started_by`` session metadata
    and the record :func:`started` completes; ``None`` for any other call, and outside
    a turn whose message summoned something.
    """

    from clio_agent.gact import context as _ctx  # noqa: PLC0415

    app, turn_id = _ctx.active_app(), _ctx.active_turn_id()
    if app is None or not turn_id:
        return None
    with _CLAIMS_LOCK:
        record = _turns(app).get(turn_id)
        if record is None:
            return None
        for row in record.get("rows", ()):
            if row["kind"] == kind and row["target"] == target and "claimed" not in row:
                row["claimed"] = True
                person = {**STARTED_BY_PERSON, "message_id": record["message_id"]}
                return {"started_by": person, "row": row}
    return None


def child_metadata(claimed: dict[str, Any] | None) -> dict[str, Any]:
    """The session metadata of a child a claimed call starts (empty when unclaimed)."""

    return {"started_by": claimed["started_by"]} if claimed else {}


def started(claimed: dict[str, Any] | None, result: dict[str, Any]) -> None:
    """Record a claimed target's outcome: the handle it started as, or why it did not."""

    if claimed is None:
        return
    row = claimed["row"]
    with _CLAIMS_LOCK:
        row["handle"] = str(result.get("handle") or "")
        if not row["handle"]:
            row["error"] = str(result.get("error") or result.get("reason") or "not accepted")


def stamp_summon_outcome(app: "FastAPI", turn_id: str, metadata: dict[str, Any]) -> None:
    """Write the turn's summons outcome onto its answer's metadata (when it had one)."""

    outcome = settle_turn(app, turn_id)
    if outcome:
        metadata[OUTCOME_KEY] = outcome


def settle_turn(app: "FastAPI", turn_id: str) -> dict[str, Any] | None:
    """The turn's outcome for what its person message summoned (assistant metadata)."""

    with _CLAIMS_LOCK:
        record = _turns(app).pop(turn_id, None)
    if record is None:
        return None
    outcome: dict[str, Any] = {"started": [], "not_started": []}
    for row in record["rows"]:
        target = {"kind": row["kind"], "target": row["target"]}
        if row.get("handle"):
            outcome["started"].append({**target, "handle": row["handle"]})
        else:
            outcome["not_started"].append({**target, "reason": row.get("error") or "not_called"})
    for row in outcome["not_started"]:
        trace.event("SUMMON", "summon_not_started kind=%s target=%s reason=%s", *row.values())
    return outcome


def expand_server_summons(entries: list[tuple[str, str]], program: Any) -> list[tuple[str, str]]:
    """Load each summoned server's deferred tools through ``search_tools``' path.

    The schemas enter the conversation in the form ``search_tools`` returns them and
    the names are recorded as loaded, so they are listed from the next prompt rebuild.
    A server whose tools are already listed loads nothing: the note alone remains.
    """

    if not any(source == SERVER_SUMMON_SOURCE for source, _ in entries):
        return entries
    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.gact.search_tools import _record_loaded, search_result  # noqa: PLC0415

    catalog = getattr(program, "_clio_deferred_catalog", None)
    expanded: list[tuple[str, str]] = []
    for source, block in entries:
        if source != SERVER_SUMMON_SOURCE:
            expanded.append((source, block))
            continue
        server = str(json.loads(block).get("server") or "")
        note = f"The person pointed this message at the {server} tools."
        names = [entry.name for entry in catalog.by_owner().get(server, [])] if catalog else []
        from clio_agent.gact.mcp_app_tools import loading_note  # noqa: PLC0415

        # A view of this server still loading: its own tools are not in the catalog yet.
        if loading := loading_note(_ctx.active_app(), _ctx.active_session_id(), [server]):
            note = f"{note}\n\n{loading}"
        if not names:
            expanded.append((source, note))
            continue
        text, structured = search_result(catalog, "select:" + ",".join(names))
        loaded = [row["name"] for row in structured["tools"]]
        _record_loaded(_ctx.active_app(), _ctx.active_session_id(), loaded)
        trace.event("SUMMON", "loaded %d %s tools for the person", len(loaded), server)
        expanded.append((source, f"{note}\n\n{text}"))
    return expanded


__all__ = [
    "DELEGATED",
    "OUTCOME_KEY",
    "SERVER_SUMMON_SOURCE",
    "SUMMONS_KEY",
    "Summons",
    "child_metadata",
    "claim",
    "expand_server_summons",
    "open_turn",
    "plan_for_request",
    "plan_summons",
    "settle_turn",
    "stamp_summon_outcome",
    "started",
    "summon_injections",
    "summon_parts",
    "summon_targets",
]
