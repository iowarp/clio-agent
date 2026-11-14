"""A message refused because clio-core refuses writes says so, typed (MC-F3).

When clio-core's store is full (``AsyncPutBlob`` returns code 12) a write is lost
(:mod:`clio_agent.arc.clio_core_retry`) and the task history waits to reconcile it
(``RecordingUnconfirmed``). Every message the person sent then answered
``503 core_recording_unconfirmed`` ("Conversation history is recovering. Try again when
the service is ready."), which never became true while the store stayed full, and a new
conversation timed out after 120 s with a bare 500. The person saw no error at all.

While a lost write is on record, those refusals answer ``503`` with the typed error
:data:`CORE_WRITES_REFUSED`, a plain message that names the store and what to do, and
the lost write's facts in ``details``; each refusal is written to the runtime trace
(``clio_core_writes_refused``) with the session and route. A refusal with no lost write
on record keeps its original answer.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi.responses import JSONResponse

from clio_agent.arc.clio_core_retry import last_lost_put_write_ever
from clio_agent.gact.session_records import WRITE_FAILED_REASON
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo
from clio_agent.runtime import trace

#: The typed error a client receives (``error.error``).
CORE_WRITES_REFUSED = "clio_core_writes_refused"
#: Native codes clio-core returns when it has no room to place a write.
NO_ROOM_CODES = frozenset({12, 13})
#: What the person reads.
MESSAGE = (
    "Not saved: CLIO's conversation store is refusing writes, so nothing new can be "
    "recorded. Free disk space or give the store more room, then restart CLIO."
)
MESSAGE_NO_ROOM = (
    "Not saved: CLIO's conversation store is full, so nothing new can be recorded. "
    "Free disk space or give the store more room, then restart CLIO."
)
_SESSION = re.compile(r"/v1/sessions/([^/]+)")


def _refusal(route: str) -> ErrorInfo | None:
    lost = last_lost_put_write_ever()
    if lost is None:
        return None
    session = _SESSION.search(route)
    trace.event(
        "CORE",
        "%s session=%s route=%s lost_at=%s native_code=%s lost_name=%s",
        CORE_WRITES_REFUSED,
        session.group(1) if session else "",
        route,
        lost.occurred_at,
        lost.native_code,
        lost.name,
    )
    return ErrorInfo(
        error=CORE_WRITES_REFUSED,
        message=MESSAGE_NO_ROOM if lost.native_code in NO_ROOM_CODES else MESSAGE,
        details={
            "reason": lost.reason,
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


def unhandled_refusal(exc: BaseException, scope: Any) -> JSONResponse | None:
    """The answer to a session record that never landed while writes are being lost."""

    if not (isinstance(exc, TimeoutError) and str(exc).startswith(WRITE_FAILED_REASON)):
        return None
    info = _refusal(str(scope.get("path") or ""))
    return _response(info) if info is not None else None


__all__ = ["CORE_WRITES_REFUSED", "recording_refusal", "unhandled_refusal"]
