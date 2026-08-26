"""clio-relay#221/#259 live-console lane: the PUSH half (relay_console_stream.py).

Unit-level acceptance against a FAKE SSE door (``httpx.MockTransport`` -- real
HTTP/SSE wire semantics, no live relay). Mirrors ``test_relay_console.py``'s
isolation philosophy: this file exercises the SSE reader/registry/capability-
probe logic on its own, so a failure here points straight at THIS module and
never at the pull-path fold logic (covered separately) or the transport
plumbing (covered in ``test_relay_transport.py``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from clio_agent.tools.mcp_task_records import (
    InMemoryTaskRecordStore,
    TaskKey,
    TaskRecord,
    set_task_console_listener,
)
from clio_agent.tools.relay_console import CONSOLE_BACKEND_KEY
from clio_agent.tools.relay_console_stream import (
    CONSOLE_SSE_FALLBACK_REASON,
    CONSOLE_SSE_GONE_REASON,
    ConsoleStreamProtocolError,
    ConsoleStreamRegistry,
    _iter_sse_frames,
    _iter_sse_lines,
    drive_console_stream,
    probe_console_sse_capability,
)


class _FakeConsoleSseClient:
    """Minimal ``RelayTransportClient`` stand-in exposing only ``_require_http_client()``
    -- mirrors ``test_relay_console.py``'s own ``_FakeRelayHttpClient``."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://relay.invalid"
        )

    def _require_http_client(self) -> httpx.AsyncClient:  # noqa: SLF001
        return self._client


def _key(task_id: str = "job-1") -> TaskKey:
    return TaskKey(server_id="relay", session_id="sess_console", task_id=task_id)


def _sse_event(event_type: str, payload: dict) -> str:
    """One well-formed SSE frame, per relay's own ``_log_tail_sse_events`` shape."""

    return f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"


async def _fake_byte_stream(chunks: list[bytes]) -> AsyncIterator[bytes]:
    """An async byte iterator delivering ``chunks`` one at a time -- proves the
    reader's line/frame buffering reassembles content split across physical
    ``aiter_bytes()`` reads, not just whatever one MockTransport happens to hand
    back in a single piece."""

    for chunk in chunks:
        yield chunk
        await asyncio.sleep(0)  # yield control, mirroring a real streamed read


def _log_chunk(job_id: str, stream: str, chunk: str, offset: int, next_offset: int) -> dict:
    return {
        "job_id": job_id,
        "stream": stream,
        "chunk": chunk,
        "offset": offset,
        "next_offset": next_offset,
    }


def _end(job_id: str, stream: str, state: str, offset: int) -> dict:
    return {
        "job_id": job_id,
        "stream": stream,
        "state": state,
        "offset": offset,
        "next_offset": offset,
    }


# --------------------------------------------------------------------------- #
# probe_console_sse_capability: capability negotiation is BY DOCUMENT.        #
# --------------------------------------------------------------------------- #


async def test_probe_reads_true_when_door_advertises_it() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/healthz"
        return httpx.Response(200, json={"ok": True, "auth": True, "console_sse": True})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://relay.invalid"
    )
    assert await probe_console_sse_capability(client) is True


async def test_probe_reads_false_when_door_omits_the_flag() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://relay.invalid"
    )
    assert await probe_console_sse_capability(client) is False


async def test_probe_reads_false_on_non_200_never_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://relay.invalid"
    )
    assert await probe_console_sse_capability(client) is False


async def test_probe_reads_false_on_malformed_json_never_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://relay.invalid"
    )
    assert await probe_console_sse_capability(client) is False


async def test_probe_reads_false_on_transport_error_never_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://relay.invalid"
    )
    assert await probe_console_sse_capability(client) is False


# --------------------------------------------------------------------------- #
# ConsoleStreamRegistry: the per-client reader lifecycle owner.               #
# --------------------------------------------------------------------------- #


async def test_registry_ensure_reader_is_idempotent_per_job_stream() -> None:
    registry = ConsoleStreamRegistry()
    starts = 0

    async def factory() -> None:
        nonlocal starts
        starts += 1
        await asyncio.sleep(10)  # never finishes on its own -- cancelled by the test

    registry.ensure_reader("job-1", "console", factory)
    registry.ensure_reader("job-1", "console", factory)  # must NOT spawn a second task
    await asyncio.sleep(0)

    assert starts == 1
    assert registry.is_active("job-1", "console") is True
    await registry.cancel_all()
    assert registry.is_active("job-1", "console") is False


async def test_registry_cancel_one_stops_only_the_named_reader() -> None:
    registry = ConsoleStreamRegistry()

    async def factory() -> None:
        await asyncio.sleep(10)

    registry.ensure_reader("job-1", "console", factory)
    registry.ensure_reader("job-2", "console", factory)
    await asyncio.sleep(0)

    await registry.cancel_one("job-1", "console")

    assert registry.is_active("job-1", "console") is False
    assert registry.is_active("job-2", "console") is True
    await registry.cancel_all()


async def test_registry_cancel_all_clears_fallen_back_too() -> None:
    registry = ConsoleStreamRegistry()
    registry.mark_fallen_back("job-1", "console")
    assert registry.has_fallen_back("job-1", "console") is True

    await registry.cancel_all()

    assert registry.has_fallen_back("job-1", "console") is False


# --------------------------------------------------------------------------- #
# _iter_sse_lines / _iter_sse_frames: bounded, spec-tolerant SSE parsing.     #
# --------------------------------------------------------------------------- #


async def test_iter_sse_lines_reassembles_a_line_split_across_chunks() -> None:
    stream = _fake_byte_stream([b"data: hel", b"lo\n", b"\n"])
    response = httpx.Response(200, content=stream)

    lines = [line async for line in _iter_sse_lines(response, max_line_bytes=1024)]

    assert lines == ["data: hello", ""]


async def test_iter_sse_lines_refuses_an_oversized_unterminated_line_without_hanging() -> None:
    """A line that never terminates must be refused the moment it crosses the
    cap, mid-stream -- not after buffering the whole (possibly enormous)
    input, and never a hang."""

    async def never_ending() -> AsyncIterator[bytes]:
        # 20 chunks of 50 bytes with NO newline anywhere -- an unterminated
        # line that would otherwise buffer forever.
        for _ in range(20):
            yield b"x" * 50
            await asyncio.sleep(0)

    response = httpx.Response(200, content=never_ending())

    with pytest.raises(ConsoleStreamProtocolError):
        async for _ in _iter_sse_lines(response, max_line_bytes=100):
            pass


async def test_iter_sse_frames_ignores_comments_and_reassembles_multiline_data() -> None:
    body = ': keepalive\n\nevent: log_chunk\ndata: {"a"\ndata: 1}\n\n'
    response = httpx.Response(200, content=_fake_byte_stream([body.encode()]))

    frames = [
        frame
        async for frame in _iter_sse_frames(response, max_line_bytes=1024, max_event_bytes=1024)
    ]

    assert frames == [("log_chunk", '{"a"\n1}')]


async def test_iter_sse_frames_bounds_accumulated_event_bytes() -> None:
    body = "event: log_chunk\n" + "".join(f"data: {'x' * 40}\n" for _ in range(5)) + "\n"
    response = httpx.Response(200, content=_fake_byte_stream([body.encode()]))

    with pytest.raises(ConsoleStreamProtocolError):
        async for _ in _iter_sse_frames(response, max_line_bytes=1024, max_event_bytes=100):
            pass


# --------------------------------------------------------------------------- #
# drive_console_stream: the full per-(job, stream) reader lifecycle.          #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clear_console_listener():
    yield
    set_task_console_listener(None)


async def test_drive_console_stream_folds_chunks_promptly_and_notifies_listener() -> None:
    """Every ``log_chunk`` folds into the record AND notifies the lean listener
    as it is parsed -- proven by asserting the notify order/content, not just
    the final tail (a batch-at-the-end implementation would also pass a
    tail-only assertion)."""

    key = _key()
    store = InMemoryTaskRecordStore()
    store.put(TaskRecord(key=key, tool="relay_run", status="working"))
    notified: list[tuple[str, int]] = []
    set_task_console_listener(
        lambda k, channel, delta, offset, truncated: notified.append((delta, offset))
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            _sse_event("log_chunk", _log_chunk("job-1", "console", "first line\n", 0, 11))
            + _sse_event("log_chunk", _log_chunk("job-1", "console", "second line\n", 11, 23))
            + _sse_event("end", _end("job-1", "console", "completed", 23))
        )
        return httpx.Response(200, content=_fake_byte_stream([body.encode()]))

    client = _FakeConsoleSseClient(handler)
    registry = ConsoleStreamRegistry()

    await drive_console_stream(client, "job-1", "console", key, store, registry)

    assert notified == [("first line\n", 11), ("second line\n", 23)]
    record = store.get(key)
    assert record is not None
    console = record.backend[CONSOLE_BACKEND_KEY]
    assert console["tail"] == "first line\nsecond line\n"
    assert console["offset"] == 23
    assert registry.is_active("job-1", "console") is False
    assert registry.has_fallen_back("job-1", "console") is False


async def test_drive_console_stream_ignores_keepalives_and_skips_unknown_events(
    caplog: pytest.LogCaptureFixture,
) -> None:
    key = _key()
    store = InMemoryTaskRecordStore()
    store.put(TaskRecord(key=key, tool="relay_run", status="working"))

    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            ": keepalive\n\n"
            + _sse_event("progress", {"job_id": "job-1", "stream": "console", "note": "unrelated"})
            + _sse_event("log_chunk", _log_chunk("job-1", "console", "only line\n", 0, 10))
            + ": keepalive\n\n"
            + _sse_event("end", _end("job-1", "console", "completed", 10))
        )
        return httpx.Response(200, content=_fake_byte_stream([body.encode()]))

    client = _FakeConsoleSseClient(handler)
    registry = ConsoleStreamRegistry()

    with caplog.at_level(logging.WARNING, logger="clio_agent.tools.relay_console_stream"):
        await drive_console_stream(client, "job-1", "console", key, store, registry)

    record = store.get(key)
    assert record is not None
    assert record.backend[CONSOLE_BACKEND_KEY]["tail"] == "only line\n"
    assert any("unrecognized event type='progress'" in m for m in caplog.messages)


async def test_drive_console_stream_resumes_once_then_succeeds() -> None:
    """The first connection drops after one chunk with no terminal ``end`` --
    the ONE resume attempt (Last-Event-ID = the last folded offset) must pick
    up from there and finish cleanly, never re-delivering the first chunk."""

    key = _key()
    store = InMemoryTaskRecordStore()
    store.put(TaskRecord(key=key, tool="relay_run", status="working"))
    attempts: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        last_event_id = request.headers.get("last-event-id")
        attempts.append(last_event_id)
        if last_event_id is None:
            # First connection: one chunk, then the body just ends (a drop --
            # never a clean `end` event).
            body = _sse_event("log_chunk", _log_chunk("job-1", "console", "first line\n", 0, 11))
            return httpx.Response(200, content=_fake_byte_stream([body.encode()]))
        assert last_event_id == "11"
        body = _sse_event(
            "log_chunk", _log_chunk("job-1", "console", "second line\n", 11, 23)
        ) + _sse_event("end", _end("job-1", "console", "completed", 23))
        return httpx.Response(200, content=_fake_byte_stream([body.encode()]))

    client = _FakeConsoleSseClient(handler)
    registry = ConsoleStreamRegistry()

    await drive_console_stream(client, "job-1", "console", key, store, registry)

    assert attempts == [None, "11"]
    record = store.get(key)
    assert record is not None
    assert record.backend[CONSOLE_BACKEND_KEY]["tail"] == "first line\nsecond line\n"
    assert registry.has_fallen_back("job-1", "console") is False


async def test_drive_console_stream_falls_back_typed_after_second_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A door that never delivers a usable connection exhausts the ONE resume
    attempt and falls back typed -- ``registry.mark_fallen_back`` is recorded
    so future ticks never retry SSE for this (job, stream) again."""

    key = _key()
    store = InMemoryTaskRecordStore()
    store.put(TaskRecord(key=key, tool="relay_run", status="working"))
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, text="log door unavailable")

    client = _FakeConsoleSseClient(handler)
    registry = ConsoleStreamRegistry()

    with caplog.at_level(logging.WARNING, logger="clio_agent.tools.relay_console_stream"):
        await drive_console_stream(client, "job-1", "console", key, store, registry)

    assert calls == 2  # the initial attempt + the ONE resume, never more
    assert registry.has_fallen_back("job-1", "console") is True
    assert registry.is_active("job-1", "console") is False
    assert any(CONSOLE_SSE_FALLBACK_REASON in m for m in caplog.messages)


async def test_drive_console_stream_gone_end_state_is_typed_and_does_not_fall_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A `gone` end state is distinct from a disconnect: it stops immediately,
    typed, WITHOUT marking a fallback (retrying a vanished job would never
    help; polling it would fail identically)."""

    key = _key()
    store = InMemoryTaskRecordStore()
    store.put(TaskRecord(key=key, tool="relay_run", status="working"))

    def handler(request: httpx.Request) -> httpx.Response:
        body = _sse_event("end", _end("job-1", "console", "gone", 0))
        return httpx.Response(200, content=_fake_byte_stream([body.encode()]))

    client = _FakeConsoleSseClient(handler)
    registry = ConsoleStreamRegistry()

    with caplog.at_level(logging.WARNING, logger="clio_agent.tools.relay_console_stream"):
        await drive_console_stream(client, "job-1", "console", key, store, registry)

    assert registry.has_fallen_back("job-1", "console") is False
    assert registry.is_active("job-1", "console") is False
    assert any(CONSOLE_SSE_GONE_REASON in m for m in caplog.messages)


async def test_drive_console_stream_no_record_is_a_quiet_noop() -> None:
    """No persisted record for the key (settled/dropped) -> nothing to watch."""

    key = _key()
    store = InMemoryTaskRecordStore()

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must never connect when there is no record to fold into")

    client = _FakeConsoleSseClient(handler)
    registry = ConsoleStreamRegistry()

    await drive_console_stream(client, "job-1", "console", key, store, registry)

    assert registry.is_active("job-1", "console") is False
