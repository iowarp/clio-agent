"""Pending-steer recovery and durable queued-message routes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field

from clio_agent.gact.events import Event
from clio_agent.gact.message_intents import (
    QueueCapacityError,
    QueuedMessage,
    RevisionConflictError,
)
from clio_agent.gact.message_submission import accept_message
from clio_agent.gact.runtime.globals import _new_message_id
from clio_agent.gact.types import (
    ErrorEnvelope,
    ErrorInfo,
    MessageBehavior,
    ModelRef,
    Part,
    PostMessageRequest,
)

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


class CreateQueuedMessageRequest(BaseModel):
    """Create one future message without adding it to the transcript."""

    parts: list[Part] = Field(default_factory=list)
    text: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    client_message_id: str = ""
    idempotency_key: str = ""
    behavior: MessageBehavior = Field(default_factory=MessageBehavior)
    model: ModelRef = Field(default_factory=ModelRef)

    def normalized_parts(self) -> list[Part]:
        if self.parts:
            return list(self.parts)
        if self.text:
            return [Part(type="text", text=self.text)]
        return []


class UpdateQueuedMessageRequest(BaseModel):
    """Revision-checked patch for one future message."""

    revision: int = Field(ge=1)
    parts: list[Part] | None = None
    metadata: dict[str, Any] | None = None
    behavior: MessageBehavior | None = None
    model: ModelRef | None = None


class ReorderQueuedMessagesRequest(BaseModel):
    """Authoritative queue order with the revisions the client observed."""

    ordered_ids: list[str]
    revisions: dict[str, int]


class PromoteQueuedMessageRequest(BaseModel):
    """Promote a future message through the ordinary acceptance service."""

    revision: int = Field(ge=1)
    delivery: Literal["start", "steer", "auto"] = "auto"


def register_message_intent_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register server-authoritative pending-steer and queue lifecycle routes."""

    def require_session(sid: str) -> None:
        if app.state.sessions.get(sid) is None:
            raise HTTPException(status_code=404, detail=f"session not found: {sid}")

    def publish(event_type: str, sid: str, payload: dict[str, Any]) -> None:
        app.state.bus.publish(Event(type=event_type, session_id=sid, payload=payload))

    def conflict(exc: RevisionConflictError) -> HTTPException:
        return HTTPException(
            status_code=409,
            detail={
                "error": "revision_conflict",
                "message": "queued message changed on the server",
                "current": exc.current.model_dump(),
            },
        )

    def redrive_queue(sid: str) -> None:
        """Re-attempt the head after any queue mutation.

        The queue's only auto-promoter used to be the turn-done hook, so a
        message queued while the session was IDLE sat forever (no turn was
        running, so no turn could end), and a promotion that failed once stayed
        frozen until some unrelated turn happened to finish. Every mutation is a
        moment the head may have become promotable, so every mutation re-drives.
        A busy or cancelled session is a no-op inside ``promote_queue_head``.
        """

        from clio_agent.gact.composer_runtime import promote_queue_head  # noqa: PLC0415

        promote_queue_head(app, deps, sid)

    @app.get("/v1/sessions/{sid}/pending-steers")
    async def list_pending_steers(sid: str) -> dict[str, Any]:
        require_session(sid)
        return {
            "pending_steers": [
                row.model_dump() for row in app.state.message_intents.list_pending(sid)
            ]
        }

    @app.get("/v1/sessions/{sid}/message-state")
    async def get_message_state(sid: str) -> dict[str, Any]:
        """Return one authoritative reconciliation snapshot for GACT 0.3 clients.

        ``next_cursor`` speaks the server-wide cursor convention (see the
        ``GET /v1/sessions/{sid}/events`` docstring): *the highest event id this
        snapshot already accounts for*, so a client can hand it straight to
        ``Last-Event-ID`` and resume exclusively from there. It used to return
        ``last id + 1`` — an inclusive convention — which pushed a reconciling
        client one id past the timeline head and tripped the epoch guard on its
        very first reconnect. ``0`` means the session has no events yet.
        """

        session = app.state.sessions.get(sid)
        if session is None:
            raise HTTPException(status_code=404, detail=f"session not found: {sid}")
        return {
            "protocol_version": "0.3",
            "authoritative": True,
            "session": session.to_wire(),
            "messages": [row.to_wire() for row in app.state.messages.get(sid, [])],
            "pending_steers": [
                row.model_dump() for row in app.state.message_intents.list_pending(sid)
            ],
            "queued_messages": [
                row.model_dump() for row in app.state.message_intents.list_queued(sid)
            ],
            "next_cursor": app.state.bus.latest_event_id(sid),
            "dropped_events": app.state.bus.dropped_total(sid),
        }

    @app.delete("/v1/sessions/{sid}/pending-steers/{message_id}")
    async def cancel_pending_steer(sid: str, message_id: str) -> dict[str, Any]:
        """Cancel one accepted-but-undelivered steer.

        Cancels a ``pending`` steer AND one a consumer merely ``claimed`` — a
        claim is a delivery reservation that can outlive its consumer, so it must
        never make a steer permanently uncancellable. Only a steer already
        ``consumed`` (the model has seen it) or already ``cancelled`` refuses,
        with the settled state named in the 409.
        """

        require_session(sid)
        cancelled = app.state.message_intents.cancel_pending(sid, message_id)
        if cancelled is None:
            current = app.state.message_intents.get_pending(sid, message_id)
            if current is None:
                raise HTTPException(status_code=404, detail="pending steer not found")
            raise HTTPException(
                status_code=409,
                detail={"error": "steer_already_settled", "state": current.state},
            )
        inbox = app.state.loop_inboxes.get(sid)
        if inbox is not None:
            inbox.cancel_user_message(message_id)
        messages = [row for row in app.state.messages.get(sid, []) if row.id != message_id]
        deps.replace_session_messages(app, sid, messages)
        payload = {"message_id": message_id, "session_id": sid}
        publish("message.cancelled", sid, payload)
        publish("pending_steer.cancelled", sid, payload)
        return payload

    @app.get("/v1/sessions/{sid}/queued-messages")
    async def list_queued_messages(sid: str) -> dict[str, Any]:
        require_session(sid)
        return {
            "queued_messages": [
                row.model_dump() for row in app.state.message_intents.list_queued(sid)
            ]
        }

    @app.post("/v1/sessions/{sid}/queued-messages", status_code=201)
    async def create_queued_message(sid: str, req: CreateQueuedMessageRequest) -> QueuedMessage:
        require_session(sid)
        parts = req.normalized_parts()
        if not parts:
            raise HTTPException(status_code=400, detail="queued message has no parts")
        prior = app.state.message_intents.find_queued_by_idempotency(
            sid, req.idempotency_key.strip()
        )
        if prior is not None:
            return prior
        message_id = req.client_message_id.strip() or _new_message_id("queued")
        existing = app.state.message_intents.get_queued(sid, message_id)
        if existing is not None:
            return existing
        try:
            row = app.state.message_intents.create_queued(
                QueuedMessage(
                    id=message_id,
                    session_id=sid,
                    parts=parts,
                    metadata=req.metadata,
                    client_message_id=req.client_message_id,
                    idempotency_key=req.idempotency_key,
                    behavior=req.behavior,
                    model=req.model,
                )
            )
        except QueueCapacityError as exc:
            # A refusal, not an eviction: the queue holds the user's un-sent
            # future intent, so the cap must never silently drop one of them.
            raise HTTPException(
                status_code=429,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="queue_capacity_exceeded",
                        message=(
                            "this session's queued-message limit is reached; promote or delete "
                            "a queued message before adding another"
                        ),
                        details={
                            "session_id": sid,
                            "limit": exc.limit,
                            "recovery_actions": ["promote_queued", "delete_queued", "retry"],
                        },
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            ) from exc
        publish("queued_message.created", sid, row.model_dump())
        redrive_queue(sid)
        return row

    @app.patch("/v1/sessions/{sid}/queued-messages/{message_id}")
    async def update_queued_message(
        sid: str, message_id: str, req: UpdateQueuedMessageRequest
    ) -> QueuedMessage:
        require_session(sid)
        try:
            row = app.state.message_intents.update_queued(
                sid,
                message_id,
                req.revision,
                parts=req.parts,
                metadata=req.metadata,
                behavior=req.behavior,
                model=req.model,
            )
        except RevisionConflictError as exc:
            raise conflict(exc) from exc
        if row is None:
            raise HTTPException(status_code=404, detail="queued message not found")
        publish("queued_message.updated", sid, row.model_dump())
        redrive_queue(sid)
        return row

    @app.delete("/v1/sessions/{sid}/queued-messages/{message_id}", status_code=204)
    async def delete_queued_message(
        sid: str, message_id: str, revision: int, response: Response
    ) -> None:
        require_session(sid)
        try:
            row = app.state.message_intents.delete_queued(sid, message_id, revision)
        except RevisionConflictError as exc:
            raise conflict(exc) from exc
        if row is None:
            raise HTTPException(status_code=404, detail="queued message not found")
        publish("queued_message.deleted", sid, row.model_dump())
        redrive_queue(sid)
        response.status_code = 204

    @app.post("/v1/sessions/{sid}/queued-messages/reorder")
    async def reorder_queued_messages(
        sid: str, req: ReorderQueuedMessagesRequest
    ) -> dict[str, Any]:
        require_session(sid)
        try:
            rows = app.state.message_intents.reorder(sid, req.ordered_ids, req.revisions)
        except RevisionConflictError as exc:
            raise conflict(exc) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        payload = {"queued_messages": [row.model_dump() for row in rows]}
        publish("queued_message.reordered", sid, payload)
        redrive_queue(sid)
        return payload

    @app.post("/v1/sessions/{sid}/queued-messages/{message_id}/promote")
    async def promote_queued_message(
        sid: str, message_id: str, req: PromoteQueuedMessageRequest
    ) -> dict[str, Any]:
        require_session(sid)
        try:
            promoted = app.state.message_intents.promote_queued(
                sid,
                message_id,
                req.revision,
                lambda row: accept_message(
                    app,
                    deps,
                    sid,
                    PostMessageRequest(
                        parts=row.parts,
                        model=row.model,
                        metadata=row.metadata,
                        client_message_id=row.client_message_id,
                        idempotency_key=row.idempotency_key or row.id,
                        delivery=req.delivery,
                        behavior=row.behavior,
                    ),
                ),
            )
        except RevisionConflictError as exc:
            raise conflict(exc) from exc
        if promoted is None:
            raise HTTPException(status_code=404, detail="queued message not found")
        _deleted, (ack, status_code) = promoted
        payload = {
            "queued_message_id": message_id,
            "acceptance": ack.model_dump(),
            "status_code": status_code,
        }
        publish("queued_message.promoted", sid, payload)
        return payload


__all__ = ["register_message_intent_routes"]
