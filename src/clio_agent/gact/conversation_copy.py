"""Copy a conversation's transcript and context files into another session.

One mechanism serves every copy: a person's fork of a conversation, a side
session, and an agent's fork of itself. The copy is a deep copy of the stored
messages through an optional message, plus the session's context files. An
agent's fork also carries the turn it forks from, which is not stored until the
turn ends: the open turn's parts through its last finished tool result, without a
call still in flight, and then the ``fork`` call itself with the fork's own result,
as ``fork()`` returns differently in the child: the original's call returns the
fork it made, the fork's copy says it is the fork and names the note. Without that
result the fork would read an unanswered request to fork and fork again. The copied
session's model
context is then restored from this transcript on its first turn
(:func:`~clio_agent.gact.agents.context_identity.restore_context`).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from clio_agent.gact.types import Message


def stored_messages(app: Any, src: str, at: str = "") -> list[Message]:
    """``src``'s stored messages, through ``at`` when it names one of them."""

    messages = list(app.state.messages.get(src, []))
    if at and any(message.id == at for message in messages):
        messages = messages[: next(i for i, m in enumerate(messages) if m.id == at) + 1]
    return messages


def open_turn_message(
    app: Any, src: str, dst: str, fork_call: dict[str, Any] | None = None
) -> Message | None:
    """The finished part of ``src``'s open turn, as an assistant message of ``dst``.

    Parts are kept through the last ``tool_result``; a ``tool_call`` with no result
    yet is left out, except the fork's own call (``fork_call``: its ``call_id`` and
    the ``result`` the fork sees), which is kept with that result. ``None`` when
    ``src`` has no open turn or nothing finished.
    """

    import json  # noqa: PLC0415

    from clio_agent.gact.parts import Part  # noqa: PLC0415
    from clio_agent.gact.tool_observer import _session_turn_transcript  # noqa: PLC0415

    transcript = _session_turn_transcript(app, src)
    parts = list(transcript.snapshot()) if transcript is not None else []
    own = str((fork_call or {}).get("call_id") or "")
    marks = [i for i, part in enumerate(parts) if part.type == "tool_result"]
    marks += [i for i, p in enumerate(parts) if own and p.type == "tool_call" and p.call_id == own]
    last = max(marks, default=-1)
    finished = {part.call_id for part in parts[: last + 1] if part.type == "tool_result"} | {own}
    kept = [
        part.model_copy(deep=True)
        for part in parts[: last + 1]
        if part.type != "tool_call" or part.call_id in finished
    ]
    if own and any(p.type == "tool_call" and p.call_id == own for p in kept):
        kept.append(
            Part(
                id=f"fork_{own}_result",
                type="tool_result",
                call_id=own,
                tool_name="fork",
                content=[Part(type="text", text=json.dumps((fork_call or {}).get("result") or {}))],
                metadata={"completed_at": datetime.now(timezone.utc).isoformat()},
            )
        )
    if not kept:
        return None
    now = datetime.now(timezone.utc).isoformat()
    return Message(
        id="msg_" + uuid4().hex[:12],
        turn_id=str(getattr(transcript, "turn_id", "") or ""),
        session_id=dst,
        role="assistant",
        created_at=now,
        updated_at=now,
        parts=kept,
    )


def copy_conversation(
    app: Any,
    src: str,
    dst: str,
    at: str = "",
    *,
    open_turn: bool = False,
    fork_call: dict[str, Any] | None = None,
) -> int:
    """Deep-copy ``src``'s messages (through ``at``) and context files into ``dst``.

    ``open_turn`` adds the finished part of ``src``'s open turn (an agent forking
    itself mid-turn), with ``fork_call`` answered as the fork sees it. Returns the
    number of messages copied. Runs off the loop.
    """

    copied = [
        m.model_copy(deep=True, update={"session_id": dst}) for m in stored_messages(app, src, at)
    ]
    if open_turn and (current := open_turn_message(app, src, dst, fork_call)) is not None:
        copied.append(current)
    app.state.gact_deps.replace_session_messages(app, dst, copied)
    if source_context_files := app.state.context_files.get(src, {}):
        app.state.context_files[dst] = {k: dict(row) for k, row in source_context_files.items()}
    return len(copied)


def fork_side_result(source: str, note: Any) -> dict[str, Any]:
    """What the ``fork`` call returns inside the fork (the original gets the fork's id)."""

    return {
        "forked": True,
        "this_session": "the fork",
        "forked_from": source,
        "note": str(note or ""),
        "meaning": (
            "You are the copy this call created. The conversation that called fork "
            "carries on with the rest of its own plan; your direction is the note."
        ),
    }


__all__ = [
    "copy_conversation",
    "create_branch",
    "fork_side_result",
    "open_turn_message",
    "stored_messages",
]


#: Configuration a branch copies from its conversation: never task ownership or pending work.
BRANCH_METADATA_KEYS = frozenset(
    {
        "agent_ref",
        "agent_delta",
        "agent_resolution",
        "effort",
        "effort_source",
        "thinking_level",
        "branch",
        "git_branch",
        "memory_snapshot",  # memory as of the fork point (MD16)
        "memory_off",
    }
)


def create_branch(
    app: Any,
    source: Any,
    *,
    title: str = "",
    at: str = "",
    open_turn: bool = False,
    origin: dict[str, Any] | None = None,
) -> Any:
    """A new conversation branch of ``source``: its configuration and its transcript
    through ``at`` (and, for an agent forking mid-turn, the open turn's finished part).

    ``origin`` adds to the branch's ``fork_origin`` record (an agent fork names its
    author and the call that made it). Runs off the loop.
    """

    from clio_agent.gact.conversation_origin import FORK_ORIGIN_KEY  # noqa: PLC0415
    from clio_agent.gact.message_marks import saved_message_marks  # noqa: PLC0415
    from clio_agent.gact.runtime.permission_policies import (  # noqa: PLC0415
        inherit_child_session_policies,
    )

    metadata = {key: value for key, value in source.metadata.items() if key in BRANCH_METADATA_KEYS}
    metadata["session_kind"] = "branch"
    branch = app.state.sessions.create(
        workspace_id=source.workspace_id,
        title=title or f"{source.title} (fork)",
        parent_session_id=source.id,
        model=dict(source.model),
        agent={key: value for key, value in source.agent.items() if key != "mode"},
        metadata=metadata,
        mode=source.mode,
        edit_mode=source.edit_mode,
        routing_mode=source.routing_mode,
        approval_mode=source.approval_mode,
        approval_profile=source.approval_profile,
    )
    inherit_child_session_policies(app, source.id, branch.id)
    invocation = str((origin or {}).get("invocation") or "")
    fork_call = (
        {"call_id": invocation, "result": fork_side_result(source.id, (origin or {}).get("note"))}
        if invocation
        else None
    )
    copied = copy_conversation(
        app, source.id, branch.id, at, open_turn=open_turn, fork_call=fork_call
    )
    messages = app.state.messages.get(branch.id, [])
    app.state.sessions.update(
        branch.id,
        message_count=copied,
        metadata_patch={
            FORK_ORIGIN_KEY: {
                "session_id": source.id,
                "message_id": messages[-1].id if messages else "",
                "message_count": copied,
                "title": source.title,
                "created_at": branch.created_at,
                **(origin or {}),
            },
            "message_marks": saved_message_marks(
                source.metadata, {message.id for message in messages}
            ),
        },
    )
    return app.state.sessions.get(branch.id)
