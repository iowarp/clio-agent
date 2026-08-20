"""Bounded relay job console tail: pull-based fold into the durable task record.

#1231 Part 2. relay's worker/door does not yet push console increments through
the per-job event stream (that is clio-relay#259, the SOURCE half this issue
was narrowed away from) -- so until it does, this module is a PULL: on every
#1115 poll round (:func:`clio_agent.tools.mcp_tasks.drive_task_to_terminal`'s
generic ``on_poll`` hook, #1231 Part 2), it fetches the next bounded increment
of a job's stdout from relay's authenticated HTTP door
(``GET /jobs/{job_id}/logs/stdout?offset=N&limit=M``, mirroring
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
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from clio_agent import conf

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastmcp_tasks.client_models import ClientGetTaskResult

    from clio_agent.tools.mcp_task_records import TaskKey, TaskRecordStore
    from clio_agent.tools.relay_transport import RelayTransportClient

logger = logging.getLogger(__name__)

__all__ = [
    "CONSOLE_BACKEND_KEY",
    "CONSOLE_TAIL_TRUNCATED_REASON",
    "RELAY_LOG_PULL_HARD_CAP_BYTES",
    "console_enabled",
    "console_pull_limit_bytes",
    "console_tail_cap_bytes",
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
        ValueError: The response is not the documented envelope. Caught by
            the caller (:func:`make_console_on_poll`) -- never propagated.
    """

    path = f"/jobs/{quote(job_id, safe='')}/logs/stdout"
    params = {"offset": offset, "limit": console_pull_limit_bytes()}
    response = await client._require_http_client().get(path, params=params)  # noqa: SLF001
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise ValueError("relay console log response is not an object")
    data = payload.get("data")
    next_offset = payload.get("next_offset")
    if not isinstance(data, str) or not isinstance(next_offset, int) or next_offset < offset:
        raise ValueError("relay console log response is missing data/next_offset")
    return data, next_offset


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
    """

    if not console_enabled():
        return None

    warned = False

    async def _on_poll(
        _current: "ClientGetTaskResult", key: "TaskKey", store: "TaskRecordStore"
    ) -> None:
        nonlocal warned
        record = store.get(key)
        if record is None:
            return
        console = record.backend.get(CONSOLE_BACKEND_KEY)
        offset = (
            int(console["offset"])
            if isinstance(console, Mapping) and isinstance(console.get("offset"), int)
            else 0
        )
        tail = str(console.get("tail", "")) if isinstance(console, Mapping) else ""
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
        new_tail, truncated = _fold_tail(tail, chunk, console_tail_cap_bytes())
        latest = store.get(key) or record
        new_backend: dict[str, Any] = dict(latest.backend)
        new_backend[CONSOLE_BACKEND_KEY] = {
            "tail": new_tail,
            "offset": next_offset,
            "truncated": truncated,
        }
        store.put(replace(latest, backend=new_backend))

    return _on_poll
