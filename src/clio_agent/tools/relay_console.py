"""Bounded relay job console tail: pull-based fold into the durable task record.

#1231 Part 2. relay's worker/door does not yet push console increments through
the per-job event stream (that is clio-relay#259, the SOURCE half this issue
was narrowed away from) -- so until it does, this module is a PULL: on every
#1115 poll round (:func:`clio_agent.tools.mcp_tasks.drive_task_to_terminal`'s
generic ``on_poll`` hook, #1231 Part 2), it fetches the next bounded increment
of a job's console from relay's authenticated HTTP door
(``GET /jobs/{job_id}/logs/{stream}?offset=N&limit=M``, mirroring
:meth:`~clio_agent.tools.relay_transport.RelayTransportClient.fetch_artifact``'s
idiom), folds it into a BOUNDED rolling tail, and writes it through
:class:`~clio_agent.tools.mcp_task_records.TaskRecordStore` -- the SAME
``put`` :mod:`clio_agent.gact.mcp_task_events` already publishes as
``mcp_task.updated`` on the owning session's SSE channel. Once #1231 Part 1
binds that channel to the live gact session (not the relay owner-session id),
every console increment reaches the session's task thread with no new
transport, no new store (RULE 4), and no new SSE route.

``mcp_tasks.py`` stays entirely backend-agnostic: it only knows to call an
optional ``on_poll`` hook. This module is the ONLY place that knows relay's
HTTP log endpoint, the tail cap, and the truncation marker -- the owner
module the file-size ratchet (#774) expects relay-specific growth to land in,
not ``relay_transport.py`` or ``mcp_tasks.py``.

A log-pull failure (relay unreachable, endpoint not yet deployed, malformed
response) is NEVER allowed to break the wait: it is caught here, reported at
WARNING once per drive and DEBUG thereafter (never floods the log on a
persistently-down door), and the poll loop continues untouched.

#1236 (the live-console last hop). The full-record ``store.put`` above already
fans a ``mcp_task.updated`` snapshot out on every fold, but that snapshot
carries the WHOLE rolling tail every time -- fine for reload/catch-up honesty,
wasteful (and, on a busy session, a real risk of crowding other events out of
the bounded per-session SSE history/queue) as the live signal for "new console
arrived." This module additionally calls the SEPARATE, LEAN
:func:`~clio_agent.tools.mcp_task_records.task_console_listener` hook with just
the new bytes whenever a fold actually grows the tail, so
:mod:`clio_agent.gact.mcp_task_events` can publish a small, dedicated
``mcp_task.console`` delta event alongside the existing snapshot one -- the
record remains the sole source of truth for the full tail.

clio-relay#221/#259 (the live-console PUSH half): once the door's ``GET
/healthz`` document advertises ``console_sse: true`` (negotiated once per
:class:`~clio_agent.tools.relay_transport.RelayTransportClient` connection --
never by timing/probing the SSE route itself), ``make_console_on_poll``'s
returned hook stops pulling on every tick and instead ensures a background SSE
reader is running for this ``(job_id, stream)`` -- the PUSH half itself lives
in the owner module :mod:`clio_agent.tools.relay_console_stream` (kept out of
this file per #774's anti-accretion rule). This module still owns the fold/tail
-cap/truncation-marker/listener-notify contract (:func:`fold_console_increment`),
shared by both the pull path below and the SSE reader, so the two paths can
never drift apart on tail semantics.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from clio_agent import conf
from clio_agent.tools.mcp_task_records import TERMINAL_TASK_STATES, task_console_listener

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastmcp_tasks.client_models import ClientGetTaskResult

    from clio_agent.tools.mcp_task_records import TaskKey, TaskRecord, TaskRecordStore
    from clio_agent.tools.relay_transport import RelayTransportClient

logger = logging.getLogger(__name__)

__all__ = [
    "CONSOLE_BACKEND_KEY",
    "CONSOLE_TAIL_TRUNCATED_REASON",
    "RELAY_LOG_PULL_HARD_CAP_BYTES",
    "console_enabled",
    "console_offset",
    "console_pull_limit_bytes",
    "console_stream",
    "console_tail",
    "console_tail_cap_bytes",
    "fold_console_increment",
    "make_console_on_poll",
]

#: Key this module owns inside ``TaskRecord.backend`` -- never a new store
#: (RULE 4: an EXISTING store, ``backend`` is already the reconnectable-locator
#: free-form dict every relay task record carries).
CONSOLE_BACKEND_KEY = "console"

#: relay's documented hard cap for one ``GET .../logs/stdout`` round trip. A
#: configured pull limit is always clamped to this, so a misconfigured knob
#: cannot mint a request relay itself would refuse.
RELAY_LOG_PULL_HARD_CAP_BYTES = 1_048_576  # 1 MiB

#: Typed reason recorded on the tail when older bytes are dropped for the cap
#: -- a truncation is a fact the UI can show, never a silent shrink.
CONSOLE_TAIL_TRUNCATED_REASON = "relay_console_tail_truncated"


def console_enabled() -> bool:
    """Whether the live console tail folds into the task record.

    Config key ``relay.console.enabled`` / env ``CLIO_RELAY_CONSOLE_ENABLED``
    (config-first per the project's env-vs-config convention -- env is the
    resolver's documented fallback tier, not a bespoke ``os.getenv`` read).
    """

    return conf.resolve(
        "relay.console.enabled",
        env="CLIO_RELAY_CONSOLE_ENABLED",
        default=True,
        cast=conf.as_bool,
    )


def console_pull_limit_bytes() -> int:
    """Bytes requested per poll round, clamped to relay's 1 MiB hard cap.

    Config key ``relay.console.pull_limit_bytes`` / env
    ``CLIO_RELAY_CONSOLE_PULL_LIMIT_BYTES``. Default 64 KiB: comfortably above
    the 8 KiB default tail cap (so one pull can refill the whole window after
    a gap) while staying far under relay's 1 MiB per-request ceiling.
    """

    limit = conf.resolve(
        "relay.console.pull_limit_bytes",
        env="CLIO_RELAY_CONSOLE_PULL_LIMIT_BYTES",
        default=65_536,
        cast=conf.as_int,
    )
    return max(1, min(limit, RELAY_LOG_PULL_HARD_CAP_BYTES))


def console_stream() -> str:
    """Which relay log stream the console fold pulls.

    Config key ``relay.console.stream`` / env ``CLIO_RELAY_CONSOLE_STREAM``.
    Default ``"console"``: the application-output stream clio-relay#259 feeds
    from the running execution (LAMMPS thermo lines, mpirun output, ...).
    ``"stdout"``/``"stderr"`` remain addressable for diagnostics, but they
    carry the job *process*'s stdio -- for an ``mcp_call`` job that is the
    MCP jsonrpc wire, which must never be presented as application console.
    """

    value = conf.resolve(
        "relay.console.stream",
        env="CLIO_RELAY_CONSOLE_STREAM",
        default="console",
        cast=str,
    )
    return str(value).strip() or "console"


def console_tail_cap_bytes() -> int:
    """Rolling tail cap retained on the durable record (default ~8 KiB).

    Config key ``relay.console.tail_cap_bytes`` / env
    ``CLIO_RELAY_CONSOLE_TAIL_CAP_BYTES``. The tail is UI-facing context, not
    the log of record -- relay retains the full stream server-side.
    """

    return max(
        1,
        conf.resolve(
            "relay.console.tail_cap_bytes",
            env="CLIO_RELAY_CONSOLE_TAIL_CAP_BYTES",
            default=8_192,
            cast=conf.as_int,
        ),
    )


def _fold_tail(existing: str, chunk: str, cap: int) -> tuple[str, bool]:
    """Append ``chunk``, keep only the last ``cap`` bytes of the result.

    Operates on UTF-8 BYTES (the cap is a byte budget, not a character count)
    so the record never grows unbounded regardless of what the console emits.
    ``cap`` is a HARD bound: the returned text never encodes to more than
    ``cap`` bytes, in every case, including a misconfigured cap smaller than
    the truncation marker itself.

    A cut that drops earlier bytes is marked with :data:`CONSOLE_TAIL_TRUNCATED_REASON`
    so the UI can show a typed "earlier output elided" notice instead of a
    tail that silently starts mid-line -- unless the cap is too small to fit
    the marker text at all, in which case the returned ``truncated=True`` flag
    alone carries the fact (still typed, never silent) and the tail is a bare
    byte-truncation with no marker. Never raises on a stray non-UTF-8 byte at
    a trim boundary -- decoded leniently.
    """

    combined = existing + chunk
    encoded = combined.encode("utf-8", errors="replace")
    if len(encoded) <= cap:
        return combined, False
    marker = f"...[{CONSOLE_TAIL_TRUNCATED_REASON}: earlier output elided]...\n"
    marker_bytes = marker.encode("utf-8")
    if len(marker_bytes) >= cap:
        kept = encoded[-cap:] if cap > 0 else b""
        return kept.decode("utf-8", errors="ignore"), True
    budget = cap - len(marker_bytes)
    kept = encoded[-budget:]
    return marker + kept.decode("utf-8", errors="ignore"), True


def console_offset(record: "TaskRecord") -> int:
    """Read the last folded console byte offset off a record (0 if none yet).

    Shared seed logic for both the pull path (:func:`make_console_on_poll`)
    and the SSE reader (:mod:`clio_agent.tools.relay_console_stream`) so a
    fresh watcher of an already-progressed job resumes from the SAME place
    either path would.
    """

    console = record.backend.get(CONSOLE_BACKEND_KEY)
    if isinstance(console, Mapping) and isinstance(console.get("offset"), int):
        return int(console["offset"])
    return 0


def console_tail(record: "TaskRecord") -> str:
    """Read the currently-stored rolling tail text off a record ("" if none yet)."""

    console = record.backend.get(CONSOLE_BACKEND_KEY)
    if isinstance(console, Mapping):
        return str(console.get("tail", ""))
    return ""


def fold_console_increment(
    store: "TaskRecordStore",
    key: "TaskKey",
    fallback: "TaskRecord",
    tail: str,
    chunk: str,
    next_offset: int,
) -> tuple[str, bool]:
    """Fold one console increment into the durable record and notify listeners.

    ``tail`` is the PRE-fetch snapshot the caller read before doing any I/O
    (network pull or SSE frame wait) -- never re-derived from a fresh
    ``store.get`` here, so a status/lease write racing the I/O cannot silently
    substitute a different tail than the one the caller's ``chunk`` was
    computed to extend. ``fallback`` is merged onto only when a concurrent
    write dropped the record between the caller's read and this call (matches
    the pre-extraction behavior of the ``on_poll`` pull path).

    Shared by :func:`make_console_on_poll`'s pull-path hook (#1231 Part 2) and
    :mod:`clio_agent.tools.relay_console_stream`'s SSE reader (clio-relay#221/
    #259's push half) -- the ONE place tail-cap/truncation-marker/listener-
    notify semantics live, so the two paths can never drift apart.

    Returns:
        The new rolling tail and whether this fold truncated it -- the SSE
        reader carries these forward across many chunks in one connection
        without re-reading the store on every single one.
    """

    new_tail, truncated = _fold_tail(tail, chunk, console_tail_cap_bytes())
    latest = store.get(key) or fallback
    new_backend: dict[str, Any] = dict(latest.backend)
    new_backend[CONSOLE_BACKEND_KEY] = {
        "tail": new_tail,
        "offset": next_offset,
        "truncated": truncated,
    }
    store.put(replace(latest, backend=new_backend))
    listener = task_console_listener()
    if listener is not None:
        try:
            listener(key, console_stream(), chunk, next_offset, truncated)
        except Exception as exc:  # noqa: BLE001 - a broken listener must never break the wait
            logger.warning(
                "relay console delta listener failed reason=relay_console_delta_listener_failed "
                "task_id=%s: %r",
                key.task_id,
                exc,
            )
    return new_tail, truncated


async def _pull_increment(
    client: "RelayTransportClient", job_id: str, offset: int
) -> tuple[str, int]:
    """Fetch one bounded stdout increment via relay's authenticated HTTP door.

    Mirrors :meth:`RelayTransportClient.fetch_artifact`'s authenticated-HTTP
    idiom: the same ``_require_http_client()`` door, a plain ``GET``, and
    ``raise_for_status`` -- the transport client owns the door, this module
    only knows the path and the envelope.

    Returns:
        The new text chunk (possibly empty) and relay's own reported
        ``next_offset`` -- authoritative over any client-side byte count so a
        multi-byte UTF-8 boundary clipped mid-character is never miscounted.

    Raises:
        ValueError: The response is not relay's documented log envelope
            (``{"text": str, "next_offset": int, "eof": bool, ...}`` --
            verified against the live door; the pre-fix client expected a
            ``data`` key that relay never serves, so every pull failed as a
            swallowed warning). Caught by the caller
            (:func:`make_console_on_poll`) -- never propagated.
    """

    path = f"/jobs/{quote(job_id, safe='')}/logs/{quote(console_stream(), safe='')}"
    params = {"offset": offset, "limit": console_pull_limit_bytes()}
    response = await client._require_http_client().get(path, params=params)  # noqa: SLF001
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise ValueError("relay console log response is not an object")
    text = payload.get("text")
    next_offset = payload.get("next_offset")
    if not isinstance(text, str) or not isinstance(next_offset, int) or next_offset < offset:
        raise ValueError("relay console log response is missing text/next_offset")
    return text, next_offset


def make_console_on_poll(
    client: "RelayTransportClient", job_id: str
) -> Callable[["ClientGetTaskResult", "TaskKey", "TaskRecordStore"], Awaitable[None]] | None:
    """Build the #1115 ``on_poll`` hook that folds relay's console tail in.

    Returns ``None`` when console tailing is disabled (:func:`console_enabled`),
    so a caller passes the result straight through
    :func:`~clio_agent.tools.mcp_tasks.drive_task_to_terminal`'s ``on_poll``
    without a feature-flag branch of its own.

    The returned hook NEVER raises: a log-pull failure is caught, reported at
    WARNING the first time and DEBUG on every subsequent poll of the same
    drive (never floods the log on a persistently-unreachable door), and the
    hook returns without touching the record. ``store.put`` is called only
    when new bytes actually arrived -- a poll that observes no growth leaves
    the record, and its ``updated_at``/SSE fan-out, untouched.

    When new bytes DID arrive, the fold also calls the installed
    :func:`~clio_agent.tools.mcp_task_records.task_console_listener` with the
    delta (never the whole tail) -- #1236's lean live-stream event, so a UI
    watching the session sees new console lines without waiting for a reload
    or re-consuming a growing snapshot on every poll.

    clio-relay#221/#259: when ``client.console_sse_supported()`` reports the
    door advertised ``console_sse: true`` at connect (see
    :class:`~clio_agent.tools.relay_transport.RelayTransportClient`), a
    NON-terminal tick instead ENSURES a background SSE reader is running for
    this ``(job_id, stream)`` (idempotent -- a reader already running is left
    alone) and returns immediately; the reader itself
    (:mod:`clio_agent.tools.relay_console_stream`) folds chunks as they
    arrive, decoupled from this tick's ~1s cadence.

    A TERMINAL tick (adversarial review D1) never ensures a reader -- it
    stops whatever IS running (:meth:`~clio_agent.tools.relay_console_stream.
    ConsoleStreamRegistry.cancel_one`, a no-op if nothing was) and falls
    through to ONE final bounded pull below, exactly matching the
    pre-#221/#259 pull-only path's own terminal-tick drain. This also covers
    #1231's fast-job/one-shot ``poll()`` guarantee: a job already terminal on
    the FIRST (and only) ``on_poll`` call still folds its tail via that pull
    -- never spawn-then-cancel a reader that read nothing.

    A ``(job_id, stream)`` the registry has already exhausted (fallen back
    after real failures, or stopped because relay reported the stream
    ``gone``/cleanly finished) is never re-ensured either -- it falls straight
    to the pull path.

    A client with no ``console_sse_supported``/``_console_stream_registry``
    attributes at all (a minimal test double, or a capability probe that
    never ran) is read as "no capability" -- the pull path below runs
    unchanged, byte-for-byte.
    """

    if not console_enabled():
        return None

    warned = False

    async def _on_poll(
        current: "ClientGetTaskResult", key: "TaskKey", store: "TaskRecordStore"
    ) -> None:
        nonlocal warned
        record = store.get(key)
        if record is None:
            return
        stream = console_stream()
        sse_supported = getattr(client, "console_sse_supported", None)
        registry = getattr(client, "_console_stream_registry", None)
        is_terminal = current is not None and current.status in TERMINAL_TASK_STATES
        if callable(sse_supported) and registry is not None and sse_supported():
            if is_terminal:
                # Adversarial review D1 (BLOCKER): a terminal tick must NEVER
                # ensure_reader -- spawning a reader only to cancel it a line
                # later reads nothing. Stop whatever IS running (a no-op if
                # nothing was, e.g. #1231's fast-job/one-shot poll() case where
                # this is the FIRST and only on_poll call) and fall through to
                # the pull path below for one final bounded drain from the
                # last durably-folded offset -- mirrors exactly what the
                # pre-#221/#259 pull-only path always did on its terminal
                # tick, so the tail is never silently dropped.
                await registry.cancel_one(job_id, stream)
            elif not registry.is_sse_exhausted(job_id, stream):
                from clio_agent.tools.relay_console_stream import (  # noqa: PLC0415
                    drive_console_stream,
                )

                registry.ensure_reader(
                    job_id,
                    stream,
                    lambda: drive_console_stream(client, job_id, stream, key, store, registry),
                )
                return
            # Exhausted (fallen back or stopped) and not terminal, OR terminal
            # (handled above) -- fall through to the pull path below.
        offset = console_offset(record)
        tail = console_tail(record)
        try:
            chunk, next_offset = await _pull_increment(client, job_id, offset)
        except Exception as exc:  # noqa: BLE001 - a log-pull failure must never break the wait
            log = logger.warning if not warned else logger.debug
            log(
                "relay console pull failed reason=relay_console_pull_failed "
                "job_id=%s offset=%d error=%r",
                job_id,
                offset,
                exc,
            )
            warned = True
            return
        if not chunk and next_offset == offset:
            return
        fold_console_increment(store, key, record, tail, chunk, next_offset)

    return _on_poll
