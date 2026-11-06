"""A command the person types runs as a turn of the conversation.

A user or plugin command (a Markdown recipe) is the person asking for that
procedure, so it is admitted exactly like a message they sent: their
transcript shows what they typed (``/new-paper a workshop paper ...``), while
the model receives the command's rendered recipe. A file's ``agent:`` summons that
subagent on the recipe instead, through the same path as ``@<subagent>``. As a turn
it streams, can
ask the person questions and can be stopped, like any other.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from clio_agent.gact import context as gact_context
from clio_agent.gact.hooks.dispatcher import dispatch_user_prompt_expansion
from clio_agent.gact.message_submission import accept_message_async
from clio_agent.gact.parts import Part
from clio_agent.gact.runtime.command_table import command_cwd_for_request
from clio_agent.gact.types import PostMessageRequest

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.routes.deps import GactDeps


def _dispatch_expansion(app: "FastAPI", *args: Any, **kwargs: Any) -> Any:
    """Fire the expansion hooks with ``app`` active: a session's plugin hooks are its own."""

    token = gact_context.set_app(app)
    try:
        return dispatch_user_prompt_expansion(*args, **kwargs)
    finally:
        gact_context.reset(token)


async def submit_user_command(
    app: "FastAPI",
    deps: "GactDeps",
    sid: str,
    *,
    cmd_id: str,
    user_input: str,
    rendered: str,
    command_meta: dict[str, Any],
    parts: list[Part] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Admit ``cmd_id`` as the person's message whose model text is ``rendered``.

    ``UserPromptExpansion`` hooks see the expansion first, under Claude Code's
    command name (``plugin:command``): a deny stops the command with the hook's
    reason, and context a hook adds reaches the model with the command.
    """

    plugin = str(command_meta.get("plugin") or "")
    bare = cmd_id.lstrip("/")
    expansion = await asyncio.to_thread(
        _dispatch_expansion,
        app,
        f"{plugin}:{bare}" if plugin else bare,
        user_input,
        rendered,
        session_id=sid,
        cwd=str(command_cwd_for_request(app, sid) or ""),
    )
    if expansion.denied:
        return {
            "command": cmd_id,
            "session_id": sid,
            "result": {
                "type": "refused",
                "reason": "hook_denied",
                "message": expansion.reason or "a UserPromptExpansion hook stopped this command",
            },
        }
    if expansion.additional_context:
        rendered = f"{rendered}\n\n{expansion.additional_context}"
    typed = f"{cmd_id} {user_input}".strip()
    subagent = str(command_meta.get("agent") or "")
    parts = [Part(type="text", text=typed), *(parts or [])]
    if subagent:
        # ``agent:`` summons that subagent on the rendered recipe, as ``@<subagent>`` does.
        parts.append(Part(type="summon", target_kind="subagent", target=subagent))
    acknowledgement, _status = await accept_message_async(
        app,
        deps,
        sid,
        PostMessageRequest(parts=parts),
        internal_model_text=typed if subagent else rendered,
        summon_task=rendered if subagent else "",
        server_metadata={
            "command": cmd_id,
            "command_source": str(command_meta.get("source") or ""),
            "command_kind": str(command_meta.get("kind") or ""),
            **({"plugin": str(command_meta["plugin"])} if command_meta.get("plugin") else {}),
            **(
                {"loaded_skill": str(command_meta["skill_id"])}
                if command_meta.get("skill_id")
                else {}
            ),
            **(metadata or {}),
        },
    )
    return {
        "command": cmd_id,
        "session_id": sid,
        "result": {
            "type": "turn",
            "message_id": acknowledgement.message_id,
            "delivery": acknowledgement.delivery,
            "state": acknowledgement.state,
        },
    }


__all__ = ["submit_user_command"]
