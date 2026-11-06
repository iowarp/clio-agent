"""Slash-command routes: list a session's command table and run one of its rows.

* ``GET /v1/commands`` returns :func:`command_table` for a session (or, without
  one, the workspace's built-ins and command files).
* ``POST /v1/sessions/{sid}/commands/{cmd}`` looks ``cmd`` up in the same table.
  A built-in runs its handler and leaves a result message; a command file becomes
  the person's message through :func:`submit_user_command`. An ambiguous bare name
  is refused with ``command_ambiguous`` and the qualified names that run each row.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, NoReturn, Optional

from fastapi import FastAPI, HTTPException, Request

from clio_agent.gact.events import Event
from clio_agent.gact.off_loop import emit_semantic_event_async, run_off_loop
from clio_agent.gact.routes._body import json_body
from clio_agent.gact.runtime.command_handlers import HANDLERS
from clio_agent.gact.runtime.command_table import (
    command_table,
    render_command,
    required_arguments,
    split_words,
)
from clio_agent.gact.runtime.globals import _active_semantic_turn_id
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo
from clio_agent.gact.user_command_turn import submit_user_command

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


def _refuse(http_status: int, error: str, message: str, **details: Any) -> NoReturn:
    raise HTTPException(
        status_code=http_status,
        detail=ErrorEnvelope(
            error=ErrorInfo(error=error, message=message, details=details, recoverable=True)
        ).model_dump(exclude_none=True),
    )


def _user_input(body: dict[str, Any]) -> str:
    text = body.get("input") or body.get("text") or body.get("prompt") or ""
    if not text and isinstance(body.get("args"), str):
        text = body["args"]
    return str(text).strip()


def register_command_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register ``GET /v1/commands`` and the command dispatch route on ``app``."""

    @app.get("/v1/commands")
    def list_commands(
        session_id: Optional[str] = None, workspace_id: Optional[str] = None
    ) -> dict[str, Any]:
        """The command table, in FastAPI's worker pool (command files are read from disk)."""

        table = command_table(app, session_id or "", workspace_id or "")
        return {"commands": table.wire(), "ambiguous": table.ambiguous}

    @app.get("/v1/sessions/{sid}/summon-targets")
    def list_summon_targets(sid: str) -> dict[str, Any]:
        """The subagents and MCP servers a person can ``@``-summon in this conversation."""

        from clio_agent.gact.summon import summon_targets  # noqa: PLC0415

        if app.state.sessions.get(sid) is None:
            _refuse(404, "not_found", f"session not found: {sid}")
        return summon_targets(app, sid)

    @app.post("/v1/sessions/{sid}/commands/{cmd}")
    async def dispatch_command(sid: str, cmd: str, request: Request) -> dict[str, Any]:
        """Run one row of the session's command table."""

        sess = app.state.sessions.get(sid)
        if sess is None:
            _refuse(404, "not_found", f"session not found: {sid}")
        cmd_id = cmd if cmd.startswith("/") else f"/{cmd}"
        # Off the loop: the table reads command files and lists MCP prompts over the
        # agent's connections, whose executor answers on this loop.
        table = await run_off_loop(command_table, app, sid)
        if cmd_id in table.ambiguous:
            _refuse(
                409,
                "command_ambiguous",
                f"{cmd_id} names more than one command; use a qualified name",
                command=cmd_id,
                qualified=table.ambiguous[cmd_id],
            )
        row = table.lookup(cmd_id)
        if row is None:
            _refuse(404, "command_unknown", f"unknown command: {cmd_id}", command=cmd_id)
        if not row["enabled"]:
            _refuse(
                409,
                row["disabled_reason"] or "command_unavailable",
                f"{cmd_id} cannot run: {row['disabled_detail'] or row['disabled_reason']}",
                command=cmd_id,
                status=row["status"],
            )
        body = await json_body(request, route="POST /v1/sessions/{sid}/commands/{cmd}")
        if row["kind"] == "builtin":
            return await _run_builtin(sess, row, body)
        user_input = _user_input(body)
        if row["kind"] == "skill":
            from clio_agent.gact.skill_commands import load_skill_for_person  # noqa: PLC0415

            loaded = await run_off_loop(load_skill_for_person, app, sid, row["skill_id"])
            return await submit_user_command(
                app,
                deps,
                sid,
                cmd_id=row["name"],
                user_input=user_input,
                rendered=f"{user_input}\n\n{loaded}" if user_input else loaded,
                command_meta=row,
            )
        if row["kind"] == "mcp_prompt":
            return await _run_prompt(sid, row, user_input)
        required = required_arguments(row)
        missing = required[len(split_words(user_input)) :]
        if missing:
            _refuse(
                422,
                "invalid_arguments",
                f"{cmd_id} is missing required arguments: {', '.join(missing)}",
                command=cmd_id,
                missing=missing,
                argument_hint=row["argument_hint"],
            )
        return await submit_user_command(
            app,
            deps,
            sid,
            cmd_id=row["name"],
            user_input=user_input,
            rendered=render_command(row["prompt_template"], user_input),
            command_meta=row,
        )

    async def _run_prompt(sid: str, row: dict[str, Any], user_input: str) -> dict[str, Any]:
        from clio_agent.gact import mcp_prompt_commands as prompts  # noqa: PLC0415

        values, missing = prompts.map_arguments(row["arguments"], split_words(user_input))
        if missing:
            _refuse(
                422,
                "invalid_arguments",
                f"{row['name']} needs {', '.join(missing)}",
                command=row["name"],
                missing=missing,
                argument_hint=row["argument_hint"],
            )
        result = await run_off_loop(prompts.fetch_prompt, app, sid, row, values)
        text, images = prompts.prompt_message(result)
        return await submit_user_command(
            app,
            deps,
            sid,
            cmd_id=row["name"],
            user_input=user_input,
            rendered=text or row["name"],
            command_meta=row,
            parts=images,
            metadata={
                "mcp_prompt": {
                    "server": row["server"],
                    "prompt": row["prompt"],
                    "arguments": values,
                    "text": text,
                }
            },
        )

    async def _run_builtin(sess: Any, row: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        from clio_agent.gact.command_presentation import command_result_part  # noqa: PLC0415
        from clio_agent.gact.types import Message, Tokens  # noqa: PLC0415

        sid, cmd_id = sess.id, row["name"]
        handler = HANDLERS.get(row["handler"])
        if handler is None:
            _refuse(
                501,
                "command_handler_unknown",
                f"{cmd_id} names handler {row['handler']!r}, which this server does not register",
                command=cmd_id,
            )
        text = await handler.run(app, deps, sess, body)
        now = datetime.now(timezone.utc).isoformat()
        message = Message(
            id=f"msg_cmd_{uuid.uuid4().hex[:10]}",
            turn_id=_active_semantic_turn_id(),
            session_id=sid,
            role="assistant",
            created_at=now,
            updated_at=now,
            parts=[command_result_part(app, sid, cmd_id, text)],
            tokens=Tokens(input=0, output=0, cache_read=0, cache_write=0),
            cost_usd=None,
            stop_reason="end_turn",
            metadata={"synthetic": "command_result", "command": cmd_id},
        )
        await emit_semantic_event_async(
            app,
            sid,
            "command.invocation.completed",
            status="completed",
            summary=f"Built-in command {cmd_id} completed.",
            actor={"caller_type": "user"},
            subject={"command": cmd_id, "message_id": message.id},
            payload={"command": cmd_id, "result": text, "handler": row["handler"]},
        )
        await run_off_loop(deps.append_session_message, app, sid, message)
        app.state.bus.publish(
            Event(type="message.created", session_id=sid, payload=message.to_wire())
        )
        if handler.after is not None:
            await handler.after(app, sid, message)
        return {
            "command": cmd_id,
            "session_id": sid,
            "result": {"type": "system_message", "text": text},
        }


__all__ = ["register_command_routes"]
