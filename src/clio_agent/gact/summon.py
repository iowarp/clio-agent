"""The person names what handles a message: ``summon`` parts (owner module).

The composer sends ``{"type": "summon", "target_kind": ..., "target": ...}`` beside
the text. The server acts only on these parts, never on an ``@`` typed in prose, and
only for a name the session's agent has:

* ``subagent``: one of the agent's defined subagents starts on the message (the text
  with the summon tokens removed), through the same spawn the ``spawn_agent_task``
  tool makes (:func:`~clio_agent.gact.spawn_tools.spawn_child`), with the
  conversation as its parent and the person as its starter. Several start together.
  The agent's own turn is told each child's task handle.
* ``server``: one of the agent's MCP servers. When the turn's agent loop starts, that
  server's deferred tools are loaded through ``search_tools``' own ``select:`` path
  and recorded as one CLIO addition ahead of the person's message, with a line saying
  the person pointed the message at them. Nothing is hidden, the tool list does not
  change, and the model still chooses which tool to call, or none.

A name the agent lacks is refused with ``summon_unknown_target``; a deny rule on
``spawn_agent_task`` refuses a subagent summons with ``summon_denied``. Either way
no turn starts.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from fastapi import HTTPException

from clio_agent.gact.types import ErrorEnvelope, ErrorInfo
from clio_agent.runtime import trace

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.parts import Part

SUMMON = "summon"
SUBAGENT = "subagent"
SERVER = "server"
#: Message metadata naming what the person summoned (the transcript reads it).
SUMMONS_KEY = "summons"
#: The injection source of a server summons, recorded ahead of the person's message.
SERVER_SUMMON_SOURCE = "summon_server"
#: Session metadata on a child the person started.
STARTED_BY_PERSON = {"kind": "person"}


def _refuse(status: int, error: str, message: str, **details: Any) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail=ErrorEnvelope(
            error=ErrorInfo(error=error, message=message, details=details, recoverable=True)
        ).model_dump(exclude_none=True),
    )


@dataclass(frozen=True)
class Child:
    role: str
    invocation: str
    handle: str


@dataclass
class Summons:
    """What one message summons, decided before it is accepted."""

    task: str = ""
    children: list[Child] = field(default_factory=list)
    servers: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.children or self.servers)

    def model_lines(self) -> str:
        """One line per child for the agent's own turn."""

        return "\n".join(
            f"The person started {child.role} as task {child.handle} on this message."
            for child in self.children
        )

    def metadata(self) -> dict[str, Any]:
        return {
            "subagents": [{"role": c.role, "handle": c.handle} for c in self.children],
            "servers": list(self.servers),
        }


def summon_parts(parts: Iterable["Part"]) -> list["Part"]:
    return [part for part in parts if part.type == SUMMON]


def _strip_tokens(text: str, names: Iterable[str]) -> str:
    for name in sorted(set(names), key=len, reverse=True):
        text = re.sub(rf"(?<![\w@.]){re.escape('@' + name)}(?![\w.-])", "", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _handle(session_id: str, invocation: str) -> str:
    # The handle ``Conversations.spawn`` gives this invocation (its idempotent identity).
    return "task_" + hashlib.sha256((session_id + "\0" + invocation).encode()).hexdigest()


def plan_summons(
    app: "FastAPI",
    sid: str,
    parts: Sequence["Part"],
    text: str,
    *,
    key: str = "",
    task: str = "",
) -> Summons:
    """Check ``parts``' summonses against the session's agent; raise a typed refusal.

    ``key`` makes the children's identities idempotent (a replayed message starts the
    same tasks); ``task`` replaces the message text as the children's task (a command
    file's ``agent:`` gives its rendered template).
    """

    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        SessionAgentUnavailable,
        delegable_subagents,
        session_resolution,
    )

    wanted = summon_parts(parts)
    if not wanted:
        return Summons()
    try:
        resolution = session_resolution(app, sid)
    except SessionAgentUnavailable as exc:
        raise _refuse(
            409, "summon_unknown_target", f"this conversation has no agent: {exc}"
        ) from exc
    subagents = sorted(row["id"] for row in delegable_subagents(resolution))
    servers = sorted(getattr(resolution, "mcp_servers", {}) or {})
    known = {SUBAGENT: set(subagents), SERVER: set(servers)}
    unknown = [
        {"target_kind": p.target_kind, "target": p.target}
        for p in wanted
        if p.target not in known.get(p.target_kind, set())
    ]
    if unknown:
        raise _refuse(
            422,
            "summon_unknown_target",
            "this agent has no "
            + ", ".join(f"{row['target_kind']} {row['target']!r}" for row in unknown),
            unknown=unknown,
            subagents=subagents,
            servers=servers,
        )
    roles = list(dict.fromkeys(p.target for p in wanted if p.target_kind == SUBAGENT))
    names = [p.target for p in wanted]
    work = (task or _strip_tokens(text, names)).strip()
    if roles and not work:
        raise _refuse(
            422,
            "summon_empty_task",
            "say what the summoned agent should do",
            subagents=roles,
        )
    if roles:
        _refuse_denied_spawn(app, sid)
    key = key or uuid4().hex
    return Summons(
        task=work,
        children=[
            Child(role, f"summon:{key}:{index}", _handle(sid, f"summon:{key}:{index}"))
            for index, role in enumerate(roles)
        ],
        servers=list(dict.fromkeys(p.target for p in wanted if p.target_kind == SERVER)),
    )


async def plan_for_request(
    app: "FastAPI", sid: str, req: Any, task: str = ""
) -> tuple[Any, Summons]:
    """``req`` (keyed, when it summons) and what it summons; raises a typed refusal."""

    from clio_agent.gact.message_submission import _idempotency_key  # noqa: PLC0415
    from clio_agent.gact.off_loop import run_off_loop  # noqa: PLC0415

    if summon_parts(req.parts) and not _idempotency_key(req):
        # The summoned children and the message share one identity, so a replay is one.
        req = req.model_copy(update={"idempotency_key": "summon-" + uuid4().hex})
    summons = await run_off_loop(
        plan_summons, app, sid, req.parts, req.extract_text(), key=_idempotency_key(req), task=task
    )
    return req, summons


async def start_for_request(
    app: "FastAPI",
    deps: Any,
    sid: str,
    req: Any,
    summons: Summons,
    prepared: Any,
    model_text: str | None,
    metadata: dict[str, Any] | None,
) -> tuple[str | None, dict[str, Any]]:
    """Start the summoned children before the message is accepted; its model text and metadata.

    The children start under the conversation's assignment before the agent's turn, so
    its turn finds them pending and cannot settle first. A child that cannot start is
    listed on the message with its reason.
    """

    from clio_agent.gact.message_submission import (  # noqa: PLC0415
        _message_identity,
        _validate_provider_and_payload,
    )
    from clio_agent.gact.off_loop import run_off_loop  # noqa: PLC0415

    lines = summons.model_lines()
    if lines:
        base = prepared.model_text if model_text is None else model_text
        model_text = f"{base}\n\n{lines}" if base else lines
    record = summons.metadata()
    if summons.children:
        await run_off_loop(_validate_provider_and_payload, app, deps, sid, req, prepared=prepared)
        identity = _message_identity(sid, req)
        await run_off_loop(open_assignment, app, sid, req.extract_text(), identity)
        record["failed"] = failures(await start_children(app, sid, identity, summons))
    return model_text, {**(metadata or {}), SUMMONS_KEY: record}


def _refuse_denied_spawn(app: "FastAPI", sid: str) -> None:
    """A deny rule on ``spawn_agent_task`` holds for the person's summons too."""

    from clio_agent.gact.permission_gate import _policy_detail_for_tool  # noqa: PLC0415

    session = app.state.sessions.get(sid)
    action, message = _policy_detail_for_tool(
        app, session_id=sid, session=session, tool_name="spawn_agent_task", args={}
    )
    if action == "deny":
        raise _refuse(
            403,
            "summon_denied",
            message or "a permission rule denies starting subagents in this conversation",
            tool_name="spawn_agent_task",
        )


async def start_children(
    app: "FastAPI", sid: str, message_id: str, summons: Summons
) -> list[dict[str, Any]]:
    """Start every summoned child together, as the person; return each outcome."""

    from clio_agent.gact.spawn_tools import spawn_child  # noqa: PLC0415

    conversations = app.state.conversations
    caller = await app.state.task_history.io.run(conversations.caller, sid)
    started_by = {**STARTED_BY_PERSON, "message_id": message_id}

    async def one(child: Child) -> dict[str, Any]:
        try:
            result = await spawn_child(
                app,
                caller,
                child.role,
                summons.task,
                child.invocation,
                metadata={"started_by": started_by},
            )
        except Exception as error:  # noqa: BLE001 - forwards: summons.failed on the message
            trace.event("SUMMON", "summoned %s did not start: %s", child.role, error)
            return {"role": child.role, "handle": child.handle, "error": str(error)}
        if result.get("handle") and result["handle"] != child.handle:
            trace.event("SUMMON", "summoned %s started as %s", child.role, result["handle"])
        return {"role": child.role, **result}

    return list(await asyncio.gather(*(one(child) for child in summons.children)))


def open_assignment(app: "FastAPI", sid: str, text: str, identity: str) -> None:
    """Open the conversation's assignment for this message, as accepting it would."""

    owner = app.state.conversations
    with owner.history.serialized():
        current = owner.history.latest_assignment(sid)
        if current is None or current.terminal_at is not None:
            owner._new_assignment(
                sid, text, owner.caller(sid).human_id, predecessor=current, invocation_id=identity
            )


def failures(outcomes: list[dict[str, Any]]) -> list[dict[str, str]]:
    """The summoned children that did not start, each with its reason (shown on the message)."""

    return [
        {"role": row["role"], "error": str(row.get("error") or row.get("reason") or "")}
        for row in outcomes
        if row.get("error") or row.get("accepted") is False
    ]


def server_summon_injections(summons_metadata: Any) -> list[tuple[str, str]]:
    """The turn's marker for each summoned server, expanded when the agent loop starts."""

    servers = (
        (summons_metadata or {}).get("servers") if isinstance(summons_metadata, dict) else None
    )
    return [(SERVER_SUMMON_SOURCE, json.dumps({"server": name})) for name in servers or ()]


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
    "SERVER_SUMMON_SOURCE",
    "SUMMONS_KEY",
    "Summons",
    "expand_server_summons",
    "plan_summons",
    "failures",
    "open_assignment",
    "plan_for_request",
    "start_for_request",
    "server_summon_injections",
    "start_children",
    "summon_parts",
]
