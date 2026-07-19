"""Message post + SSE consumption through the SDK (SPEC §6.3 + §7).

Drives a stubbed turn end-to-end against the real in-process app:
post → ack → live SSE events → settled ledger, plus Last-Event-ID
resume semantics and the bounded, logged reconnect path.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from clio_agent.sdk import (
    ClioClient,
    ClioConnectionError,
    Message,
    MessageCompleted,
    MessageCreated,
    ServerConnected,
    SessionSnapshot,
    SessionStatusChanged,
    StreamEvent,
)
from tests.test_sdk.conftest import StreamingASGITransport, StubAgent

# #948 S4b: default sessions run the blueprint react ``main``; route it to each
# test's ``build_app(agent=...)`` host/stub fake.
pytestmark = pytest.mark.usefixtures("host_agent_executor")


def _wait_for_settled_turn(
    client: ClioClient, session_id: str, user_message_id: str, timeout: float = 10.0
) -> Message:
    """Poll the ledger until the assistant reply for one turn lands."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for message in client.messages.list(session_id):
            if (
                message.role == "assistant"
                and message.turn_id == user_message_id
                and not message.metadata.get("live")
            ):
                return message
        time.sleep(0.05)
    raise TimeoutError(f"turn {user_message_id} did not settle within {timeout:g}s")


def test_post_text_and_read_settled_ledger(client: ClioClient, stub_agent: StubAgent) -> None:
    sess = client.sessions.create(title="turn test")

    ack = client.messages.post(sess.id, text="hello clio")
    assert ack.message_id.startswith("msg_")
    assert ack.accepted_at

    assistant = _wait_for_settled_turn(client, sess.id, ack.message_id)
    assert stub_agent.calls == [("hello clio", sess.id)]
    assert "stub answer" in assistant.text()
    assert assistant.stop_reason == "end_turn"

    fetched = client.messages.get(sess.id, ack.message_id)
    assert fetched.role == "user"
    assert fetched.text() == "hello clio"
    assert fetched.turn_id == ack.message_id


def test_live_sse_consume_of_a_stubbed_turn(client: ClioClient) -> None:
    sess = client.sessions.create(title="sse live")

    with client.sessions.events(sess.id) as stream:
        events = iter(stream)

        # Preamble (SPEC §7.1): server.connected + session.snapshot, id 0.
        connected = next(events)
        assert isinstance(connected, ServerConnected)
        assert connected.id == 0
        assert connected.server_version
        snapshot = next(events)
        assert isinstance(snapshot, SessionSnapshot)
        assert snapshot.id == 0
        assert snapshot.status == "idle"

        ack = client.messages.post(sess.id, text="stream me")

        seen: list[StreamEvent] = []
        for event in events:
            seen.append(event)
            if isinstance(event, MessageCompleted):
                break

        types = [e.type for e in seen]
        assert types[0] == "session.status_changed"
        running = seen[0]
        assert isinstance(running, SessionStatusChanged)
        assert running.status == "running"

        created = [e for e in seen if isinstance(e, MessageCreated)]
        roles = [e.message.role for e in created]
        assert roles[0] == "user"
        assert created[0].message.id == ack.message_id
        assert "assistant" in roles

        completed = seen[-1]
        assert isinstance(completed, MessageCompleted)
        assert completed.stop_reason == "end_turn"
        assert completed.error_info is None

        # Real event ids are >= 1, strictly ascending (SPEC §7.1).
        real_ids = [e.id for e in seen]
        assert all(i >= 1 for i in real_ids)
        assert real_ids == sorted(real_ids)
        # Live events never carry the replay marker.
        assert not any(e.replay for e in seen)


def test_replay_and_last_event_id_resume(client: ClioClient) -> None:
    sess = client.sessions.create(title="sse resume")
    ack = client.messages.post(sess.id, text="replay me")
    _wait_for_settled_turn(client, sess.id, ack.message_id)

    # Full replay from 0: everything re-delivered with replay=True.
    replayed: list[StreamEvent] = []
    with client.sessions.events(sess.id) as stream:
        for event in stream:
            if event.id == 0:
                continue  # preamble
            replayed.append(event)
            if isinstance(event, MessageCompleted):
                break
    assert replayed, "settled turn must be replayable"
    assert all(e.replay for e in replayed)
    assert stream.last_event_id == replayed[-1].id

    # Resume mid-stream: only events with id > Last-Event-ID arrive.
    cursor = replayed[1].id
    with client.sessions.events(sess.id, last_event_id=cursor) as resumed_stream:
        resumed: list[StreamEvent] = []
        for event in resumed_stream:
            if event.id == 0:
                continue
            resumed.append(event)
            if isinstance(event, MessageCompleted):
                break
    assert resumed
    assert all(e.id > cursor for e in resumed)
    assert [e.id for e in resumed] == [e.id for e in replayed if e.id > cursor]


class _DropAfterStream(httpx.SyncByteStream):
    """Wraps a response stream and injects a ReadError after N chunks."""

    def __init__(self, inner: httpx.SyncByteStream, allowed_chunks: int) -> None:
        self._inner = inner
        self._allowed = allowed_chunks

    def __iter__(self) -> Iterator[bytes]:
        for i, chunk in enumerate(self._inner):
            if i >= self._allowed:
                self._inner.close()
                raise httpx.ReadError("injected connection drop")
            yield chunk

    def close(self) -> None:
        self._inner.close()


class _FlakyTransport(httpx.BaseTransport):
    """Drops the FIRST SSE response after a few frames; passthrough after."""

    def __init__(self, inner: StreamingASGITransport, allowed_chunks: int = 4) -> None:
        self._inner = inner
        self._allowed_chunks = allowed_chunks
        self.drops = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self._inner.handle_request(request)
        if request.url.path.endswith("/events") and self.drops == 0:
            self.drops += 1
            return httpx.Response(
                status_code=response.status_code,
                headers=response.headers,
                stream=_DropAfterStream(response.stream, self._allowed_chunks),  # type: ignore[arg-type]
                request=request,
            )
        return response


def test_graceful_reconnect_resumes_without_duplicates(
    transport: StreamingASGITransport,
    caplog: pytest.LogCaptureFixture,
) -> None:
    flaky = _FlakyTransport(transport)
    with ClioClient("http://testserver", transport=flaky) as client:
        sess = client.sessions.create(title="sse reconnect")
        ack = client.messages.post(sess.id, text="drop me")
        _wait_for_settled_turn(client, sess.id, ack.message_id)

        seen: list[StreamEvent] = []
        with caplog.at_level(logging.WARNING, logger="clio_agent.sdk"):
            with client.sessions.events(
                sess.id, reconnect_attempts=2, reconnect_wait_s=0.0
            ) as stream:
                for event in stream:
                    if event.id == 0:
                        continue
                    seen.append(event)
                    if isinstance(event, MessageCompleted):
                        break

    assert flaky.drops == 1, "the first SSE connection must have been dropped"
    # The reconnect is explicit and logged — never silent.
    assert any("reconnecting with Last-Event-ID" in r.message for r in caplog.records)
    # Resume must not re-deliver events the first connection already yielded.
    real_ids = [e.id for e in seen]
    assert len(real_ids) == len(set(real_ids)), f"duplicate event ids: {real_ids}"
    assert isinstance(seen[-1], MessageCompleted)


def test_stream_drop_without_reconnect_budget_raises(
    transport: StreamingASGITransport,
) -> None:
    flaky = _FlakyTransport(transport, allowed_chunks=2)
    with ClioClient("http://testserver", transport=flaky) as client:
        sess = client.sessions.create(title="sse no budget")
        ack = client.messages.post(sess.id, text="drop hard")
        _wait_for_settled_turn(client, sess.id, ack.message_id)

        with pytest.raises(ClioConnectionError):
            with client.sessions.events(sess.id) as stream:
                for _ in stream:
                    pass


def test_unknown_event_types_are_preserved_not_dropped(app: Any, client: ClioClient) -> None:
    """SPEC §2: an unrecognized event type must parse into the base
    StreamEvent with its payload intact, never fail or vanish."""

    from clio_agent.gact.events import Event

    sess = client.sessions.create(title="unknown events")

    with client.sessions.events(sess.id) as stream:
        events = iter(stream)
        assert next(events).type == "server.connected"
        assert next(events).type == "session.snapshot"

        app.state.bus.publish(
            Event(type="vendor.future_thing", session_id=sess.id, payload={"k": "v"})
        )

        event = next(events)
        assert type(event) is StreamEvent
        assert event.type == "vendor.future_thing"
        assert event.payload == {"k": "v"}
        assert event.id >= 1
