"""clio-relay#221/#259 live-console lane: the PUSH half (SSE) clio-agent consumes.

``relay_console.py`` (#1231 Part 2) is a PULL: one bounded ``GET .../logs/{stream}``
round trip per #1115 poll tick (~1s, relay's fixed cadence). clio-relay#221/#259 landed
the SOURCE half this issue was originally narrowed away from -- the door now advertises
``console_sse: true`` on ``GET /healthz`` (:func:`probe_console_sse_capability`, read
ONCE per :class:`~clio_agent.tools.relay_transport.RelayTransportClient` connection,
never by timing/probing the SSE route itself) and serves
``GET /jobs/{job_id}/logs/{stream}/sse?offset=N`` -- ``log_chunk``/``end`` Server-Sent
Events, ``: keepalive`` comments every ~10s, resumable via ``Last-Event-ID`` or
``?offset=``.

This module is the consumer of that route. :func:`drive_console_stream` owns the whole
lifecycle of ONE ``(job_id, stream)`` background reader: connect, fold every
``log_chunk`` through :func:`~clio_agent.tools.relay_console.fold_console_increment`
(the SAME tail-cap/truncation-marker/listener-notify contract the pull path uses, so
the two paths can never drift apart on tail semantics) as it arrives -- decoupled from
the outer #1115 poll tick, so a delta reaches
:func:`~clio_agent.tools.mcp_task_records.task_console_listener` the moment it is
parsed, not up to ~1s later. A disconnect (bad status, dropped socket, an oversized/
malformed frame) gets ONE resume attempt from the last folded offset
(``Last-Event-ID``); a second failure falls back to the existing pull path with a
TYPED, LOGGED reason -- never a silent downgrade (the #775 cleanup-program ground
rule, styled after ``gact/streaming.py``'s ``_stream_fallback_payload`` catalog). A
``gone`` ``end`` state (the job record vanished mid-stream) surfaces its own typed
reason and stops without retrying.

:class:`ConsoleStreamRegistry` is the per-client lifecycle owner: one registry per
open :class:`RelayTransportClient`, holding the live ``asyncio.Task`` per watched
``(job_id, stream)``. Purely in-process task handles -- never a new durable store
(RULE 4). ``relay_console.py``'s ``on_poll`` hook ensures a reader is running (or reuses
one already active) on every tick and cancels it the moment it observes the task's own
terminal status -- mirroring exactly how the polling loop itself stops. The registry's
:meth:`~ConsoleStreamRegistry.cancel_all` is the client's OWN ``__aexit__`` safety net,
so a caller that detaches (cancels, raises, or simply stops driving) before a job
settles never leaves an orphaned reader running past its client's own lifetime.

Bounded like every other network-facing module here: :data:`CONSOLE_SSE_READ_TIMEOUT_SECONDS`
sits comfortably above relay's 10s keepalive cadence so a genuinely stalled connection
is detected well before the client's own generous 120s RPC timeout would notice;
:data:`CONSOLE_SSE_MAX_LINE_BYTES`/:data:`CONSOLE_SSE_MAX_EVENT_BYTES` cap one physical
SSE line and one reassembled event's accumulated ``data:`` bytes respectively -- an
oversized or never-terminated line is a typed refusal (counts as a disconnect, subject
to the same one-resume-then-fallback contract), never an unbounded buffer or a hang.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from clio_agent.tools.relay_console import (
    console_offset,
    console_tail,
    fold_console_increment,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from clio_agent.tools.mcp_task_records import TaskKey, TaskRecordStore
    from clio_agent.tools.relay_transport import RelayTransportClient

logger = logging.getLogger(__name__)

__all__ = [
    "CONSOLE_SSE_HEALTHZ_PATH",
    "CONSOLE_SSE_MAX_ATTEMPTS",
    "CONSOLE_SSE_MAX_EVENT_BYTES",
    "CONSOLE_SSE_MAX_LINE_BYTES",
    "CONSOLE_SSE_READ_TIMEOUT_SECONDS",
    "CONSOLE_STREAM_FALLBACK_REASONS",
    "ConsoleStreamDisconnected",
    "ConsoleStreamError",
    "ConsoleStreamGone",
    "ConsoleStreamProtocolError",
    "ConsoleStreamRegistry",
    "drive_console_stream",
    "probe_console_sse_capability",
]

#: The door's capability document -- read once per connection, never polled/timed.
CONSOLE_SSE_HEALTHZ_PATH = "/healthz"
CONSOLE_SSE_HEALTHZ_TIMEOUT_SECONDS = 3.0

#: Per-read socket timeout for an open console SSE stream. Comfortably above
#: relay's ~10s keepalive cadence (``LOG_SSE_KEEPALIVE_INTERVAL_SECONDS`` on the
#: door) so a genuinely stalled connection is caught well before the client's
#: own 120s generous RPC timeout would notice.
CONSOLE_SSE_READ_TIMEOUT_SECONDS = 30.0

#: Bound on one physical SSE line's bytes -- generous over relay's 1 MiB raw log
#: read (``MAX_LOG_READ_BYTES``) plus JSON-escaping overhead. A line that never
#: terminates is refused the moment the unterminated buffer crosses this cap
#: (never an unbounded allocation or a hang).
CONSOLE_SSE_MAX_LINE_BYTES = 4 * 1024 * 1024

#: Bound on one reassembled event's accumulated ``data:`` bytes (relay only ever
#: emits a single ``data:`` line per event today; this guards the spec-compliant
#: multi-line-reassembly path too).
CONSOLE_SSE_MAX_EVENT_BYTES = 4 * 1024 * 1024

#: The initial connection plus ONE resume attempt -- a second failure falls back
#: to the existing polling path (design point 1: "on disconnect, ONE resume
#: attempt ... then fall back").
CONSOLE_SSE_MAX_ATTEMPTS = 2

CONSOLE_SSE_DISCONNECTED_REASON = "relay_console_sse_disconnected"
CONSOLE_SSE_PROTOCOL_ERROR_REASON = "relay_console_sse_protocol_error"
CONSOLE_SSE_FALLBACK_REASON = "relay_console_sse_fallback_to_polling"
CONSOLE_SSE_GONE_REASON = "relay_console_sse_gone"
CONSOLE_SSE_CAPABILITY_PROBE_FAILED_REASON = "relay_console_sse_capability_probe_failed"

#: The audited, closed catalog of typed reasons this module ever logs for a
#: degraded/fallen-back console stream -- styled after ``gact/streaming.py``'s
#: ``_STREAM_FALLBACK_REASON_DEFINITIONS`` (#775 no-silent-fallback ground rule):
#: every degradation carries a typed reason, never a bare "it just stopped."
CONSOLE_STREAM_FALLBACK_REASONS: dict[str, dict[str, Any]] = {
    CONSOLE_SSE_DISCONNECTED_REASON: {
        "category": "relay_connectivity",
        "description": "The console SSE connection dropped or refused mid-stream.",
    },
    CONSOLE_SSE_PROTOCOL_ERROR_REASON: {
        "category": "relay_connectivity",
        "description": "The console SSE peer sent a malformed or oversized frame.",
    },
    CONSOLE_SSE_FALLBACK_REASON: {
        "category": "relay_connectivity",
        "description": (
            "The console SSE reader exhausted its one resume attempt; this job's "
            "console tail now folds through the existing byte-range polling path."
        ),
    },
    CONSOLE_SSE_GONE_REASON: {
        "category": "relay_job_state",
        "description": "relay reported the job record no longer exists mid-stream.",
    },
    CONSOLE_SSE_CAPABILITY_PROBE_FAILED_REASON: {
        "category": "relay_connectivity",
        "description": (
            "The door's /healthz capability document could not be fetched or "
            "parsed; console tailing runs the polling path as if unadvertised."
        ),
    },
}


def _typed_reason(reason: str) -> dict[str, Any]:
    """Validate ``reason`` against the closed catalog and return its metadata."""

    definition = CONSOLE_STREAM_FALLBACK_REASONS.get(reason)
    if definition is None:
        raise ValueError(f"Unknown console stream fallback reason: {reason}")
    return definition


class ConsoleStreamError(Exception):
    """Base for every typed console-SSE reader failure (never escapes the reader task)."""


class ConsoleStreamDisconnected(ConsoleStreamError):
    """The SSE connection dropped, refused, or sent an unusable frame."""


class ConsoleStreamProtocolError(ConsoleStreamDisconnected):
    """The peer sent a malformed or oversized frame -- treated as a disconnect."""


class ConsoleStreamGone(ConsoleStreamError):
    """relay's own ``end`` event reported ``state == "gone"``."""

    def __init__(self, job_id: str, stream: str) -> None:
        super().__init__(f"relay job {job_id!r} stream {stream!r} reported state=gone")
        self.job_id = job_id
        self.stream = stream


async def probe_console_sse_capability(http_client: httpx.AsyncClient) -> bool:
    """Read the door's ``GET /healthz`` document for ``console_sse: true``.

    clio-relay#221/#259: capability negotiation is BY DOCUMENT, never timing --
    an older relay, one whose ``/healthz`` route is briefly unreachable, or one
    that answers with malformed JSON is read as "no capability": this function
    returns ``False`` and the caller keeps today's polling path byte-for-byte
    (design point 2), never hangs or raises. Bounded to
    :data:`CONSOLE_SSE_HEALTHZ_TIMEOUT_SECONDS` so a slow door never stalls a
    relay connect by the client's own generous 120s RPC timeout.
    """

    try:
        response = await http_client.get(
            CONSOLE_SSE_HEALTHZ_PATH, timeout=CONSOLE_SSE_HEALTHZ_TIMEOUT_SECONDS
        )
        if response.status_code != 200:
            raise ValueError(f"HTTP {response.status_code}")
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise ValueError("healthz response is not an object")
        return bool(payload.get("console_sse", False))
    except Exception as exc:  # noqa: BLE001 - a probe failure must never break connect
        logger.debug(
            "relay console SSE reason=%s: %s (%r)",
            CONSOLE_SSE_CAPABILITY_PROBE_FAILED_REASON,
            _typed_reason(CONSOLE_SSE_CAPABILITY_PROBE_FAILED_REASON)["description"],
            exc,
        )
        return False


@dataclass
class ConsoleStreamRegistry:
    """Tracks active per-``(job_id, stream)`` SSE console-tail readers.

    One instance per open :class:`~clio_agent.tools.relay_transport.RelayTransportClient`.
    Purely in-process ``asyncio.Task`` handles -- never a new durable store (RULE 4).
    Gone the moment the owning client's ``__aexit__`` calls :meth:`cancel_all`, so a
    caller that detaches before a job settles never leaves an orphaned reader running.
    """

    _tasks: dict[tuple[str, str], "asyncio.Task[None]"] = field(default_factory=dict)
    _fallen_back: set[tuple[str, str]] = field(default_factory=set)

    def has_fallen_back(self, job_id: str, stream: str) -> bool:
        """Whether ``(job_id, stream)`` already gave up on SSE this client's lifetime."""

        return (job_id, stream) in self._fallen_back

    def mark_fallen_back(self, job_id: str, stream: str) -> None:
        """Record that ``(job_id, stream)`` exhausted its resume attempt -- never retried again."""

        self._fallen_back.add((job_id, stream))

    def is_active(self, job_id: str, stream: str) -> bool:
        """Whether a reader task for ``(job_id, stream)`` is currently running."""

        task = self._tasks.get((job_id, stream))
        return task is not None and not task.done()

    def ensure_reader(
        self,
        job_id: str,
        stream: str,
        factory: Callable[[], Coroutine[Any, Any, None]],
    ) -> None:
        """Start the reader for ``(job_id, stream)`` unless one is already active.

        Idempotent by design: every #1115 poll tick calls this, and a
        transparently-claimed drive on the same job (:mod:`task_observers`) may
        call it again from a different closure -- only the FIRST live call per
        ``(job_id, stream)`` spawns a connection; the rest reuse it.
        """

        if self.is_active(job_id, stream):
            return
        self._tasks[(job_id, stream)] = asyncio.ensure_future(factory())

    def discard(self, job_id: str, stream: str) -> None:
        """Drop a finished reader's handle -- called by the reader itself on exit."""

        self._tasks.pop((job_id, stream), None)

    async def cancel_one(self, job_id: str, stream: str) -> None:
        """Cancel and await one ``(job_id, stream)`` reader.

        Called from the SAME on_poll tick that observes the task's own terminal
        status -- mirrors exactly how the outer #1115 poll loop stops itself.
        """

        task = self._tasks.pop((job_id, stream), None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def cancel_all(self) -> None:
        """Cancel and await every still-active reader.

        The owning client's ``__aexit__`` safety net for a caller that detaches
        (cancels, raises, or simply stops driving) before any watched job settles.
        """

        tasks = list(self._tasks.values())
        self._tasks.clear()
        self._fallen_back.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def _iter_sse_lines(response: httpx.Response, *, max_line_bytes: int) -> AsyncIterator[str]:
    """Yield decoded SSE lines from a response body, bounding buffered bytes.

    Reads raw bytes (never :meth:`httpx.Response.aiter_lines`, which buffers a
    physical line internally with no size bound) so a line that never
    terminates cannot grow the buffer past ``max_line_bytes`` --
    :class:`ConsoleStreamProtocolError` is raised the moment the unterminated
    buffer crosses the cap: a typed refusal, never a hang or an unbounded
    allocation.
    """

    buffer = bytearray()
    async for piece in response.aiter_bytes():
        buffer.extend(piece)
        while True:
            newline_index = buffer.find(b"\n")
            if newline_index == -1:
                if len(buffer) > max_line_bytes:
                    raise ConsoleStreamProtocolError(
                        f"SSE line exceeded {max_line_bytes} bytes without a terminator"
                    )
                break
            raw_line = bytes(buffer[:newline_index])
            del buffer[: newline_index + 1]
            if raw_line.endswith(b"\r"):
                raw_line = raw_line[:-1]
            if len(raw_line) > max_line_bytes:
                raise ConsoleStreamProtocolError(f"SSE line exceeded {max_line_bytes} bytes")
            yield raw_line.decode("utf-8", errors="replace")
    if buffer:
        tail_bytes = bytes(buffer)
        if tail_bytes.endswith(b"\r"):
            tail_bytes = tail_bytes[:-1]
        if tail_bytes:
            yield tail_bytes.decode("utf-8", errors="replace")


async def _iter_sse_frames(
    response: httpx.Response, *, max_line_bytes: int, max_event_bytes: int
) -> AsyncIterator[tuple[str, str]]:
    """Yield ``(event_type, data)`` SSE frames per the SSE spec.

    A comment line (``:...``) is ignored; multiple ``data:`` lines within one
    frame are reassembled joined by ``\\n``; a blank line dispatches the frame
    and resets state. Bounds the SUM of one frame's ``data:`` bytes to
    ``max_event_bytes``, separate from :func:`_iter_sse_lines`'s
    per-physical-line bound.
    """

    event_type = "message"
    data_lines: list[str] = []
    data_bytes = 0
    async for line in _iter_sse_lines(response, max_line_bytes=max_line_bytes):
        if line == "":
            if data_lines:
                yield event_type, "\n".join(data_lines)
            event_type = "message"
            data_lines = []
            data_bytes = 0
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            value = line[5:]
            if value.startswith(" "):
                value = value[1:]
            data_bytes += len(value.encode("utf-8"))
            if data_bytes > max_event_bytes:
                raise ConsoleStreamProtocolError(
                    f"SSE event exceeded {max_event_bytes} accumulated data bytes"
                )
            data_lines.append(value)
        elif line.startswith("event:"):
            event_type = line[6:].strip()
        # ``id:`` and any other field name are ignored: relay's own JSON body
        # carries next_offset too, which is what this reader trusts (matches
        # the pull path's authoritative-server-offset contract).
    if data_lines:
        yield event_type, "\n".join(data_lines)


def _parse_log_frame(data: str, *, job_id: str, stream: str) -> dict[str, Any]:
    """Parse and validate one ``log_chunk``/``end`` frame's JSON body."""

    try:
        payload = json.loads(data) if data else {}
    except json.JSONDecodeError as exc:
        raise ConsoleStreamProtocolError(f"SSE frame was not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConsoleStreamProtocolError("SSE frame JSON was not an object")
    if payload.get("job_id") != job_id or payload.get("stream") != stream:
        raise ConsoleStreamProtocolError("SSE frame did not match the requested job/stream")
    if not isinstance(payload.get("next_offset"), int):
        raise ConsoleStreamProtocolError("SSE frame carried no integer next_offset")
    return payload


async def _read_console_sse_once(
    client: "RelayTransportClient",
    job_id: str,
    stream: str,
    offset: int,
    key: "TaskKey",
    store: "TaskRecordStore",
    fallback: Any,
    tail: str,
    *,
    resume: bool,
) -> tuple[str, int]:
    """Read one SSE connection, folding every ``log_chunk`` as it arrives.

    Returns the ``(tail, offset)`` pair once relay sends a clean, non-``gone``
    ``end`` event. Raises :class:`ConsoleStreamGone` on ``state: "gone"``, or
    :class:`ConsoleStreamDisconnected`/:class:`ConsoleStreamProtocolError` on
    any bad status, drop, or malformed frame -- the caller
    (:func:`drive_console_stream`) turns those into the ONE resume attempt,
    then a typed fallback.
    """

    path = f"/jobs/{quote(job_id, safe='')}/logs/{quote(stream, safe='')}/sse"
    params: dict[str, Any] = {"offset": offset}
    headers = {"Last-Event-ID": str(offset)} if resume else None
    http_client = client._require_http_client()  # noqa: SLF001 - same door relay_console.py's pull path already reaches through
    try:
        async with http_client.stream(
            "GET",
            path,
            params=params,
            headers=headers,
            timeout=CONSOLE_SSE_READ_TIMEOUT_SECONDS,
        ) as response:
            if response.status_code != 200:
                body = await response.aread()
                raise ConsoleStreamDisconnected(
                    f"HTTP {response.status_code} opening {path}: {body[:200]!r}"
                )
            async for event_type, data in _iter_sse_frames(
                response,
                max_line_bytes=CONSOLE_SSE_MAX_LINE_BYTES,
                max_event_bytes=CONSOLE_SSE_MAX_EVENT_BYTES,
            ):
                if event_type not in {"log_chunk", "end"}:
                    logger.warning(
                        "relay console SSE unrecognized event type=%r job_id=%s "
                        "stream=%s -- skipped",
                        event_type,
                        job_id,
                        stream,
                    )
                    continue
                payload = _parse_log_frame(data, job_id=job_id, stream=stream)
                next_offset = int(payload["next_offset"])
                if event_type == "end":
                    if payload.get("state") == "gone":
                        raise ConsoleStreamGone(job_id, stream)
                    return tail, next_offset
                chunk = payload.get("chunk")
                if not isinstance(chunk, str):
                    raise ConsoleStreamProtocolError("log_chunk frame carried no chunk text")
                if chunk:
                    tail, _truncated = fold_console_increment(
                        store, key, fallback, tail, chunk, next_offset
                    )
                offset = next_offset
    except httpx.HTTPError as exc:
        raise ConsoleStreamDisconnected(str(exc)) from exc
    raise ConsoleStreamDisconnected("relay console SSE stream closed before a terminal end event")


async def drive_console_stream(
    client: "RelayTransportClient",
    job_id: str,
    stream: str,
    key: "TaskKey",
    store: "TaskRecordStore",
    registry: ConsoleStreamRegistry,
) -> None:
    """Own the whole lifecycle of one ``(job_id, stream)`` SSE console reader.

    Connects, reads until a clean end/gone/disconnect, makes ONE resume attempt
    on disconnect (``Last-Event-ID`` from the last folded offset), then falls
    back to the existing polling path with a typed, logged reason -- never
    silent (the #775 cleanup-program no-silent-fallback ground rule). Runs as
    one background ``asyncio.Task`` owned by ``registry``, self-removing on
    every exit path so :meth:`ConsoleStreamRegistry.is_active` never reports a
    finished reader as running.
    """

    record = store.get(key)
    if record is None:
        registry.discard(job_id, stream)
        return
    offset = console_offset(record)
    tail = console_tail(record)
    for attempt in range(1, CONSOLE_SSE_MAX_ATTEMPTS + 1):
        try:
            tail, offset = await _read_console_sse_once(
                client, job_id, stream, offset, key, store, record, tail, resume=(attempt > 1)
            )
        except ConsoleStreamGone:
            logger.warning(
                "relay console SSE reason=%s job_id=%s stream=%s: %s",
                CONSOLE_SSE_GONE_REASON,
                job_id,
                stream,
                _typed_reason(CONSOLE_SSE_GONE_REASON)["description"],
            )
            registry.discard(job_id, stream)
            return
        except asyncio.CancelledError:
            registry.discard(job_id, stream)
            raise
        except Exception as exc:  # noqa: BLE001 - any disconnect/protocol failure retries once, then falls back typed -- never raised into the caller
            latest = store.get(key)
            if latest is not None:
                offset = console_offset(latest)
                tail = console_tail(latest)
            reason = (
                CONSOLE_SSE_PROTOCOL_ERROR_REASON
                if isinstance(exc, ConsoleStreamProtocolError)
                else CONSOLE_SSE_DISCONNECTED_REASON
            )
            if attempt >= CONSOLE_SSE_MAX_ATTEMPTS:
                logger.warning(
                    "relay console SSE reason=%s job_id=%s stream=%s attempts=%d "
                    "last_reason=%s: %s (%r)",
                    CONSOLE_SSE_FALLBACK_REASON,
                    job_id,
                    stream,
                    attempt,
                    reason,
                    _typed_reason(CONSOLE_SSE_FALLBACK_REASON)["description"],
                    exc,
                )
                registry.mark_fallen_back(job_id, stream)
                registry.discard(job_id, stream)
                return
            logger.info(
                "relay console SSE reason=%s job_id=%s stream=%s: reconnecting after %r",
                reason,
                job_id,
                stream,
                exc,
            )
            continue
        else:
            registry.discard(job_id, stream)
            return
