"""A message refused because clio-core refuses writes says so, typed (MC-F3).

When clio-core's store is full (``AsyncPutBlob`` returns code 12) a write is lost
(:mod:`clio_agent.arc.clio_core_retry`) and the task history waits to reconcile it
(``RecordingUnconfirmed``). Every message the person sent then answered
``503 core_recording_unconfirmed`` ("Conversation history is recovering. Try again when
the service is ready.") or, after a restart, ``503 lane_replace_failed`` ("Retry
shortly."), neither of which became true while the store stayed full, and a new
conversation timed out after 120 s with a bare 500. The person saw no error at all.

While a lost write is on record, those refusals answer ``503`` with the typed error
:data:`CORE_WRITES_REFUSED`, a plain message that names the store and what to do, and
the lost write's facts in ``details``; each refusal is written to the runtime trace
(``clio_core_writes_refused``) with the session and route, once per route and lost
write. A refusal with no lost write
on record keeps its original answer.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi.responses import JSONResponse

from clio_agent.arc.clio_core_disk_guard import DISK_LOW
from clio_agent.arc.clio_core_retry import last_lost_put_write, last_lost_put_write_ever
from clio_agent.gact.session_records import WRITE_FAILED_REASON
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo
from clio_agent.runtime import trace

#: The typed error a client receives (``error.error``).
CORE_WRITES_REFUSED = "clio_core_writes_refused"
#: Native codes clio-core returns when it has no room to place a write.
NO_ROOM_CODES = frozenset({12, 13})
#: iowarp-core 2.5's code for a write its storage device refused with ENOSPC (33fedf5d).
#: Before 2.5 the same failure was the generic rc 21.
DEVICE_FULL_CODE = 28
#: The typed cause a device-full refusal carries (``details.cause``).
DEVICE_FULL = "clio_core_device_full"
#: What the person reads.
MESSAGE = (
    "CLIO's conversation store is refusing writes, so nothing new can be recorded. "
    "Free disk space or give the store more room, then restart CLIO."
)
MESSAGE_DISK_LOW = (
    "The disk that holds CLIO's conversation store is nearly full, so CLIO stopped "
    "writing to it before the store could be damaged. Free disk space; writing resumes "
    "by itself."
)
MESSAGE_NO_ROOM = (
    "CLIO's conversation store is full, so nothing new can be recorded. "
    "Free disk space or give the store more room, then restart CLIO."
)
MESSAGE_DEVICE_FULL = (
    "The disk that holds CLIO's conversation store is full, so the store refused the "
    "last write. Free disk space, then restart CLIO."
)
#: (session, route, lost write) already traced: a page polling a refused route is one line.
_TRACED: set[tuple[str, str, str]] = set()
_SESSION = re.compile(r"/v1/sessions/([^/]+)")


def _cause(lost: Any) -> tuple[str, str]:
    """The typed cause and the person's message for one lost write."""
    if DISK_LOW in lost.error:  # CLIO's own refusal (FN-14), not clio-core's
        return DISK_LOW, MESSAGE_DISK_LOW
    if lost.native_code == DEVICE_FULL_CODE:
        return DEVICE_FULL, MESSAGE_DEVICE_FULL
    if lost.native_code in NO_ROOM_CODES:
        return "", MESSAGE_NO_ROOM
    return "", MESSAGE


def _refusal(route: str) -> ErrorInfo | None:
    lost = last_lost_put_write_ever()
    if lost is None:
        return None
    session = _SESSION.search(route)
    key = (session.group(1) if session else "", route, lost.name)
    if key not in _TRACED:
        _TRACED.add(key)
        trace.event(
            "CORE",
            "%s session=%s route=%s lost_at=%s native_code=%s lost_name=%s",
            CORE_WRITES_REFUSED,
            key[0],
            route,
            lost.occurred_at,
            lost.native_code,
            lost.name,
        )
    cause, message = _cause(lost)
    return ErrorInfo(
        error=CORE_WRITES_REFUSED,
        message=message,
        details={
            "reason": lost.reason,
            "cause": cause,
            "native_code": lost.native_code,
            "lost_at": lost.occurred_at,
            "lost_payload_bytes": lost.payload_bytes,
            "recovery_actions": ["free_disk_space", "enlarge_store", "restart"],
        },
        recoverable=False,
    )


def _response(info: ErrorInfo) -> JSONResponse:
    return JSONResponse(
        status_code=503, content=ErrorEnvelope(error=info).model_dump(exclude_none=True)
    )


def recording_refusal(request: Any) -> JSONResponse | None:
    """The answer to ``RecordingUnconfirmed`` when a lost write explains it, else ``None``."""

    info = _refusal(str(getattr(getattr(request, "url", None), "path", "") or ""))
    return _response(info) if info is not None else None


def _refused_write_in(exc: BaseException) -> bool:
    """Whether ``exc`` was caused by clio-core refusing a write (its native code, chained)."""

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(getattr(current, "native_code", None), int):
            return True
        current = current.__cause__ or current.__context__
    return False


def write_refusal(exc: BaseException, request: Any) -> JSONResponse | None:
    """The answer to a change clio-core did not store, when a refused write caused it."""

    if not (_refused_write_in(exc) or last_lost_put_write() is not None):
        return None
    return recording_refusal(request)


def unhandled_refusal(exc: BaseException, scope: Any) -> JSONResponse | None:
    """The answer to a session record that never landed while writes are being lost."""

    if not (isinstance(exc, TimeoutError) and str(exc).startswith(WRITE_FAILED_REASON)):
        return None
    info = _refusal(str(scope.get("path") or ""))
    return _response(info) if info is not None else None


__all__ = [
    "CORE_WRITES_REFUSED",
    "DEVICE_FULL",
    "recording_refusal",
    "unhandled_refusal",
    "write_refusal",
]
