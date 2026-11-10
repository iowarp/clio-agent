"""Move a user conversation to another workspace: ``POST /v1/sessions/{sid}/move``.

Contract (the gact-tui "Move to › Conversations / <workspace>" action):

* body ``{"workspace_id": "<target>"}``; a missing or empty id is 422 ``validation_error``;
* 404 ``not_found`` for an unknown session or target workspace;
* 409 ``move_not_allowed`` for a session a person cannot move on its own (delegated
  agent sessions and side sessions move with their conversation);
* 200 with the unchanged session when it is already in the target workspace;
* 409 ``session_has_active_work`` while a turn runs or tasks are active in the
  conversation or a session it owns (the session-delete guard);
* 200 with the moved session, same id, in the v2/v3 projection; ``session.updated``
  is published for it and for each owned session that moved with it.

What moves and what stays is documented in :mod:`clio_agent.gact.session_move`.
"""

from __future__ import annotations

from contextlib import ExitStack

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from clio_agent.gact.events import Event
from clio_agent.gact.message_submission_errors import session_not_found
from clio_agent.gact.off_loop import join_off_loop
from clio_agent.gact.protocol_v3 import project_for_request, session_to_v3
from clio_agent.gact.session_kind import session_kind
from clio_agent.gact.session_move import (
    MOVABLE_KINDS,
    active_work,
    move_session,
    owned_sessions,
)
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo, MoveSessionRequest, Session

__all__ = ["register_session_move_route"]


def _error(status: int, code: str, message: str, details: dict, recoverable: bool) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail=ErrorEnvelope(
            error=ErrorInfo(error=code, message=message, details=details, recoverable=recoverable)
        ).model_dump(exclude_none=True),
    )


def _active_work_error(sid: str, busy: dict[str, list[str]]) -> HTTPException:
    return _error(
        409,
        "session_has_active_work",
        "Cancel and settle active work before moving this conversation.",
        {
            "session_id": sid,
            "busy_session_ids": sorted(busy),
            "handles": sorted({handle for handles in busy.values() for handle in handles}),
        },
        True,
    )


def register_session_move_route(app: FastAPI) -> None:
    """Register ``POST /v1/sessions/{sid}/move``."""

    @app.post("/v1/sessions/{sid}/move", response_model=Session)
    async def move_session_route(
        sid: str, req: MoveSessionRequest, request: Request
    ) -> Session | JSONResponse:
        """Move a conversation (and the sessions it owns) to another workspace."""
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise session_not_found(sid)
        target = req.workspace_id
        if app.state.workspaces.get(target) is None:
            raise _error(
                404,
                "not_found",
                f"workspace not found: {target}",
                {"workspace_id": target},
                False,
            )
        kind = session_kind(sess)
        if kind not in MOVABLE_KINDS:
            raise _error(
                409,
                "move_not_allowed",
                "Only conversations and user branches can be moved. "
                "Move the conversation that owns this session instead.",
                {"session_id": sid, "session_kind": kind},
                False,
            )
        if sess.workspace_id != target:
            owned = owned_sessions(app, sid)
            busy = active_work(app, [sess, *owned])
            if busy:
                raise _active_work_error(sid, busy)
            runner = getattr(app.state, "turn_runner", None)
            with ExitStack() as slots:
                if runner is not None:
                    from clio_agent.gact.turn_runner import TurnAdmissionError  # noqa: PLC0415

                    for row in [sess, *owned]:
                        try:
                            slots.enter_context(runner.session_change(row.id))
                        except TurnAdmissionError as exc:
                            raise _active_work_error(sid, {row.id: []}) from exc
                # A task can start between the first check and taking the slots.
                busy = {
                    key: handles
                    for key, handles in active_work(app, [sess, *owned]).items()
                    if handles
                }
                if busy:
                    raise _active_work_error(sid, busy)
                result = await join_off_loop(move_session, app, sess, target, owned)
            sess = result.session
            for row in result.moved:
                app.state.bus.publish(
                    Event(
                        type="session.updated",
                        session_id=row.id,
                        payload=Session(**row.to_wire()).model_dump(exclude_none=True),
                    )
                )
        moved = sess
        return project_for_request(
            request,
            v3=lambda: JSONResponse(content=session_to_v3(moved)),
            v2=lambda: Session(**moved.to_wire()),
        )
