"""A command the person types runs as a turn of the conversation.

A user or plugin command (a Markdown recipe) is the person asking for that
procedure, so it is admitted exactly like a message they sent: their
transcript shows what they typed (``/new-paper a workshop paper ...``), while
the model receives the command's rendered recipe. As a turn it streams, can
ask the person questions and can be stopped, like any other.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from clio_agent.gact import context as gact_context
from clio_agent.gact.hooks.dispatcher import dispatch_user_prompt_expansion
from clio_agent.gact.message_contract import PostMessageRequest
from clio_agent.gact.message_submission import accept_message_async
from clio_agent.gact.parts import Part
from clio_agent.gact.runtime.commands import command_cwd_for_request

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
    submission = PostMessageRequest(
        parts=[Part(type="text", text=typed)],
        agent_id=str(command_meta.get("agent_id") or "") or None,
    )
    acknowledgement, _status = await accept_message_async(
        app,
        deps,
        sid,
        submission,
        internal_model_text=rendered,
        server_metadata={
            "command": cmd_id,
            "command_source": str(command_meta.get("command_source") or ""),
            **({"plugin": str(command_meta["plugin"])} if command_meta.get("plugin") else {}),
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
