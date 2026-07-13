"""SSE event stream for ``GET /v1/sessions/{sid}/events`` (SPEC §7).

Provides the typed event union for the major wire event types plus
:class:`EventStream`, a synchronous iterator that:

* parses the SSE framing (``event:`` / ``id:`` / ``data:``),
* tracks the last *real* event id (ids ``>= 1``; the ``id: 0``
  preamble is connection meta and never advances the cursor — §7.1),
* resumes with the standard ``Last-Event-ID`` header, and
* reconnects gracefully on transport drops — only when the caller
  opted in (``reconnect_attempts > 0``), each attempt logged, never
  silent, never unbounded.

Unknown event types are yielded as the base :class:`StreamEvent`
(never dropped, never a parse failure — SPEC §2 forward compat).

Example:
    >>> with client.sessions.events(sid) as stream:
    ...     for event in stream:
    ...         if isinstance(event, MessagePartDelta):
    ...             print(event.text_append, end="")
    ...         elif isinstance(event, MessageCompleted):
    ...             break
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import Field

from clio_agent.sdk.errors import ClioConnectionError, error_from_response
from clio_agent.sdk.types import ErrorInfo, Message, PermissionRequest, _WireModel

if TYPE_CHECKING:  # pragma: no cover - typing only
    from clio_agent.sdk.client import ClioClient

logger = logging.getLogger("clio_agent.sdk")

#: The preamble (server.connected / session.snapshot) is pinned to id 0
#: and re-sent on every (re)connect — SPEC §7.1.
PREAMBLE_EVENT_ID = 0


class StreamEvent(_WireModel):
    """Base wire event (SPEC §7.2 envelope + the SSE ``id:`` line).

    ``payload`` holds the raw event payload; typed subclasses expose
    the load-bearing fields as properties on top of it.
    """

    id: int = 0
    type: str = ""
    occurred_at: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    replay: bool = False


class ServerConnected(StreamEvent):
    """``server.connected`` — preamble, id 0."""

    @property
    def server_version(self) -> str:
        return str(self.payload.get("server_version", ""))


class ServerHeartbeat(StreamEvent):
    """``server.heartbeat`` — transient liveness ping (never replayed)."""


class SessionSnapshot(StreamEvent):
    """``session.snapshot`` — authoritative status right after connect."""

    @property
    def status(self) -> str:
        return str(self.payload.get("status", ""))


class SessionStatusChanged(StreamEvent):
    """``session.status_changed``."""

    @property
    def status(self) -> str:
        return str(self.payload.get("status", ""))

    @property
    def prev_status(self) -> str:
        return str(self.payload.get("prev_status", ""))


class SessionUpdated(StreamEvent):
    """``session.updated`` — payload IS the full Session object."""


class MessageCreated(StreamEvent):
    """``message.created`` — payload IS the flat wire Message."""

    @property
    def message(self) -> Message:
        return Message.model_validate(self.payload)


class MessagePartAdded(StreamEvent):
    """``message.part.added``."""

    @property
    def message_id(self) -> str:
        return str(self.payload.get("message_id", ""))

    @property
    def part(self) -> dict[str, Any]:
        part = self.payload.get("part")
        return part if isinstance(part, dict) else {}


class MessagePartDelta(StreamEvent):
    """``message.part.delta`` — text/thinking both use ``text_append``."""

    @property
    def part_id(self) -> str:
        return str(self.payload.get("part_id", ""))

    @property
    def text_append(self) -> str:
        delta = self.payload.get("delta")
        if isinstance(delta, dict):
            return str(delta.get("text_append", ""))
        return ""


class MessagePartCompleted(StreamEvent):
    """``message.part.completed`` — ``final_text`` is authoritative:
    replace buffered deltas with it."""

    @property
    def part_id(self) -> str:
        return str(self.payload.get("part_id", ""))

    @property
    def final_text(self) -> str:
        return str(self.payload.get("final_text", ""))


class MessageCompleted(StreamEvent):
    """``message.completed`` — the turn settled (exactly one per turn,
    except the ask-user pause)."""

    @property
    def message_id(self) -> str:
        return str(self.payload.get("message_id", ""))

    @property
    def stop_reason(self) -> str:
        return str(self.payload.get("stop_reason", ""))

    @property
    def error_info(self) -> ErrorInfo | None:
        raw = self.payload.get("error_info")
        return ErrorInfo.model_validate(raw) if isinstance(raw, dict) else None


class MessageDeleted(StreamEvent):
    """``message.deleted``."""

    @property
    def message_id(self) -> str:
        return str(self.payload.get("message_id", ""))


class ToolCallStarted(StreamEvent):
    """``tool.call.started`` (payload key is ``tool``, not ``tool_name``)."""

    @property
    def tool(self) -> str:
        return str(self.payload.get("tool", ""))

    @property
    def call_id(self) -> str:
        return str(self.payload.get("call_id", ""))


class ToolCallCompleted(StreamEvent):
    """``tool.call.completed`` (payload key is ``ok``, not ``is_error``)."""

    @property
    def tool(self) -> str:
        return str(self.payload.get("tool", ""))

    @property
    def call_id(self) -> str:
        return str(self.payload.get("call_id", ""))

    @property
    def ok(self) -> bool:
        return bool(self.payload.get("ok", False))


class PermissionRequested(StreamEvent):
    """``permission.requested`` — payload IS the flat permission row."""

    @property
    def permission(self) -> PermissionRequest:
        return PermissionRequest.model_validate(self.payload)


class PermissionResolved(StreamEvent):
    """``permission.resolved`` — may arrive WITHOUT a matching
    ``permission.requested`` (every auto/direct resolution)."""

    @property
    def permission_id(self) -> str:
        return str(self.payload.get("permission_id", ""))

    @property
    def action(self) -> str:
        return str(self.payload.get("action", ""))


_EVENT_TYPES: dict[str, type[StreamEvent]] = {
    "server.connected": ServerConnected,
    "server.heartbeat": ServerHeartbeat,
    "session.snapshot": SessionSnapshot,
    "session.status_changed": SessionStatusChanged,
    "session.updated": SessionUpdated,
    "message.created": MessageCreated,
    "message.part.added": MessagePartAdded,
    "message.part.delta": MessagePartDelta,
    "message.part.completed": MessagePartCompleted,
    "message.completed": MessageCompleted,
    "message.deleted": MessageDeleted,
    "tool.call.started": ToolCallStarted,
    "tool.call.completed": ToolCallCompleted,
    "permission.requested": PermissionRequested,
    "permission.resolved": PermissionResolved,
}


def parse_event(event_id: int, data: dict[str, Any]) -> StreamEvent:
    """Build the typed event for one decoded ``data:`` payload.

    Unknown ``type`` values return the base :class:`StreamEvent`
    with everything preserved (SPEC §2).
    """

    event_type = str(data.get("type", ""))
    cls = _EVENT_TYPES.get(event_type, StreamEvent)
    raw_payload = data.get("payload")
    payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
    return cls(
        id=event_id,
        type=event_type,
        occurred_at=str(data.get("occurred_at", "")),
        payload=payload,
        replay=bool(data.get("replay", False)),
    )


class EventStream:
    """Iterator over one session's SSE feed with resume + reconnect.

    Use as a context manager (or call :meth:`close`) so the underlying
    HTTP stream is released when you stop consuming.
    """

    def __init__(
        self,
        client: ClioClient,
        session_id: str,
        *,
        last_event_id: int | None = None,
        reconnect_attempts: int = 0,
        reconnect_wait_s: float = 1.0,
    ) -> None:
        self._client = client
        self._session_id = session_id
        self._reconnect_attempts = reconnect_attempts
        self._reconnect_wait_s = reconnect_wait_s
        #: Highest real (>=1) event id seen; sent as ``Last-Event-ID``.
        self.last_event_id = int(last_event_id or 0)
        self._response_cm: Any = None
        self._closed = False

    # -- lifecycle ---------------------------------------------------- #

    def __enter__(self) -> EventStream:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        """Stop consuming and release the underlying HTTP stream."""

        self._closed = True
        if self._response_cm is not None:
            self._response_cm.__exit__(None, None, None)
            self._response_cm = None

    # -- iteration ---------------------------------------------------- #

    def __iter__(self) -> Iterator[StreamEvent]:
        attempts_left = self._reconnect_attempts
        while True:
            try:
                yield from self._consume_once()
                # Server closed the stream cleanly. SPEC §7.1 says it
                # streams forever, so a close is a drop in practice.
                drop: Exception | None = None
            except (httpx.TransportError, httpx.StreamError) as exc:
                drop = exc
            finally:
                if self._response_cm is not None:
                    self._response_cm.__exit__(None, None, None)
                    self._response_cm = None
            if self._closed:
                return
            if attempts_left <= 0:
                raise ClioConnectionError(
                    f"event stream for {self._session_id} dropped "
                    f"(last_event_id={self.last_event_id})"
                ) from drop
            attempts_left -= 1
            logger.warning(
                "SSE stream for %s dropped (%s); reconnecting with "
                "Last-Event-ID=%d (%d attempt(s) left)",
                self._session_id,
                drop or "server closed stream",
                self.last_event_id,
                attempts_left,
            )
            if self._reconnect_wait_s > 0:
                time.sleep(self._reconnect_wait_s)

    def _consume_once(self) -> Iterator[StreamEvent]:
        """Open one HTTP stream and yield its events until it ends."""

        headers = {"Accept": "text/event-stream"}
        if self.last_event_id > 0:
            headers["Last-Event-ID"] = str(self.last_event_id)
        cm = self._client._stream(
            "GET",
            f"/v1/sessions/{self._session_id}/events",
            headers=headers,
        )
        response = cm.__enter__()
        self._response_cm = cm
        if response.status_code >= 400:
            response.read()
            raise error_from_response(response)

        event_id = 0
        data_lines: list[str] = []
        for line in response.iter_lines():
            if line == "":
                # Frame boundary — dispatch what we buffered.
                if data_lines:
                    event = self._decode("\n".join(data_lines), event_id)
                    data_lines = []
                    if event is not None:
                        if event.id > PREAMBLE_EVENT_ID:
                            self.last_event_id = max(self.last_event_id, event.id)
                        yield event
                event_id = 0
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            elif line.startswith("id:"):
                try:
                    event_id = int(line[3:].strip())
                except ValueError:
                    event_id = 0
            # `event:` lines are redundant with data.type (SPEC §7.2)
            # and comment lines (`:`) are keep-alive noise — skipped.

    def _decode(self, raw: str, event_id: int) -> StreamEvent | None:
        try:
            data = json.loads(raw)
        except ValueError:
            logger.warning(
                "SSE frame id=%d for %s carried undecodable data (%d bytes) — skipped",
                event_id,
                self._session_id,
                len(raw),
            )
            return None
        if not isinstance(data, dict):
            return None
        return parse_event(event_id, data)
